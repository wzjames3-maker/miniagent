"""Shared Rich renderables for agent output.

Both the Rich console and the Textual TUI render the same things (tool
arguments, tool output, truncation, a resumed session's history). Keeping the
helpers here means the two front-ends cannot drift, and that neither of them
owns "how a tool result looks" -- that is presentation, and presentation
belongs to the UI layer.
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Mapping, Sequence
from typing import Any

from rich.syntax import Syntax
from rich.text import Text

from minicode.providers.base import ToolCall

__all__ = ["format_arguments", "clip_lines", "render_output", "transcript"]


def format_arguments(arguments: Any) -> str:
    """Compact ``key=value`` rendering of a tool call's arguments."""
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


def clip_lines(text: str, limit: int) -> str:
    """Keep the head and tail of a long output, with a marker in between."""
    lines = text.splitlines()
    limit = max(1, limit)
    if len(lines) <= limit:
        return text
    head = lines[: limit // 2]
    tail = lines[-limit // 2 :]
    return "\n".join(head + [f"  ... {len(lines) - limit} lines omitted ..."] + tail)


def render_output(text: str, tool_call: ToolCall) -> Any:
    """Highlight diffs/patches, otherwise plain text with folded lines."""
    stripped = text.lstrip()
    if stripped.startswith(("---", "+++", "@@")):
        return Syntax(text, "diff", theme="ansi_dark", word_wrap=True)
    if len(text) > 4000:
        return textwrap.shorten(text, 4000, placeholder="\n... truncated for display ...")
    return Text(text, overflow="fold")


# --------------------------------------------------------------------------- #
# transcript replay
# --------------------------------------------------------------------------- #
#: ``role -> (label, Rich style)`` for a replayed session.
_ROLE_STYLES: dict[str, tuple[str, str]] = {
    "user": ("you", "bold green"),
    "assistant": ("agent", "bold cyan"),
    "tool": ("tool", "grey62"),
    "system": ("system", "grey50"),
}


def transcript(messages: Sequence[Mapping[str, Any]], *, max_output_lines: int = 20) -> list[Text]:
    """Turn stored messages back into renderables, one per message.

    Resuming a session used to print one summary line and leave the timeline
    blank, so every past session looked empty. Replaying the stored messages is
    what makes the session rail worth having -- and it belongs here rather than
    in the session model, because it is presentation.
    """
    renderables: list[Text] = []
    for message in messages:
        rendered = _render_message(message, max_output_lines=max_output_lines)
        if rendered is not None:
            renderables.append(rendered)
    return renderables


def _render_message(message: Mapping[str, Any], *, max_output_lines: int) -> Text | None:
    """One message as a single (possibly multi-line) ``Text``, or ``None`` to skip."""
    role = str(message.get("role") or "")
    extra = message.get("extra") or {}
    content = message.get("content")
    if isinstance(content, str):
        body = content
    elif content:
        body = json.dumps(content, ensure_ascii=False, indent=2)
    else:
        body = ""
    body = body.strip()

    if role == "tool":
        name = str(extra.get("tool_name") or "")
        text = Text()
        text.append(f"{'tool ' + name if name else 'tool'} ", style="grey62")
        if extra.get("skipped"):
            text.append("(not executed)", style="grey50")
        text.append("\n")
        text.append(clip_lines(body, max_output_lines) or "(no output)", style="grey62")
        return text

    if role == "assistant":
        calls = [call for call in (extra.get("tool_calls") or []) if isinstance(call, Mapping)]
        if not body and not calls:
            return None
        text = Text()
        text.append("agent ", style="bold cyan")
        if body:
            text.append(body)
        for call in calls:
            text.append("\n  > ", style="bold yellow")
            text.append(str(call.get("name") or "?"), style="bold yellow")
            text.append("  " + format_arguments(call.get("arguments")), style="grey62")
        return text

    if role == "user":
        if not body:
            return None
        text = Text()
        text.append("you ", style="bold green")
        text.append(body)
        return text

    label, style = _ROLE_STYLES.get(role, (role or "message", "grey50"))
    text = Text()
    text.append(f"{label} ", style=style)
    text.append(clip_lines(body, max_output_lines), style=style)
    return text
