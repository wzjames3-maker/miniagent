"""Slash commands: one registry, shared by the Rich REPL and the Textual TUI.

The registry is the single source for three things that used to be three
separate literals that could drift apart:

* ``/help`` renders it,
* :func:`handle_slash` dispatches through it,
* the TUI composer offers it as a filterable popover the moment ``/`` is typed
  (which is how OpenCode's ``/`` behaves).

Handlers only ever touch :class:`~minicode.ui.port.UIPort`, never a Rich
console, so both front-ends run exactly the same command implementation.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # import cycle: cli.app owns a CommandStore, which owns these
    from minicode.cli.app import InteractiveApp

__all__ = [
    "COMMANDS",
    "COMMAND_HELP",
    "SlashCommand",
    "available_commands",
    "find_command",
    "handle_slash",
    "match_commands",
]

#: Return values of a handler: keep going, or leave the app.
CONTINUE = "continue"
EXIT = "exit"


@dataclass(frozen=True)
class SlashCommand:
    """One slash command.

    Shaped after OpenCode's ``SlashCommand``: ``trigger`` is what the user
    types, ``title`` is what the popover shows (it may carry an argument hint),
    ``description`` is the help line and ``keybind`` the shortcut, if any.

    ``aliases`` are accepted by the dispatcher but never *offered* by the
    popover, so the list stays one row per action -- ``/status`` and ``/stats``
    are the same command, not two entries competing for the same keystrokes.
    """

    trigger: str
    title: str
    description: str
    handler: Callable[[InteractiveApp, list[str]], str] | None = None
    aliases: tuple[str, ...] = ()
    keybind: str = ""
    #: ``"builtin"`` or ``"custom"``. A custom command has no handler: its
    #: template is rendered and sent to the agent instead.
    type: str = "builtin"
    #: ``"project"`` / ``"user"`` for custom commands, empty for builtins.
    source: str = ""
    #: File a custom command was loaded from, shown by the manager.
    path: Path | None = None
    #: The prompt body of a custom command.
    template: str = ""

    @property
    def takes_args(self) -> bool:
        """Whether the command still needs something typed after it.

        Read off ``title``, which is where the argument hint lives
        (``/resume <id>``), so one string drives the display, the filter and
        this. OpenCode uses it to decide what picking a command does: fire it
        immediately, or drop it into the composer with the caret waiting.
        """
        return " " in self.title


# --------------------------------------------------------------------------- #
# render helpers
# --------------------------------------------------------------------------- #
def _code(text: str) -> str:
    """Wrap pre-formatted text in a fenced block so both UIs render it verbatim."""
    return "```\n" + text.rstrip() + "\n```"


def _time(value: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))


def _short(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 3] + "..."


# --------------------------------------------------------------------------- #
# handlers -- signature is always (app, args) -> "continue" | "exit"
# --------------------------------------------------------------------------- #
def _help(app: InteractiveApp, args: list[str]) -> str:
    app.ui.print_markdown(available_commands())
    return CONTINUE


def _exit(app: InteractiveApp, args: list[str]) -> str:
    return EXIT


def _model(app: InteractiveApp, args: list[str]) -> str:
    ui = app.ui
    if not args:
        ui.print_markdown(_code(app.providers.describe()))
        if app.provider is None:
            ui.print_info("no provider configured yet - use /login to set one up")
        else:
            ui.print_info(f"current: {app.provider.model_id}. Switch with /model provider/model")
        return CONTINUE
    spec = " ".join(args)
    try:
        app.set_model(spec)
    except (KeyError, ValueError) as exc:
        ui.print_error(str(exc))
        return CONTINUE
    ui.print_info(f"switched to {app.provider.model_id}")
    return CONTINUE


def _login(app: InteractiveApp, args: list[str]) -> str:
    from minicode.cli.provider_config import configure_provider
    from minicode.config.settings import load_settings
    from minicode.providers.registry import build_registry

    ui = app.ui
    name = args[0] if args else None
    ui.print_info("Configure the model/API now (answers are written to the global config file).")
    try:
        result = configure_provider(name)
    except (ValueError, KeyboardInterrupt) as exc:
        ui.print_error(str(exc))
        return CONTINUE

    try:
        app.settings = load_settings(cwd=app.cwd)
        app.providers = build_registry(app.settings.model_dump())
    except Exception as exc:  # pragma: no cover - defensive reload
        ui.print_error(f"reload failed: {exc}")
        return CONTINUE

    try:
        app.set_model(f"{result.name}/{result.default_model}")
    except (KeyError, ValueError) as exc:
        ui.print_error(str(exc))
        return CONTINUE

    ui.print_info(f"provider '{result.name}' configured (saved to {result.path})")
    return CONTINUE


def _session(app: InteractiveApp, args: list[str]) -> str:
    session = app.session
    lines = [
        f"**{session.id}** — {session.title}",
        "",
        f"- model: `{session.model_id or '(none)'}`",
        f"- cwd: `{session.cwd}`",
        f"- messages: {session.message_count}",
        f"- tools: {session.tool_call_count}",
        f"- created: {_time(session.created_at)}",
        f"- updated: {_time(session.updated_at)}",
    ]
    if session.parent_id:
        lines.append(f"- forked from: `{session.parent_id}`")
    if session.tool_calls:
        lines += ["", "**recent tool calls**", ""]
        lines += [
            f"- {'ok ' if call.ok else 'err'} `{call.name}` — {_short(str(call.arguments), 70)}"
            for call in session.tool_calls[-8:]
        ]
    app.ui.print_markdown("\n".join(lines))
    return CONTINUE


def _sessions(app: InteractiveApp, args: list[str]) -> str:
    sessions = app.known_sessions()
    if not sessions:
        app.ui.print_info("no saved sessions yet")
        return CONTINUE
    lines = ["**Sessions**", ""]
    for session in sessions:
        marker = "*" if session.id == app.session.id else " "
        lines.append(
            f"- {marker} `{session.id}` — {_short(session.title, 46)} "
            f"({session.message_count} msg, {_time(session.updated_at)})"
        )
    app.ui.print_markdown("\n".join(lines))
    return CONTINUE


def _resume(app: InteractiveApp, args: list[str]) -> str:
    ui = app.ui
    if not args:
        ui.print_error("usage: /resume <session-id>  (use /sessions to list)")
        return CONTINUE
    try:
        session = app.resume(args[0])
    except KeyError as exc:
        ui.print_error(str(exc))
        return CONTINUE
    ui.print_info(f"resumed {session.id} ({session.message_count} messages, {session.tool_call_count} tool calls)")
    return CONTINUE


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
        return CONTINUE
    app.ui.print_info(f"forked into {session.id} ({session.message_count} messages)")
    return CONTINUE


def _models(app: InteractiveApp, args: list[str]) -> str:
    app.ui.print_markdown(_code(app.providers.describe()))
    if app.provider is None:
        app.ui.print_info("no provider configured yet - use /login to set one up")
    else:
        app.ui.print_info(f"current: {app.provider.model_id}")
    return CONTINUE


def _title(app: InteractiveApp, args: list[str]) -> str:
    if not args:
        app.ui.print_error("usage: /title <new title>")
        return CONTINUE
    title = " ".join(args).strip()
    try:
        app.sessions.retitle(app.session.id, title)
        app.session.title = title
        app.ui.print_info(f"renamed to {title!r}")
    except Exception as exc:  # pragma: no cover
        app.ui.print_error(str(exc))
    return CONTINUE


def _new(app: InteractiveApp, args: list[str]) -> str:
    session = app.new_session()
    app.ui.print_info(f"new session {session.id}")
    return CONTINUE


def _compact(app: InteractiveApp, args: list[str]) -> str:
    app.compact()
    return CONTINUE


def _tools(app: InteractiveApp, args: list[str]) -> str:
    app.ui.print_markdown(_code(app.tools.describe()))
    return CONTINUE


def _commands(app: InteractiveApp, args: list[str]) -> str:
    """List the commands loaded from markdown files (builtins are in /help)."""
    store = app.commands
    custom = store.refresh()
    if not custom:
        app.ui.print_info(
            "no custom commands yet - drop a .md file in .minicode/commands/, "
            "or use /command to create one"
        )
        return CONTINUE
    lines = ["**Custom commands**", ""]
    for command in custom:
        lines.append(
            f"- `{command.title}` ({command.source}) — {command.description or '(no description)'}"
        )
        lines.append(f"    `{command.path}`")
    app.ui.print_markdown("\n".join(lines))
    return CONTINUE


def _command(app: InteractiveApp, args: list[str]) -> str:
    """Create a command file. Editing them is a TUI affair (``/command`` there)."""
    # Imported here: custom_commands imports this module for the registry.
    from minicode.custom_commands import normalize_name, scaffold, write_command

    if args and args[0] in {"new", "add", "create"}:
        if len(args) < 2:
            app.ui.print_error("usage: /command new <name>")
            return CONTINUE
        try:
            name = normalize_name(args[1])
        except ValueError as exc:
            app.ui.print_error(str(exc))
            return CONTINUE
        path = write_command(name, scaffold(name), cwd=app.cwd)
        app.commands.refresh()
        app.ui.print_info(f"created {path} - edit it, then run /{name}")
        return CONTINUE

    store = app.commands
    app.ui.print_markdown(
        "\n".join(
            [
                "**Custom commands**",
                "",
                f"- project: `{Path(app.cwd) / '.minicode' / 'commands'}/*.md`",
                f"- user: `{store.data_dir / 'commands'}/*.md`",
                "",
                "One file is one command: the path is the name "
                "(`git/commit.md` -> `/git/commit`) and `$ARGUMENTS` is replaced "
                "by whatever you type after it. `/command new <name>` writes a "
                "starter file here; the TUI can create and edit them in place.",
            ]
        )
    )
    return CONTINUE


def _permission(app: InteractiveApp, args: list[str]) -> str:
    lines = [f"mode: `{app.permission.mode.value}`", "", "```"]
    for rule in app.permission.ruleset:
        lines.append(f"  {rule.permission:<14} {rule.pattern:<40} {rule.action.value}")
    if app.permission.approved_ruleset:
        lines.append("  [session approvals]")
        for rule in app.permission.approved_ruleset:
            lines.append(f"    {rule.permission:<12} {rule.pattern:<40} {rule.action.value}")
    lines.append("```")
    app.ui.print_markdown("\n".join(lines))
    return CONTINUE


def _status(app: InteractiveApp, args: list[str]) -> str:
    if app.agent is None:
        app.ui.print_info("no provider configured yet - use /login to set one up")
        return CONTINUE
    stats = app.agent.stats()
    app.ui.print_markdown("\n".join(f"- `{key}`: {value}" for key, value in stats.items()))
    return CONTINUE


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #
COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("/help", "/help", "show this help", _help, aliases=("/h", "/?"), keybind="ctrl+p"),
    SlashCommand("/commands", "/commands", "list your custom commands", _commands),
    SlashCommand("/command", "/command [new <name>]", "create or manage a custom command", _command),
    SlashCommand("/login", "/login [provider]", "add/update an API key and model", _login, aliases=("/provider",)),
    SlashCommand("/model", "/model [provider/model]", "show providers+models, or switch model", _model),
    SlashCommand("/models", "/models", "list configured providers and models", _models),
    SlashCommand("/session", "/session", "show the current session", _session),
    SlashCommand("/sessions", "/sessions", "list saved sessions", _sessions, aliases=("/ls",)),
    SlashCommand("/resume", "/resume <id>", "resume another session", _resume),
    SlashCommand("/fork", "/fork [id]", "fork the session (optionally at an earlier point)", _fork),
    SlashCommand("/title", "/title <text>", "rename the current session", _title),
    SlashCommand("/new", "/new", "start a brand new session", _new, keybind="ctrl+n"),
    SlashCommand("/clear", "/clear", "same as /new (fresh history)", _new),
    SlashCommand("/compact", "/compact", "force context compaction", _compact),
    SlashCommand("/tools", "/tools", "list the enabled tools", _tools),
    SlashCommand("/permission", "/permission", "show the active permission rules", _permission),
    SlashCommand("/status", "/status", "show agent/session statistics", _status, aliases=("/stats",)),
    SlashCommand("/exit", "/exit", "leave minicode", _exit, aliases=("/quit", "/q"), keybind="ctrl+c"),
)

#: Derived, so ``/help`` can never disagree with the dispatcher.
COMMAND_HELP: list[tuple[str, str]] = [(command.title, command.description) for command in COMMANDS]

_BY_NAME: dict[str, SlashCommand] = {
    name: command for command in COMMANDS for name in (command.trigger, *command.aliases)
}


def available_commands() -> str:
    """Markdown help text for ``/help``."""
    lines = ["**Commands**", ""]
    lines += [f"- `{title}` — {description}" for title, description in COMMAND_HELP]
    return "\n".join(lines)


def find_command(name: str) -> SlashCommand | None:
    """Look a command up by trigger or alias (case-insensitive)."""
    return _BY_NAME.get(name.lower())


def handle_slash(app: InteractiveApp, text: str) -> str:
    """Dispatch a slash command. Returns :data:`CONTINUE` or :data:`EXIT`.

    Custom commands are dispatched here too, but with no handler to call: the
    rendered template becomes a prompt for the agent (see
    ``InteractiveApp.run_custom``).
    """
    parts = text.split()
    if not parts:
        return CONTINUE
    command = app.commands.find(parts[0])
    if command is None:
        app.ui.print_error(f"unknown command {parts[0]!r}. Type /help for the list.")
        app.ui.print_info("custom commands live in .minicode/commands/*.md - /commands lists them")
        return CONTINUE
    if command.type == "custom":
        app.run_custom(command, parts[1:])
        return CONTINUE
    if command.handler is None:
        app.ui.print_error(f"{command.trigger} cannot be run")
        return CONTINUE
    return command.handler(app, parts[1:])


# --------------------------------------------------------------------------- #
# matching -- the popover's filter
# --------------------------------------------------------------------------- #
def match_commands(query: str, commands: Sequence[SlashCommand] | None = None) -> list[SlashCommand]:
    """Commands matching a partial ``/name``, best match first.

    ``commands`` defaults to the builtin registry; the app passes
    ``CommandStore.all`` so custom commands are filtered by the same rule.
    """
    needle = query.lstrip("/").lower()
    pool = tuple(COMMANDS if commands is None else commands)
    if not needle:
        return list(pool)
    scored: list[tuple[int, int, int, SlashCommand]] = []
    for index, command in enumerate(pool):
        score = _fuzzy_score(needle, command.trigger.lstrip("/"))
        if score is None:
            # The popover also matches argument hints in the title, so typing
            # /fork [id] still offers /fork.
            score = _fuzzy_score(needle, command.title.lstrip("/"))
        if score is not None:
            # Ties break on the shorter trigger (/session before /sessions) and
            # then on registry order, so the list never reshuffles between runs.
            scored.append((-score, len(command.trigger), index, command))
    scored.sort(key=lambda item: item[:3])
    return [command for *_, command in scored]


#: Characters that start a new word inside a trigger (``git/commit``).
_WORD_SEPARATORS = "/_-. "


def _fuzzy_score(needle: str, haystack: str) -> int | None:
    """Score ``needle`` as a word-boundary abbreviation of ``haystack``.

    Every character has to *continue* the previous hit or *start a word* (index
    0, or right after ``/ - _ .`` or a space). That one rule is the whole
    difference between a useful filter and a slot machine: without it, ``/fi``
    selected ``/fork`` -- the ``f`` is at the start of "fork" and the ``i`` is
    three words later inside the ``[id]`` argument hint, which is noise, not an
    abbreviation. In the composer that silently rewrote what the user was
    typing, and there was no way to keep typing the command you meant.
    """
    if not needle:
        return 0
    target = haystack.lower()
    score = 0
    cursor = 0
    previous = -2
    for char in needle:
        found = target.find(char, cursor)
        # A hit that is neither consecutive nor a word start is not a hit at
        # all, but a later occurrence of the same character might be.
        while found >= 0 and found != previous + 1 and found != 0 and target[found - 1] not in _WORD_SEPARATORS:
            found = target.find(char, found + 1)
        if found < 0:
            return None
        if found == previous + 1:
            score += 8  # consecutive
        if found == 0 or target[found - 1] in _WORD_SEPARATORS:
            score += 4  # start of a word
        score += max(1, 6 - (found - cursor))  # earlier is better
        cursor = found + 1
        previous = found
    return score - len(target) // 8
