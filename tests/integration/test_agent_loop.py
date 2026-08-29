"""Integration tests: the full loop User -> Agent -> Model -> Tool -> Result -> Model.

Everything here is real except the *model*, which is replaced by a deterministic
scripted provider. The agent loop, tool dispatch, permissions, session
persistence and context management all run for real - no mocking of the parts
under test.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from minicode.agent.core import CodingAgent
from minicode.agent.state import AgentStatus
from minicode.context.manager import ContextManager
from minicode.permission.manager import AskReply, PermissionManager, PermissionMode
from minicode.permission.policy import Action, Rule
from minicode.providers.scripted import ScriptedProvider
from minicode.session.manager import SessionManager
from minicode.session.models import Session
from minicode.tools.registry import ToolRegistry, build_default_registry
from minicode.ui.events import CollectingSink

pytestmark = pytest.mark.integration


@dataclass
class Harness:
    """Everything wired together, ready to build agents with scripted replies."""

    build: Callable[..., CodingAgent]
    sink: CollectingSink
    sessions: SessionManager
    session: Session
    registry: ToolRegistry
    project: Path


@pytest.fixture
def harness(project, tmp_path):
    """Everything wired together, with a scripted model standing in."""
    sink = CollectingSink()
    sessions = SessionManager(directory=tmp_path / "sessions")
    session = sessions.create(provider="scripted", model="scripted-model", cwd=str(project))
    registry = build_default_registry(cwd=str(project))

    def build(responses, *, permission=None, context=None, stream=True):
        provider = ScriptedProvider(responses=responses)
        agent = CodingAgent(
            provider,
            registry,
            permission=permission or PermissionManager(mode=PermissionMode.AUTO),
            context=context or ContextManager(),
            session=session,
            sessions=sessions,
            sink=sink,
            cwd=str(project),
            stream=stream,
        )
        return agent

    return Harness(build=build, sink=sink, sessions=sessions, session=session, registry=registry, project=project)


# --------------------------------------------------------------------------- #
# the core loop
# --------------------------------------------------------------------------- #
def test_user_to_model_to_tool_to_result_to_model(harness):
    """The canonical closed loop, asserted end to end."""
    target = harness.project / "calc.py"
    agent = harness.build(
        [
            {"tool_calls": [{"name": "read", "arguments": {"file_path": str(target)}}]},
            {
                "tool_calls": [
                    {
                        "name": "edit",
                        "arguments": {"file_path": str(target), "old_string": "a + b", "new_string": "a * b"},
                    }
                ]
            },
            {"content": "Changed add() to multiply."},
        ]
    )

    result = agent.run("Please make add() multiply instead.")

    assert result["exit_status"] == "Submitted"
    assert "multiply" in result["submission"]
    # 1. the file really changed
    assert b"return a * b" in target.read_bytes()
    # 2. the model saw the tool result (it is in the next request)
    observation = [
        message
        for request in agent.provider.requests
        for message in request["messages"]
        if message.get("role") == "tool"
    ]
    assert observation, "tool result was never fed back to the model"
    assert "def add" in observation[0]["content"]
    # 3. every step was recorded
    assert agent.state.tool_calls == 2
    assert agent.state.step == 3
    assert [call.name for call, _ in harness.sink.tool_results] == ["read", "edit"]


def test_tool_errors_are_returned_to_the_model_and_recovered(harness):
    """A failing tool must become an observation the agent can react to."""
    missing = str(harness.project / "nope.py")
    agent = harness.build(
        [
            {"tool_calls": [{"name": "read", "arguments": {"file_path": missing}}]},
            {"content": "The file was missing, so I stopped."},
        ]
    )
    result = agent.run("Read nope.py")

    assert result["exit_status"] == "Submitted"
    assert agent.state.tool_errors == 1
    tool_message = [
        message
        for request in agent.provider.requests
        for message in request["messages"]
        if message.get("role") == "tool"
    ][0]
    assert "file_not_found" in tool_message["content"]
    assert tool_message["extra"]["error"]["code"] == "file_not_found"
    # the second model call must have received the error before answering
    second_request = agent.provider.requests[1]
    assert any(m.get("role") == "tool" for m in second_request["messages"])


def test_model_receives_tool_schemas(harness):
    agent = harness.build([{"content": "ok"}])
    agent.run("hi")
    tools = agent.provider.requests[0]["tools"]
    names = {tool["name"] for tool in tools}
    assert {"read", "write", "edit", "apply_patch", "glob", "grep", "bash"} <= names


def test_streaming_events_reach_the_ui(harness):
    agent = harness.build([{"content": "Hello from the agent."}], stream=True)
    agent.run("hi")
    assert "Hello from the agent." in harness.sink.text
    assert any(event.type == "usage" for event in harness.sink.stream_events)


def test_non_streaming_mode_also_works(harness):
    agent = harness.build([{"content": "Quiet answer."}], stream=False)
    result = agent.run("hi")
    assert result["submission"] == "Quiet answer."


# --------------------------------------------------------------------------- #
# limits and loop detection
# --------------------------------------------------------------------------- #
def test_step_limit_stops_a_runaway_agent(harness):
    agent = harness.build([{"tool_calls": [{"name": "glob", "arguments": {"pattern": "**/*.py"}}]}])
    agent.config.step_limit = 3
    result = agent.run("loop forever")
    assert result["exit_status"] != "Submitted"
    assert agent.state.step <= 4


def test_doom_loop_is_interrupted(harness):
    """Repeating the identical tool call must not spin forever."""
    agent = harness.build([{"tool_calls": [{"name": "glob", "arguments": {"pattern": "**/*.py"}}]}])
    agent.config.doom_loop_threshold = 3
    agent.config.step_limit = 10
    agent.run("find python files")
    # the interrupt message reached the model
    interrupts = [
        message
        for request in agent.provider.requests
        for message in request["messages"]
        if (message.get("extra") or {}).get("interrupt_type") == "DoomLoop"
    ]
    assert interrupts, "doom loop was never detected"
    assert "loop" in interrupts[0]["content"].lower()
    # the agent must have been pulled out of the repetition, not left to spin
    assert agent.state.step <= 10
    assert agent.state.tool_calls <= 10


def test_doom_loop_detection_can_be_disabled(harness):
    agent = harness.build([{"tool_calls": [{"name": "glob", "arguments": {"pattern": "**/*.py"}}]}])
    agent.config.doom_loop_threshold = 0
    agent.config.step_limit = 4
    agent.run("find python files")
    assert agent.state.tool_calls >= 4


# --------------------------------------------------------------------------- #
# permissions
# --------------------------------------------------------------------------- #
def test_permission_denial_becomes_a_tool_error(harness):
    permission = PermissionManager([Rule(permission="edit", pattern="**/*.py", action=Action.DENY)])
    target = harness.project / "calc.py"
    agent = harness.build(
        [
            {
                "tool_calls": [
                    {"name": "edit", "arguments": {"file_path": str(target), "old_string": "a + b", "new_string": "x"}}
                ]
            },
            {"content": "I was not allowed to edit."},
        ],
        permission=permission,
    )
    result = agent.run("edit calc.py")
    assert result["exit_status"] == "Submitted"
    assert b"a + b" in target.read_bytes()  # untouched
    denied = [
        message
        for request in agent.provider.requests
        for message in request["messages"]
        if message.get("role") == "tool"
    ][0]
    assert denied["extra"]["error"]["code"] == "permission_denied"


def test_permission_ask_pauses_for_the_user(harness):
    asked = []

    def ask(request):
        asked.append(request)
        return AskReply.ONCE

    permission = PermissionManager(
        [Rule(permission="bash", pattern="*", action=Action.ASK)], ask_callback=ask, mode=PermissionMode.DEFAULT
    )
    agent = harness.build(
        [{"tool_calls": [{"name": "bash", "arguments": {"command": "echo hi"}}]}, {"content": "done"}],
        permission=permission,
    )
    agent.run("run a command")
    assert asked
    assert asked[0].permission == "bash"
    assert asked[0].tool == "bash"


def test_permission_rejection_feeds_user_feedback_to_the_model(harness):
    permission = PermissionManager(
        [Rule(permission="bash", pattern="*", action=Action.ASK)],
        ask_callback=lambda request: (AskReply.REJECT, "do not run shell commands"),
    )
    agent = harness.build(
        [{"tool_calls": [{"name": "bash", "arguments": {"command": "echo hi"}}]}, {"content": "understood"}],
        permission=permission,
    )
    agent.run("run a command")
    feedback = [
        message
        for request in agent.provider.requests
        for message in request["messages"]
        if "do not run shell commands" in str(message.get("content", ""))
    ]
    assert feedback, "the user's reason never reached the model"


def test_auto_mode_skips_prompts_entirely(harness):
    permission = PermissionManager(
        [Rule(permission="edit", pattern="*", action=Action.ASK)],
        ask_callback=lambda request: AskReply.REJECT,  # would reject if consulted
        mode=PermissionMode.AUTO,
    )
    target = harness.project / "calc.py"
    agent = harness.build(
        [
            {
                "tool_calls": [
                    {
                        "name": "edit",
                        "arguments": {"file_path": str(target), "old_string": "a + b", "new_string": "a - b"},
                    }
                ]
            },
            {"content": "edited"},
        ],
        permission=permission,
    )
    agent.run("edit it")
    assert b"a - b" in target.read_bytes()


# --------------------------------------------------------------------------- #
# session persistence
# --------------------------------------------------------------------------- #
def test_session_records_messages_and_tool_calls(harness):
    target = harness.project / "calc.py"
    agent = harness.build(
        [
            {"tool_calls": [{"name": "read", "arguments": {"file_path": str(target)}}]},
            {"content": "done"},
        ]
    )
    agent.run("read calc.py")
    harness.sessions.save(harness.session)

    restored = harness.sessions.require(harness.session.id)
    assert restored.message_count >= 3
    assert restored.tool_call_count == 1
    assert restored.tool_calls[0].name == "read"
    assert restored.provider == "scripted"
    assert restored.title != "New session"  # auto-titled from the task


def test_a_fork_can_be_resumed_and_continues_from_the_same_history(harness):
    target = harness.project / "calc.py"
    agent = harness.build(
        [
            {"tool_calls": [{"name": "read", "arguments": {"file_path": str(target)}}]},
            {"content": "first answer"},
        ]
    )
    agent.run("read calc.py")
    harness.sessions.save(harness.session)

    fork = harness.sessions.fork(harness.session.id)
    assert fork.message_count == harness.session.message_count

    # a brand new agent resumes the fork - it must see the earlier history
    resume_sink = CollectingSink()
    provider = ScriptedProvider(responses=[{"content": "second answer"}])
    resumed = CodingAgent(
        provider,
        harness.registry,
        permission=PermissionManager(mode=PermissionMode.AUTO),
        context=ContextManager(),
        session=fork,
        sessions=harness.sessions,
        sink=resume_sink,
        cwd=str(harness.project),
    )
    from minicode.context.manager import ContextManager as CM

    resumed.messages = CM().rebuild(fork.messages)
    result = resumed.run("now what?")
    assert result["submission"] == "second answer"
    # the earlier turn is still in the request the model saw
    assert any(m.get("role") == "tool" for request in provider.requests for m in request["messages"]), (
        "resumed agent lost the previous tool result"
    )


# --------------------------------------------------------------------------- #
# context management
# --------------------------------------------------------------------------- #
def test_context_compaction_keeps_the_agent_working(harness):
    """After compaction the agent must continue, not restart or crash."""
    from minicode.context.manager import ContextConfig

    context = ContextManager(ContextConfig(max_tokens=300, compact_threshold=0.3, prune=False, tail_turns=1))
    context.summarizer = lambda messages: "Earlier: read calc.py, nothing changed yet."
    agent = harness.build(
        [
            {"tool_calls": [{"name": "read", "arguments": {"file_path": str(harness.project / "calc.py")}}]},
            {"tool_calls": [{"name": "glob", "arguments": {"pattern": "**/*.py"}}]},
            {"content": "Finished after compaction."},
        ],
        context=context,
    )
    result = agent.run("do some work")
    assert result["exit_status"] == "Submitted"
    assert "compaction" in result["submission"] or result["submission"] == "Finished after compaction."
    assert agent.state.compactions >= 1
    assert harness.sink.compactions
    # the model must be told what happened before
    summary_message = [
        message
        for request in agent.provider.requests
        for message in request["messages"]
        if (message.get("extra") or {}).get("compacted")
    ]
    assert summary_message


def test_large_tool_output_is_truncated_before_reaching_the_model(harness):
    big = harness.project / "big.txt"
    big.write_bytes(b"x" * 200_000)
    agent = harness.build(
        [
            {"tool_calls": [{"name": "read", "arguments": {"file_path": str(big)}}]},
            {"content": "done"},
        ]
    )
    agent.run("read the big file")
    tool_message = [
        message
        for request in agent.provider.requests
        for message in request["messages"]
        if message.get("role") == "tool"
    ][0]
    # the raw 200k chars never reach the model
    assert len(tool_message["content"]) < 100_000


# --------------------------------------------------------------------------- #
# statistics / state
# --------------------------------------------------------------------------- #
def test_agent_state_is_reported(harness):
    agent = harness.build([{"content": "done"}])
    agent.run("hi")
    stats = agent.stats()
    assert stats["status"] == AgentStatus.FINISHED
    assert stats["steps"] >= 1
    assert stats["provider"] == "scripted"
    assert "compactions" in stats
    assert "tokens" in stats


def test_multi_turn_conversation_continues(harness):
    """The TUI calls run() repeatedly on the same agent - history must accumulate."""
    agent = harness.build([{"content": "first"}, {"content": "second"}])
    first = agent.run("one")
    second = agent.run("two")
    assert first["submission"] == "first"
    assert second["submission"] == "second"
    # the second request contains the first exchange
    assert len(agent.provider.requests[1]["messages"]) > len(agent.provider.requests[0]["messages"])
