"""The Textual application shell.

Holds the layout, owns the worker thread that runs the agent, and mediates the
blocking prompts (permission, login) that the agent needs answered. No agent
logic lives here -- the core is driven through ``InteractiveApp`` and observed
through :class:`~minicode.ui.textual.bridge.TextualUI`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rich.rule import Rule
from rich.text import Text
from textual.app import App, ComposeResult
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Header

from minicode import __version__
from minicode.cli.app import InteractiveApp
from minicode.cli.commands import find_command, handle_slash, match_commands
from minicode.config.settings import Settings, load_settings
from minicode.permission.manager import AskReply
from minicode.providers.registry import build_registry
from minicode.session.models import Session
from minicode.ui.render import transcript
from minicode.ui.textual import theme
from minicode.ui.textual.bridge import TextualUI
from minicode.ui.textual.modals import push_model_picker
from minicode.ui.textual.widgets import (
    Composer,
    ContextBar,
    HintsBar,
    MessageList,
    MessageLog,
    PermissionBar,
    SessionFooter,
    SessionSidebar,
    SlashPopover,
    StreamArea,
)

__all__ = ["MiniTUI", "run_tui"]

_PERMISSION_ANSWERS = {"y": AskReply.ONCE, "a": AskReply.ALWAYS, "n": AskReply.REJECT}


@dataclass
class _PendingPrompt:
    """A blocking question asked by the agent thread, answered by the UI thread."""

    event: threading.Event
    kind: str
    answer: Any = None


class _Commands(Provider):
    """Command palette (ctrl+p)."""

    async def discover(self) -> Hits:
        app = self.app
        entries = [
            ("New session", "Start a fresh session", app.action_new_session),
            ("Sessions", "List saved sessions", app.action_show_sessions),
            ("Models", "List providers and models", app.action_show_models),
            ("Tools", "List enabled tools", app.action_show_tools),
            ("Replay session", "Redraw the current session's history", app.action_replay_session),
            ("Interrupt", "Stop the running turn", app.action_interrupt),
            ("Toggle theme", "Cycle the color palette", app.action_cycle_theme),
            ("Clear transcript", "Clear the message log", app.action_clear_log),
            ("Quit", "Exit minicode", app.action_quit),
        ]
        for name, help_text, callback in entries:
            yield Hit(1, name, callback, help=help_text)

    async def search(self, query: str) -> Hits:
        needle = query.lower()
        async for hit in self.discover():
            if needle in hit.text.lower():
                yield hit


class MiniTUI(App[None]):
    """Full-screen TUI in the spirit of OpenCode."""

    TITLE = "minicode"
    CSS = theme.CSS
    COMMANDS = {_Commands}

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+l", "clear_log", "Clear"),
        ("ctrl+p", "command_palette", "Commands"),
        ("ctrl+n", "new_session", "New session"),
        ("ctrl+t", "toggle_dark", "Dark"),
        ("ctrl+e", "cycle_theme", "Palette"),
        ("escape", "interrupt", "Interrupt"),
    ]

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        cwd: str | None = None,
        model: str | None = None,
        session_id: str | None = None,
        yolo: bool = False,
    ) -> None:
        super().__init__()
        self.settings = settings or Settings()
        self.cwd = cwd
        self.model = model
        self.session_id = session_id
        self.yolo = yolo
        self.ui: TextualUI | None = None
        self.core: InteractiveApp | None = None
        self._pending: _PendingPrompt | None = None
        self._busy = False

    # ------------------------------------------------------------------ #
    # layout
    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            yield SessionSidebar(id="sidebar")
            with Vertical(id="main"):
                yield MessageList(id="messages")
                yield MessageLog(id="log", highlight=True, markup=True, wrap=True)
                yield StreamArea(id="stream")
                yield PermissionBar(id="permission")
                yield ContextBar(id="context")
                yield SessionFooter(id="session-footer")
                yield HintsBar(id="hints")
                yield SlashPopover(id="slash")
                yield Composer(id="composer", placeholder="Ask minicode anything...")
        yield Footer()

    def on_mount(self) -> None:
        theme.register_themes(self)
        theme.apply_theme(self, self.settings.ui.theme if self.settings.ui.theme != "auto" else "default")
        self.ui = TextualUI(self.settings.ui, self)
        self.core = InteractiveApp(
            self.settings,
            cwd=self.cwd,
            model=self.model,
            session_id=self.session_id,
            yolo=self.yolo,
            non_interactive=False,
            stream=True,
            ui=self.ui,
            sink=self.ui,
        )
        self._sync_sub_title()
        self.ui.banner(
            version=__version__,
            provider=self.core.provider.name if self.core.provider is not None else "not-configured",
            model=self.core.provider.model if self.core.provider is not None else "use /login",
            cwd=self.core.cwd,
            session=self.core.session.id,
        )
        self.refresh_sessions()
        self.query_one("#hints", HintsBar).reset()
        self.query_one("#composer", Composer).focus()

    # ------------------------------------------------------------------ #
    # widget helpers (called from the bridge, from either thread)
    # ------------------------------------------------------------------ #
    def post(self, callback: Callable[..., Any], *args: Any) -> None:
        """Run ``callback`` on the UI thread, from either thread.

        ``call_from_thread`` deliberately refuses to run *on* the app thread (it
        would deadlock the very loop it is queuing onto), but half of the core's
        output is produced there -- the banner during mount, slash-command
        replies, the final stats line. This is the one place that knows which
        case it is looking at.
        """
        if threading.get_ident() == self._thread_id:
            callback(*args)
        else:
            self.call_from_thread(callback, *args)

    def append_to_log(self, renderable: Any) -> None:
        # System output (slash replies, banner, errors, replay) is mirrored into
        # the main message stream so it is actually visible; the RichLog below
        # is hidden (kept only so tests that patch append_to_log still pass).
        log = self.query_one("#log", MessageLog)
        log.write(renderable)
        log.scroll_end(animate=False)
        self._append_system_message_ui(str(renderable))

    def _append_system_message_ui(self, text: str) -> None:
        text = text.strip()
        if text:
            self.query_one("#messages", MessageList).append_system_message(text)

    def set_stream(self, renderable: Any) -> None:
        self.query_one("#stream", StreamArea).update(renderable)

    # ------------------------------------------------------------------ #
    # message stream (called from the bridge, from either thread)
    # ------------------------------------------------------------------ #
    def append_user_message(self, text: str) -> None:
        self.post(self._append_user_message_ui, text)

    def _append_user_message_ui(self, text: str) -> None:
        self.query_one("#messages", MessageList).append_user_message(text)

    def append_assistant_text(self, text: str) -> None:
        self.post(self._append_assistant_text_ui, text)

    def _append_assistant_text_ui(self, text: str) -> None:
        messages = self.query_one("#messages", MessageList)
        if messages.current_assistant is None:
            messages.begin_assistant_message()
        assert messages.current_assistant is not None
        messages.current_assistant.append_text(text)
        messages.scroll_end(animate=False)

    def append_assistant_thinking(self, text: str) -> None:
        self.post(self._append_assistant_thinking_ui, text)

    def _append_assistant_thinking_ui(self, text: str) -> None:
        messages = self.query_one("#messages", MessageList)
        if messages.current_assistant is None:
            messages.begin_assistant_message()
        assert messages.current_assistant is not None
        messages.current_assistant.append_thinking(text)
        messages.scroll_end(animate=False)

    def finalize_assistant_thinking(self) -> None:
        self.post(self._finalize_assistant_thinking_ui)

    def _finalize_assistant_thinking_ui(self) -> None:
        messages = self.query_one("#messages", MessageList)
        if messages.current_assistant is not None:
            messages.current_assistant.finalize_thinking()
            messages.scroll_end(animate=False)

    def add_tool_call(self, tool_name: str, args: dict[str, Any], call_id: str) -> None:
        self.post(self._add_tool_call_ui, tool_name, args, call_id)

    def _add_tool_call_ui(self, tool_name: str, args: dict[str, Any], call_id: str) -> None:
        messages = self.query_one("#messages", MessageList)
        if messages.current_assistant is None:
            messages.begin_assistant_message()
        assert messages.current_assistant is not None
        messages.current_assistant.add_tool_call(tool_name, args, call_id)
        messages.scroll_end(animate=False)

    def complete_tool_call(self, call_id: str, result: str, error: bool = False) -> None:
        self.post(self._complete_tool_call_ui, call_id, result, error)

    def _complete_tool_call_ui(self, call_id: str, result: str, error: bool = False) -> None:
        messages = self.query_one("#messages", MessageList)
        if messages.current_assistant is not None:
            messages.current_assistant.complete_tool_call(call_id, result, 0.0, error)
            messages.scroll_end(animate=False)

    def show_stats(self, stats: dict[str, Any]) -> None:
        self.query_one("#context", ContextBar).show_stats(stats)
        self._refresh_session_footer(
            provider=str(stats.get("provider", "") or ""),
            model=str(stats.get("model", "") or ""),
            cwd=str(stats.get("cwd", "") or (self.core.cwd if self.core else "")),
        )

    def _refresh_session_footer(self, *, provider: str = "", model: str = "", cwd: str = "") -> None:
        if self.core is None:
            return
        if not provider:
            provider = self.core.provider.name if self.core.provider is not None else ""
        if not model:
            model = self.core.provider.model if self.core.provider is not None else ""
        if not cwd:
            cwd = self.core.cwd
        self.query_one("#session-footer", SessionFooter).refresh_session(
            provider=provider, model=model, cwd=cwd
        )

    # ------------------------------------------------------------------ #
    # blocking prompts (agent thread -> UI thread -> agent thread)
    # ------------------------------------------------------------------ #
    def request_permission(self, request: Any) -> AskReply:
        """Called on the agent's worker thread; blocks until the user answers."""
        answer = self._prompt(request.render(), kind="permission")
        return _PERMISSION_ANSWERS.get(answer, AskReply.REJECT)

    def ask_text(self, prompt: str) -> str:
        """Free-form line input, used by the /login wizard.

        Rendered as plain text (not markup): prompts such as "Provider name or
        number [openai]:" contain square brackets that would otherwise be
        parsed as a Rich style and crash with MissingStyle.
        """
        return str(self._prompt(Text(prompt, style="bold cyan"), kind="text") or "")

    def _prompt(self, renderable: Any, *, kind: str) -> Any:
        pending = _PendingPrompt(threading.Event(), kind=kind)
        self._pending = pending
        if kind == "text":
            self.call_from_thread(self._focus_composer_for_prompt, renderable)
        else:
            self.call_from_thread(self._show_prompt, renderable)
        # Park the agent thread until the UI thread answers. Teardown happens on
        # the UI thread (see ``on_permission_bar_answered`` / ``on_composer_submitted``):
        # the thread that owns the widgets is the one that puts them back.
        pending.event.wait()
        return pending.answer

    def _show_prompt(self, renderable: Any) -> None:
        self.query_one("#permission", PermissionBar).ask(renderable)

    def _hide_prompt(self) -> None:
        bar = self.query_one("#permission", PermissionBar)
        bar.close()
        self.query_one("#composer", Composer).focus()

    def _focus_composer_for_prompt(self, renderable: Any) -> None:
        """Text answers come from the composer; the prompt is shown inline."""
        bar = self.query_one("#permission", PermissionBar)
        bar.show_prompt(renderable)
        self.query_one("#composer", Composer).focus()

    def on_permission_bar_answered(self, event: PermissionBar.Answered) -> None:
        """Close the prompt and release the agent thread, whoever asked for it."""
        pending, self._pending = self._pending, None
        self._hide_prompt()
        if pending is not None:
            pending.answer = event.answer
            pending.event.set()

    # ------------------------------------------------------------------ #
    # slash command popover
    # ------------------------------------------------------------------ #
    def on_composer_slash_query(self, event: Composer.SlashQuery) -> None:
        """Narrow the popover to whatever matches the partial command."""
        popover = self.query_one("#slash", SlashPopover)
        suggestions = match_commands(event.query)
        if suggestions:
            popover.show(list(suggestions))
        else:
            popover.hide()
            # Nothing to pick from, so stop intercepting up/down/enter.
            self.query_one("#composer", Composer).dismiss_slash()

    def on_composer_slash_dismissed(self, event: Composer.SlashDismissed) -> None:
        self.query_one("#slash", SlashPopover).hide()

    def on_composer_slash_moved(self, event: Composer.SlashMoved) -> None:
        self.query_one("#slash", SlashPopover).move(event.delta)

    def on_composer_slash_accepted(self, event: Composer.SlashAccepted) -> None:
        self.query_one("#slash", SlashPopover).accept()

    def on_slash_popover_picked(self, event: SlashPopover.Picked) -> None:
        """Run the chosen command, or insert it if it still needs an argument.

        OpenCode fires builtins the moment you pick them (picking ``/help`` shows
        help) and only leaves you typing when there is an argument to fill in.
        """
        command = find_command(event.trigger)
        composer = self.query_one("#composer", Composer)
        if command is not None and command.takes_args:
            composer.apply_completion(f"{command.trigger} ")
            return
        # Routed through the normal submit path so it lands in the input
        # history like anything else the user typed.
        composer.load_text(command.trigger if command is not None else event.trigger)
        composer.action_submit()

    # ------------------------------------------------------------------ #
    # input
    # ------------------------------------------------------------------ #
    def on_composer_submitted(self, event: Composer.Submitted) -> None:
        text = event.text.strip()
        if not text:
            return
        if self._pending is not None and self._pending.kind == "text":
            pending, self._pending = self._pending, None
            self._hide_prompt()
            pending.answer = text
            pending.event.set()
            return
        if text.startswith("/"):
            self.run_command(text)
        else:
            self.run_task(text)

    def run_command(self, text: str) -> None:
        """Run a slash command. Returns immediately; long ones use a worker."""
        if self.core is None or self.ui is None:
            return
        head = text.split()[0].lower() if text.split() else ""
        command = find_command(head)
        if command is not None and command.trigger == "/exit":
            self.exit()
            return
        if command is not None and command.trigger == "/login":
            self._login(text.split()[1] if len(text.split()) > 1 else None)
            return
        if command is not None and command.trigger == "/model" and len(text.split()) == 1:
            self._open_model_picker()
            return
        previous_id = self.core.session.id
        if handle_slash(self.core, text) == "continue":
            self._sync_sub_title()
            self.refresh_sessions()
            # /resume and /fork switched sessions: show that history, not the
            # one we were looking at. A /new lands on an empty session, which
            # has nothing to replay (and the log is already cleared).
            if self.core.session.id != previous_id and self.core.session.messages:
                self.replay_session(self.core.session)

    def _open_model_picker(self) -> None:
        if self.core is None:
            return
        models = self.core.providers.list_models()
        current = self.core.provider.model_id if self.core.provider is not None else ""
        if not models:
            self.ui.print_info("no providers configured - use /login to set one up")
            return

        def on_pick(spec: str | None) -> None:
            if not spec or self.core is None:
                return
            try:
                self.core.set_model(spec)
            except (KeyError, ValueError) as exc:
                self.ui.print_error(str(exc))
                return
            self._sync_sub_title()
            self.refresh_sessions()
            self.ui.print_info(f"switched to {self.core.provider.model_id}")

        push_model_picker(self, models, current, on_pick)

    def run_task(self, text: str) -> None:
        """Run one agent turn on a worker thread so the UI stays responsive."""
        if self._busy:
            self.ui.print_info("still busy - wait for the current turn to finish")
            return
        self._busy = True
        self.query_one("#hints", HintsBar).set_running(True)
        threading.Thread(target=self._task_worker, args=(text,), daemon=True).start()

    def _task_worker(self, text: str) -> None:
        try:
            if self.core is not None:
                self.core.run_task(text)
        finally:
            self._busy = False
            self.call_from_thread(self._hint_idle)

    def _hint_idle(self) -> None:
        self.query_one("#hints", HintsBar).set_running(False)
        self.refresh_sessions()

    def _login(self, name: str | None) -> None:
        if self._busy:
            self.ui.print_info("still busy - wait for the current turn to finish")
            return
        self._busy = True
        threading.Thread(target=self._login_worker, args=(name,), daemon=True).start()

    def _login_worker(self, name: str | None) -> None:
        try:
            from minicode.cli.provider_config import configure_provider

            result = configure_provider(name, input_fn=self.ask_text, password_fn=self.ask_text)
            self.core.settings = load_settings(cwd=self.core.cwd)
            self.core.providers = build_registry(self.core.settings.model_dump())
            self.core.set_model(f"{result.name}/{result.default_model}")
            # Reactive attributes belong to the UI thread; the header update has
            # to be queued like any other widget mutation from a worker.
            self.call_from_thread(self._sync_sub_title)
            self.ui.print_info(f"provider '{result.name}' configured (saved to {result.path})")
        except (ValueError, KeyError, KeyboardInterrupt) as exc:
            self.ui.print_error(f"login failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
            self.ui.print_error(f"login failed: {type(exc).__name__}: {exc}")
        finally:
            self._busy = False
            self.call_from_thread(self.refresh_sessions)

    # ------------------------------------------------------------------ #
    # session rail
    # ------------------------------------------------------------------ #
    def _sync_sub_title(self) -> None:
        """Re-read provider/model into the header and the session footer.

        ``/model`` and a session switch both change it, and a header that lies
        about which model answered you is worse than one that says nothing.
        """
        if self.core is None:
            return
        provider = self.core.provider.name if self.core.provider is not None else "not-configured"
        model = self.core.provider.model if self.core.provider is not None else "use /login"
        self.sub_title = f"{provider}/{model}"
        self._refresh_session_footer()

    def refresh_sessions(self) -> None:
        if self.core is None:
            return
        sidebar = self.query_one("#sidebar", SessionSidebar)
        sidebar.set_sessions(self.core.known_sessions(cwd=self.core.cwd), current_id=self.core.session.id)
        self._refresh_session_footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "new-session":
            self.action_new_session()

    def on_session_sidebar_selected(self, event: SessionSidebar.Selected) -> None:
        if self.core is None or self.ui is None or self._busy:
            return
        if event.session_id == self.core.session.id:
            return
        try:
            session = self.core.resume(event.session_id)
        except (KeyError, ValueError) as exc:
            self.ui.print_error(str(exc))
            return
        self.replay_session(session)
        self._sync_sub_title()
        self.refresh_sessions()
        self.query_one("#composer", Composer).focus()

    def on_session_sidebar_delete_requested(self, event: SessionSidebar.DeleteRequested) -> None:
        if self.core is None or self.ui is None or self._busy:
            return
        session_id = event.session_id
        was_current = self.core.session.id == session_id
        if self.core.delete_session(session_id):
            self.ui.print_info(f"deleted session {session_id}")
            if was_current:
                self.action_clear_log()
                self.ui.print_info("started a new session")
        else:
            self.ui.print_error(f"session not found: {session_id}")
            return
        self.refresh_sessions()
        self.query_one("#composer", Composer).focus()

    def replay_session(self, session: Session | None = None) -> None:
        """Redraw a session's stored messages into the transcript.

        Resuming used to print one summary line and leave the timeline blank, so
        every past session looked empty. The messages were on disk the whole
        time -- nothing was rendering them.
        """
        if self.core is None or self.ui is None:
            return
        session = session or self.core.session
        self.action_clear_log()
        self.append_to_log(Rule(f"{session.title} ({session.id})", style="grey35"))
        for renderable in transcript(session.messages, max_output_lines=self.settings.ui.max_output_lines):
            self.append_to_log(renderable)
        if session.messages:
            self.ui.print_info(f"replayed {session.message_count} messages, {session.tool_call_count} tool calls")
        else:
            self.ui.print_info("this session has no messages yet")

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #
    def action_clear_log(self) -> None:
        self.query_one("#log", MessageLog).clear()
        self.query_one("#messages", MessageList).clear_messages()

    def action_cycle_theme(self) -> None:
        """Cycle through the ported deepagents palettes (ctrl+e)."""
        names = theme.available_themes()
        if not names:
            return
        current = theme.short_name(self)
        index = names.index(current) if current in names else 0
        next_name = names[(index + 1) % len(names)]
        theme.apply_theme(self, next_name)
        self.notify(f"palette: {next_name}", timeout=2)

    def action_new_session(self) -> None:
        if self.core is None or self.ui is None:
            return
        session = self.core.new_session()
        self.action_clear_log()
        self.ui.print_info(f"new session {session.id}")
        self.refresh_sessions()

    def action_replay_session(self) -> None:
        self.replay_session()

    def action_interrupt(self) -> None:
        """Stop the running turn at the next step boundary (OpenCode's escape)."""
        if self.core is None or self.ui is None:
            return
        if not self._busy:
            self.ui.print_info("nothing is running")
            return
        self.core.interrupt()
        self.ui.print_info("interrupting after the current step...")

    def action_show_sessions(self) -> None:
        self.run_command("/sessions")

    def action_show_models(self) -> None:
        self.run_command("/models")

    def action_show_tools(self) -> None:
        self.run_command("/tools")

    def action_quit(self) -> None:
        self.exit()


def run_tui(
    settings: Settings | None = None,
    *,
    cwd: str | None = None,
    model: str | None = None,
    session_id: str | None = None,
    yolo: bool = False,
) -> None:
    """Start the full-screen TUI (blocking)."""
    MiniTUI(settings, cwd=cwd, model=model, session_id=session_id, yolo=yolo).run()
