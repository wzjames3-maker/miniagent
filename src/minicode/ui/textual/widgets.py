"""The widgets that make up the TUI shell.

Each widget owns exactly one concern and knows nothing about the agent: the
bridge pushes renderables in, the app wires messages out. That is why the two
list widgets here (sessions, slash commands) describe their contents with
plain data -- they are told what to draw, not where it came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from rich.markdown import Markdown
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from minicode.session.models import Session
from minicode.ui.port import cache_label, fmt_tokens
from minicode.ui.render import format_arguments
from minicode.ui.textual.theme import context_bar, context_style

__all__ = [
    "AssistantMessage",
    "Composer",
    "ContextBar",
    "HintsBar",
    "MessageList",
    "MessageLog",
    "PermissionBar",
    "PermissionRequest",
    "SessionFooter",
    "SessionSidebar",
    "SlashPopover",
    "StreamArea",
    "ToolCallWidget",
    "UserMessage",
    "group_sessions",
]

_GROUP_ORDER = ("Today", "Yesterday", "Older")
_MAX_TITLE = 24

#: How many trailing lines of the live reasoning stream to keep on screen.
_THINKING_TAIL_LINES = 6


def _esc(text: str) -> str:
    """Escape ``[`` so Rich markup can't mis-pair tags inside model output."""
    return text.replace("[", r"\[")


def group_sessions(sessions: list[Session], *, now: datetime | None = None) -> list[tuple[str, list[Session]]]:
    """Bucket sessions into Today / Yesterday / Older, newest first.

    OpenCode groups its session rail exactly this way, which keeps a long
    history navigable without needing a search box.
    """
    today = (now or datetime.now()).date()
    buckets: dict[str, list[Session]] = {name: [] for name in _GROUP_ORDER}
    for session in sorted(sessions, key=lambda item: item.updated_at, reverse=True):
        delta = (today - datetime.fromtimestamp(session.updated_at).date()).days
        if delta <= 0:
            buckets["Today"].append(session)
        elif delta == 1:
            buckets["Yesterday"].append(session)
        else:
            buckets["Older"].append(session)
    return [(name, buckets[name]) for name in _GROUP_ORDER if buckets[name]]


def _shorten(text: str, width: int = _MAX_TITLE) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 3] + "..."


def _session_label(session: Session, *, current_id: str) -> str:
    """One row in the session rail.

    Old sessions created before auto-titling are all called "New session",
    which makes the rail impossible to read. Fall back to the first user
    message (or the id) so every row says something meaningful, and append the
    message count so you can tell empty sessions apart from real history.
    """
    marker = ">" if session.id == current_id else " "
    title = session.title or ""
    if title == "New session" or not title:
        first_user = next(
            (str(m.get("content")) for m in session.messages if m.get("role") == "user" and m.get("content")), None
        )
        title = _shorten(first_user) if first_user else session.id
    else:
        title = _shorten(title)
    return f"{marker} {title} ({session.message_count})"


class SessionSidebar(Vertical):
    """The session rail: recency groups plus a marker on the active session."""

    BINDINGS = [Binding("d", "delete_selected", "Delete session", show=False)]

    class Selected(Message):
        """Posted when the user picks a session."""

        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    class DeleteRequested(Message):
        """Posted when the user asks to delete the highlighted session."""

        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    def compose(self) -> ComposeResult:
        yield Static("sessions", classes="sidebar-title")
        yield Button("＋ New session", id="new-session", classes="sidebar-new")
        yield OptionList(id="session-list")

    def set_sessions(self, sessions: list[Session], *, current_id: str = "") -> None:
        """Rebuild the rail. Group headers are disabled so they cannot be picked."""
        listing = self.query_one("#session-list", OptionList)
        listing.clear_options()
        for group, members in group_sessions(sessions):
            listing.add_option(Option(group.upper(), disabled=True, id=f"group-{group}"))
            for session in members:
                listing.add_option(Option(_session_label(session, current_id=current_id), id=session.id))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id and not event.option.disabled:
            self.post_message(self.Selected(event.option.id))

    def action_delete_selected(self) -> None:
        listing = self.query_one("#session-list", OptionList)
        option = listing.highlighted_option
        if option is not None and option.id and not option.disabled:
            self.post_message(self.DeleteRequested(option.id))


class MessageLog(RichLog):
    """Scrolling transcript of the session. The only place agent output lands."""


class StreamArea(Static):
    """Live text while the model streams; cleared once the turn settles."""


class ContextBar(Static):
    """One-line status bar, ported from pydantic-deepagents.

    Shows cost · in/out tokens · prompt-cache tokens · context usage · message
    count · model in a single row. It is the "how is the run going" glance line
    above the input.
    """

    def show_stats(self, stats: dict[str, Any]) -> None:
        ratio = float(stats.get("ratio", 0.0) or 0.0)
        cost = float(stats.get("cost", 0.0) or 0.0)
        tokens_in = int(stats.get("tokens_in", 0) or 0)
        tokens_out = int(stats.get("tokens_out", 0) or 0)
        messages = int(stats.get("messages", 0) or 0)
        model = str(stats.get("model", "") or "")
        short_model = model.split("/")[-1] if "/" in model else model.split(":")[-1]

        parts: list[tuple[str, str]] = []
        if cost > 0:
            parts.append((f"${cost:.2f}", "bold"))
        total = tokens_in + tokens_out
        if total > 0:
            parts.append((f"in:{fmt_tokens(tokens_in)} out:{fmt_tokens(tokens_out)}", "dim"))
        label = cache_label(stats)
        if label:
            parts.append((label, "dim"))
        if ratio > 0:
            bar = f"{context_bar(ratio)} {ratio:>3.0%}"
            parts.append((bar, context_style(ratio)))
        if messages:
            parts.append((f"{messages} msgs", "dim"))
        if short_model:
            parts.append((short_model, "bold"))
        if stats.get("compactions"):
            parts.append((f"compacted x{stats['compactions']}", "dim"))

        text = Text()
        for index, (chunk, style) in enumerate(parts):
            if index:
                text.append("  ", style="dim")
            text.append(chunk, style=style)
        self.update(text)


class SessionFooter(Static):

    """One-line footer: provider · model · workspace, ported from deepagents."""

    DEFAULT_CSS = """
    SessionFooter {
        height: 1;
        color: $text-muted;
        padding: 0 2;
    }
    """

    def refresh_session(self, *, provider: str = "", model: str = "", cwd: str = "") -> None:
        from rich.cells import cell_len

        short_model = model.split("/")[-1] if "/" in model else (model.split(":")[-1] if ":" in model else model)
        session = []
        if provider:
            session.append(f"[$accent]{provider}[/]")
        if short_model:
            session.append(short_model)
        workspace = self._short_path(cwd or ".")
        left = "  [$text-muted]·[/]  ".join(session) if session else ""
        full = f"{left}   {workspace}" if left else workspace
        if cell_len(self._strip(full)) > (self.size.width or 200) - 2 and left:
            full = left
        self.update(full)

    @staticmethod
    def _strip(markup: str) -> str:
        import re

        return re.sub(r"\[/?[^\]]*\]", "", markup)

    @staticmethod
    def _short_path(path: str) -> str:
        from pathlib import Path

        try:
            p = Path(path).expanduser()
            home = Path.home()
            text = f"~/{p.relative_to(home)}" if p.is_relative_to(home) else str(p)
        except Exception:
            text = path
        return text if len(text) <= 36 else "…" + text[-35:]


class HintsBar(Static):
    """One-line hint strip under the input, ported from pydantic-deepagents.

    Tells the user the core keys at a glance instead of hiding them in /help:
    history, slash commands, multiline, and the running state.
    """

    def reset(self) -> None:
        self.update(
            "[$accent]↑[/] history   "
            "[$accent]/[/] commands   "
            "[$accent]Shift+Enter[/] newline   "
            "[$accent]Enter[/] send   "
            "[$accent]Esc[/] interrupt"
        )

    def set_running(self, running: bool) -> None:
        if running:
            self.update("[$warning]● running — Esc to interrupt[/]")
        else:
            self.reset()


# --------------------------------------------------------------------------- #
# message stream (user / assistant / tool calls) — ported from deepagents
# --------------------------------------------------------------------------- #
class UserMessage(Static):
    """A single user turn: label + markdown body."""

    DEFAULT_CSS = """
    UserMessage {
        height: auto;
        padding: 0 2;
        margin: 1 0 0 0;
        border-left: thick $primary;
    }
    UserMessage .user-label {
        color: $accent;
        text-style: bold;
    }
    UserMessage .user-text {
        padding: 0 0 0 2;
    }
    """

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    def compose(self) -> ComposeResult:
        yield Static("you", classes="user-label")
        yield Static(Markdown(self._text), classes="user-text")


class SystemMessage(Static):
    """A dim system/status line (slash replies, errors, banner)."""

    DEFAULT_CSS = """
    SystemMessage {
        height: auto;
        padding: 0 2;
        margin: 0 0 0 0;
        color: $text-muted;
    }
    """

    def __init__(self, text: str) -> None:
        super().__init__(text, classes="system-message")


class ToolCallWidget(Static):
    """A collapsible tool call row: header + optional output preview."""

    DEFAULT_CSS = """
    ToolCallWidget {
        height: auto;
        padding: 0 2;
        margin: 0 0 0 1;
        border-left: tall $primary 40%;
    }
    ToolCallWidget .tool-header {
        height: 1;
        color: $text;
    }
    ToolCallWidget .tool-output {
        display: none;
        padding: 0 0 0 3;
        color: $text-muted;
    }
    ToolCallWidget .tool-output.visible {
        display: block;
    }
    """

    def __init__(self, tool_name: str, args: dict[str, Any], call_id: str) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.args = args
        self.call_id = call_id
        self.status = "pending"
        self._expanded = False
        self._header: Static | None = None
        self._output: Static | None = None
        self._pending_result: str | None = None

    def compose(self) -> ComposeResult:
        self._header = Static("", classes="tool-header")
        self._output = Static("", classes="tool-output")
        yield self._header
        yield self._output
        self._refresh_header()
        # A result may have arrived before the widget was mounted/composed
        # (tool_call_start and its result can arrive back-to-back). Render it
        # now that the output Static exists.
        if self._pending_result is not None:
            result, error = self._pending_result
            self._pending_result = None
            self._apply_result(result, error)

    def complete(self, result: str, elapsed: float, error: bool = False) -> None:
        self.status = "error" if error else "done"
        if self._output is None:
            self._pending_result = (result, error)
        else:
            self._apply_result(result, error)
        self._refresh_header()

    def _apply_result(self, result: str, error: bool) -> None:
        if self._output is not None and result.strip():
            self._output.update(self._render_output(result))
    def action_toggle_expand(self) -> None:
        self._expanded = not self._expanded
        if self._output is not None:
            self._output.set_class(self._expanded, "visible")

    def on_click(self, event) -> None:
        event.stop()
        self.action_toggle_expand()

    def _render_output(self, text: str, limit: int = 30) -> Text:
        """Render tool output, colouring unified-diff hunks green/red.

        ``edit``/``write`` return a ``difflib`` unified diff; bash/read return
        plain text. Only treat output as a diff when it has the tell-tale
        ``---``/``+++`` or ``@@`` headers, so a shell that echoes ``+1`` never
        gets painted like a code change.
        """
        lines = text.splitlines()
        is_diff = any(
            line.startswith(("+++", "---", "@@")) for line in lines[:8]
        )
        if len(lines) > limit:
            lines = lines[:limit]
            truncated = len(text.splitlines()) - limit
        else:
            truncated = 0

        rendered = Text()
        if not is_diff:
            rendered.append("\n".join(lines))
        else:
            for line in lines:
                if line.startswith(("+++", "---")):
                    rendered.append(line + "\n", style="bold")
                elif line.startswith("@@"):
                    rendered.append(line + "\n", style="cyan")
                elif line.startswith("+"):
                    rendered.append(line + "\n", style="green")
                elif line.startswith("-"):
                    rendered.append(line + "\n", style="red")
                else:
                    rendered.append(line + "\n", style="default")
        if truncated:
            rendered.append(f"\n... {truncated} more lines", style="dim")
        return rendered

    def _refresh_header(self) -> None:
        if self._header is None:
            return
        marker = {"pending": "●", "done": "✓", "error": "✗", "cancelling": "…"}.get(self.status, "›")
        color = {"pending": "$warning", "done": "$success", "error": "$error", "cancelling": "$text-muted"}.get(
            self.status, "$text"
        )
        args = format_arguments(self.args) if self.args else ""
        self._header.update(f"[{color}]{marker}[/] [b]{self.tool_name}[/] [dim]{args}[/]")


class AssistantMessage(Widget):
    """A single assistant turn: optional tool calls, then streaming markdown."""

    DEFAULT_CSS = """
    AssistantMessage {
        height: auto;
        margin: 1 0 0 0;
    }
    AssistantMessage .assistant-label {
        padding: 0 2;
        color: $accent;
        text-style: bold;
    }
    AssistantMessage .assistant-thinking {
        display: none;
        padding: 0 2;
        color: $text-muted;
    }
    AssistantMessage .assistant-text {
        padding: 0 2;
        height: auto;
    }
    AssistantMessage .assistant-usage {
        display: none;
        padding: 0 2;
        color: $text-muted;
    }
    """

    def __init__(self, timestamp: datetime | None = None) -> None:
        super().__init__()
        self._timestamp = timestamp or datetime.now()
        self._text = ""
        self._thinking = ""
        self._tool_widgets: dict[str, ToolCallWidget] = {}
        self._text_widget: Static | None = None
        self._label_widget: Static | None = None
        self._thinking_widget: Static | None = None

    def compose(self) -> ComposeResult:
        self._label_widget = Static(
            f"[$primary b]assistant[/]  [$text-muted]{self._timestamp.strftime('%H:%M')}[/]",
            classes="assistant-label",
        )
        yield self._label_widget
        self._thinking_widget = Static("", classes="assistant-thinking")
        self._thinking_widget.display = False
        yield self._thinking_widget
        self._text_widget = Static("", classes="assistant-text")
        yield self._text_widget

    def append_thinking(self, delta: str) -> None:
        """Append a streaming thinking delta, shown as a live dim panel."""
        self._thinking += delta
        if self._thinking_widget is not None:
            self._thinking_widget.display = True
            self._thinking_widget.update(self._render_thinking())

    def finalize_thinking(self) -> None:
        """Collapse the thinking panel to a one-line summary once it completes."""
        if self._thinking_widget is None or not self._thinking.strip():
            return
        lines = [ln for ln in self._thinking.splitlines() if ln.strip()]
        n = len(lines)
        plural = "s" if n != 1 else ""
        self._thinking_widget.update(f"[dim italic]💭 Thought for {n} line{plural}[/dim italic]")

    def _render_thinking(self) -> str:
        lines = [ln.strip() for ln in self._thinking.splitlines() if ln.strip()]
        header = "[dim italic]💭 Thinking…[/dim italic]"
        if not lines:
            return header
        tail = lines[-_THINKING_TAIL_LINES:]
        body = "\n".join(f"[dim italic]{_esc(ln)}[/dim italic]" for ln in tail)
        return f"{header}\n{body}"

    def add_tool_call(self, tool_name: str, args: dict[str, Any], call_id: str) -> ToolCallWidget:
        widget = ToolCallWidget(tool_name, args, call_id)
        self._tool_widgets[call_id] = widget
        if self._text_widget is not None:
            self.mount(widget, before=self._text_widget)
        else:
            self.mount(widget)
        return widget

    def complete_tool_call(self, call_id: str, result: str, elapsed: float, error: bool = False) -> None:
        widget = self._tool_widgets.get(call_id)
        if widget is not None and widget.status == "pending":
            widget.complete(result, elapsed, error)
    def append_text(self, delta: str) -> None:
        self._text += delta
        self._render_text()
    @property
    def text(self) -> str:
        return self._text
    def _render_text(self) -> None:
        if self._text_widget is None:
            return
        body = self._text.strip()
        self._text_widget.update(Markdown(body) if body else "")


class MessageList(VerticalScroll):
    """Scrollable transcript of user/assistant messages with tool calls."""

    _current_assistant: AssistantMessage | None = None

    def clear_messages(self) -> None:
        """Clear the visible transcript (used for /new, /clear and session replay)."""
        self._current_assistant = None
        self.remove_children()

    def append_user_message(self, text: str) -> UserMessage:
        self._current_assistant = None
        message = UserMessage(text)
        self.mount(message)
        self.scroll_end(animate=False)
        return message

    def append_system_message(self, text: str) -> SystemMessage:
        message = SystemMessage(text)
        self.mount(message)
        self.scroll_end(animate=False)
        return message

    def begin_assistant_message(self) -> AssistantMessage:
        message = AssistantMessage()
        self._current_assistant = message
        self.mount(message)
        self.scroll_end(animate=False)
        return message

    @property
    def current_assistant(self) -> AssistantMessage | None:
        return self._current_assistant


# --------------------------------------------------------------------------- #
# slash command popover
# --------------------------------------------------------------------------- #
class Suggestion(Protocol):
    """Anything the popover can list -- in practice, a ``SlashCommand``.

    Duck-typed on purpose: the widget knows how to lay a row out, not what a
    command is or where commands come from.
    """

    trigger: str
    title: str
    description: str
    keybind: str


class SlashPopover(OptionList):
    """The list of commands that opens when you type ``/``.

    It never takes focus -- OpenCode keeps the caret in the composer and
    forwards the navigation keys to the list, so you can keep typing while the
    list narrows. Selection therefore arrives as a message, not as a binding.
    """

    class Picked(Message):
        """The user accepted a row.

        ``submit`` separates completing from running: ``tab`` fills the command
        in and leaves the caret there, ``enter`` fills it in *and* sends it.
        """

        def __init__(self, trigger: str, *, submit: bool = False) -> None:
            super().__init__()
            self.trigger = trigger
            self.submit = submit

    #: Hidden until the composer asks for suggestions.
    display = False

    def show(self, suggestions: list[Suggestion], *, width: int | None = None) -> None:
        """Rebuild the list. Empty input means the popover stays hidden."""
        # Custom commands carry longer names (and argument hints) than builtins,
        # so the title column is sized to what is actually on screen.
        if width is None:
            width = min(34, max((len(suggestion.title) for suggestion in suggestions), default=12) + 2)
        self.clear_options()
        for suggestion in suggestions:
            self.add_option(Option(self._row(suggestion, width=width), id=suggestion.trigger))
        self.display = bool(suggestions)
        self.highlighted = 0 if suggestions else None
        if suggestions:
            self.scroll_to_highlight()

    def hide(self) -> None:
        self.display = False
        self.clear_options()
        self.highlighted = None

    @property
    def is_open(self) -> bool:
        return self.display

    def move(self, delta: int) -> None:
        """Move the highlight, wrapping at both ends (OpenCode loops too)."""
        if not self.display or not self.option_count:
            return
        current = self.highlighted or 0
        self.highlighted = (current + delta) % self.option_count
        self.scroll_to_highlight()

    def accept(self, *, submit: bool = False) -> None:
        """Confirm the highlighted row.

        ``submit`` is what makes ``tab`` and ``enter`` behave differently: both
        complete the name, only ``enter`` runs it.
        """
        if not self.display or self.highlighted is None or not self.option_count:
            return
        option = self.get_option_at_index(self.highlighted)
        if option is not None and option.id:
            self.post_message(self.Picked(option.id, submit=submit))

    # A click is an accept too; the keyboard path goes through the composer.
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self.post_message(self.Picked(event.option.id))

    @staticmethod
    def _row(suggestion: Suggestion, *, width: int) -> Text:
        text = Text()
        text.append(suggestion.title.ljust(width), style="bold")
        text.append("  ")
        text.append(suggestion.description, style="dim")
        if suggestion.keybind:
            text.append(f"  {suggestion.keybind}", style="grey62")
        return text


class Composer(TextArea):
    """Multi-line prompt input with an OpenCode-style ``/`` popover.

    ``enter`` submits, ``shift+enter`` starts a new line, ``ctrl+up``/``ctrl+down``
    walk back through previous submissions. While the command popover is open,
    ``up``/``down``/``tab``/``enter`` belong to it and ``escape`` dismisses it.
    """

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class SlashQuery(Message):
        """The composer is asking for suggestions for this partial command."""

        def __init__(self, query: str) -> None:
            super().__init__()
            self.query = query

    class SlashDismissed(Message):
        """The popover should close."""

    class SlashAccepted(Message):
        """Confirm whatever row the popover has highlighted."""

    class SlashMoved(Message):
        def __init__(self, delta: int) -> None:
            super().__init__()
            self.delta = delta

    # ``enter``/``shift+enter`` are handled in ``_on_key`` below: TextArea's own
    # bindings would otherwise swallow them before an action could run. Only the
    # history keys are declared here, where they are handled as normal actions.
    BINDINGS = [
        Binding("ctrl+up", "history_previous", "Previous", show=False),
        Binding("ctrl+down", "history_next", "Next", show=False),
    ]

    def __init__(self, placeholder: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.placeholder = placeholder
        self._history: list[str] = []
        self._history_index = -1
        self._slash_query: str | None = None

    # ------------------------------------------------------------------ #
    # slash popover
    # ------------------------------------------------------------------ #
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Keep the popover in sync with whatever is being typed.

        A space ends the command (you are on to the arguments), and so does
        anything that is not a command at all.
        """
        text = self.text
        if text.startswith("/") and not any(char.isspace() for char in text):
            if text != self._slash_query:
                self._slash_query = text
                self.post_message(self.SlashQuery(text))
        else:
            self.dismiss_slash()

    def dismiss_slash(self) -> None:
        """Close the popover (idempotent, and safe to call from the app)."""
        if self._slash_query is not None:
            self._slash_query = None
            self.post_message(self.SlashDismissed())

    def apply_completion(self, text: str) -> None:
        """Replace the input with a completed command and close the popover."""
        self._slash_query = None
        self.load_text(text)
        self.move_cursor(self.document.end)
        self.post_message(self.SlashDismissed())

    async def _on_key(self, event) -> None:
        if self._slash_query is not None:
            if event.key in {"down", "ctrl+n"}:
                event.stop()
                event.prevent_default()
                self.post_message(self.SlashMoved(1))
                return
            if event.key in {"up", "ctrl+p"}:
                event.stop()
                event.prevent_default()
                self.post_message(self.SlashMoved(-1))
                return
            if event.key in {"tab", "enter"}:
                event.stop()
                event.prevent_default()
                self.post_message(self.SlashAccepted())
                return
            if event.key == "escape":
                event.stop()
                event.prevent_default()
                self.dismiss_slash()
                return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.action_submit()
            return
        if event.key == "shift+enter":
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        await super()._on_key(event)

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #
    def action_submit(self) -> None:
        text = self.text.strip()
        if not text:
            return
        self.dismiss_slash()
        self._history.append(text)
        self._history_index = -1
        self.clear()
        self.post_message(self.Submitted(text))

    def action_history_previous(self) -> None:
        self._recall(-1)

    def action_history_next(self) -> None:
        self._recall(1)

    def _recall(self, step: int) -> None:
        if not self._history:
            return
        if self._history_index == -1:
            if step > 0:
                return  # not browsing history yet - "next" has nothing to return to
            self._history_index = len(self._history)
        self._history_index = max(0, min(len(self._history) - 1, self._history_index + step))
        self.load_text(self._history[self._history_index])


@dataclass
class PermissionRequest:
    """A permission question, rendered by :class:`PermissionBar`."""

    permission: str
    patterns: list[str]
    tool: str = ""

    def render(self) -> Text:
        text = Text()
        text.append(" permission ", style="bold black on yellow")
        text.append("  ")
        text.append(self.permission, style="bold")
        if self.tool:
            text.append(f" ({self.tool})", style="dim")
        for pattern in self.patterns:
            text.append(f"\n   {pattern}", style="dim")
        text.append("\n ")
        text.append("[y]", style="bold green")
        text.append(" once   ")
        text.append("[a]", style="bold green")
        text.append(" always   ")
        text.append("[n]", style="bold red")
        text.append(" reject")
        return text


class PermissionBar(Vertical):
    """Inline permission prompt built from native Textual components.

    The three answers are real :class:`~textual.widgets.Button` widgets rather
    than hand-mapped text regions, so clicking and keyboard activation come
    from the framework instead of being re-implemented here. ``y``/``a``/``n``
    and ``escape`` still work as shortcuts via the container key handler.
    """

    class Answered(Message):
        def __init__(self, answer: str) -> None:
            super().__init__()
            self.answer = answer

    can_focus = True
    display = False

    _ANSWER_BY_ID = {"perm-y": "y", "perm-a": "a", "perm-n": "n"}

    def compose(self) -> ComposeResult:
        yield Static(id="permission-prompt")
        with Horizontal(id="permission-actions"):
            yield Button("[y] once", id="perm-y", variant="success")
            yield Button("[a] always", id="perm-a", variant="primary")
            yield Button("[n] reject", id="perm-n", variant="error")

    def ask(self, renderable: Any) -> None:
        """Show ``renderable`` and take focus so the answer keys reach us.

        ``focus()`` cannot run in the same synchronous block as ``display = True``:
        Textual only lets a widget take focus once it has been laid out, so the
        call would silently fail and the composer would keep the keys. Deferring
        to just after the next refresh makes the prompt actually grab the input.
        """
        self._prompt.update(renderable)
        self.display = True
        self.call_after_refresh(self.focus)

    def show_prompt(self, renderable: Any) -> None:
        """Show a text-only prompt (e.g. the /login wizard); no answer buttons.

        The composer keeps focus and supplies the answer, so this variant must
        not steal focus the way a permission question does.
        """
        self._prompt.update(renderable)
        self.display = True

    def close(self) -> None:
        self.display = False
        self._prompt.update("")

    @property
    def _prompt(self) -> Static:
        return self.query_one("#permission-prompt", Static)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if not self.display:
            return
        answer = self._ANSWER_BY_ID.get(event.button.id)
        if answer is not None:
            self.post_message(self.Answered(answer))

    def on_key(self, event) -> None:
        if not self.display:
            return
        key = event.key.lower()
        if key in {"y", "a", "n"}:
            event.stop()
            self.post_message(self.Answered(key))
        elif key == "escape":
            event.stop()
            self.post_message(self.Answered("n"))
