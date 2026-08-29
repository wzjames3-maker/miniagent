"""The interactive application: wires config -> registry -> session -> agent -> UI."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from minicode.agent.core import CodingAgent
from minicode.agent.state import AgentStatus
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
from minicode.ui.prompt import create_reader

__all__ = ["InteractiveApp", "AppHooks"]


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
        ui: ConsoleUI | None = None,
        sink: EventSink | None = None,
        sessions: SessionManager | None = None,
        hooks: AppHooks | None = None,
        reader: Any = None,
    ):
        self.settings = settings or Settings()
        self.cwd = str(Path(cwd).resolve()) if cwd else os.getcwd()
        self.non_interactive = non_interactive
        self.hooks = hooks
        self.ui = ui or ConsoleUI(self.settings.ui)
        self.sink = sink or self.ui
        self.sessions = sessions or SessionManager()
        self.reader = reader

        # --- providers ------------------------------------------------- #
        self.providers: ProviderRegistry = build_registry(self.settings.model_dump())
        if model:
            self.provider = self.providers.get(model)
        else:
            self.provider = self._pick_provider()
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

        # --- session ---------------------------------------------------- #
        self.session: Session = self._load_session(session_id)

        # --- agent ------------------------------------------------------ #
        self.stream = self.settings.ui.stream if stream is None else stream
        self.agent = self._build_agent()
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
        return self.sessions.create(
            provider=self.provider.name,
            model=self.provider.model,
            cwd=self.cwd,
            metadata={"cwd": self.cwd},
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
    def _switch_session(self, session: Session) -> None:
        self.session = session
        self.session.provider = self.provider.name
        self.session.model = self.provider.model
        self.sessions.save(self.session)
        restored = self.context.rebuild(session.messages)
        self.agent = self._build_agent(messages=restored)
        self.agent.messages = restored

    def new_session(self) -> Session:
        session = self.sessions.create(
            provider=self.provider.name, model=self.provider.model, cwd=self.cwd, metadata={"cwd": self.cwd}
        )
        self._switch_session(session)
        return session

    def resume(self, session_id: str) -> Session:
        session = self.sessions.require(session_id)
        self._switch_session(session)
        return session

    def fork(self, session_id: str | None = None, *, at_message: int | None = None) -> Session:
        target = session_id or self.session.id
        forked = self.sessions.fork(target, at_message=at_message)
        self._switch_session(forked)
        return forked

    def clear(self) -> Session:
        """Start a fresh history in a brand new session."""
        return self.new_session()

    # ------------------------------------------------------------------ #
    # model switching
    # ------------------------------------------------------------------ #
    def set_model(self, spec: str) -> None:
        provider = self.providers.get(spec)
        messages = self.agent.messages
        self.provider = provider
        self.providers.default_provider = provider.name
        self.providers.default_model = provider.model
        self.session.provider = provider.name
        self.session.model = provider.model
        self.agent = self._build_agent(messages=messages)
        self.sessions.save(self.session)

    # ------------------------------------------------------------------ #
    # running
    # ------------------------------------------------------------------ #
    def run_task(self, task: str) -> dict[str, Any]:
        """Run a single turn (used by both the REPL and `--run`)."""
        self.ui.print_user(task)
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
            self.sessions.save(self.session)
        self.ui.status_line(self.agent.stats())
        if self.hooks:
            self.hooks.on_turn_end(self, result)
        return result

    def compact(self) -> None:
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
            version=_version(),
            provider=self.provider.name,
            model=self.provider.model,
            cwd=self.cwd,
            session=self.session.id,
        )
        self.reader = self.reader or create_reader(
            history_file=str(Path(self.sessions.store.directory).parent / "history"),
            use_prompt_toolkit=not self.non_interactive,
        )

        while True:
            try:
                raw = self.reader.read("[minicode.user]>[/] ")
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


def _version() -> str:
    import minicode

    return minicode.__version__
