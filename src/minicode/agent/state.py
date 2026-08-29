"""Agent state bookkeeping (status, counters, last error)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

__all__ = ["AgentState", "AgentStatus"]


class AgentStatus:
    IDLE = "idle"
    RUNNING = "running"
    WAITING_PERMISSION = "waiting_permission"
    FINISHED = "finished"
    INTERRUPTED = "interrupted"
    ERROR = "error"

    ALL = (IDLE, RUNNING, WAITING_PERMISSION, FINISHED, INTERRUPTED, ERROR)


@dataclass
class AgentState:
    status: str = AgentStatus.IDLE
    step: int = 0
    cost: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    compactions: int = 0
    pruned_outputs: int = 0
    last_error: str = ""
    last_tool: str = ""

    def record_usage(self, usage: Any) -> None:
        if usage is None:
            return
        self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def reset_counters(self) -> None:
        self.step = 0
        self.tool_calls = 0
        self.tool_errors = 0
        self.last_error = ""
        self.last_tool = ""
