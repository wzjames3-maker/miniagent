"""OpenAI-compatible provider (OpenAI, DeepSeek, Qwen, vLLM, OpenRouter, Ollama, ...).

Implements both blocking and streaming completions with real server-sent-event
tool-call accumulation, and maps SDK errors onto the provider-neutral error
types so the agent can react (e.g. compact the context on overflow).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from minicode.context.tokens import content_to_text
from minicode.providers.base import (
    AssistantMessage,
    AuthenticationError,
    ContextLengthError,
    Provider,
    ProviderAPIError,
    RateLimitError,
    StreamCallback,
    StreamEvent,
    ToolCall,
    Usage,
    tool_schema_to_openai,
)

__all__ = ["OpenAICompatProvider"]

logger = logging.getLogger("minicode.providers.openai_compat")

_CONTEXT_HINTS = (
    "context length",
    "context_length",
    "maximum context",
    "max context",
    "too many tokens",
    "token limit",
    "context window",
    "prompt is too long",
    "string above maximum",
)


def _extract_reasoning(obj: Any) -> str:
    """Pull ``reasoning_content`` off an SDK object.

    Reasoning fields are not in the OpenAI SDK's typed schema, so they land in
    pydantic's ``model_extra``. Servers disagree on the name, hence the aliases.
    """
    if obj is None:
        return ""
    for attr in ("reasoning_content", "reasoning", "thinking"):
        value = getattr(obj, attr, None)
        if isinstance(value, str) and value:
            return value
    extra = getattr(obj, "model_extra", None)
    if isinstance(extra, dict):
        for key in ("reasoning_content", "reasoning", "thinking"):
            value = extra.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


class OpenAICompatProvider(Provider):
    kind = "openai_compat"
    default_api_key_env = "OPENAI_API_KEY"

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        from openai import OpenAI

        #: ``include_usage`` only affects streaming; some servers reject it.
        self.include_usage: bool = bool(self.options.pop("include_usage", True))
        self._last_request_at: float = 0.0
        self.client = OpenAI(
            api_key=self.api_key or "EMPTY",
            base_url=self.base_url or None,
            timeout=self.timeout,
            max_retries=0,  # we do our own retrying (needed for compaction-aware handling)
        )

    # ------------------------------------------------------------------ #
    # message conversion
    # ------------------------------------------------------------------ #
    def _convert_messages(self, messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            content = content_to_text(message.get("content"))
            extra = message.get("extra") or {}

            if role == "system":
                converted.append({"role": "system", "content": content})
            elif role == "user":
                converted.append({"role": "user", "content": content})
            elif role == "assistant":
                entry: dict[str, Any] = {"role": "assistant", "content": content or ""}
                # deepseek-v4-flash thinking mode: when previous assistant had reasoning_content
                # and next turn is tool_calls, reasoning_content must be echoed back or API 400001
                reasoning = extra.get("reasoning") or extra.get("reasoning_content") or ""
                if reasoning and isinstance(reasoning, str):
                    entry["reasoning_content"] = reasoning
                calls = extra.get("tool_calls") or []
                if calls:
                    entry["tool_calls"] = [
                        {
                            "id": str(call.get("id", "")),
                            "type": "function",
                            "function": {
                                "name": str(call.get("name", "")),
                                "arguments": call.get("raw_arguments")
                                or json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                            },
                        }
                        for call in calls
                        if isinstance(call, Mapping)
                    ]
                converted.append(entry)
            elif role == "tool":
                converted.append(
                    {"role": "tool", "tool_call_id": str(message.get("tool_call_id", "")), "content": content}
                )
            elif role == "exit":  # mini-swe-agent internal marker
                continue
            else:  # pragma: no cover - defensive
                converted.append({"role": "user", "content": content})
        return converted

    def _payload(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None,
        *,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._convert_messages(messages),
            "max_tokens": max_tokens or self.max_tokens,
        }
        if tools:
            payload["tools"] = [tool_schema_to_openai(tool) for tool in tools]
            payload["tool_choice"] = "auto"
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.top_p is not None:
            payload["top_p"] = self.top_p
        payload.update(self.extra_body)
        return payload

    # ------------------------------------------------------------------ #
    # request
    # ------------------------------------------------------------------ #
    def generate(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        *,
        stream: bool = False,
        on_event: StreamCallback | None = None,
        max_tokens: int | None = None,
    ) -> AssistantMessage:
        payload = self._payload(messages, tools, max_tokens=max_tokens)
        if stream:
            payload["stream"] = True
            if self.include_usage:
                payload["stream_options"] = {"include_usage": True}
            return self._with_retries(lambda: self._run_stream(payload, on_event))
        return self._with_retries(lambda: self._run_blocking(payload, on_event))

    def _run_blocking(self, payload: dict[str, Any], on_event: StreamCallback | None) -> AssistantMessage:
        try:
            response = self._create(payload)
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc

        choice = response.choices[0]
        message = choice.message
        content = content_to_text(message.content)
        reasoning = _extract_reasoning(message)
        tool_calls = []
        for call in message.tool_calls or []:
            arguments, raw = self._parse_tool_arguments(call.function.arguments or "")
            tool_calls.append(ToolCall(id=call.id, name=call.function.name, arguments=arguments, raw_arguments=raw))
        usage = self._usage(getattr(response, "usage", None))
        result = AssistantMessage(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=getattr(choice, "finish_reason", "") or "",
            reasoning=reasoning,
            raw=response.model_dump() if hasattr(response, "model_dump") else {"raw": str(response)},
        )
        if on_event is not None:
            if reasoning:
                on_event(StreamEvent(type="reasoning_delta", text=reasoning))
            if content:
                on_event(StreamEvent(type="text_delta", text=content))
            for call in tool_calls:
                on_event(StreamEvent(type="tool_call_end", tool_call=call))
            on_event(StreamEvent(type="usage", usage=usage))
        return result

    def _run_stream(self, payload: dict[str, Any], on_event: StreamCallback | None) -> AssistantMessage:
        collected: list[str] = []
        reasoning_parts: list[str] = []
        pending: dict[int, dict[str, Any]] = {}
        finish_reason = ""
        usage = Usage()
        raw_chunks: list[dict[str, Any]] = []

        try:
            stream = self._create(payload)
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc

        try:
            for chunk in stream:
                if hasattr(chunk, "model_dump"):
                    raw_chunks.append(chunk.model_dump())
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    usage = self._usage(chunk_usage)
                if not getattr(chunk, "choices", None):
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                thinking = _extract_reasoning(delta)
                if thinking:
                    reasoning_parts.append(thinking)
                    if on_event:
                        on_event(StreamEvent(type="reasoning_delta", text=thinking))
                text = getattr(delta, "content", None)
                if text:
                    collected.append(text)
                    if on_event:
                        on_event(StreamEvent(type="text_delta", text=text))
                for call_delta in getattr(delta, "tool_calls", None) or []:
                    index = int(getattr(call_delta, "index", 0) or 0)
                    entry = pending.setdefault(index, {"id": "", "name": "", "args": "", "started": False})
                    if getattr(call_delta, "id", None):
                        entry["id"] += call_delta.id
                    function = getattr(call_delta, "function", None)
                    if function is None:
                        continue
                    if getattr(function, "name", None):
                        entry["name"] += function.name
                        if not entry["started"] and on_event:
                            entry["started"] = True
                            on_event(
                                StreamEvent(
                                    type="tool_call_start", tool_call=ToolCall(id=entry["id"], name=entry["name"])
                                )
                            )
                    if getattr(function, "arguments", None):
                        entry["args"] += function.arguments
                        if on_event:
                            on_event(StreamEvent(type="tool_call_delta", text=function.arguments))
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc

        tool_calls: list[ToolCall] = []
        for index in sorted(pending):
            entry = pending[index]
            arguments, raw = self._parse_tool_arguments(entry["args"])
            call = ToolCall(
                id=entry["id"] or f"call_{index}",
                name=entry["name"],
                arguments=arguments,
                raw_arguments=raw,
            )
            tool_calls.append(call)
            if on_event:
                on_event(StreamEvent(type="tool_call_end", tool_call=call))

        if on_event:
            on_event(StreamEvent(type="usage", usage=usage))
        return AssistantMessage(
            content="".join(collected),
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish_reason,
            reasoning="".join(reasoning_parts),
            raw={"chunks": raw_chunks[-1:]} if raw_chunks else {},
        )

    # ------------------------------------------------------------------ #
    # transport hook (overridden by subclasses such as the litellm adapter)
    # ------------------------------------------------------------------ #
    def _create(self, payload: dict[str, Any]) -> Any:
        """Perform the actual request. Returns a response or an iterator of chunks."""
        return self.client.chat.completions.create(**payload)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _usage(usage: Any) -> Usage:
        """Map provider usage onto :class:`Usage`, incl. prompt-cache tokens.

        OpenAI reports cache reads inside ``prompt_tokens_details.cached_tokens``;
        DeepSeek reports ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``
        at the top level of ``usage``. Both are accepted. Cache *writes* are only
        reported by Anthropic (``cache_creation_input_tokens``), so they stay 0
        here - the automatic-cache providers never charge a separate write.
        """
        if usage is None:
            return Usage()
        details = getattr(usage, "prompt_tokens_details", None) or {}
        cached = (
            getattr(details, "cached_tokens", None) if not isinstance(details, dict) else details.get("cached_tokens")
        )
        if cached is None:
            cached = OpenAICompatProvider._field(usage, "prompt_cache_hit_tokens")
        return Usage(
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            cache_read_tokens=int(cached or 0),
        )

    @staticmethod
    def _field(obj: Any, name: str) -> Any:
        """Read a usage field from an SDK object or a plain dict."""
        if isinstance(obj, Mapping):
            return obj.get(name)
        return getattr(obj, name, None)

    def _map_error(self, exc: Exception) -> Exception:
        import openai

        message = str(exc)
        lowered = message.lower()
        if any(hint in lowered for hint in _CONTEXT_HINTS):
            return ContextLengthError(message)
        if isinstance(exc, openai.AuthenticationError):
            return AuthenticationError(f"{message} (provider={self.name}, base_url={self.base_url})")
        if isinstance(exc, openai.RateLimitError):
            return RateLimitError(message)
        if isinstance(exc, openai.APIStatusError):
            status = getattr(exc, "status_code", 0)
            if status in (401, 403):
                return AuthenticationError(f"{message} (provider={self.name})")
            if status == 429:
                return RateLimitError(message)
            if status >= 500:
                return ProviderAPIError(message)
            return ProviderAPIError(message)
        if isinstance(exc, openai.APIConnectionError):
            return ProviderAPIError(message)
        return ProviderAPIError(message)
