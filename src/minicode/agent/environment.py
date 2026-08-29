"""A mini-swe-agent ``Environment`` whose "commands" are minicode tool calls.

mini-swe-agent's agents call ``env.execute(action)`` where ``action`` is a dict.
Its own environments interpret ``action["command"]`` as a shell command. Here we
keep the *protocol* and reuse mini's result contract
(``{"output", "returncode", "exception_info", "extra"}``) but interpret the
action as ``{"tool": name, "args": {...}}`` and dispatch through the registry.

That single substitution is what turns mini's bash-only loop into a real
multi-tool coding agent without touching mini's control flow.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

from minicode.tools.base import ToolContext, ToolResult

__all__ = ["ToolEnvironment"]


from dataclasses import dataclass


@dataclass
class EnvConfig:
    cwd: str = ""
    timeout: int = 60


class ToolEnvironment:
    """Implements mini-swe-agent's ``Environment`` protocol on top of the registry."""

    def __init__(
        self,
        registry: Any,
        *,
        cwd: str = "",
        permission: Any = None,
        context: Any = None,
        session_id: str = "",
    ):
        self.registry = registry
        self.cwd = cwd
        self.permission = permission
        self.context = context
        self.session_id = session_id
        self.config = EnvConfig(cwd=cwd)

    # ------------------------------------------------------------------ #
    # mini-swe-agent Environment protocol
    # ------------------------------------------------------------------ #
    def execute(self, action: Mapping[str, Any], cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        """Execute one tool call and return a mini-swe-agent style result dict."""
        name = str(action.get("tool", ""))
        args = action.get("args") or {}
        started = time.time()

        ctx = ToolContext(
            cwd=cwd or self.cwd or ".",
            session_id=self.session_id,
            truncate=(self.context.truncate_tool_output if self.context else None),
            extra={"timeout": timeout} if timeout else {},
        )

        result: ToolResult = self.registry.execute(
            name, args, ctx, permission=self.permission, session_id=self.session_id
        )
        duration_ms = int((time.time() - started) * 1000)

        return {
            "output": result.render(),
            "returncode": 0 if result.ok else 1,
            "exception_info": "" if result.ok else (result.error.message if result.error else ""),
            "extra": {
                "result": result,
                "tool": name,
                "args": args,
                "tool_call_id": action.get("id", ""),
                "duration_ms": duration_ms,
                "metadata": result.metadata,
                "truncated": result.truncated,
                "error": result.error.to_dict() if result.error else None,
            },
        }

    def get_template_vars(self, **kwargs: Any) -> dict[str, Any]:
        return {"cwd": self.cwd, "tools": self.registry.names(), **kwargs}

    def serialize(self) -> dict[str, Any]:
        return {
            "info": {
                "config": {
                    "environment": {"cwd": self.cwd, "tools": self.registry.names()},
                    "environment_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                }
            }
        }

    # ------------------------------------------------------------------ #
    # convenience
    # ------------------------------------------------------------------ #
    @staticmethod
    def action_from_tool_call(tool_call: Any) -> dict[str, Any]:
        """Normalized :class:`ToolCall` -> environment action dict."""
        return {"tool": tool_call.name, "args": tool_call.arguments, "id": tool_call.id}

    @staticmethod
    def fingerprint(name: str, args: Mapping[str, Any]) -> str:
        """Stable hash of a tool call, used for doom-loop detection."""
        try:
            canonical = json.dumps({"tool": name, "args": args}, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):  # pragma: no cover
            canonical = f"{name}:{args!r}"
        return canonical
