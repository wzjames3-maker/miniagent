"""The boundary between the agent and any user interface.

The agent only ever talks to an :class:`EventSink`. The TUI implements it; tests
use :class:`NullSink`; a headless run uses :class:`CollectingSink`. Tools, the
provider and the session know nothing about rendering.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from minicode.providers.base import AssistantMessage, StreamEvent, ToolCall

__all__ = ["EventSink", "NullSink", "CollectingSink", "TurnResult"]


@runtime_checkable
class EventSink(Protocol):
    """No-op default implementation - override what you need.

    A Protocol rather than a plain base class so a front-end can be recognised
    as one without inheriting from it. Subclasses still inherit the no-op
    bodies, so overriding only the events you care about keeps working.
    """

    # streaming / messages
    def on_stream_event(self, event: StreamEvent) -> None: ...
    def on_assistant_message(self, message: AssistantMessage) -> None: ...
    def on_user_message(self, content: str) -> None: ...

    # tools
    def on_tool_start(self, tool_call: ToolCall) -> None: ...
    def on_tool_result(self, tool_call: ToolCall, result: Any) -> None: ...

    # lifecycle
    def on_turn_start(self, task: str) -> None: ...
    def on_turn_end(self, result: TurnResult) -> None: ...
    def on_error(self, message: str, *, details: Mapping[str, Any] | None = None) -> None: ...
    def on_status(self, status: Mapping[str, Any]) -> None: ...
    def on_compaction(self, info: Mapping[str, Any]) -> None: ...
    def on_permission_denied(self, permission: str, patterns: Any) -> None: ...


class NullSink(EventSink):
    """Silently discards everything (headless / tests)."""


@dataclass
class CollectingSink(EventSink):
    """Records events so tests can assert on them."""

    stream_events: list[StreamEvent] = field(default_factory=list)
    assistant_messages: list[AssistantMessage] = field(default_factory=list)
    tool_starts: list[ToolCall] = field(default_factory=list)
    tool_results: list[tuple[ToolCall, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    compactions: list[Mapping[str, Any]] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(event.text for event in self.stream_events if event.type == "text_delta")

    @property
    def reasoning(self) -> str:
        return "".join(event.text for event in self.stream_events if event.type == "reasoning_delta")

    def on_stream_event(self, event: StreamEvent) -> None:
        self.stream_events.append(event)

    def on_assistant_message(self, message: AssistantMessage) -> None:
        self.assistant_messages.append(message)

    def on_tool_start(self, tool_call: ToolCall) -> None:
        self.tool_starts.append(tool_call)

    def on_tool_result(self, tool_call: ToolCall, result: Any) -> None:
        self.tool_results.append((tool_call, result))

    def on_error(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.errors.append(message)

    def on_compaction(self, info: Mapping[str, Any]) -> None:
        self.compactions.append(dict(info))


@dataclass
class TurnResult:
    """What a single user turn produced."""

    exit_status: str = ""
    submission: str = ""
    steps: int = 0
    cost: float = 0.0
    tool_calls: int = 0
    tool_errors: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_status in {"Submitted", "Finished", ""}
