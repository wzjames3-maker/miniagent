"""Tests for the slash-command registry.

The registry is machine-readable on purpose: it drives ``/help``, the
dispatcher and the TUI's ``/`` popover from one tuple. These tests pin the
invariants that make that safe -- notably that a command cannot exist in the
help text without being runnable, and that filtering behaves the way the
popover promises.
"""

from __future__ import annotations

import pytest

from minicode.cli.commands import (
    COMMAND_HELP,
    COMMANDS,
    SlashCommand,
    find_command,
    handle_slash,
    match_commands,
)
from minicode.commands import CommandStore

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# the registry is internally consistent
# --------------------------------------------------------------------------- #
def test_every_command_is_runnable_and_shown():
    for command in COMMANDS:
        assert command.handler is not None, f"{command.trigger} has no handler"
        assert command.trigger.startswith("/")
        assert command.title.startswith(command.trigger)
        assert command.description


def test_help_text_is_derived_not_duplicated():
    assert len(COMMAND_HELP) == len(COMMANDS)
    for (title, description), command in zip(COMMAND_HELP, COMMANDS, strict=True):
        assert (title, description) == (command.title, command.description)


def test_triggers_are_unique_across_the_whole_registry():
    names = [name for command in COMMANDS for name in (command.trigger, *command.aliases)]
    assert len(names) == len(set(names)), f"duplicate command name: {names}"


def test_no_alias_shadows_another_command():
    for command in COMMANDS:
        for alias in command.aliases:
            assert find_command(alias).trigger == command.trigger


def test_find_command_is_case_insensitive_and_returns_none_for_junk():
    assert find_command("/HELP").trigger == "/help"
    assert find_command("/Q").trigger == "/exit"
    assert find_command("/nope") is None


def test_takes_args_follows_the_argument_hint_in_the_title():
    assert find_command("/resume").takes_args is True
    assert find_command("/title").takes_args is True
    assert find_command("/help").takes_args is False
    assert find_command("/exit").takes_args is False


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
class _RecordingUI:
    """The smallest thing a command handler can talk to."""

    def __init__(self) -> None:
        self.markdown: list[str] = []
        self.infos: list[str] = []
        self.errors: list[str] = []

    def print_markdown(self, text: str) -> None:
        self.markdown.append(text)

    def print_info(self, message: str) -> None:
        self.infos.append(message)

    def print_error(self, message: str, *, details=None) -> None:
        self.errors.append(message)

    def banner(self, **kwargs) -> None: ...
    def rule(self, title="") -> None: ...
    def status_line(self, stats) -> None: ...
    def print_user(self, text: str) -> None: ...
    def ask_permission(self, request): ...
    def confirm(self, question, *, default=True) -> bool:
        return default


class _FakeApp:
    def __init__(self) -> None:
        self.ui = _RecordingUI()
        self.commands = CommandStore(cwd=".", refresh=False)


def test_help_renders_every_command():
    app = _FakeApp()
    assert handle_slash(app, "/help") == "continue"
    rendered = app.ui.markdown[0]
    for command in COMMANDS:
        assert command.trigger in rendered


def test_unknown_command_is_reported_not_raised():
    app = _FakeApp()
    assert handle_slash(app, "/definitely-not-a-command") == "continue"
    assert app.ui.errors and "unknown command" in app.ui.errors[0]


def test_exit_commands_all_leave_the_app():
    for name in ("/exit", "/quit", "/q"):
        assert handle_slash(_FakeApp(), name) == "exit"


def test_aliases_resolve_to_the_same_command():
    assert find_command("/ls") is find_command("/sessions")
    assert find_command("/provider") is find_command("/login")
    assert find_command("/stats") is find_command("/status")
    assert find_command("/clear").handler is find_command("/new").handler


# --------------------------------------------------------------------------- #
# filtering (what the popover shows)
# --------------------------------------------------------------------------- #
def test_an_empty_query_returns_everything_in_registry_order():
    assert match_commands("/") == list(COMMANDS)


def test_a_prefix_narrows_the_list():
    triggers = [command.trigger for command in match_commands("/ses")]
    assert "/sessions" in triggers
    assert "/help" not in triggers


def test_filtering_matches_the_description_too():
    # "compact" is only in the description of /compact's title, and in the
    # trigger itself -- check the title path with an argument hint.
    triggers = [command.trigger for command in match_commands("/fork [id]")]
    assert "/fork" in triggers


def test_a_query_with_no_match_returns_nothing():
    assert match_commands("/zzzzz") == []


def test_best_match_comes_first():
    assert match_commands("/session")[0].trigger == "/session"
    assert match_commands("/exit")[0].trigger == "/exit"


def test_the_popover_offers_one_row_per_action():
    """Aliases are accepted, never offered -- otherwise /stats and /status
    would sit side by side in the list."""
    shown = [command.trigger for command in match_commands("/")]
    assert shown.count("/status") == 1
    assert "/stats" not in shown


def test_slash_command_exposes_what_the_popover_renders():
    command = SlashCommand("/new", "/new", "start a new session", None, keybind="ctrl+n")
    assert (command.title, command.description, command.keybind) == ("/new", "start a new session", "ctrl+n")
