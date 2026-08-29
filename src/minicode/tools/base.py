"""Unified tool interface + result protocol.

Every tool in minicode has the same shape:

* a JSON-Schema description of its arguments (fed straight to the model)
* a ``permission`` key used by the permission system
* an :meth:`Tool.execute` that **returns** a :class:`ToolResult` instead of
  raising, so that failures become structured observations the agent can
  recover from (this is what makes "tool error -> agent retries" work).

Tools never import anything from the UI or the session layer.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "ToolContext",
    "ToolError",
    "ToolResult",
    "Tool",
    "validate_args",
    "SchemaError",
]


class SchemaError(ValueError):
    """Raised when tool arguments do not match the tool's JSON schema."""


@dataclass
class ToolContext:
    """Everything a tool is allowed to see about its environment."""

    cwd: str
    session_id: str = ""
    #: callable(text, *, tool) -> str ; applies output truncation (context module)
    truncate: Any = None
    #: extra, tool-specific settings (e.g. bash timeout)
    extra: dict[str, Any] = field(default_factory=dict)

    def resolve(self, path: str) -> str:
        """Resolve a user/LLM supplied path against the working directory."""
        from pathlib import Path

        p = Path(path).expanduser()
        if not p.is_absolute():
            p = Path(self.cwd) / p
        return str(p)


@dataclass
class ToolError:
    """Structured, machine-readable tool failure."""

    code: str
    message: str
    hint: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        parts = [f"<error code={self.code!r}>"]
        parts.append(self.message)
        if self.details:
            parts.append("details: " + json.dumps(self.details, ensure_ascii=False, default=str))
        parts.append("</error>")
        if self.hint:
            parts.append(f"hint: {self.hint}")
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "hint": self.hint, "details": self.details}


@dataclass
class ToolResult:
    """The single return type of every tool."""

    title: str
    output: str
    metadata: dict[str, Any] = field(default_factory=dict)
    error: ToolError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def truncated(self) -> bool:
        return bool(self.metadata.get("truncated"))

    def render(self) -> str:
        """Text handed back to the model as the tool observation."""
        if self.error is not None:
            return self.error.render()
        return self.output

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "output": self.output,
            "metadata": self.metadata,
            "error": self.error.to_dict() if self.error else None,
        }


@runtime_checkable
class Tool(Protocol):
    """Protocol implemented by every tool (also usable as an ABC base)."""

    name: str
    description: str
    parameters: dict[str, Any]
    permission: str

    def execute(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult: ...

    def patterns(self, args: Mapping[str, Any]) -> list[str]: ...


class BaseTool(ABC):
    """Convenience base class providing schema validation + helpers."""

    name: str = ""
    description: str = ""
    permission: str = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.name and not cls.__doc__:
            cls.__doc__ = cls.description

    # -- schema ---------------------------------------------------------- #
    def schema(self) -> dict[str, Any]:
        """The OpenAI/Anthropic tool definition for this tool."""
        return {
            "name": self.name,
            "description": self.description or (self.__doc__ or "").strip(),
            "parameters": self.parameters,
        }

    def validate(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Validate arguments against :attr:`parameters`, raising :class:`SchemaError`."""
        return validate_args(dict(args or {}), self.parameters, tool_name=self.name)

    # -- hooks ----------------------------------------------------------- #
    def patterns(self, args: Mapping[str, Any]) -> list[str]:
        """Values that permission patterns are matched against.

        Defaults to the string form of every argument value; tools override
        this to expose the semantically meaningful target (a path, a command).
        """
        out: list[str] = []
        for value in (args or {}).values():
            if isinstance(value, str) and value:
                out.append(value)
        return out or ["*"]

    # -- execution -------------------------------------------------------- #
    @abstractmethod
    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult: ...

    def execute(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        """Validate -> run -> never raise (errors become :class:`ToolResult`)."""
        try:
            validated = self.validate(args)
        except SchemaError as exc:
            return ToolResult(
                title=self.name,
                output="",
                error=ToolError(
                    code="invalid_arguments",
                    message=str(exc),
                    hint=f"Check the arguments of the {self.name} tool and try again.",
                ),
            )
        try:
            result = self.run(validated, ctx)
        except PermissionError as exc:  # pragma: no cover - defensive
            return ToolResult(title=self.name, output="", error=ToolError(code="permission_denied", message=str(exc)))
        except Exception as exc:  # noqa: BLE001 - tools must never blow up the loop
            return ToolResult(
                title=self.name,
                output="",
                error=ToolError(
                    code=type(exc).__name__,
                    message=str(exc) or repr(exc),
                    hint="The tool call failed. Fix the arguments or use another tool.",
                ),
            )
        return result


# --------------------------------------------------------------------------- #
# minimal JSON-schema validation (avoids pulling in `jsonschema`)
# --------------------------------------------------------------------------- #

_TYPE_CHECKS: dict[str, Any] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate_args(args: Mapping[str, Any], schema: Mapping[str, Any], *, tool_name: str = "") -> dict[str, Any]:
    """Validate ``args`` against a (restricted) JSON schema.

    Supports ``type``, ``properties``, ``required``, ``enum`` and ``additionalProperties: false``.
    Unknown keys are passed through so that models adding extra keys still work.
    """
    if not isinstance(args, dict):
        raise SchemaError(f"{tool_name}: arguments must be an object, got {type(args).__name__}")

    prefix = f"{tool_name}: " if tool_name else ""
    properties: Mapping[str, Any] = schema.get("properties", {}) or {}
    required: Sequence[str] = schema.get("required", []) or []

    for key in required:
        if key not in args or args[key] is None:
            raise SchemaError(f"{prefix}missing required argument {key!r}")

    for key, value in args.items():
        spec = properties.get(key)
        if spec is None:
            continue
        expected = spec.get("type")
        if expected and expected in _TYPE_CHECKS and value is not None:
            if isinstance(expected, list):
                ok = any(_TYPE_CHECKS[t](value) for t in expected if t in _TYPE_CHECKS)
            else:
                ok = _TYPE_CHECKS[expected](value)
            if not ok:
                # be forgiving: coerce numbers/ints when the model stringifies them
                coerced = _coerce(value, expected)
                if coerced is None:
                    raise SchemaError(f"{prefix}argument {key!r} must be of type {expected}, got {type(value).__name__}")
                args[key] = coerced  # type: ignore[index]
                value = coerced
        enum = spec.get("enum")
        if enum and value not in enum:
            raise SchemaError(f"{prefix}argument {key!r} must be one of {enum}, got {value!r}")

    return dict(args)


def _coerce(value: Any, expected: str) -> Any:
    try:
        if expected in {"integer", "number"} and isinstance(value, str):
            number = int(value) if expected == "integer" else float(value)
            return number
    except ValueError:
        return None
    return None
