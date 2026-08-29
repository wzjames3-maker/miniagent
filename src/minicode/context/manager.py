"""Context management: truncation, pruning and compaction.

Long coding sessions die from unbounded message history. Three mechanisms keep
the agent alive, applied in increasing aggressiveness:

1. **truncation** - every tool result is capped (see :mod:`minicode.tools.truncate`)
2. **pruning**    - old *tool outputs* are erased (the tool call itself stays, so
   the history remains readable and the model still knows what it did)
3. **compaction** - old turns are summarised into one message; the most recent
   turns are preserved verbatim inside a token budget (OpenCode's ``tail_turns``
   idea, made budget-driven so it works without a real tokenizer)

After compaction the agent simply keeps going with the shortened history.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from minicode.context.tokens import content_to_text, estimate_messages_tokens, estimate_tokens
from minicode.tools.truncate import truncate_output

__all__ = ["ContextConfig", "CompactionResult", "ContextManager", "PRUNED_PLACEHOLDER"]

PRUNED_PLACEHOLDER = "[tool output removed to save context - re-run the tool or read the file if you need it again]"

#: Summarizer signature: takes the messages to compress, returns the summary text.
Summarizer = Callable[[list[dict[str, Any]]], str]


class ContextConfig(BaseModel):
    model_config = {"extra": "forbid"}

    max_tokens: int = 120_000
    """Soft ceiling for the message history handed to the model."""
    auto_compact: bool = True
    compact_threshold: float = 0.85
    """Compaction triggers at ``compact_threshold * max_tokens``."""
    prune: bool = True
    prune_protect_tokens: int = 40_000
    """Keep (at least) this many tokens of the most recent tool outputs intact."""
    prune_minimum_tokens: int = 20_000
    """Only rewrite history when pruning would actually free this much."""
    preserve_recent_tokens: int = 20_000
    """Budget for the tail of turns that compaction never summarises."""
    tail_turns: int | None = None
    """Optional hard cap on the number of preserved turns (overrides the budget)."""
    tool_output_max_lines: int = 2000
    tool_output_max_bytes: int = 50 * 1024
    keep_system_message: bool = True


@dataclass
class CompactionResult:
    messages: list[dict[str, Any]]
    compacted: bool = False
    pruned_tokens: int = 0
    summary: str | None = None
    before_tokens: int = 0
    after_tokens: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def saved_tokens(self) -> int:
        return max(0, self.before_tokens - self.after_tokens) + self.pruned_tokens


class ContextManager:
    """Owns all history-shrinking logic. Knows nothing about providers or tools."""

    def __init__(self, config: ContextConfig | None = None, *, summarizer: Summarizer | None = None):
        self.config = config or ContextConfig()
        self.summarizer = summarizer
        self.compaction_count = 0
        self.pruned_count = 0
        self.truncated_count = 0
        self.last_summary: str | None = None

    # ------------------------------------------------------------------ #
    # measurement
    # ------------------------------------------------------------------ #
    def estimate(self, messages: Sequence[Mapping[str, Any]]) -> int:
        return estimate_messages_tokens(messages)

    def usage_ratio(self, messages: Sequence[Mapping[str, Any]]) -> float:
        if self.config.max_tokens <= 0:
            return 0.0
        return self.estimate(messages) / self.config.max_tokens

    def stats(self, messages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        tokens = self.estimate(messages)
        return {
            "tokens": tokens,
            "max_tokens": self.config.max_tokens,
            "ratio": round(self.usage_ratio(messages), 3),
            "messages": len(messages),
            "compactions": self.compaction_count,
            "pruned_outputs": self.pruned_count,
            "truncated_outputs": self.truncated_count,
        }

    def needs_compaction(self, messages: Sequence[Mapping[str, Any]]) -> bool:
        if not self.config.auto_compact or self.config.max_tokens <= 0:
            return False
        return self.estimate(messages) >= self.config.max_tokens * self.config.compact_threshold

    # ------------------------------------------------------------------ #
    # 1. truncation
    # ------------------------------------------------------------------ #
    def truncate_tool_output(self, result: Any) -> Any:
        """Apply output truncation to a :class:`ToolResult` (in place)."""
        if not getattr(result, "output", ""):
            return result
        truncated = truncate_output(
            result.output,
            max_lines=self.config.tool_output_max_lines,
            max_bytes=self.config.tool_output_max_bytes,
            tool=getattr(result, "title", "") or "",
        )
        if truncated.truncated:
            self.truncated_count += 1
            result.output = truncated.content
            result.metadata = {**(result.metadata or {}), "truncated": True, "output_path": truncated.output_path}
        return result

    # ------------------------------------------------------------------ #
    # 2. pruning
    # ------------------------------------------------------------------ #
    def prune_tool_outputs(self, messages: list[dict[str, Any]]) -> int:
        """Erase old tool outputs. Returns the number of tokens freed.

        The most recent ``prune_protect_tokens`` worth of output is left alone,
        and the last assistant turn is always protected so the model never loses
        the result of the step it just took.
        """
        if not self.config.prune:
            return 0

        candidates: list[int] = []
        protected = 0
        total = 0
        # walk backwards; skip the final assistant message (protect current step)
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.get("role") == "tool":
                extra = message.setdefault("extra", {})
                if extra.get("compacted"):
                    break  # already compacted region
                if extra.get("pruned"):
                    continue
                size = estimate_tokens(content_to_text(message.get("content")))
                total += size
                if total <= self.config.prune_protect_tokens:
                    protected += size
                    continue
                candidates.append(index)
            elif message.get("role") == "assistant":
                if not candidates:
                    continue  # protect the trailing assistant block
                break

        freed = sum(estimate_tokens(content_to_text(messages[i].get("content"))) for i in candidates)
        if freed < self.config.prune_minimum_tokens:
            return 0

        for index in candidates:
            message = messages[index]
            extra = message.setdefault("extra", {})
            extra["original_tokens"] = estimate_tokens(content_to_text(message.get("content")))
            extra["pruned"] = True
            title = (extra.get("metadata") or {}).get("title", "")
            message["content"] = f"{PRUNED_PLACEHOLDER}\n{title}".strip()
        self.pruned_count += len(candidates)
        return freed

    # ------------------------------------------------------------------ #
    # 3. compaction
    # ------------------------------------------------------------------ #
    def _turn_start_indices(self, messages: Sequence[Mapping[str, Any]]) -> list[int]:
        """Candidate indices at which the preserved tail may start.

        Multi-turn sessions are split on ``user`` messages. A long *single*-turn
        coding task - the common case, where the agent hammers away for dozens of
        tool steps - would otherwise have only one split point and could never be
        compacted, which is exactly the runaway-history problem this module
        exists to solve. So when there is just one user turn, fall back to
        ``assistant`` boundaries (one per tool step).
        """
        user_turns = [i for i, m in enumerate(messages) if m.get("role") == "user"]
        if len(user_turns) > 1:
            return user_turns
        return [i for i, m in enumerate(messages) if m.get("role") == "assistant"]

    def _select_split(self, messages: Sequence[Mapping[str, Any]]) -> int:
        """Index at which the preserved tail starts (0 = nothing to compact)."""
        turns = self._turn_start_indices(messages)
        if len(turns) <= 1:
            return 0
        system_offset = (
            1 if (self.config.keep_system_message and messages and messages[0].get("role") == "system") else 0
        )
        turns = [t for t in turns if t >= system_offset]
        if not turns:
            return 0

        if self.config.tail_turns is not None:
            if self.config.tail_turns <= 0:
                return 0
            keep_from = turns[-min(self.config.tail_turns, len(turns))]
            return keep_from if keep_from > system_offset else 0

        budget = max(1, self.config.preserve_recent_tokens)
        used = 0
        keep_from = turns[-1]
        for start in reversed(turns):
            size = self.estimate(messages[start:])
            if used + size > budget and start != turns[-1]:
                break
            used += size
            keep_from = start
        # never compact away everything, and never split inside the system message
        return keep_from if keep_from > system_offset else 0

    def compact(self, messages: list[dict[str, Any]]) -> CompactionResult:
        """Summarise the older part of the history, preserving the recent tail."""
        before = self.estimate(messages)
        result = CompactionResult(messages=list(messages), before_tokens=before, after_tokens=before)

        freed = self.prune_tool_outputs(result.messages)
        result.pruned_tokens = freed
        if freed:
            result.notes.append(f"pruned {freed} tokens of old tool output")

        split = self._select_split(result.messages)
        if split <= 0:
            result.after_tokens = self.estimate(result.messages)
            return result

        system_offset = 1 if (self.config.keep_system_message and result.messages[0].get("role") == "system") else 0
        head = result.messages[system_offset:split]
        tail = result.messages[split:]

        summary = self._summarize(head)
        if summary is None:
            summary = self._fallback_summary(head)
            result.notes.append("no summarizer available - used an extractive fallback summary")

        compacted_message = {
            "role": "user",
            "content": (
                "<compaction>\n"
                "The conversation above was compacted to free context space. "
                "Here is a summary of everything that happened before this point. "
                "Continue from where it left off; the messages below are verbatim.\n\n"
                f"{summary}\n"
                "</compaction>"
            ),
            "extra": {"compacted": True, "summary": summary, "compacted_messages": len(head)},
        }

        result.messages = result.messages[:system_offset] + [compacted_message] + tail
        result.compacted = True
        result.summary = summary
        result.after_tokens = self.estimate(result.messages)
        self.compaction_count += 1
        self.last_summary = summary
        result.notes.append(f"compacted {len(head)} messages into a summary ({before} -> {result.after_tokens} tokens)")
        return result

    def _summarize(self, head: list[dict[str, Any]]) -> str | None:
        if self.summarizer is None:
            return None
        try:
            summary = self.summarizer(head)
        except Exception:  # noqa: BLE001 - never let summarization kill the run
            return None
        return (summary or "").strip() or None

    @staticmethod
    def _fallback_summary(head: list[dict[str, Any]]) -> str:
        """Extractive summary used when no model-backed summarizer is configured."""
        lines: list[str] = []
        for message in head:
            role = message.get("role", "unknown")
            text = content_to_text(message.get("content")).strip()
            if not text:
                continue
            extra = message.get("extra") or {}
            if role == "assistant":
                for call in extra.get("tool_calls") or []:
                    name = call.get("name") if isinstance(call, Mapping) else call
                    args = call.get("arguments") if isinstance(call, Mapping) else {}
                    lines.append(f"- called {name} with {str(args)[:160]}")
                if text:
                    lines.append(f"- said: {text[:200]}")
            elif role == "user":
                lines.append(f"- user: {text[:300]}")
            elif role == "tool":
                lines.append(f"- result: {text[:160]}")
            elif role == "system":
                lines.append("- [system prompt]")
        return "\n".join(lines[:80]) or "(no content)"

    # ------------------------------------------------------------------ #
    # entry point used by the agent
    # ------------------------------------------------------------------ #
    def prepare(self, messages: list[dict[str, Any]], *, force: bool = False) -> CompactionResult:
        """Shrink the history if needed. Safe to call before every model query."""
        if force or self.needs_compaction(messages):
            return self.compact(messages)
        before = self.estimate(messages)
        freed = self.prune_tool_outputs(messages)
        return CompactionResult(
            messages=messages,
            compacted=False,
            pruned_tokens=freed,
            before_tokens=before,
            after_tokens=self.estimate(messages),
        )

    def rebuild(self, messages: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Rebuild a usable context from persisted session messages.

        Used when resuming / forking a session: the history is restored as-is,
        then pruned (never summarised - that would need a model call).

        Messages coming from older session files may lack an ``extra`` dict;
        they are normalised here so every downstream consumer can rely on it.
        """
        restored = []
        for message in messages:
            item = dict(message)
            if not isinstance(item.get("extra"), dict):
                item["extra"] = {}
            restored.append(item)
        self.prune_tool_outputs(restored)
        return restored
