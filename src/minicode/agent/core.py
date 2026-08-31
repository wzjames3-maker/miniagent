"""The coding agent: mini-swe-agent's control flow, extended with tools,
permissions, sessions and context management.

What is inherited from mini-swe-agent :class:`DefaultAgent`:

* the ``run()`` step loop (limits, format errors, exceptions, exit handling)
* ``query()``'s limit checks, call counting and cost accounting
* jinja2 template rendering with ``StrictUndefined``
* :meth:`serialize` / :meth:`save` trajectory output

What is added / overridden here: streaming model calls, tool-call dispatch
through the registry, permission enforcement, doom-loop detection, context
compaction, and session bookkeeping.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import StrictUndefined, Template

from minicode.agent.environment import ToolEnvironment
from minicode.agent.prompts import COMPACTION_TEMPLATE, INSTANCE_TEMPLATE, SYSTEM_TEMPLATE
from minicode.agent.state import AgentState, AgentStatus
from minicode.context.manager import ContextManager
from minicode.permission.manager import PermissionManager
from minicode.project import ProjectProfile, detect_project
from minicode.providers.base import ContextLengthError, Provider, ToolCall, format_tool_results
from minicode.session.manager import SessionManager
from minicode.session.models import Session, ToolCallRecord
from minicode.tools.base import ToolError, ToolResult
from minicode.tools.registry import ToolRegistry
from minicode.ui.events import EventSink, NullSink, TurnResult

logger = logging.getLogger("minicode.agent")

try:
    from minisweagent.agents.default import AgentConfig, DefaultAgent
    from minisweagent.exceptions import InterruptAgentFlow, Submitted
except ImportError as exc:  # pragma: no cover
    raise ImportError("mini-swe-agent is required: pip install mini-swe-agent>=2.4.0") from exc

__all__ = ["CodingAgentConfig", "CodingAgent", "MiniModelAdapter"]


class CodingAgentConfig(AgentConfig):
    """mini-swe-agent's :class:`AgentConfig` plus minicode's knobs."""

    system_template: str = SYSTEM_TEMPLATE
    instance_template: str = INSTANCE_TEMPLATE
    doom_loop_threshold: int = 3
    """Interrupt after this many identical consecutive tool calls (0 disables)."""
    output_path: Path | None = None


class MiniModelAdapter:
    """Adapts a minicode :class:`Provider` to mini-swe-agent's ``Model`` protocol.

    mini calls ``model.query(messages)``; minicode needs the tool schemas and the
    streaming callback passed along. Instead of changing mini's loop, the
    adapter injects them.
    """

    def __init__(
        self,
        provider: Provider,
        *,
        tools_provider: Callable[[], list[dict[str, Any]]],
        stream: bool = True,
        sink: EventSink | None = None,
    ):
        self.provider = provider
        self._tools_provider = tools_provider
        self.stream = stream
        self.sink = sink or NullSink()
        self.config = provider

    def query(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        tools = kwargs.pop("tools", None)
        if tools is None:
            tools = self._tools_provider()
        message = self.provider.generate(
            messages,
            tools,
            stream=self.stream,
            on_event=self.sink.on_stream_event,
            **kwargs,
        )
        return message.to_message_dict()

    def format_message(self, **kwargs: Any) -> dict[str, Any]:
        return self.provider.format_message(**kwargs)

    def format_observation_messages(
        self,
        message: Mapping[str, Any],
        outputs: Sequence[Any],
        template_vars: dict | None = None,
    ) -> list[dict[str, Any]]:
        return self.provider.format_observation_messages(message, outputs, template_vars)

    def get_template_vars(self, **kwargs: Any) -> dict[str, Any]:
        return self.provider.get_template_vars(**kwargs)

    def serialize(self) -> dict[str, Any]:
        return self.provider.serialize()


class CodingAgent(DefaultAgent):
    """A multi-step, tool-using coding agent."""

    def __init__(
        self,
        provider: Provider,
        registry: ToolRegistry,
        *,
        permission: PermissionManager | None = None,
        context: ContextManager | None = None,
        session: Session | None = None,
        sessions: SessionManager | None = None,
        sink: EventSink | None = None,
        cwd: str = "",
        config_class: type = CodingAgentConfig,
        **kwargs: Any,
    ):
        self.provider = provider
        self.registry = registry
        self.permission = permission
        self.context = context or ContextManager()
        self.session = session
        self.sessions = sessions or SessionManager()
        self.sink = sink or NullSink()
        self.cwd = cwd or os.getcwd()
        self.state = AgentState()
        self.stream = bool(kwargs.get("stream", True))
        self._fingerprints: list[str] = []
        self._context_retried = False
        self._empty_reply_nudged = False
        self._interrupted = False
        #: Detected once: reading the directory on every render would be wasteful.
        self.project: ProjectProfile = detect_project(self.cwd)

        env = ToolEnvironment(
            registry,
            cwd=self.cwd,
            permission=self.permission,
            context=self.context,
            session_id=session.id if session else "",
        )
        model = MiniModelAdapter(
            provider,
            tools_provider=self.registry.schemas,
            stream=self.stream,
            sink=self.sink,
        )
        super().__init__(model, env, config_class=config_class, **kwargs)
        if session is not None:
            session.provider = provider.name
            session.model = provider.model
        self.context.summarizer = self.context.summarizer or self._summarize

    # ------------------------------------------------------------------ #
    # templates
    # ------------------------------------------------------------------ #
    def get_template_vars(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_template_vars(
            tools_list=self.registry.describe(),
            cwd=self.cwd,
            os_name=f"{platform.system()} {platform.release()}",
            date=datetime.now().strftime("%Y-%m-%d"),
            project=self.project.describe(),
            **kwargs,
        )

    # ------------------------------------------------------------------ #
    # message bookkeeping
    # ------------------------------------------------------------------ #
    def add_messages(self, *messages: dict) -> list[dict]:
        out = super().add_messages(*messages)
        for message in messages:
            role = message.get("role")
            if role == "assistant":
                self.state.step += 1
                usage = (message.get("extra") or {}).get("usage") or {}
                self.state.input_tokens += int(usage.get("input_tokens", 0) or 0)
                self.state.output_tokens += int(usage.get("output_tokens", 0) or 0)
        if self.session is not None:
            self.sessions.extend_messages(self.session, messages)
            if any(m.get("role") == "user" for m in messages):
                self.sessions.maybe_auto_title(self.session)
        return out

    def sync_session(self) -> None:
        """Mirror the full history into the session and persist it."""
        if self.session is None:
            return
        self.session.messages = list(self.messages)
        self.session.provider = self.provider.name
        self.session.model = self.provider.model
        self.sessions.save(self.session)

    # ------------------------------------------------------------------ #
    # model call
    # ------------------------------------------------------------------ #
    def query(self) -> dict:
        """Prepare the context, call the model, and recover from overflow."""
        self._prepare_context()
        self.state.status = AgentStatus.RUNNING
        try:
            message = super().query()
        except ContextLengthError:
            if self._context_retried or not self._force_compact():
                raise
            self._context_retried = True
            message = super().query()
        self._context_retried = False
        self.sink.on_assistant_message(_assistant_from_message(message))
        return message

    def _prepare_context(self) -> None:
        if self.context is None:
            return
        result = self.context.prepare(self.messages)
        if result.compacted:
            self.messages = result.messages
            self.state.compactions += 1
            self._fingerprints.clear()
            self.sync_session()
            self.sink.on_compaction(
                {
                    "before_tokens": result.before_tokens,
                    "after_tokens": result.after_tokens,
                    "summary": result.summary or "",
                    "notes": result.notes,
                }
            )

    def _force_compact(self) -> bool:
        """Emergency compaction after a context-window error."""
        if self.context is None:
            return False
        result = self.context.compact(self.messages)
        if not result.compacted:
            # nothing left to summarise - drop the oldest turn instead of looping
            if len(self.messages) <= 2:
                return False
            self.messages = self.messages[:1] + self.messages[2:]
            result = self.context.compact(self.messages)
        self.messages = result.messages
        self.state.compactions += 1
        self.sync_session()
        self.sink.on_compaction(
            {
                "before_tokens": result.before_tokens,
                "after_tokens": result.after_tokens,
                "summary": result.summary or "",
                "notes": result.notes + ["triggered by a context-window error"],
            }
        )
        return True

    # ------------------------------------------------------------------ #
    # tool execution
    # ------------------------------------------------------------------ #
    def execute_actions(self, message: dict) -> list[dict]:
        """Run every tool call in the assistant message and record the results."""
        raw_calls = (message.get("extra") or {}).get("tool_calls") or []
        calls = [ToolCall.from_mapping(call) for call in raw_calls]

        if not calls:
            return self._finish(message)

        tool_messages: list[dict[str, Any]] = []
        for call in calls:
            self._check_doom_loop(call)
            self.sink.on_tool_start(call)
            started = time.time()
            unknown_name = call.name not in self.registry
            parse_error = _tool_call_parse_error(call)
            if unknown_name:
                error = ToolError(
                    code="unknown_tool",
                    message=f"Unknown tool {call.name!r}.",
                    hint=f"Available tools: {', '.join(sorted(self.registry.names())) or '(none)'}",
                )
                result = ToolResult(title=call.name, output="", error=error)
                duration_ms = 0
            elif parse_error is not None:
                error = ToolError(code="parse_error", message=parse_error)
                result = ToolResult(title=call.name, output="", error=error)
                duration_ms = 0
            else:
                output = self.env.execute(ToolEnvironment.action_from_tool_call(call))
                duration_ms = int((time.time() - started) * 1000)
                result: ToolResult = output["extra"]["result"]

            self.sink.on_tool_result(call, result)
            self._record_tool_call(call, result, duration_ms)
            self.state.tool_calls += 1
            if not result.ok:
                self.state.tool_errors += 1

            if unknown_name:
                # OpenCode-style: tell the model exactly which tool is invalid.
                tool_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": (
                            f"Invalid tool_call: {json.dumps(call.name)}. "
                            f"Available options are: {json.dumps(list(self.registry.names()))}. Please try again"
                        ),
                        "extra": {
                            "tool_name": call.name,
                            "tool_arguments": call.arguments,
                            "timestamp": time.time(),
                        },
                    }
                )
            elif parse_error is not None:
                # OpenCode-style: feed the raw parser error straight back to the
                # model as a plain tool message, without executing the tool.
                tool_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": parse_error,
                        "extra": {
                            "tool_name": call.name,
                            "tool_arguments": call.arguments,
                            "timestamp": time.time(),
                        },
                    }
                )
            else:
                tool_messages.extend(format_tool_results([call], [result]))

        self.sync_session()
        return self.add_messages(*tool_messages)

    def _record_tool_call(self, call: ToolCall, result: ToolResult, duration_ms: int) -> None:
        if self.session is None:
            return
        self.sessions.record_tool_call(
            self.session,
            ToolCallRecord(
                id=call.id,
                name=call.name,
                arguments=call.arguments if isinstance(call.arguments, dict) else {"value": call.arguments},
                ok=result.ok,
                error_code=result.error.code if result.error else "",
                duration_ms=duration_ms,
                output_chars=len(result.render()),
                truncated=result.truncated,
            ),
        )

    def _finish(self, message: dict) -> list[dict]:
        """The model replied with text and no tool calls -> the turn is over.

        Reasoning models (DeepSeek-V4/R1, o-series, QwQ) routinely emit an empty
        ``content`` with all of the thinking in ``reasoning_content`` - often
        because ``max_tokens`` was spent on reasoning. Ending the turn there would
        silently throw the work away, so we nudge the model exactly once.
        """
        content = (message.get("content") or "").strip()
        reasoning = ((message.get("extra") or {}).get("reasoning") or "").strip()
        if not content and not self._empty_reply_nudged:
            self._empty_reply_nudged = True
            hint = (
                "Your previous reply had no tool calls and no visible answer text"
                + (" (it contained only reasoning)." if reasoning else ".")
                + "\nReply now with the final answer in plain text: state what you did and the result."
                + " Do not call tools and do not think further."
            )
            return self.add_messages({"role": "user", "content": hint, "extra": {"interrupt_type": "EmptyReply"}})
        raise Submitted(
            {
                "role": "exit",
                "content": content,
                "extra": {
                    "exit_status": "Submitted",
                    "submission": content,
                    "reasoning": reasoning,
                },
            }
        )

    # ------------------------------------------------------------------ #
    # doom loop detection
    # ------------------------------------------------------------------ #
    def _check_doom_loop(self, call: ToolCall) -> None:
        threshold = int(getattr(self.config, "doom_loop_threshold", 0) or 0)
        if threshold <= 0:
            return
        fingerprint = ToolEnvironment.fingerprint(call.name, call.arguments or {})
        self._fingerprints.append(fingerprint)
        if len(self._fingerprints) < threshold:
            return
        window = self._fingerprints[-threshold:]
        if len(set(window)) == 1:
            self._fingerprints.clear()
            raise InterruptAgentFlow(
                {
                    "role": "user",
                    "content": (
                        f"You called `{call.name}` with identical arguments {threshold} times in a row.\n"
                        "That looks like a loop. Stop repeating it: explain what you expected, then either\n"
                        "(a) change the arguments, (b) use a different tool, or (c) if the task is done, "
                        "summarise your work and finish."
                    ),
                    "extra": {"interrupt_type": "DoomLoop"},
                }
            )

    # ------------------------------------------------------------------ #
    # compaction summarizer
    # ------------------------------------------------------------------ #
    def _summarize(self, messages: list[dict[str, Any]]) -> str:
        conversation = _render_conversation(messages)
        prompt = Template(COMPACTION_TEMPLATE, undefined=StrictUndefined).render(conversation=conversation)
        # Reasoning models burn their budget on thinking, so ask for a bigger
        # allowance here than for a normal step.
        response = self.provider.generate(
            [{"role": "user", "content": prompt}],
            None,
            max_tokens=max(int(self.provider.max_tokens), 4096),
        )
        summary = (response.content or "").strip()
        if summary:
            return summary
        # Never compact into nothing: fall back to a truncated transcript so the
        # agent keeps its bearings after compaction.
        logger.warning("Compaction summarizer returned an empty summary; falling back to transcript excerpt.")
        return "(auto-summary, model returned no text)\n" + conversation[-4000:]

    # ------------------------------------------------------------------ #
    # run
    # ------------------------------------------------------------------ #
    def request_interrupt(self) -> None:
        """Ask the running turn to stop at the next step boundary.

        The agent runs on a worker thread, so the UI cannot kill it mid-request;
        it can only ask. :meth:`step` checks this before every model call, so a
        long tool call still finishes, but no new one starts.
        """
        self._interrupted = True

    def step(self) -> list[dict]:
        """One step, or an immediate stop if the UI asked for one.

        Stopping means emitting mini's own trailing ``exit`` message rather than
        raising: both loops (mini's and ours) break on it, so the turn unwinds
        through the normal path and the partial transcript stays in the session.
        """
        if self._interrupted:
            return self.add_messages(
                {
                    "role": "exit",
                    "content": "Interrupted",
                    "extra": {"exit_status": "Interrupted", "submission": ""},
                }
            )
        return super().step()

    def run(self, task: str = "", **kwargs: Any) -> dict:
        """Run until the turn ends.

        The first call starts a fresh conversation (mini's behaviour). Later
        calls *continue* it, which is what the interactive TUI needs.
        """
        self._empty_reply_nudged = False
        self._interrupted = False
        if self.messages:
            if task:
                self.add_messages({"role": "user", "content": task, "extra": {}})
            result = self._run_loop()
        else:
            self.sink.on_turn_start(task)
            result = super().run(task, **kwargs)

        self.state.status = AgentStatus.INTERRUPTED if self._interrupted else AgentStatus.FINISHED
        self.sync_session()
        if self.session is not None:
            self.sessions.maybe_auto_title(self.session)
            self.sessions.save(self.session)
        self.sink.on_turn_end(self.turn_result(result))
        return result

    def _run_loop(self) -> dict:
        """Continuation loop (same semantics as mini's ``run``, minus the reset)."""
        from minisweagent.exceptions import FormatError

        self.sink.on_turn_start("")
        while True:
            try:
                self.step()
                self.n_consecutive_format_errors = 0
            except FormatError as e:
                self.cost += e.messages[0].get("extra", {}).get("cost", 0.0)
                self.n_consecutive_format_errors += 1
                if 0 < self.config.max_consecutive_format_errors <= self.n_consecutive_format_errors:
                    self.add_messages(
                        *e.messages,
                        {
                            "role": "exit",
                            "content": "RepeatedFormatError",
                            "extra": {"exit_status": "RepeatedFormatError", "submission": ""},
                        },
                    )
                else:
                    self.add_messages(*e.messages)
            except InterruptAgentFlow as e:
                self.add_messages(*e.messages)
            except Exception as e:
                self.handle_uncaught_exception(e)
                raise
            finally:
                self.save(self.config.output_path)
            if self.messages[-1].get("role") == "exit":
                break
        return self.messages[-1].get("extra", {})

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def turn_result(self, extra: dict[str, Any]) -> TurnResult:
        return TurnResult(
            exit_status=str(extra.get("exit_status", "")),
            submission=str(extra.get("submission", "")),
            steps=self.state.step,
            cost=self.cost,
            tool_calls=self.state.tool_calls,
            tool_errors=self.state.tool_errors,
            extra=dict(extra),
        )

    def stats(self) -> dict[str, Any]:
        context_stats = self.context.stats(self.messages) if self.context else {}
        return {
            "status": self.state.status,
            "provider": self.provider.name,
            "model": self.provider.model,
            "steps": self.state.step,
            "cost": round(self.cost, 4),
            "tokens_in": self.state.input_tokens,
            "tokens_out": self.state.output_tokens,
            "tool_calls": self.state.tool_calls,
            "tool_errors": self.state.tool_errors,
            "compactions": self.state.compactions,
            "session": self.session.id if self.session else "",
            "cwd": self.cwd,
            **context_stats,
        }

    def serialize(self, *extra_dicts: Any) -> dict[str, Any]:
        data = super().serialize(*extra_dicts)
        data.setdefault("info", {})["minicode"] = self.stats()
        return data


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _assistant_from_message(message: Mapping[str, Any]) -> Any:
    """Rebuild an :class:`AssistantMessage` from a stored message dict."""
    from minicode.providers.base import AssistantMessage, Usage

    extra = message.get("extra") or {}
    usage = Usage(**{k: v for k, v in (extra.get("usage") or {}).items() if k in Usage.__dataclass_fields__})
    return AssistantMessage(
        content=message.get("content") or "",
        tool_calls=[ToolCall.from_mapping(call) for call in (extra.get("tool_calls") or [])],
        usage=usage,
        finish_reason=str(extra.get("finish_reason", "")),
        reasoning=str(extra.get("reasoning", "")),
    )


def _tool_call_parse_error(call: ToolCall) -> str | None:
    """Return the parser error for a tool call, or ``None`` if it can run.

    Mirrors OpenCode: when a provider returns non-empty raw arguments that do
    not parse as JSON, the raw parser error is fed straight back to the model
    and the tool is not executed.
    """
    if not call.raw_arguments or call.arguments:
        return None
    try:
        json.loads(call.raw_arguments)
    except (json.JSONDecodeError, TypeError) as exc:
        return str(exc)
    return None


def _render_conversation(messages: Sequence[Mapping[str, Any]], *, limit_chars: int = 60_000) -> str:
    from minicode.context.tokens import content_to_text

    lines: list[str] = []
    for message in messages:
        role = message.get("role", "unknown")
        text = content_to_text(message.get("content")).strip()
        if role == "assistant":
            for call in (message.get("extra") or {}).get("tool_calls") or []:
                name = call.get("name") if isinstance(call, Mapping) else call
                args = call.get("arguments") if isinstance(call, Mapping) else {}
                lines.append(f"[assistant -> {name}] {str(args)[:300]}")
        if text:
            lines.append(f"[{role}] {text[:2000]}")
    rendered = "\n".join(lines)
    if len(rendered) > limit_chars:
        rendered = "...(earlier messages omitted)...\n" + rendered[-limit_chars:]
    return rendered
