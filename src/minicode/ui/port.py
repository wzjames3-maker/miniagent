"""The contract between the application core and any user interface.

``InteractiveApp`` and the slash commands only ever talk to a :class:`UIPort`.
The Rich console implements it, the Textual TUI implements it, and a test double
can implement it. Nothing in the core may reach for ``ui.console`` or any other
renderer-specific attribute -- that is what kept the previous Textual UI welded
to the Rich implementation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from minicode.permission.manager import AskReply, AskRequest
from minicode.ui.events import EventSink

__all__ = ["UIFrontEnd", "UIPort", "format_status_line", "cache_label", "fmt_tokens"]


def fmt_tokens(count: int) -> str:
    """Format a token count compactly: 500, 1.2K, 150K."""
    count = int(count or 0)
    if count < 1000:
        return str(count)
    if count < 100_000:
        return f"{count / 1000:.1f}K"
    return f"{count // 1000}K"


def cache_label(stats: Mapping[str, Any]) -> str | None:
    """Short status-line fragment for prompt-cache tokens, or ``None``.

    First number is cache *reads* (tokens served from the provider cache), the
    second cache *writes* (cache creation; Anthropic reports these, automatic
    prefix caches like OpenAI/DeepSeek do not).
    """
    read = int(stats.get("cache_read_tokens", 0) or 0)
    write = int(stats.get("cache_write_tokens", 0) or 0)
    if not read and not write:
        return None
    if read and write:
        return f"cache {fmt_tokens(read)}r/{fmt_tokens(write)}w"
    if read:
        return f"cache {fmt_tokens(read)}"
    return f"cache w:{fmt_tokens(write)}"


@runtime_checkable
class UIPort(Protocol):
    """Everything the core is allowed to ask a UI to do."""

    def banner(self, *, version: str, provider: str, model: str, cwd: str, session: str) -> None: ...

    def rule(self, title: str = "") -> None: ...

    def status_line(self, stats: Mapping[str, Any]) -> None: ...

    def print_user(self, text: str) -> None: ...

    def print_error(self, message: str, *, details: Mapping[str, Any] | None = None) -> None: ...

    def print_info(self, message: str) -> None: ...

    def print_markdown(self, text: str) -> None: ...

    def ask_permission(self, request: AskRequest) -> AskReply: ...


@runtime_checkable
class UIFrontEnd(UIPort, EventSink, Protocol):
    """What a complete front-end must be: both halves of the contract.

    ``UIPort`` is what the core *asks* of a UI; ``EventSink`` is what the agent
    *reports* to it. ``InteractiveApp`` uses one object for both (``sink or ui``),
    so a half-implemented front-end would only fail later, at the first event.
    Naming the combination makes that a construction-time error instead.
    """


def format_status_line(stats: Mapping[str, Any]) -> str:
    """Render an agent stats mapping as a single status line.

    Shared so the Rich console and the Textual status bar cannot drift apart.
    """
    tokens = int(stats.get("tokens", 0) or 0)
    ratio = float(stats.get("ratio", 0.0) or 0.0)
    parts = [
        f"{stats.get('provider', '')}/{stats.get('model', '')}",
        f"step {stats.get('steps', 0)}",
        f"tools {stats.get('tool_calls', 0)}",
        f"ctx {tokens}t ({ratio:.0%})",
        f"${float(stats.get('cost', 0.0) or 0.0):.3f}",
    ]
    label = cache_label(stats)
    if label:
        parts.insert(4, label)
    if stats.get("compactions"):
        parts.append(f"compacted x{stats['compactions']}")
    return " | ".join(parts)
