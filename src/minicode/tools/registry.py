"""The tool registry: discovery, schema export and execution.

Tools register themselves here; the agent, the model adapters and the TUI all
talk to the registry and never to a concrete tool class. New tools are added by
defining a :class:`~minicode.tools.base.Tool` and calling :meth:`register` (or
by pointing ``tools.extra_modules`` at a module exposing ``register_tools``).
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from minicode.permission.manager import PermissionManager
from minicode.permission.policy import PermissionDenied, PermissionRejected
from minicode.tools.base import Tool, ToolContext, ToolError, ToolResult

__all__ = ["ToolRegistry", "build_default_registry"]


class ToolRegistry:
    """Holds every tool the agent may call."""

    def __init__(self, tools: Iterable[Tool] | None = None):
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    # ------------------------------------------------------------------ #
    # registration / discovery
    # ------------------------------------------------------------------ #
    def register(self, tool: Tool, *, replace: bool = False) -> None:
        if not getattr(tool, "name", ""):
            raise ValueError(f"Tool must define a name: {tool!r}")
        if tool.name in self._tools and not replace:
            raise ValueError(f"Tool {tool.name!r} is already registered (use replace=True to override)")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(
                f"Unknown tool {name!r}. Available tools: {', '.join(sorted(self._tools)) or '(none)'}"
            ) from None

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> list[str]:
        return list(self._tools)

    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self, only: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """Tool definitions in the OpenAI/Anthropic format, ready to send to a model."""
        if only is None:
            return [tool.schema() for tool in self._tools.values()]
        return [self.get(name).schema() for name in only]

    def describe(self) -> str:
        """Human readable listing used by ``/tools`` and the system prompt."""
        return "\n".join(f"- {tool.name}: {(tool.description or '').strip().splitlines()[0]}" for tool in self._tools.values())

    def subset(self, names: Sequence[str]) -> ToolRegistry:
        return ToolRegistry([self.get(name) for name in names])

    def load_module(self, dotted_path: str) -> None:
        """Extension point: import ``dotted_path`` and call its ``register_tools(registry)``."""
        module = importlib.import_module(dotted_path)
        register_fn = getattr(module, "register_tools", None)
        if register_fn is None:
            raise AttributeError(f"Module {dotted_path!r} must expose a register_tools(registry) function")
        register_fn(self)

    # ------------------------------------------------------------------ #
    # execution
    # ------------------------------------------------------------------ #
    def check_permissions(
        self,
        name: str,
        args: Mapping[str, Any],
        permission: PermissionManager | None,
        **kwargs: Any,
    ) -> None:
        """Raise :class:`PermissionDenied` / :class:`PermissionRejected` if not allowed."""
        if permission is None:
            return
        tool = self.get(name)
        checker = getattr(tool, "required_permissions", None)
        checks = checker(args) if checker else [(tool.permission, tool.patterns(args))]
        for perm, patterns in checks:
            permission.check(perm, patterns, tool=name, session_id=kwargs.get("session_id", ""))

    def execute(
        self,
        name: str,
        args: Mapping[str, Any],
        ctx: ToolContext,
        *,
        permission: PermissionManager | None = None,
        session_id: str = "",
    ) -> ToolResult:
        """Run a tool. Never raises: permission problems become structured errors."""
        if name not in self._tools:
            return ToolResult(
                title=name,
                output="",
                error=ToolError(
                    code="unknown_tool",
                    message=f"Unknown tool {name!r}.",
                    hint=f"Available tools: {', '.join(sorted(self._tools))}",
                ),
            )
        try:
            self.check_permissions(name, args, permission, session_id=session_id)
        except PermissionRejected as exc:
            return ToolResult(
                title=name,
                output="",
                error=ToolError(code="permission_rejected", message=str(exc), details={"permission": exc.permission}),
            )
        except PermissionDenied as exc:
            return ToolResult(
                title=name,
                output="",
                error=ToolError(
                    code="permission_denied",
                    message=str(exc),
                    hint="This operation is blocked by the permission configuration. Ask the user or use another approach.",
                    details={"permission": exc.permission, "patterns": exc.patterns},
                ),
            )
        return self._tools[name].execute(args, ctx)


def build_default_registry(
    *,
    enabled: Sequence[str] | None = None,
    cwd: str = "",
    bash_timeout: int = 60,
    env: Mapping[str, str] | None = None,
) -> ToolRegistry:
    """Create the registry with all builtin tools.

    Import order matters not at all - tools are independent and only need a
    :class:`ToolContext` at call time.
    """
    from minicode.tools.bash_tool import BashTool
    from minicode.tools.file_tools import EditTool, ReadTool, WriteTool
    from minicode.tools.patch_tool import ApplyPatchTool
    from minicode.tools.search_tools import GlobTool, GrepTool

    registry = ToolRegistry()
    all_tools: list[Tool] = [
        ReadTool(),
        WriteTool(),
        EditTool(),
        ApplyPatchTool(),
        GlobTool(),
        GrepTool(),
        BashTool(default_timeout=bash_timeout, cwd=cwd, env=dict(env or {})),
    ]
    wanted = set(enabled) if enabled else None
    for tool in all_tools:
        if wanted is None or tool.name in wanted:
            registry.register(tool)
    return registry
