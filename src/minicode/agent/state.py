"""Agent state bookkeeping (status, counters, last error)."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["AgentState", "AgentStatus"]


class AgentStatus:
    IDLE = "idle"
    RUNNING = "running"
    FINISHED = "finished"
    INTERRUPTED = "interrupted"
    ERROR = "error"


@dataclass
class AgentState:
    status: str = AgentStatus.IDLE
    step: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    compactions: int = 0
