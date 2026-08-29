"""Rich-based terminal rendering.

Implements :class:`~minicode.ui.events.EventSink`, so the agent stays unaware of
how things are displayed, and provides the permission prompt that
:class:`PermissionManager` calls back into.
"""

from __future__ import annotations

import textwrap
from collections.abc import Mapping
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme

from minicode.config.settings import UISettings
from minicode.permission.manager import AskReply, AskRequest
from minicode.providers.base import StreamEvent, ToolCall
from minicode.tools.base import ToolResult
from minicode.ui.events import EventSink

__all__ = ["ConsoleUI", "MINICODE_THEME"]

MINICODE_THEME = Theme(
    {
        "minicode.accent": "bold cyan",
        "minicode.user": "bold green",
        "minicode.tool": "bold yellow",
        "minicode.error": "bold red",
        "minicode.dim": "grey62",
        "minicode.ok": "green",
    }
)


class ConsoleUI(EventSink):
    """Renders agent activity to the terminal."""

    def __init__(self, settings: UISettings | None = None, console: Console | None = None, *, markdown: bool = True):
        self.settings = settings or UISettings()
        self.console = console or Console(theme=MINICODE_THEME, highlight=False)
        self.markdown = markdown
        self._streaming = False
        self._streamed_text: list[str] = []
        self._thinking = False
        self._reasoning_text: list[str] = []

    # ------------------------------------------------------------------ #
    # chrome
    # ------------------------------------------------------------------ #
    def banner(self, *, version: str, provider: str, model: str, cwd: str, session: str) -> None:
        self.console.print()
        self.console.print(
            Panel.fit(
                f"[minicode.accent]minicode[/] [minicode.dim]v{version}[/]\n"
                f"[minicode.dim]model:[/] {provider}/{model}\n"
                f"[minicode.dim]cwd:[/]    {cwd}\n"
                f"[minicode.dim]session:[/] {session}",
                title="[minicode.accent]coding agent[/]",
                border_style="cyan",
            )
        )
        self.console.print("[minicode.dim]Type /help for commands, /exit to quit.[/]")

    def rule(self, title: str = "") -> None:
        self.console.print(Rule(title, style="grey35"))

    def status_line(self, stats: Mapping[str, Any]) -> None:
        tokens = stats.get("tokens", 0)
        ratio = stats.get("ratio", 0.0)
        parts = [
            f"{stats.get('provider', '')}/{stats.get('model', '')}",
            f"step {stats.get('steps', 0)}",
            f"tools {stats.get('tool_calls', 0)}",
            f"ctx {tokens}t ({ratio:.0%})",
            f"${stats.get('cost', 0.0):.3f}",
        ]
        if stats.get("compactions"):
            parts.append(f"compacted x{stats['compactions']}")
        self.console.print("[minicode.dim]" + " | ".join(parts) + "[/]")

    def print_user(self, text: str) -> None:
        self.console.print()
        self.console.print("[minicode.user]you[/]", end="")
        self.console.print(f" {text}", markup=False, highlight=False)

    def print_error(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.console.print()
        self.console.print(f"[minicode.error]error[/] {message}", markup=False, highlight=False)
        if details:
            for key, value in details.items():
                self.console.print(f"  [minicode.dim]{key}:[/] {value}", markup=False, highlight=False)

    def print_info(self, message: str) -> None:
        self.console.print(f"[minicode.dim]{message}[/]", markup=False, highlight=False)

    def print_markdown(self, text: str) -> None:
        if self.markdown:
            self.console.print(Markdown(text))
        else:
            self.console.print(text, markup=False, highlight=False)

    # ------------------------------------------------------------------ #
    # EventSink
    # ------------------------------------------------------------------ #
    def on_stream_event(self, event: StreamEvent) -> None:
        if event.type == "text_delta":
            if self._thinking:
                self._end_thinking()
            if not self._streaming:
                self._start_stream()
            self._streamed_text.append(event.text)
            self.console.print(event.text, end="", markup=False, highlight=False)
        elif event.type == "reasoning_delta":
            # Chain-of-thought from reasoning models. Shown dimmed so it is
            # clearly not part of the answer.
            if self._streaming:
                self._end_stream()
            if not self._thinking:
                self._thinking = True
                self.console.print()
                self.console.print("[minicode.dim]thinking…[/]", end=" ")
            self._reasoning_text.append(event.text)
            self.console.print(event.text, end="", style="grey50", highlight=False, markup=False)
        elif event.type == "tool_call_start":
            if self._thinking:
                self._end_thinking()
            self._end_stream()
            self.console.print()
            self.console.print(f"[minicode.tool]▸ {event.tool_call.name}[/]")
        elif event.type == "tool_call_end":
            if self._thinking:
                self._end_thinking()
            if self.settings.show_tool_arguments:
                self.console.print(f"  [minicode.dim]{_format_arguments(event.tool_call.arguments)}[/]")
        elif event.type == "usage":
            if self._thinking:
                self._end_thinking()
            self._end_stream()

    def on_tool_start(self, tool_call: ToolCall) -> None:
        if self._thinking:
            self._end_thinking()
        self._end_stream()

    def on_tool_result(self, tool_call: ToolCall, result: ToolResult) -> None:
        body = result.render()
        if not body.strip():
            return
        style = "red" if not result.ok else "grey35"
        lines = body.splitlines()
        limit = max(1, self.settings.max_output_lines)
        if len(lines) > limit:
            head = lines[: limit // 2]
            tail = lines[-limit // 2 :]
            shown = "\n".join(head + [f"  … {len(lines) - limit} lines omitted …"] + tail)
        else:
            shown = body
        title = f"{tool_call.name}" + ("" if result.ok else " [minicode.error]failed[/]")
        self.console.print(
            Panel(
                _maybe_syntax(shown, tool_call),
                title=title,
                title_align="left",
                border_style=style,
                padding=(0, 1),
                expand=False,
            )
        )
        if result.truncated:
            path = (result.metadata or {}).get("output_path")
            if path:
                self.print_info(f"full output: {path}")

    def on_error(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.print_error(message, details=details)

    def on_compaction(self, info: Mapping[str, Any]) -> None:
        self.console.print()
        self.print_info(f"context compacted: {info.get('before_tokens', 0)} -> {info.get('after_tokens', 0)} tokens")
        for note in info.get("notes") or []:
            self.print_info(f"  {note}")

    def _start_stream(self) -> None:
        self._streaming = True
        self._streamed_text = []
        self.console.print()
        self.console.print("[minicode.accent]minicode[/] ", end="")

    def _end_stream(self) -> None:
        if self._streaming:
            self.console.print()
            self._streaming = False

    def _end_thinking(self) -> None:
        """Close the dimmed reasoning block (collapsed to a single summary line)."""
        if not self._thinking:
            return
        self._thinking = False
        chars = sum(len(part) for part in self._reasoning_text)
        self._reasoning_text = []
        self.console.print()
        self.console.print(f"[minicode.dim]… thought for {chars} chars[/]", markup=False)

    # ------------------------------------------------------------------ #
    # permission prompt
    # ------------------------------------------------------------------ #
    def ask_permission(self, request: AskRequest) -> AskReply:
        """Present a permission request and return the user's answer."""
        self.console.print()
        self.console.print(
            Panel(
                f"[minicode.accent]{request.permission}[/]"
                + (f" [minicode.dim]({request.tool})[/]" if request.tool else "")
                + "\n"
                + "\n".join(f"  {pattern}" for pattern in request.patterns),
                title="[bold yellow]permission required[/]",
                border_style="yellow",
                expand=False,
            )
        )
        try:
            from rich.prompt import Prompt

            answer = (
                Prompt.ask(
                    "[bold][y][/] once  [bold][a][/] always  [bold][n][/] reject",
                    choices=["y", "a", "n", ""],
                    default="y",
                    console=self.console,
                    show_choices=False,
                )
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            return AskReply.REJECT
        if answer in {"", "y", "yes"}:
            return AskReply.ONCE
        if answer in {"a", "always"}:
            return AskReply.ALWAYS
        return AskReply.REJECT

    def confirm(self, question: str, *, default: bool = True) -> bool:
        try:
            from rich.prompt import Prompt

            answer = Prompt.ask(
                question, choices=["y", "n"], default="y" if default else "n", console=self.console, show_choices=True
            )
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip().lower() in {"y", "yes", ""}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _format_arguments(arguments: Any) -> str:
    if not arguments:
        return "{}"
    if isinstance(arguments, Mapping):
        rendered = []
        for key, value in arguments.items():
            text = str(value)
            if len(text) > 90:
                text = text[:87] + "..."
            rendered.append(f"{key}={text}")
        return "\n  ".join(rendered)
    text = str(arguments)
    return text if len(text) <= 200 else text[:197] + "..."


def _maybe_syntax(text: str, tool_call: ToolCall) -> Any:
    """Highlight diffs/patches when the tool produced them."""
    stripped = text.lstrip()
    if stripped.startswith(("---", "+++", "@@")):
        return Syntax(text, "diff", theme="ansi_dark", word_wrap=True)
    if len(text) > 4000:
        return textwrap.shorten(text, 4000, placeholder="\n… truncated for display …")
    return Text(text, overflow="fold")
