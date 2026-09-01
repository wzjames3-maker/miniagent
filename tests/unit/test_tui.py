"""Tests for the Textual front-end.

The async tests drive the real :class:`MiniTUI` through Textual's ``run_test``
pilot, so a broken mount or a renamed widget id fails here rather than on
someone's terminal. The trickiest part of a TUI - a worker thread blocking on a
question only the UI thread can answer - is covered end to end.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import pytest

from minicode.config.settings import Settings
from minicode.permission.manager import AskReply
from minicode.session.models import Session
from minicode.ui.console import ConsoleUI
from minicode.ui.events import EventSink
from minicode.ui.port import UIFrontEnd, UIPort

pytestmark = [pytest.mark.unit, pytest.mark.anyio]

#: A fixed "now" (midday). Bucketing by day is only stable if the clock is:
#: at 01:00 a session from two hours ago belongs to *yesterday*.
_NOW = datetime(2026, 6, 15, 12, 0)


def _session(title: str, *, at: datetime) -> Session:
    return Session(id=f"ses_{title}", title=title, updated_at=at.timestamp())


# --------------------------------------------------------------------------- #
# session rail grouping
# --------------------------------------------------------------------------- #
def test_group_sessions_buckets_by_recency():
    from minicode.ui.textual.widgets import group_sessions

    groups = group_sessions(
        [
            _session("today", at=_NOW - timedelta(hours=2)),
            _session("yesterday", at=_NOW - timedelta(days=1)),
            _session("older", at=_NOW - timedelta(days=9)),
        ],
        now=_NOW,
    )
    assert [name for name, _ in groups] == ["Today", "Yesterday", "Older"]
    assert [[s.title for s in members] for _, members in groups] == [["today"], ["yesterday"], ["older"]]


def test_group_sessions_orders_newest_first_and_drops_empty_buckets():
    from minicode.ui.textual.widgets import group_sessions

    groups = group_sessions(
        [_session("earlier", at=_NOW - timedelta(hours=5)), _session("later", at=_NOW - timedelta(minutes=5))],
        now=_NOW,
    )
    assert [name for name, _ in groups] == ["Today"]
    assert [s.title for s in groups[0][1]] == ["later", "earlier"]


# --------------------------------------------------------------------------- #
# context meter
# --------------------------------------------------------------------------- #
def test_cache_label_formats_prompt_cache_tokens():
    from minicode.ui.port import cache_label, format_status_line

    assert cache_label({}) is None
    assert cache_label({"cache_read_tokens": 0, "cache_write_tokens": 0}) is None
    assert cache_label({"cache_read_tokens": 12_345}) == "cache 12.3K"
    assert cache_label({"cache_write_tokens": 5000}) == "cache w:5.0K"
    assert cache_label({"cache_read_tokens": 12_345, "cache_write_tokens": 5000}) == "cache 12.3Kr/5.0Kw"
    # the shared status line shows it too, and skips it when there is nothing
    assert "cache 12.3K" in format_status_line({"provider": "ds", "model": "m", "cache_read_tokens": 12_345})
    assert "cache" not in format_status_line({"provider": "ds", "model": "m"})


def test_context_bar_fills_proportionally():
    from minicode.ui.textual.theme import context_bar

    assert context_bar(0.0, width=4) == "\u2591\u2591\u2591\u2591"
    assert context_bar(1.0, width=4) == "\u2593\u2593\u2593\u2593"
    assert context_bar(0.5, width=4) == "\u2593\u2593\u2591\u2591"
    # out-of-range ratios clamp instead of blowing up the layout
    assert context_bar(-1.0, width=2) == "\u2591\u2591"
    assert context_bar(9.0, width=2) == "\u2593\u2593"


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [(0.1, "green"), (0.8, "bold dark_orange"), (0.95, "bold red")],
)
def test_context_style_escalates(ratio, expected):
    from minicode.ui.textual.theme import context_style

    # These must stay *real* Rich style names: Rich renders unknown styles
    # unstyled instead of raising, so a typo would silently flatten the warning.
    assert context_style(ratio) == expected


# --------------------------------------------------------------------------- #
# the port contract
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("factory", [ConsoleUI, lambda: _textual_ui()])
def test_each_front_end_is_a_complete_front_end(factory):
    """`InteractiveApp` uses one object as both UIPort and EventSink.

    A front-end implementing only half of it would not fail at construction --
    it would fall over on the first agent event. This asserts the whole
    contract, so the failure lands here instead.
    """
    front_end = factory()
    assert isinstance(front_end, UIFrontEnd)
    assert isinstance(front_end, UIPort)
    assert isinstance(front_end, EventSink)


def test_a_half_implemented_front_end_is_rejected():
    """Guards the guard: the combined protocol must actually discriminate."""

    class OnlyThePort:
        def banner(self, **kwargs): ...
        def rule(self, title=""): ...
        def status_line(self, stats): ...
        def print_user(self, text): ...
        def print_error(self, message, *, details=None): ...
        def print_info(self, message): ...
        def print_markdown(self, text): ...
        def ask_permission(self, request): ...
        def confirm(self, question, *, default=True): ...

    assert isinstance(OnlyThePort(), UIPort)
    assert not isinstance(OnlyThePort(), UIFrontEnd)


def _textual_ui():
    from minicode.ui.textual.bridge import TextualUI

    return TextualUI(Settings().ui, app=None)


# --------------------------------------------------------------------------- #
# the app shell
# --------------------------------------------------------------------------- #
async def test_tui_mounts_every_region():
    from minicode.ui.textual.app import MiniTUI
    from minicode.ui.textual.widgets import (
        Composer,
        ContextBar,
        MessageLog,
        PermissionBar,
        SessionSidebar,
        StreamArea,
    )

    app = MiniTUI()
    async with app.run_test(size=(120, 40)):
        assert app.query_one("#sidebar", SessionSidebar)
        assert app.query_one("#log", MessageLog)
        assert app.query_one("#stream", StreamArea)
        assert app.query_one("#permission", PermissionBar)
        assert app.query_one("#context", ContextBar)
        assert app.query_one("#composer", Composer)
        assert app.core is not None
        # the banner is the only thing written before the user types anything
        assert app.core.session.id


async def test_slash_command_renders_through_the_port():
    from minicode.ui.textual.app import MiniTUI
    from minicode.ui.textual.widgets import Composer

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        written: list[object] = []
        app.append_to_log = written.append

        app.query_one("#composer", Composer).load_text("/help")
        await pilot.press("enter")
        await pilot.pause()

        assert written, "/help must reach the message log through the UI port"


async def test_exit_command_quits():
    from minicode.ui.textual.app import MiniTUI
    from minicode.ui.textual.widgets import Composer

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        app.query_one("#composer", Composer).load_text("/exit")
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_code is not None


async def test_dark_mode_toggles_through_textuals_own_theme():
    """Dark/light toggling stays Textual's job on top of the ported palette."""
    from minicode.ui.textual.app import MiniTUI

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        # The brand palette from pydantic-deepagents is the default now.
        assert app.theme == "minicode-default"
        await app.run_action("toggle_dark")
        await pilot.pause()
        assert app.theme == "textual-light"
        await app.run_action("toggle_dark")
        await pilot.pause()
        assert app.theme == "textual-dark"


async def test_cycle_theme_steps_through_ported_palettes():
    """ctrl+e cycles the five deepagents palettes and wraps around."""
    from minicode.ui.textual.app import MiniTUI
    from minicode.ui.textual.theme import THEMES

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        names = list(THEMES.keys())
        assert app.theme == f"minicode-{names[0]}"
        await app.run_action("cycle_theme")
        await pilot.pause()
        assert app.theme == f"minicode-{names[1]}"
        # step through the rest and confirm wraparound
        for _ in range(len(names) - 1):
            await app.run_action("cycle_theme")
            await pilot.pause()
        assert app.theme == f"minicode-{names[0]}"


# --------------------------------------------------------------------------- #
# the blocking prompt round trip (agent thread <-> UI thread)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("key", "expected"),
    [("y", AskReply.ONCE), ("a", AskReply.ALWAYS), ("n", AskReply.REJECT)],
)
async def test_permission_prompt_unblocks_the_agent_thread(key, expected):
    """The agent parks on a worker thread; only the UI thread can answer it."""
    from minicode.ui.textual.app import MiniTUI
    from minicode.ui.textual.widgets import PermissionBar, PermissionRequest

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        answers: list[AskReply] = []
        worker = threading.Thread(
            target=lambda: answers.append(app.request_permission(PermissionRequest("bash", ["ls"], "bash"))),
            daemon=True,
        )
        worker.start()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not app.query_one("#permission", PermissionBar).display:
            await pilot.pause(0.01)
        bar = app.query_one("#permission", PermissionBar)
        assert bar.display, "prompt never appeared"

        # The bar takes focus just after the refresh that renders it, mirroring
        # real-terminal timing; wait for it before pressing the answer key.
        while time.monotonic() < deadline and app.focused is not bar:
            await pilot.pause(0.01)
        assert app.focused is bar, "permission bar never took focus"

        await pilot.press(key)
        worker.join(timeout=5)

        assert not worker.is_alive(), "agent thread stayed blocked after the answer"
        assert answers == [expected]
        assert app._pending is None
        # teardown belongs to the UI thread: the bar closes itself
        assert app.query_one("#permission", PermissionBar).display is False


async def test_escape_rejects_a_permission_request():
    from minicode.ui.textual.app import MiniTUI
    from minicode.ui.textual.widgets import PermissionBar, PermissionRequest

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        answers: list[AskReply] = []
        worker = threading.Thread(
            target=lambda: answers.append(app.request_permission(PermissionRequest("bash", ["rm -rf *"], "bash"))),
            daemon=True,
        )
        worker.start()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not app.query_one("#permission", PermissionBar).display:
            await pilot.pause(0.01)
        bar = app.query_one("#permission", PermissionBar)
        assert bar.display, "prompt never appeared"

        while time.monotonic() < deadline and app.focused is not bar:
            await pilot.pause(0.01)
        assert app.focused is bar, "permission bar never took focus"

        await pilot.press("escape")
        worker.join(timeout=5)
        assert answers == [AskReply.REJECT]
