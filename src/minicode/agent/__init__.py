"""Agent layer: mini-swe-agent's loop, extended with tools/permissions/context."""

from minicode.agent.core import CodingAgent, CodingAgentConfig, MiniModelAdapter
from minicode.agent.environment import ToolEnvironment
from minicode.agent.prompts import COMPACTION_TEMPLATE, INSTANCE_TEMPLATE, SYSTEM_TEMPLATE
from minicode.agent.state import AgentState, AgentStatus

__all__ = [
    "AgentState",
    "AgentStatus",
    "COMPACTION_TEMPLATE",
    "CodingAgent",
    "CodingAgentConfig",
    "INSTANCE_TEMPLATE",
    "MiniModelAdapter",
    "SYSTEM_TEMPLATE",
    "ToolEnvironment",
]
