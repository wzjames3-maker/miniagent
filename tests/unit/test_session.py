"""Unit tests: session CRUD, persistence, fork, title, tool-call history."""

from __future__ import annotations

import pytest

from minicode.session.manager import SessionManager, auto_title
from minicode.session.models import Session, ToolCallRecord

pytestmark = pytest.mark.unit


@pytest.fixture
def sessions(tmp_path):
    return SessionManager(directory=tmp_path / "sessions")


# --------------------------------------------------------------------------- #
# titles
# --------------------------------------------------------------------------- #
def test_auto_title_collapses_whitespace_and_truncates():
    assert auto_title("  fix   the  bug  ") == "fix the bug"
    assert auto_title("") == "New session"
    long = "x" * 200
    assert auto_title(long).endswith("...")
    assert len(auto_title(long)) <= 70


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def test_create_persists_and_can_be_read_back(sessions):
    session = sessions.create(provider="openai", model="gpt-x", cwd="/tmp")
    assert sessions.exists(session.id)
    loaded = sessions.get(session.id)
    assert loaded is not None
    assert loaded.provider == "openai"
    assert loaded.model == "gpt-x"
    assert loaded.cwd == "/tmp"


def test_list_is_sorted_by_recency(sessions, monkeypatch):
    import time as time_module

    now = [1_000.0]
    monkeypatch.setattr(time_module, "time", lambda: now[0])
    sessions.create(title="first")
    now[0] += 100
    sessions.create(title="second")
    titles = [s.title for s in sessions.list()]
    assert titles == ["second", "first"]


def test_list_limit(sessions):
    for index in range(5):
        sessions.create(title=f"s{index}")
    assert len(sessions.list(limit=2)) == 2


def test_delete_removes_the_document(sessions):
    session = sessions.create()
    assert sessions.delete(session.id)
    assert sessions.get(session.id) is None
    assert not sessions.delete(session.id)


def test_delete_many_and_delete_all(sessions):
    first = sessions.create()
    second = sessions.create()
    third = sessions.create()
    assert sessions.delete_many([first.id, second.id]) == 2
    assert not sessions.exists(first.id)
    assert not sessions.exists(second.id)
    assert sessions.exists(third.id)
    assert sessions.delete_all() == 1
    assert not sessions.exists(third.id)
    assert sessions.delete_all() == 0


def test_list_and_delete_all_can_filter_by_cwd(sessions):
    project_a = sessions.create(title="a", cwd="/tmp/project-a")
    project_b = sessions.create(title="b", cwd="/tmp/project-b")
    assert [s.id for s in sessions.list(cwd="/tmp/project-a")] == [project_a.id]
    assert sessions.delete_all(cwd="/tmp/project-a") == 1
    assert not sessions.exists(project_a.id)
    assert sessions.exists(project_b.id)


def test_require_raises_on_missing(sessions):
    with pytest.raises(KeyError, match="Session not found"):
        sessions.require("nope")


def test_missing_get_returns_none(sessions):
    assert sessions.get("missing") is None


# --------------------------------------------------------------------------- #
# messages + tool calls
# --------------------------------------------------------------------------- #
def test_messages_and_tool_calls_are_persisted(sessions):
    session = sessions.create(provider="p", model="m")
    sessions.extend_messages(session, [{"role": "user", "content": "hello", "extra": {}}])
    sessions.extend_messages(
        session,
        [
            {"role": "assistant", "content": "", "extra": {"tool_calls": []}},
            {"role": "tool", "content": "output", "extra": {}},
        ],
    )
    sessions.record_tool_call(
        session,
        ToolCallRecord(id="call_1", name="read", arguments={"file_path": "a.py"}, ok=True, duration_ms=12),
    )
    sessions.save(session)

    restored = sessions.require(session.id)
    assert restored.message_count == 3
    assert restored.tool_call_count == 1
    assert restored.tool_calls[0].name == "read"
    assert restored.last_user_message == "hello"


def test_auto_title_uses_first_user_message_once(sessions):
    session = sessions.create()
    assert session.title == "New session"
    sessions.extend_messages(session, [{"role": "user", "content": "refactor the parser", "extra": {}}])
    assert sessions.maybe_auto_title(session)
    assert session.title == "refactor the parser"
    # a later message must not overwrite it
    sessions.extend_messages(session, [{"role": "user", "content": "now fix tests", "extra": {}}])
    assert not sessions.maybe_auto_title(session)
    assert session.title == "refactor the parser"


def test_retitle(sessions):
    session = sessions.create()
    sessions.retitle(session.id, "my title")
    assert sessions.require(session.id).title == "my title"
    # empty title is a no-op
    sessions.retitle(session.id, "")
    assert sessions.require(session.id).title == "my title"


def test_set_model(sessions):
    session = sessions.create(provider="a", model="b")
    sessions.set_model(session, "anthropic", "claude")
    restored = sessions.require(session.id)
    assert (restored.provider, restored.model) == ("anthropic", "claude")


# --------------------------------------------------------------------------- #
# fork
# --------------------------------------------------------------------------- #
def test_fork_copies_history_and_is_independent(sessions):
    parent = sessions.create(provider="p", model="m", title="parent")
    for text in ("one", "two", "three"):
        sessions.extend_messages(parent, [{"role": "user", "content": text, "extra": {}}])
    sessions.record_tool_call(parent, ToolCallRecord(id="c1", name="read", arguments={}))
    sessions.save(parent)

    fork = sessions.fork(parent.id)
    assert fork.id != parent.id
    assert fork.parent_id == parent.id
    assert fork.title == "parent (fork)"
    assert fork.message_count == 3
    assert fork.tool_call_count == 1
    assert fork.metadata["forked_from"] == parent.id

    # mutating the fork must not touch the parent
    sessions.extend_messages(fork, [{"role": "user", "content": "four", "extra": {}}])
    sessions.save(fork)
    assert sessions.require(parent.id).message_count == 3
    assert sessions.require(fork.id).message_count == 4


def test_fork_can_truncate_history(sessions):
    parent = sessions.create()
    for text in ("one", "two", "three"):
        sessions.extend_messages(parent, [{"role": "user", "content": text, "extra": {}}])
    sessions.save(parent)

    fork = sessions.fork(parent.id, at_message=2, title="rewind")
    assert fork.message_count == 2
    assert fork.fork_point == 2
    assert fork.title == "rewind"


def test_fork_of_missing_session_raises(sessions):
    with pytest.raises(KeyError):
        sessions.fork("nope")


def test_fork_deep_copies_message_payloads(sessions):
    parent = sessions.create()
    sessions.extend_messages(parent, [{"role": "user", "content": "hi", "extra": {"nested": {"a": 1}}}])
    sessions.save(parent)
    fork = sessions.fork(parent.id)
    fork.messages[0]["extra"]["nested"]["a"] = 99
    assert sessions.require(parent.id).messages[0]["extra"]["nested"]["a"] == 1


# --------------------------------------------------------------------------- #
# convenience
# --------------------------------------------------------------------------- #
def test_most_recent(sessions):
    b = sessions.create(title="beta task")
    b.updated_at += 10
    sessions.save(b)
    assert sessions.most_recent().id == b.id


def test_summaries_are_lightweight(sessions):
    sessions.create(title="hello")
    summary = sessions.summaries()[0]
    assert summary["title"] == "hello"
    # a summary must not carry the full message history
    assert "messages" in summary
    assert "messages" in summary and not isinstance(summary["messages"], list)


def test_session_round_trip_keeps_metadata(sessions):
    session = sessions.create(metadata={"foo": "bar"})
    restored = sessions.require(session.id)
    assert restored.metadata["foo"] == "bar"


def test_session_model_defaults_are_sane():
    session = Session()
    assert session.id
    assert session.messages == []
    assert session.tool_calls == []
    assert session.title == "New session"


# --------------------------------------------------------------------------- #
# auto-title peeling (wrapped Task:/Instructions: template)
# --------------------------------------------------------------------------- #
_WRAPPED_TASK = (
    "Task:\nread the file\n\n"
    "Instructions:\n- Treat the task above as the single source of truth.\n"
    "- Follow the Workflow in the system prompt exactly.\n"
    "- When you finish, summarize and stop calling tools."
)


def test_auto_title_peels_the_task_template():
    """Titles must come from the real request, not the Task:/Instructions: shell."""
    assert auto_title(_WRAPPED_TASK) == "read the file"


def test_auto_title_peels_non_ascii_task_template():
    wrapped = "Task:\n你好\n\nInstructions:\n- Treat the task above as the single source of truth."
    assert auto_title(wrapped) == "你好"


def test_auto_title_keeps_plain_messages_unwrapped():
    assert auto_title("refactor the parser") == "refactor the parser"


def test_maybe_auto_title_titles_from_unwrapped_task(sessions):
    session = sessions.create()
    sessions.extend_messages(session, [{"role": "user", "content": _WRAPPED_TASK, "extra": {}}])
    assert sessions.maybe_auto_title(session)
    assert session.title == "read the file"


# --------------------------------------------------------------------------- #
# prune empty placeholder sessions
# --------------------------------------------------------------------------- #
def test_prune_removes_only_empty_placeholders(sessions):
    empty = sessions.create()  # "New session", no messages -> persisted
    real = sessions.create(title="real work")
    sessions.extend_messages(real, [{"role": "user", "content": "hi", "extra": {}}])
    sessions.save(real)
    assert sessions.exists(empty.id)
    assert sessions.exists(real.id)

    removed = sessions.prune_empty()
    assert removed == 1
    assert not sessions.exists(empty.id)
    assert sessions.exists(real.id)


def test_repair_titles_fixes_old_wrapped_titles(sessions):
    """Sessions written by the old titler keep a Task:/Instructions: title; repair it."""
    session = sessions.create(title="Task: read the file Instructions: - Treat the task above as the single so...")
    sessions.extend_messages(session, [{"role": "user", "content": _WRAPPED_TASK, "extra": {}}])
    sessions.save(session)

    fixed = sessions.repair_titles()
    assert fixed == 1
    restored = sessions.require(session.id)
    assert restored.title == "read the file"


def test_repair_titles_leaves_clean_titles_alone(sessions):
    session = sessions.create(title="clean title")
    sessions.extend_messages(session, [{"role": "user", "content": "hi", "extra": {}}])
    sessions.save(session)
    assert sessions.repair_titles() == 0
    assert sessions.require(session.id).title == "clean title"
