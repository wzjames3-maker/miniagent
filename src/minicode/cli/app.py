"""The interactive application: wires config -> registry -> session -> agent -> UI."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from minicode import __version__
from minicode.agent.core import CodingAgent
from minicode.agent.state import AgentStatus
from minicode.cli.commands import SlashCommand
from minicode.commands import CommandStore, render_template
from minicode.config.settings import Settings
from minicode.context.manager import ContextManager
from minicode.permission.manager import PermissionManager, PermissionMode
from minicode.permission.policy import Rule
from minicode.providers.registry import ProviderRegistry, build_registry
from minicode.session.manager import SessionManager
from minicode.session.models import Session
from minicode.tools.registry import ToolRegistry, build_default_registry
from minicode.ui.console import ConsoleUI
from minicode.ui.events import EventSink
from minicode.ui.port import UIFrontEnd
from minicode.ui.prompt import create_reader

__all__ = ["InteractiveApp", "AppHooks"]


def _same_cwd(path: str, target: str) -> bool:
    """Compare two working-directory strings after normalising symlinks/``..``."""
    try:
        return str(Path(path).expanduser().resolve()) == target
    except OSError:
        return str(Path(path).expanduser().absolute()) == target


class AppHooks:
    """Optional callbacks so tests/embedders can observe the app."""

    def on_ready(self, app: InteractiveApp) -> None: ...
    def on_turn_end(self, app: InteractiveApp, result: dict[str, Any]) -> None: ...


class InteractiveApp:
    """Owns the whole interactive session lifecycle."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        cwd: str | None = None,
        model: str | None = None,
        session_id: str | None = None,
        yolo: bool = False,
        non_interactive: bool = False,
        stream: bool | None = None,
        ui: UIFrontEnd | None = None,
        sink: EventSink | None = None,
        sessions: SessionManager | None = None,
        hooks: AppHooks | None = None,
        reader: Any = None,
    ):
        self.settings = settings or Settings()
        self.cwd = str(Path(cwd).resolve()) if cwd else os.getcwd()
        self.non_interactive = non_interactive
        self.hooks = hooks
        self.ui: UIFrontEnd = ui or ConsoleUI(self.settings.ui)
        self.sink: EventSink = sink or self.ui
        self.sessions = sessions or SessionManager()
        self.reader = reader

        # --- providers ------------------------------------------------- #
        self.providers: ProviderRegistry = build_registry(self.settings.model_dump())
        if model:
            self.provider = self.providers.get(model)
        else:
            try:
                self.provider = self._pick_provider()
            except RuntimeError:
                if non_interactive:
                    raise
                # Interactive TUI: allow starting with no provider so the user
                # can configure one from inside via /login.
                self.provider = None
        if self.provider is not None:
            self.providers.default_provider = self.provider.name
            self.providers.default_model = self.provider.model

        # --- tools ------------------------------------------------------ #
        self.tools: ToolRegistry = self._build_tools()

        # --- permissions ------------------------------------------------ #
        # Auto-approval is opt-in via --yolo / mode: auto. Running
        # non-interactively (`minicode run`, CI, pipes) is NOT a request to
        # approve everything: with nobody to ask, unmatched rules fail closed.
        mode = PermissionMode.AUTO if yolo else PermissionMode.DEFAULT
        self.permission = PermissionManager(
            ruleset=self._permission_rules(),
            ask_callback=None if non_interactive else self.ui.ask_permission,
            mode=mode,
            non_interactive=non_interactive,
        )

        # --- context ---------------------------------------------------- #
        self.context = ContextManager(self.settings.context)

        # --- commands ----------------------------------------------- #
        # Builtins plus whatever .md files are on disk. The popover, the
        # dispatcher and /help all read from this one object.
        self.commands = CommandStore(self.cwd)

        # --- session ---------------------------------------------------- #
        self.session: Session = self._load_session(session_id)

        # --- agent ------------------------------------------------------ #
        self.stream = self.settings.ui.stream if stream is None else stream
        self.agent = self._build_agent() if self.provider is not None else None
        if hooks:
            hooks.on_ready(self)

    # ------------------------------------------------------------------ #
    # construction helpers
    # ------------------------------------------------------------------ #
    def _pick_provider(self):
        try:
            return self.providers.create(self.providers.default_provider, self.providers.default_model or None)
        except (KeyError, ValueError):
            return self.providers.first_available()

    def _build_tools(self) -> ToolRegistry:
        registry = build_default_registry(
            enabled=self.settings.tools.enabled or None,
            cwd=self.cwd,
            bash_timeout=self.settings.tools.bash_timeout,
        )
        for dotted in self.settings.tools.extra_modules:
            registry.load_module(dotted)
        if self.settings.env and "bash" in registry:
            registry.get("bash").env.config.env.update(self.settings.env)
        return registry

    def _permission_rules(self) -> list[Rule]:
        from minicode.permission.policy import ruleset_from_config

        if self.settings.permission:
            return ruleset_from_config(self.settings.permission)
        from minicode.permission.manager import default_ruleset

        return default_ruleset()

    def _load_session(self, session_id: str | None) -> Session:
        if session_id:
            session = self.sessions.get(session_id)
            if session is None:
                raise KeyError(f"Session not found: {session_id}")
            session.cwd = session.cwd or self.cwd
            return session
        provider = self.provider.name if self.provider is not None else ""
        model = self.provider.model if self.provider is not None else ""
        return self.sessions.create(
            provider=provider,
            model=model,
            cwd=self.cwd,
            metadata={"cwd": self.cwd},
            persist=False,
        )

    def _build_agent(self, messages: list[dict[str, Any]] | None = None) -> CodingAgent:
        agent = CodingAgent(
            self.provider,
            self.tools,
            permission=self.permission,
            context=self.context,
            session=self.session,
            sessions=self.sessions,
            sink=self.sink,
            cwd=self.cwd,
            stream=self.stream,
            step_limit=self.settings.agent.step_limit,
            cost_limit=self.settings.agent.cost_limit,
            wall_time_limit_seconds=self.settings.agent.wall_time_limit_seconds,
            doom_loop_threshold=self.settings.agent.doom_loop_threshold,
        )
        if messages is not None:
            agent.messages = messages
        return agent

    # ------------------------------------------------------------------ #
    # session operations
    # ------------------------------------------------------------------ #
    def _persist(self, session: Session) -> None:
        """Write a session to disk, but only once it is worth keeping.

        A brand-new session with no messages is just a placeholder for the
        composer. Saving it on start-up is what used to fill the session rail
        with rows called "New session" that resumed to an empty timeline.
        """
        if session.messages or self.sessions.exists(session.id):
            self.sessions.save(session)

    def _switch_session(self, session: Session) -> None:
        self.session = session
        if self.provider is not None:
            self.session.provider = self.provider.name
            self.session.model = self.provider.model
            self._persist(self.session)
            restored = self.context.rebuild(session.messages)
            self.agent = self._build_agent(messages=restored)
            self.agent.messages = restored
        else:
            self.agent = None

    def new_session(self) -> Session:
        provider = self.provider.name if self.provider is not None else ""
        model = self.provider.model if self.provider is not None else ""
        session = self.sessions.create(
            provider=provider, model=model, cwd=self.cwd, metadata={"cwd": self.cwd}, persist=False
        )
        self._switch_session(session)
        return session

    def known_sessions(self, *, cwd: str | None = None) -> list[Session]:
        """Every session the rail should show: persisted ones, plus the live one.

        The session you are typing into is not on disk yet (see :meth:`_persist`),
        but it still belongs in the rail -- otherwise starting the TUI shows an
        empty sidebar with no marker on the session you are actually using.

        ``cwd`` narrows the rail to the current project (OpenCode-style). Legacy
        sessions without a recorded working directory are kept visible so old
        history does not silently disappear.
        """
        # Hide old empty placeholder sessions that were created before the
        # "don't persist an empty session" fix. They have no messages and are
        # still titled "New session", so showing them only makes history
        # impossible to find.
        sessions = [
            session
            for session in self.sessions.list()
            if session.messages or (session.title and session.title != "New session")
        ]
        if cwd is not None:
            try:
                target = str(Path(cwd).resolve())
            except OSError:
                target = str(Path(cwd).absolute())
            sessions = [
                session
                for session in sessions
                if not session.cwd or _same_cwd(session.cwd, target)
            ]
        if all(session.id != self.session.id for session in sessions):
            sessions.insert(0, self.session)
        return sessions

    def interrupt(self) -> None:
        """Ask the running turn to stop at the next step boundary."""
        if self.agent is not None:
            self.agent.request_interrupt()

    def resume(self, session_id: str) -> Session:
        session = self.sessions.require(session_id)
        self._switch_session(session)
        return session

    def fork(self, session_id: str | None = None, *, at_message: int | None = None) -> Session:
        target = session_id or self.session.id
        forked = self.sessions.fork(target, at_message=at_message)
        self._switch_session(forked)
        return forked

    def delete_session(self, session_id: str) -> bool:
        """Delete a saved session, switching to a fresh one if it was current."""
        if self.session.id == session_id:
            self.new_session()
            return True
        return self.sessions.delete(session_id)

    def clear(self) -> Session:
        """Start a fresh history in a brand new session."""
        return self.new_session()

    # ------------------------------------------------------------------ #
    # model switching
    # ------------------------------------------------------------------ #
    def set_model(self, spec: str) -> None:
        provider = self.providers.get(spec)
        messages = self.agent.messages if self.agent is not None else []
        self.provider = provider
        self.providers.default_provider = provider.name
        self.providers.default_model = provider.model
        self.session.provider = provider.name
        self.session.model = provider.model
        self.agent = self._build_agent(messages=messages)
        self._persist(self.session)

    # ------------------------------------------------------------------ #
    # running
    # ------------------------------------------------------------------ #
    def run_task(self, task: str) -> dict[str, Any]:
        """Run a single turn (used by both the REPL and `--run`)."""
        self.ui.print_user(task)
        if self.agent is None:
            self.ui.print_error("no provider configured yet - use /login to set one up")
            return {"exit_status": "NoProvider", "submission": ""}
        try:
            result = self.agent.run(task)
        except KeyboardInterrupt:
            self.agent.state.status = AgentStatus.INTERRUPTED
            self.ui.print_info("interrupted")
            return {"exit_status": "Interrupted", "submission": ""}
        except Exception as exc:  # noqa: BLE001
            self.agent.state.status = AgentStatus.ERROR
            self.ui.on_error(f"{type(exc).__name__}: {exc}")
            return {"exit_status": "Error", "submission": "", "error": str(exc)}
        finally:
            self._persist(self.session)
        self.ui.status_line(self.agent.stats())
        if self.hooks:
            self.hooks.on_turn_end(self, result)
        return result

    def run_custom(self, command: SlashCommand, args: list[str]) -> dict[str, Any]:
        """Run a custom command: render its template, then run it as a turn.

        There is nothing to call -- the rendered template *is* the prompt, which
        is why a command file can never reach into the process the way a plugin
        could. ``model:`` in the frontmatter wins for this turn only; the
        previous model is put back afterwards, whatever happens.
        """
        custom = self.commands.custom_for(command.trigger) if command.type == "custom" else None
        if custom is None:
            self.ui.print_error(f"{command.trigger} is not a custom command")
            return {"exit_status": "Error", "submission": ""}
        prompt = render_template(custom.template, args)
        if not prompt.strip():
            self.ui.print_error(f"{custom.trigger} has no prompt - edit {custom.path}")
            return {"exit_status": "Error", "submission": ""}

        restore = None
        if custom.model and self.provider is not None:
            previous = self.provider.model_id
            try:
                self.set_model(custom.model)
            except (KeyError, ValueError) as exc:
                self.ui.print_error(f"{custom.trigger}: cannot use model {custom.model!r}: {exc}")
                return {"exit_status": "Error", "submission": ""}
            restore = previous
        try:
            return self.run_task(prompt)
        finally:
            if restore:
                self.set_model(restore)

    def compact(self) -> None:
        if self.agent is None:
            self.ui.print_error("no provider configured yet - use /login to set one up")
            return
        outcome = self.context.compact(self.agent.messages)
        self.agent.messages = outcome.messages
        self.agent.sync_session()
        self.ui.on_compaction(
            {
                "before_tokens": outcome.before_tokens,
                "after_tokens": outcome.after_tokens,
                "summary": outcome.summary or "",
                "notes": outcome.notes or ["forced by /compact"],
            }
        )

    # ------------------------------------------------------------------ #
    # REPL
    # ------------------------------------------------------------------ #
    def repl(self) -> int:
        from minicode.cli.commands import handle_slash

        self.ui.banner(
            version=__version__,
            provider=self.provider.name if self.provider is not None else "not-configured",
            model=self.provider.model if self.provider is not None else "use /login",
            cwd=self.cwd,
            session=self.session.id,
        )
        self.reader = self.reader or create_reader(
            history_file=str(Path(self.sessions.store.directory).parent / "history"),
            use_prompt_toolkit=not self.non_interactive,
        )

        while True:
            try:
                raw = self.reader.read("> ", prompt_html="<ansigreen>❯ </ansigreen>")
            except KeyboardInterrupt:
                continue
            text = (raw or "").strip()
            if not text:
                continue
            if text.startswith("/"):
                action = handle_slash(self, text)
                if action == "exit":
                    break
                continue
            self.run_task(text)
        self.ui.print_info("bye.")
        return 0
