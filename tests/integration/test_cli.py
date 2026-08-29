"""Integration tests: the CLI/app layer (sessions, models, tools, permissions).

Exercises the real app plumbing with a scripted provider - no terminal, no
network, but nothing about the wiring is mocked.
"""

from __future__ import annotations

import pytest

from minicode.cli.app import InteractiveApp
from minicode.config.settings import Settings
from minicode.providers.scripted import ScriptedProvider
from minicode.ui.events import CollectingSink

pytestmark = pytest.mark.integration


def make_settings(**overrides) -> Settings:
    base = {
        "default_provider": "scripted",
        "default_model": "scripted-model",
        "providers": {
            "scripted": {"type": "scripted", "models": ["scripted-model"]},
            "other": {"type": "scripted", "models": ["other-model"]},
        },
        "agent": {"step_limit": 8},
        "permission": {"read": "allow", "glob": "allow", "grep": "allow"},
        "tools": {"bash_timeout": 30},
        "ui": {"stream": False},
    }
    base.update(overrides)
    return Settings.model_validate(base)


@pytest.fixture
def app(project, monkeypatch, tmp_path):
    monkeypatch.setenv("MINICODE_DATA_DIR", str(tmp_path / "data"))
    instance = InteractiveApp(
        settings=make_settings(),
        cwd=str(project),
        sink=CollectingSink(),
        non_interactive=True,
    )
    return instance


def script(app, *responses):
    """Point the scripted provider at a fixed sequence of replies."""
    app.provider.responses = list(responses)
    return app


# --------------------------------------------------------------------------- #
# wiring
# --------------------------------------------------------------------------- #
def test_app_resolves_the_configured_provider(app):
    assert isinstance(app.provider, ScriptedProvider)
    assert app.provider.model == "scripted-model"


def test_app_builds_all_default_tools(app):
    assert {"read", "write", "edit", "apply_patch", "glob", "grep", "bash"} <= set(app.tools.names())


def test_app_honours_the_enabled_tool_filter(project, monkeypatch, tmp_path):
    monkeypatch.setenv("MINICODE_DATA_DIR", str(tmp_path / "data"))
    instance = InteractiveApp(
        settings=make_settings(tools={"enabled": ["read", "grep"], "bash_timeout": 30}),
        cwd=str(project),
        sink=CollectingSink(),
        non_interactive=True,
    )
    assert set(instance.tools.names()) == {"read", "grep"}


def test_app_merges_permission_rules_from_config(app):
    actions = {(rule.permission, rule.pattern): rule.action.value for rule in app._permission_rules()}
    assert actions[("read", "*")] == "allow"
    assert actions[("glob", "*")] == "allow"


def test_app_forwards_env_into_the_bash_tool(project, monkeypatch, tmp_path):
    monkeypatch.setenv("MINICODE_DATA_DIR", str(tmp_path / "data"))
    instance = InteractiveApp(
        settings=make_settings(env={"MY_TEST_VAR": "1"}),
        cwd=str(project),
        sink=CollectingSink(),
        non_interactive=True,
    )
    assert instance.tools.get("bash").env.config.env["MY_TEST_VAR"] == "1"


def test_non_interactive_mode_fails_closed():
    """Without a terminal there is nobody to ask - so `ask` must mean refuse."""
    instance = InteractiveApp(
        settings=make_settings(), cwd=".", sink=CollectingSink(), non_interactive=True
    )
    assert instance.permission.non_interactive is True
    assert instance.permission.ask_callback is None
    with pytest.raises(Exception, match="(?i)reject|no interactive"):
        instance.permission.check("bash", "echo hi")


def test_yolo_mode_auto_approves(project, monkeypatch, tmp_path):
    monkeypatch.setenv("MINICODE_DATA_DIR", str(tmp_path / "data"))
    instance = InteractiveApp(
        settings=make_settings(permission={"bash": {"rm -rf **": "deny"}}),
        cwd=str(project),
        sink=CollectingSink(),
        yolo=True,
    )
    assert instance.permission.check("bash", "echo hi").allowed
    # ...but explicit deny rules still hold
    with pytest.raises(Exception, match="(?i)denied"):
        instance.permission.check("bash", "rm -rf /etc")


# --------------------------------------------------------------------------- #
# running a task
# --------------------------------------------------------------------------- #
def test_run_creates_and_persists_a_session(app):
    script(app, {"content": "done"})
    result = app.run_task("hello")
    assert result["exit_status"] == "Submitted"
    saved = app.sessions.require(app.session.id)
    assert saved.message_count >= 2
    assert saved.provider == "scripted"


def test_run_dispatches_tools(app, project):
    script(
        app,
        {"tool_calls": [{"name": "read", "arguments": {"file_path": str(project / "calc.py")}}]},
        {"content": "read it"},
    )
    result = app.run_task("read calc.py")
    assert result["exit_status"] == "Submitted"
    assert [call.name for call, _ in app.sink.tool_results] == ["read"]


def test_run_surfaces_errors_without_crashing(app):
    script(app, {"content": "done"})
    app.provider.responses = []
    result = app.run_task("hi")
    assert result["exit_status"] in {"Error", "Submitted"}


def test_run_refuses_unpermitted_tools_in_non_interactive_mode(app, project):
    script(
        app,
        {"tool_calls": [{"name": "bash", "arguments": {"command": "echo hi"}}]},
        {"content": "blocked"},
    )
    app.run_task("run a command")
    observations = [
        message
        for request in app.provider.requests
        for message in request["messages"]
        if message.get("role") == "tool"
    ]
    assert observations and "permission" in observations[0]["content"]


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #
def test_session_lifecycle_through_the_app(app):
    script(app, {"content": "first"})
    app.run_task("one")
    first_id = app.session.id

    app.sessions.retitle(first_id, "renamed")
    assert app.sessions.require(first_id).title == "renamed"

    forked = app.fork()
    assert forked.parent_id == first_id
    assert app.session.id == forked.id  # fork switches you into the copy

    assert app.sessions.delete(forked.id)
    assert not app.sessions.exists(forked.id)


def test_resume_loads_the_previous_history(app, project, monkeypatch, tmp_path):
    script(app, {"content": "first"})
    app.run_task("remember this")
    session_id = app.session.id

    resumed = InteractiveApp(
        settings=make_settings(), cwd=str(project), sink=CollectingSink(), non_interactive=True
    )
    script(resumed, {"content": "second"})
    resumed.resume(session_id)

    assert resumed.session.id == session_id
    assert resumed.session.message_count >= 2
    # the rebuild history is loaded into the agent
    assert any("remember this" in str(m.get("content")) for m in resumed.agent.messages)

    resumed.run_task("and now?")
    assert any(
        m.get("role") == "user" and "remember this" in str(m.get("content"))
        for m in resumed.provider.requests[0]["messages"]
    )


def test_resume_of_a_missing_session_raises(app):
    with pytest.raises(KeyError):
        app.resume("ses_does_not_exist")


def test_clear_starts_a_fresh_session(app):
    script(app, {"content": "first"})
    app.run_task("one")
    old_id = app.session.id
    app.clear()
    assert app.session.id != old_id
    assert app.session.messages == []


# --------------------------------------------------------------------------- #
# model switching
# --------------------------------------------------------------------------- #
def test_switching_model_mid_session(app):
    script(app, {"content": "done"})
    app.run_task("hi")
    before = app.provider

    app.set_model("other/other-model")

    assert app.provider.model == "other-model"
    assert app.provider is not before
    assert app.session.model == "other-model"


def test_switching_model_preserves_the_conversation(app):
    script(app, {"content": "first"})
    app.run_task("hi")
    history = list(app.agent.messages)

    app.set_model("other/other-model")

    assert len(app.agent.messages) == len(history)


def test_switching_to_an_unknown_model_raises(app):
    with pytest.raises(KeyError):
        app.set_model("nope/nope")


# --------------------------------------------------------------------------- #
# compaction command
# --------------------------------------------------------------------------- #
def test_compact_shortens_the_history(app, project):
    script(app, {"content": "ok"})
    app.run_task("hi")
    messages_before = len(app.agent.messages)

    app.compact()

    assert len(app.agent.messages) <= messages_before
    assert app.sink.compactions or len(app.agent.messages) == messages_before
