"""Provider-agnostic model abstraction.

Every provider speaks two things:

* a **wire format** (OpenAI chat-completions JSON vs. Anthropic messages JSON)
* a **transport** (blocking request vs. server-sent-event stream)

Both are normalised here into the provider-neutral shapes used by the rest of
minicode:

* :class:`ToolCall` - ``{id, name, arguments: dict}``
* :class:`AssistantMessage` - text + tool calls + usage
* tool results are plain ``{"role": "tool", "tool_call_id": ..., "content": str}``

The remaining ``format_*`` / ``get_template_vars`` / ``serialize`` methods are
the parts of mini-swe-agent's ``Model`` protocol that
:class:`~minicode.agent.core.MiniModelAdapter` delegates to; the adapter itself
implements the protocol entry point (``query``).
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = [
    "AssistantMessage",
    "StreamEvent",
    "ToolCall",
    "Usage",
    "Provider",
    "ProviderError",
    "AuthenticationError",
    "RateLimitError",
    "ContextLengthError",
    "ProviderAPIError",
    "format_tool_results",
    "tool_schema_to_openai",
    "tool_schema_to_anthropic",
]


# --------------------------------------------------------------------------- #
# errors — re-export central hierarchy for backwards compatibility
# --------------------------------------------------------------------------- #
from minicode.errors import (  # noqa: F401  re-export
    AuthenticationError,
    ContextLengthError,
    ProviderAPIError,
    ProviderError,
    RateLimitError,
)


# --------------------------------------------------------------------------- #
# normalized data
# --------------------------------------------------------------------------- #
@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ToolCall:
        raw = data.get("raw_arguments") or ""
        arguments = data.get("arguments")
        if not isinstance(arguments, dict):
            try:
                arguments = json.loads(raw or "{}")
            except (json.JSONDecodeError, TypeError):
                arguments = {}
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            arguments=arguments if isinstance(arguments, dict) else {},
            raw_arguments=raw,
        )


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AssistantMessage:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""

    #: Chain-of-thought emitted by reasoning models (DeepSeek-R1/V3/V4, o-series,
    #: QwQ, ...). Kept **separate** from ``content``: it must never be treated as
    #: the model's answer, but it is worth showing in the UI and persisting.
    reasoning: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def to_message_dict(self) -> dict[str, Any]:
        """Convert to the neutral message dict stored in the history."""
        extra: dict[str, Any] = {
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "usage": self.usage.to_dict(),
            "finish_reason": self.finish_reason,
            "timestamp": time.time(),
        }
        if self.reasoning:
            extra["reasoning"] = self.reasoning
        return {"role": "assistant", "content": self.content, "extra": extra}


@dataclass
class StreamEvent:
    """Incremental update emitted while streaming."""

    # text_delta | reasoning_delta | tool_call_start | tool_call_delta | tool_call_end | usage | error
    type: str
    text: str = ""
    tool_call: ToolCall | None = None
    usage: Usage | None = None
    data: dict[str, Any] = field(default_factory=dict)


StreamCallback = Callable[[StreamEvent], None]


# --------------------------------------------------------------------------- #
# tool schema conversion
# --------------------------------------------------------------------------- #
def tool_schema_to_openai(schema: Mapping[str, Any]) -> dict[str, Any]:
    """``{name, description, parameters}`` -> OpenAI chat-completions tool."""
    return {
        "type": "function",
        "function": {
            "name": schema["name"],
            "description": schema.get("description", ""),
            "parameters": schema.get("parameters", {"type": "object", "properties": {}}),
        },
    }


def tool_schema_to_anthropic(schema: Mapping[str, Any]) -> dict[str, Any]:
    """``{name, description, parameters}`` -> Anthropic tool."""
    return {
        "name": schema["name"],
        "description": schema.get("description", ""),
        "input_schema": schema.get("parameters", {"type": "object", "properties": {}}),
    }


def format_tool_results(
    tool_calls: Sequence[ToolCall],
    outputs: Sequence[Any],
) -> list[dict[str, Any]]:
    """Pair tool calls with their rendered results into history messages.

    ``outputs`` are :class:`~minicode.tools.base.ToolResult` objects (or anything
    with a ``render()`` method / plain strings). Missing results are padded with
    an explicit "not executed" observation so the history stays consistent.
    """
    messages: list[dict[str, Any]] = []
    for index, call in enumerate(tool_calls):
        if index < len(outputs):
            result = outputs[index]
            content = result.render() if hasattr(result, "render") else str(result)
            metadata = getattr(result, "metadata", {}) or {}
            error = getattr(result, "error", None)
            extra: dict[str, Any] = {
                "tool_name": call.name,
                "tool_arguments": call.arguments,
                "metadata": metadata,
                "timestamp": time.time(),
            }
            if error is not None:
                extra["error"] = error.to_dict()
        else:
            content = "The tool call was not executed."
            extra = {"tool_name": call.name, "skipped": True, "timestamp": time.time()}
        messages.append({"role": "tool", "tool_call_id": call.id, "content": content, "extra": extra})
    return messages


# --------------------------------------------------------------------------- #
# provider base
# --------------------------------------------------------------------------- #
class Provider(ABC):
    """Abstract provider. Subclasses only implement the wire-level calls."""

    #: provider type name used in the config file (``type: openai_compat``)
    kind: str = "base"
    #: env var consulted for the API key when none is configured
    default_api_key_env: str = ""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 8192,
        temperature: float | None = None,
        timeout: float = 120.0,
        top_p: float | None = None,
        extra_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        name: str = "",
        **kwargs: Any,
    ):
        self.name = name or self.kind
        self.model = model
        self.base_url = base_url
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = timeout
        self.extra_body = dict(extra_body or {})
        self.headers = dict(headers or {})
        self.api_key = api_key or self._api_key_from_env()
        self.options = dict(kwargs)
        self._last_request_at: float = 0.0

    # -- identity -------------------------------------------------------- #
    @property
    def model_id(self) -> str:
        return f"{self.name}/{self.model}" if self.name else self.model

    def _api_key_from_env(self) -> str:
        import os

        for var in (self.default_api_key_env, "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            if var and os.getenv(var):
                return os.environ[var]
        return ""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}(name={self.name!r}, model={self.model!r}, base_url={self.base_url!r})"

    # -- the only method a subclass must implement ----------------------- #
    @abstractmethod
    def generate(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        *,
        stream: bool = False,
        on_event: StreamCallback | None = None,
        max_tokens: int | None = None,
    ) -> AssistantMessage:
        """Call the model. Implemented per wire protocol."""

    # -- mini-swe-agent ``Model`` protocol (delegated to by MiniModelAdapter) -- #
    def format_message(self, **kwargs: Any) -> dict[str, Any]:
        role = kwargs.pop("role", "user")
        return {"role": role, **kwargs}

    def format_observation_messages(
        self,
        message: Mapping[str, Any],
        outputs: Sequence[Any],
        template_vars: dict | None = None,
    ) -> list[dict[str, Any]]:
        calls = [ToolCall.from_mapping(call) for call in (message.get("extra", {}) or {}).get("tool_calls", [])]
        return format_tool_results(calls, outputs)

    def get_template_vars(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.name,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            **kwargs,
        }

    def serialize(self) -> dict[str, Any]:
        return {
            "info": {
                "config": {
                    "model": {
                        "provider": self.name,
                        "provider_kind": self.kind,
                        "model_name": self.model,
                        "base_url": self.base_url,
                        "max_tokens": self.max_tokens,
                        "temperature": self.temperature,
                    },
                    "model_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                }
            }
        }

    # -- shared retry / throttle ---------------------------------------- #
    def _throttle(self) -> None:
        interval = float(self.options.get("min_request_interval", 0) or 0)
        if interval <= 0:
            return
        elapsed = time.monotonic() - getattr(self, "_last_request_at", 0.0)
        if elapsed < interval:
            time.sleep(interval - elapsed)
        self._last_request_at = time.monotonic()  # type: ignore[attr-defined]

    @staticmethod
    def _retry_after_seconds(exc: Exception) -> float:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if not headers:
            return 0.0
        for key in ("retry-after", "Retry-After", "x-ratelimit-reset-after", "x-ratelimit-reset"):
            raw = headers.get(key)
            if raw is None:
                continue
            try:
                return max(0.0, float(str(raw).strip()))
            except ValueError:
                continue
        return 0.0

    def _with_retries(self, fn: Callable[[], AssistantMessage]) -> AssistantMessage:  # type: ignore[no-redef]
        attempts = int(self.options.get("max_retries", 5))
        base_delay = float(self.options.get("retry_delay", 5.0))
        max_delay = float(self.options.get("retry_max_delay", 120.0))
        last_error: Exception | None = None
        from minicode.errors import AuthenticationError, ContextLengthError, ProviderAPIError, RateLimitError

        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                return fn()
            except ContextLengthError:
                raise
            except AuthenticationError:
                raise
            except (RateLimitError, ProviderAPIError) as exc:
                last_error = exc
            if attempt < attempts:
                backoff = min(base_delay * (2 ** (attempt - 1)), max_delay)
                delay = max(backoff, self._retry_after_seconds(last_error))  # type: ignore[arg-type]
                import logging

                logging.getLogger("minicode.providers.base").warning(
                    "provider %s returned %s; retrying in %.1fs (attempt %d/%d)",
                    self.name,
                    type(last_error).__name__,
                    delay,
                    attempt,
                    attempts,
                )
                time.sleep(delay)
        assert last_error is not None
        raise last_error

    # -- helpers ---------------------------------------------------------- #
    @staticmethod
    def _parse_tool_arguments(raw: str) -> tuple[dict[str, Any], str]:
        raw = raw or ""
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return {}, raw
        return (parsed if isinstance(parsed, dict) else {"value": parsed}), raw
