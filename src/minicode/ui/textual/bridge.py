"""The bridge: turns agent events into widget updates.

This is the only place that knows about both the agent and Textual. It fulfils
:class:`~minicode.ui.events.EventSink` and :class:`~minicode.ui.port.UIPort`,
so the core keeps talking to abstractions and the widgets stay dumb.

Every mutation is funnelled through ``App.call_from_thread`` because the agent
runs on a worker thread while Textual owns the main loop.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from minicode.config.settings import UISettings
from minicode.permission.manager import AskReply, AskRequest
from minicode.providers.base import StreamEvent, ToolCall
from minicode.tools.base import ToolResult
from minicode.ui.events import EventSink
from minicode.ui.textual.widgets import PermissionRequest

__all__ = ["TextualUI"]


class TextualUI(EventSink):
    """Renders agent activity into the Textual shell."""

    def __init__(self, settings: UISettings, app: Any) -> None:
        self.settings = settings
        self._app = app
        self.markdown = True
        self._reasoning_buffer: list[str] = []
        self._thinking = False

    # ------------------------------------------------------------------ #
    # plumbing
    # ------------------------------------------------------------------ #
    def _post(self, callback: Callable[..., Any], *args: Any) -> None:
        """Run ``callback`` on the app thread, whichever thread we are on now."""
        self._app.post(callback, *args)

    def _write(self, renderable: Any) -> None:
        self._post(self._app.append_to_log, renderable)

    def _set_stream(self, renderable: Any) -> None:
        self._post(self._app.set_stream, renderable)

    def _clear_stream(self) -> None:
        self._set_stream("")

    def _flush_stream(self) -> None:
        # Streaming text now lands directly in the message stream as it arrives
        # (see on_stream_event), so there is nothing left to flush here.
        self._clear_stream()

    def _flush_reasoning(self) -> None:
        if not self._thinking:
            return
        self._thinking = False
        self._reasoning_buffer = []
        self._post(self._app.finalize_assistant_thinking)
        self._clear_stream()

    # ------------------------------------------------------------------ #
    # UIPort
    # ------------------------------------------------------------------ #
    def banner(self, *, version: str, provider: str, model: str, cwd: str, session: str) -> None:
        self._write(
            Panel.fit(
                Text.from_markup(
                    f"[bold cyan]minicode[/] [dim]v{version}[/]\n"
                    f"[dim]model:[/] {provider}/{model}\n"
                    f"[dim]cwd:[/]    {cwd}\n"
                    f"[dim]session:[/] {session}"
                ),
                title="[bold cyan]coding agent[/]",
                border_style="cyan",
            )
        )
        self.print_info("Type / for commands, /exit to quit.")

    def rule(self, title: str = "") -> None:
        self._write(Rule(title, style="grey35"))

    def status_line(self, stats: Mapping[str, Any]) -> None:
        self._post(self._app.show_stats, dict(stats))

    def print_user(self, text: str) -> None:
        self._flush_stream()
        self._post(self._app.append_user_message, text)

    def print_error(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        lines = [f"[bold red]error[/] {message}"]
        for key, value in (details or {}).items():
            lines.append(f"  [dim]{key}:[/] {value}")
        self._write(Text.from_markup("\n".join(lines)))

    def print_info(self, message: str) -> None:
        self._write(Text.from_markup(f"[dim]{message}[/]"))

    def print_markdown(self, text: str) -> None:
        self._write(Markdown(text))

    def ask_permission(self, request: AskRequest) -> AskReply:
        return self._app.request_permission(
            PermissionRequest(permission=request.permission, patterns=list(request.patterns), tool=request.tool)
        )

    # ------------------------------------------------------------------ #
    # EventSink
    # ------------------------------------------------------------------ #
    def on_stream_event(self, event: StreamEvent) -> None:
        if event.type == "text_delta":
            if self._thinking:
                self._flush_reasoning()
            self._post(self._app.append_assistant_text, event.text)
        elif event.type == "reasoning_delta":
            self._thinking = True
            self._reasoning_buffer.append(event.text)
            self._post(self._app.append_assistant_thinking, event.text)
        elif event.type == "tool_call_start":
            self._flush_reasoning()
            call = event.tool_call
            if call is not None:
                self._post(
                    self._app.add_tool_call,
                    call.name,
                    call.arguments if isinstance(call.arguments, dict) else {},
                    call.id,
                )
        elif event.type == "tool_call_end":
            self._flush_reasoning()
        elif event.type == "usage":
            self._flush_reasoning()

    def on_tool_start(self, tool_call: ToolCall) -> None:
        self._flush_reasoning()

    def on_tool_result(self, tool_call: ToolCall, result: ToolResult) -> None:
        body = result.render()
        if not body.strip():
            return
        self._post(self._app.complete_tool_call, tool_call.id, body, not result.ok)
        if result.truncated:
            path = (result.metadata or {}).get("output_path")
            if path:
                self.print_info(f"full output: {path}")

    def on_error(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.print_error(message, details=details)

    def on_compaction(self, info: Mapping[str, Any]) -> None:
        self._write(
            Text.from_markup(
                f"[dim]context compacted: {info.get('before_tokens', 0)} -> {info.get('after_tokens', 0)} tokens[/]"
            )
        )
        for note in info.get("notes") or []:
            self.print_info(f"  {note}")
