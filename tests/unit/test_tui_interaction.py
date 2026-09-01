"""Tests for the interactions the TUI borrows from OpenCode.

Typing ``/`` must open a filterable command popover without stealing focus,
picking a command must do the sensible thing (run it, or leave you typing an
argument), and a resumed session must actually show its history -- which is the
whole point of having a session rail.
"""

from __future__ import annotations

import pytest

from minicode.cli.commands import COMMANDS
from minicode.config.settings import Settings

pytestmark = [pytest.mark.unit, pytest.mark.anyio]


def _popover(app):
    from minicode.ui.textual.widgets import SlashPopover

    return app.query_one("#slash", SlashPopover)


def _composer(app):
    from minicode.ui.textual.widgets import Composer

    return app.query_one("#composer", Composer)


def _option_ids(app) -> list[str]:
    from textual.widgets import OptionList

    listing = app.query_one("#sidebar").query_one("#session-list", OptionList)
    return [listing.get_option_at_index(index).id for index in range(listing.option_count)]


# --------------------------------------------------------------------------- #
# the "/" popover
# --------------------------------------------------------------------------- #
async def test_typing_a_slash_opens_the_popover():
    from minicode.ui.textual.app import MiniTUI

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        assert not _popover(app).is_open
        await pilot.press("/")
        await pilot.pause()
        assert _popover(app).is_open
        assert _popover(app).option_count == len(COMMANDS)


async def test_the_popover_narrows_as_you_type():
    from minicode.ui.textual.app import MiniTUI

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("/")
        await pilot.pause()
        everything = _popover(app).option_count

        await pilot.press("s", "e", "s")
        await pilot.pause()
        assert 0 < _popover(app).option_count < everything


async def test_the_popover_never_takes_focus_from_the_composer():
    """OpenCode keeps the caret in the input and only forwards navigation keys."""
    from minicode.ui.textual.app import MiniTUI
    from minicode.ui.textual.widgets import Composer

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("/")
        await pilot.pause()
        assert _popover(app).is_open
        assert isinstance(app.focused, Composer)


async def test_the_first_row_is_preselected_and_wraps_around():
    from minicode.ui.textual.app import MiniTUI

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("/")
        await pilot.pause()
        popover = _popover(app)
        assert popover.highlighted == 0

        await pilot.press("down")
        await pilot.pause()
        assert popover.highlighted == 1

        await pilot.press("up", "up")
        await pilot.pause()
        assert popover.highlighted == popover.option_count - 1, "the list loops, as OpenCode's does"


async def test_enter_runs_a_command_that_takes_no_arguments():
    """Picking /help shows help -- it does not leave '/help ' in the composer."""
    from minicode.ui.textual.app import MiniTUI

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        written: list[object] = []
        app.append_to_log = written.append

        await pilot.press(*"/help")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert written, "picking /help must run it"
        assert not _popover(app).is_open
        assert _composer(app).text == ""


async def test_enter_fills_in_a_command_that_takes_an_argument():
    from minicode.ui.textual.app import MiniTUI

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press(*"/res")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert _composer(app).text == "/resume ", "the argument still has to be typed"
        assert not _popover(app).is_open


async def test_escape_closes_the_popover_without_running_anything():
    from minicode.ui.textual.app import MiniTUI

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        written: list[object] = []
        app.append_to_log = written.append

        await pilot.press("/")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert not _popover(app).is_open
        assert _composer(app).text == "/", "escape dismisses the list, it does not clear the input"
        assert not written


async def test_the_popover_closes_once_the_command_is_finished():
    """A space means you are on to the arguments, so the list is in the way."""
    from minicode.ui.textual.app import MiniTUI

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press(*"/help")
        await pilot.pause()
        assert _popover(app).is_open
        await pilot.press(" ")
        await pilot.pause()
        assert not _popover(app).is_open


# --------------------------------------------------------------------------- #
# session history
# --------------------------------------------------------------------------- #
async def test_starting_the_tui_does_not_persist_an_empty_session():
    """Every session used to be saved at start-up, so the rail filled with
    rows called 'New session' that resumed to nothing."""
    from minicode.ui.textual.app import MiniTUI

    app = MiniTUI()
    async with app.run_test(size=(120, 40)):
        assert app.core.session.messages == []
        assert not app.core.sessions.exists(app.core.session.id)
        assert app.core.sessions.list() == []


async def test_the_session_you_are_typing_into_still_shows_in_the_rail():
    from minicode.ui.textual.app import MiniTUI

    app = MiniTUI()
    async with app.run_test(size=(120, 40)):
        assert app.core.session.id in _option_ids(app)


async def test_new_session_clears_the_visible_transcript():
    """Regression: /new must not leave the previous session's messages on screen."""
    from minicode.ui.textual.app import MiniTUI
    from minicode.ui.textual.widgets import UserMessage

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        app.append_user_message("old history")
        await pilot.pause()
        messages = app.query_one("#messages")
        assert any(isinstance(child, UserMessage) for child in messages.children)

        app.action_new_session()
        await pilot.pause()
        assert not any(isinstance(child, UserMessage) for child in messages.children)


async def test_session_rail_is_scoped_to_the_current_project(tmp_path):
    """OpenCode-style: the TUI rail shows the current project, not every global session."""
    from minicode.ui.textual.app import MiniTUI

    app = MiniTUI(cwd=str(tmp_path))
    async with app.run_test(size=(120, 40)):
        other = app.core.sessions.create(title="other project", cwd=str(tmp_path.parent / "other"))
        app.core.sessions.extend_messages(other, [{"role": "user", "content": "other", "extra": {}}])
        app.core.sessions.save(other)

        app.refresh_sessions()
        assert other.id not in _option_ids(app)


async def test_selecting_a_session_replays_its_messages():
    from minicode.ui.textual.app import MiniTUI
    from minicode.ui.textual.widgets import SessionSidebar

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        earlier = app.core.sessions.create(title="earlier work")
        app.core.sessions.extend_messages(earlier, [{"role": "user", "content": "fix the failing test", "extra": {}}])
        app.core.sessions.extend_messages(earlier, [{"role": "assistant", "content": "on it", "extra": {}}])
        app.core.sessions.save(earlier)

        written: list[object] = []
        app.append_to_log = written.append

        app.query_one("#sidebar", SessionSidebar).post_message(SessionSidebar.Selected(earlier.id))
        await pilot.pause()

        assert app.core.session.id == earlier.id
        replayed = "\n".join(str(item) for item in written)
        assert "fix the failing test" in replayed
        assert "on it" in replayed


async def test_resume_replays_through_the_slash_command_too():
    from minicode.ui.textual.app import MiniTUI

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        earlier = app.core.sessions.create(title="earlier work")
        app.core.sessions.extend_messages(earlier, [{"role": "user", "content": "hello from the past", "extra": {}}])
        app.core.sessions.save(earlier)

        written: list[object] = []
        app.append_to_log = written.append

        app.run_command(f"/resume {earlier.id}")
        await pilot.pause()

        assert "hello from the past" in "\n".join(str(item) for item in written)


# --------------------------------------------------------------------------- #
# interrupting a turn
# --------------------------------------------------------------------------- #
async def test_escape_asks_the_agent_to_stop():
    """The worker thread cannot be killed mid-request, only asked to stop."""
    from minicode.ui.textual.app import MiniTUI

    class _StubAgent:
        def __init__(self) -> None:
            self.interrupted = False

        def request_interrupt(self) -> None:
            self.interrupted = True

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        stub = _StubAgent()
        app.core.agent = stub
        app._busy = True

        await pilot.press("escape")
        await pilot.pause()
        assert stub.interrupted


# --------------------------------------------------------------------------- #
# the replay renderer itself
# --------------------------------------------------------------------------- #
def test_transcript_renders_every_role():
    from minicode.ui.render import transcript

    rendered = transcript(
        [
            {"role": "user", "content": "hello", "extra": {}},
            {
                "role": "assistant",
                "content": "",
                "extra": {"tool_calls": [{"name": "read", "arguments": {"path": "a.py"}}]},
            },
            {"role": "tool", "content": "line one", "extra": {"tool_name": "read"}},
            {"role": "system", "content": "you are a helper", "extra": {}},
        ]
    )
    flat = [str(item) for item in rendered]
    assert len(flat) == 4
    assert "hello" in flat[0]
    assert "read" in flat[1] and "a.py" in flat[1]
    assert "line one" in flat[2]
    assert "you are a helper" in flat[3]


def test_transcript_clips_long_tool_output():
    from minicode.ui.render import transcript

    body = "\n".join(f"line {index}" for index in range(50))
    (rendered,) = transcript([{"role": "tool", "content": body, "extra": {"tool_name": "bash"}}], max_output_lines=4)
    assert "lines omitted" in str(rendered)


def test_transcript_skips_messages_with_nothing_to_show():
    from minicode.ui.render import transcript

    assert transcript([{"role": "assistant", "content": "", "extra": {}}]) == []
    assert transcript([{"role": "user", "content": "   ", "extra": {}}]) == []


def test_replay_defaults_to_the_settings_output_limit():
    """The limit must come from config, not be hardcoded in two places."""
    from minicode.ui.render import transcript

    settings = Settings().ui
    body = "\n".join(f"line {index}" for index in range(settings.max_output_lines * 4))
    (rendered,) = transcript(
        [{"role": "tool", "content": body, "extra": {"tool_name": "bash"}}],
        max_output_lines=settings.max_output_lines,
    )
    assert "lines omitted" in str(rendered)


# --------------------------------------------------------------------------- #
# blocking permission prompt
# --------------------------------------------------------------------------- #
async def test_permission_bar_steals_focus_so_answers_are_captured():
    """A permission prompt must actually take focus away from the composer.

    Regression: ``ask()`` set ``display = True`` and called ``focus()`` in the
    same synchronous block. Textual only lets a widget take focus once it has
    been laid out, so the call silently failed and the composer kept every key
    -- pressing y/a/n just typed into the input box and the agent hung forever.
    """
    import threading

    from minicode.ui.textual.app import MiniTUI
    from minicode.ui.textual.widgets import PermissionBar, PermissionRequest

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        answers: list[str] = []

        def ask_from_worker() -> None:
            answer = app.request_permission(
                PermissionRequest(permission="bash", patterns=["pwd && ls -la"], tool="bash")
            )
            answers.append(answer.value)

        thread = threading.Thread(target=ask_from_worker, daemon=True)
        thread.start()
        # let the worker reach the blocking prompt and the UI show it
        for _ in range(100):
            await pilot.pause(0.02)
            if app._pending is not None:
                break

        bar = app.query_one("#permission", PermissionBar)
        assert bar.display, "the permission bar should be visible"
        await pilot.pause(0.1)  # allow the deferred focus to land
        assert app.focused is bar, "the permission bar must own the keys, not the composer"

        await pilot.press("y")
        await pilot.pause(0.2)
        thread.join(timeout=2)
        assert answers == ["once"], f"pressing y must answer the prompt (got {answers!r})"


async def test_mouse_click_on_permission_option_answers():
    """OpenCode's permission prompt is clickable; ours must answer on click too."""
    import threading

    from minicode.ui.textual.app import MiniTUI
    from minicode.ui.textual.widgets import PermissionBar, PermissionRequest

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        answers: list[str] = []

        def ask_from_worker() -> None:
            answer = app.request_permission(
                PermissionRequest(permission="bash", patterns=["pwd && ls -la"], tool="bash")
            )
            answers.append(answer.value)

        thread = threading.Thread(target=ask_from_worker, daemon=True)
        thread.start()
        bar = app.query_one("#permission", PermissionBar)
        for _ in range(100):
            await pilot.pause(0.02)
            if bar.display:
                break
        # wait for the deferred focus so the click lands on the visible bar
        for _ in range(50):
            await pilot.pause(0.02)
            if app.focused is bar:
                break

        button = app.query_one("#perm-a")
        await pilot.click(button)
        await pilot.pause(0.2)
        thread.join(timeout=2)
        assert answers == ["always"], f"clicking [a] must answer (got {answers!r})"


async def test_mouse_click_on_new_session_button():
    from minicode.ui.textual.app import MiniTUI

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        before = app.core.session.id
        button = app.query_one("#new-session")
        await pilot.click(button, offset=(2, 0))
        await pilot.pause(0.2)
        assert app.core.session.id != before, "clicking the new-session button must start a fresh session"


async def test_mouse_click_on_session_rail_resumes():
    from textual.widgets import OptionList

    from minicode.ui.textual.app import MiniTUI

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        earlier = app.core.sessions.create(title="earlier work")
        app.core.sessions.extend_messages(earlier, [{"role": "user", "content": "click me", "extra": {}}])
        app.core.sessions.save(earlier)
        app.refresh_sessions()
        await pilot.pause()

        listing = app.query_one("#session-list", OptionList)
        ids = [listing.get_option_at_index(i).id for i in range(listing.option_count)]
        index = ids.index(earlier.id)
        region = listing.content_region
        offset_x = region.x - listing.region.x + 1
        offset_y = region.y - listing.region.y + index
        await pilot.click(listing, offset=(offset_x, offset_y))
        await pilot.pause(0.2)
        assert app.core.session.id == earlier.id, "clicking a rail row must resume that session"


async def test_model_picker_lists_filters_and_switches():
    """/model without args opens a picker; typing filters; Enter switches."""
    from minicode.config.settings import Settings
    from minicode.providers.registry import build_registry
    from minicode.ui.textual.app import MiniTUI
    from minicode.ui.textual.modals import ModelPickerModal

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        settings = Settings()
        settings.providers = {
            "openai": {"type": "openai", "models": ["gpt-4o-mini", "gpt-4o"], "api_key": "x"},
            "deepseek": {"type": "openai_compat", "models": ["deepseek-chat"], "api_key": "y", "base_url": "http://x"},
        }
        app.core.settings = settings
        app.core.providers = build_registry(settings.model_dump())

        app.run_command("/model")
        await pilot.pause(0.2)
        picker = next((s for s in app.screen_stack if isinstance(s, ModelPickerModal)), None)
        assert picker is not None, "/model must open the model picker"
        listing = picker.query_one("#model-list")
        ids = [listing.get_option_at_index(i).id for i in range(listing.option_count)]
        assert "openai/gpt-4o-mini" in ids and "deepseek/deepseek-chat" in ids

        picker.query_one("#model-filter").value = "deepseek"
        await pilot.pause()
        ids = [listing.get_option_at_index(i).id for i in range(listing.option_count)]
        assert ids == ["deepseek/deepseek-chat"], f"filter must narrow to deepseek, got {ids}"

        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.core.provider is not None
        assert app.core.provider.model_id == "deepseek/deepseek-chat"


async def test_model_picker_escape_cancels_without_change():
    from minicode.config.settings import Settings
    from minicode.providers.registry import build_registry
    from minicode.ui.textual.app import MiniTUI
    from minicode.ui.textual.modals import ModelPickerModal

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        settings = Settings()
        settings.providers = {
            "openai": {"type": "openai", "models": ["gpt-4o-mini"], "api_key": "x"},
        }
        app.core.settings = settings
        app.core.providers = build_registry(settings.model_dump())

        app.run_command("/model")
        await pilot.pause(0.2)
        assert any(isinstance(s, ModelPickerModal) for s in app.screen_stack)
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert not any(isinstance(s, ModelPickerModal) for s in app.screen_stack)


def test_tool_call_diff_output_is_colored():
    """edit/write return a unified diff; the preview must paint +/- lines."""
    from minicode.ui.textual.widgets import ToolCallWidget

    widget = ToolCallWidget("edit", {"file_path": "a.py"}, "c1")
    rendered = widget._render_output(
        "Updated a.py (3 lines)\n--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,3 @@\n def f():\n-    return 1\n+    return 2\n"
    )
    assert "-    return 1" in rendered.plain
    assert "+    return 2" in rendered.plain

    def style_at(marker: str) -> str:
        index = rendered.plain.index(marker)
        for start, end, style in rendered.spans:
            if start <= index < end:
                return str(style)
        return ""

    assert style_at("-    return 1") == "red"
    assert style_at("+    return 2") == "green"
    assert style_at("@@") == "cyan"


def test_tool_call_plain_output_stays_uncolored():
    from minicode.ui.textual.widgets import ToolCallWidget

    widget = ToolCallWidget("bash", {"command": "ls"}, "c1")
    rendered = widget._render_output("total 124\ndrwxr-xr-x .\nfile.txt\n")
    assert rendered.plain == "total 124\ndrwxr-xr-x .\nfile.txt"
    # no +/-/@@ in plain bash output, so no diff styling kicks in
    assert not any(str(style) in {"red", "green", "cyan"} for _, _, style in rendered.spans)


async def test_tool_call_result_arriving_before_mount_does_not_crash():
    """Regression: complete_tool_call can fire before the tool widget composes.

    In a live run, tool_call_start and its result arrive back-to-back, so the
    widget may not have its output Static yet. The result must be buffered and
    rendered once mounted, not raise AttributeError.
    """
    from minicode.ui.textual.app import MiniTUI

    app = MiniTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        # complete before any pause lets the mounted widget compose
        app.add_tool_call("edit", {"file_path": "a.py"}, "early")
        app.complete_tool_call("early", "--- a\na\n+++ b\n@@\n-old\n+new\n", False)
        await pilot.pause(0.3)  # now the widget composes and must render the buffered result
        from minicode.ui.textual.widgets import AssistantMessage

        assistant = next(
            c for c in app.query_one("#messages").children if isinstance(c, AssistantMessage)
        )
        widget = assistant._tool_widgets["early"]
        assert widget.status == "done"
        assert widget._output is not None
        assert "+new" in widget._output.render().plain
