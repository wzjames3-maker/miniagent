"""Slash commands for the interactive REPL."""

from __future__ import annotations

import time

from minicode.cli.app import InteractiveApp

__all__ = ["handle_slash", "COMMAND_HELP", "available_commands"]

COMMAND_HELP: list[tuple[str, str]] = [
    ("/help", "show this help"),
    ("/model [provider/model]", "show providers+models, or switch the current model"),
    ("/models", "list configured providers and models"),
    ("/session", "show the current session"),
    ("/sessions", "list saved sessions"),
    ("/resume <id>", "resume another session"),
    ("/fork [id]", "fork the session (optionally at an earlier point)"),
    ("/title <text>", "rename the current session"),
    ("/new", "start a brand new session"),
    ("/clear", "same as /new (fresh history)"),
    ("/compact", "force context compaction"),
    ("/tools", "list the enabled tools"),
    ("/permission", "show the active permission rules"),
    ("/status, /stats", "show agent/session statistics"),
    ("/exit, /quit", "leave minicode"),
]


def available_commands() -> str:
    return "\n".join(f"  {name:<28} {desc}" for name, desc in COMMAND_HELP)


def handle_slash(app: InteractiveApp, text: str) -> str:
    """Dispatch a slash command. Returns ``"continue"`` or ``"exit"``."""
    parts = text.split()
    command = parts[0].lower()
    args = parts[1:]
    ui = app.ui

    if command in {"/exit", "/quit", "/q"}:
        return "exit"

    if command in {"/help", "/h", "/?"}:
        ui.console.print(available_commands())
        return "continue"

    if command == "/model":
        return _model(app, args)
    if command == "/models":
        return _models(app)
    if command == "/session":
        return _session(app)
    if command in {"/sessions", "/ls"}:
        return _sessions(app)
    if command == "/resume":
        return _resume(app, args)
    if command == "/fork":
        return _fork(app, args)
    if command == "/title":
        return _title(app, args)
    if command in {"/new", "/clear"}:
        session = app.new_session()
        ui.print_info(f"new session {session.id}")
        return "continue"
    if command == "/compact":
        app.compact()
        return "continue"
    if command == "/tools":
        ui.console.print(app.tools.describe())
        return "continue"
    if command == "/permission":
        return _permission(app)
    if command in {"/status", "/stats"}:
        return _status(app)

    ui.print_error(f"unknown command {command!r}. Type /help for the list.")
    return "continue"


# --------------------------------------------------------------------------- #
def _model(app: InteractiveApp, args: list[str]) -> str:
    ui = app.ui
    if not args:
        ui.console.print(app.providers.describe())
        ui.print_info(f"current: {app.provider.model_id}. Switch with /model provider/model")
        return "continue"
    spec = " ".join(args)
    try:
        app.set_model(spec)
    except (KeyError, ValueError) as exc:
        ui.print_error(str(exc))
        return "continue"
    ui.print_info(f"switched to {app.provider.model_id}")
    return "continue"


def _session(app: InteractiveApp) -> str:
    session = app.session
    ui = app.ui
    ui.console.print(
        f"[minicode.accent]{session.id}[/]  {session.title}\n"
        f"  model:    {session.model_id or '(none)'}\n"
        f"  cwd:      {session.cwd}\n"
        f"  messages: {session.message_count}\n"
        f"  tools:    {session.tool_call_count}\n"
        f"  created:  {_time(session.created_at)}\n"
        f"  updated:  {_time(session.updated_at)}"
        + (f"\n  forked from: {session.parent_id}" if session.parent_id else "")
    )
    if session.tool_calls:
        ui.console.print("  recent tool calls:")
        for call in session.tool_calls[-8:]:
            flag = "ok " if call.ok else "err"
            ui.console.print(f"    [{flag}] {call.name}  {_short(str(call.arguments), 70)}")
    return "continue"


def _sessions(app: InteractiveApp) -> str:
    sessions = app.sessions.list()
    ui = app.ui
    if not sessions:
        ui.print_info("no saved sessions yet")
        return "continue"
    for session in sessions:
        marker = "*" if session.id == app.session.id else " "
        ui.console.print(
            f"{marker} [minicode.accent]{session.id}[/]  {_short(session.title, 46):<46} "
            f"{session.model_id or '-':<28} {session.message_count:>4} msg  {_time(session.updated_at)}"
        )
    return "continue"


def _resume(app: InteractiveApp, args: list[str]) -> str:
    ui = app.ui
    if not args:
        ui.print_error("usage: /resume <session-id>  (use /sessions to list)")
        return "continue"
    try:
        session = app.resume(args[0])
    except KeyError as exc:
        ui.print_error(str(exc))
        return "continue"
    ui.print_info(f"resumed {session.id} ({session.message_count} messages, {session.tool_call_count} tool calls)")
    return "continue"


def _fork(app: InteractiveApp, args: list[str]) -> str:
    at = None
    target = None
    for arg in args:
        if arg.isdigit():
            at = int(arg)
        else:
            target = arg
    try:
        session = app.fork(target, at_message=at)
    except KeyError as exc:
        app.ui.print_error(str(exc))
        return "continue"
    app.ui.print_info(f"forked into {session.id} ({session.message_count} messages)")
    return "continue"


def _models(app: InteractiveApp) -> str:
    app.ui.console.print(app.providers.describe())
    app.ui.print_info(f"current: {app.provider.model_id}")
    return "continue"


def _title(app: InteractiveApp, args: list[str]) -> str:
    if not args:
        app.ui.print_error("usage: /title <new title>")
        return "continue"
    title = " ".join(args).strip()
    try:
        app.sessions.retitle(app.session.id, title)
        app.session.title = title
        app.ui.print_info(f"renamed to {title!r}")
    except Exception as exc:  # pragma: no cover
        app.ui.print_error(str(exc))
    return "continue"


def _permission(app: InteractiveApp) -> str:
    ui = app.ui
    ui.console.print(f"mode: {app.permission.mode.value}")
    for rule in app.permission.ruleset:
        ui.console.print(f"  {rule.permission:<14} {rule.pattern:<40} {rule.action.value}")
    if app.permission.approved_ruleset:
        ui.console.print("  [session approvals]")
        for rule in app.permission.approved_ruleset:
            ui.console.print(f"    {rule.permission:<12} {rule.pattern:<40} {rule.action.value}")
    return "continue"


def _status(app: InteractiveApp) -> str:
    stats = app.agent.stats()
    app.ui.console.print("\n".join(f"  {key}: {value}" for key, value in stats.items()))
    return "continue"


def _time(value: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))


def _short(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 3] + "..."
