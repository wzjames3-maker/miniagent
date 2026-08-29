"""Anthropic-compatible provider (Anthropic Messages API and proxies).

Anthropic's wire format differs structurally from OpenAI's: the system prompt is
a top-level field, tool calls are ``tool_use`` content blocks whose JSON input
arrives as *fragments*, and tool results are ``tool_result`` blocks inside a
``user`` message. All of that is translated here.
"""

from __future__ import annotations

import json
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
    tool_schema_to_anthropic,
)

__all__ = ["AnthropicCompatProvider"]


class AnthropicCompatProvider(Provider):
    kind = "anthropic_compat"
    default_api_key_env = "ANTHROPIC_API_KEY"

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        import anthropic

        self.max_tokens = self.max_tokens or 8192
        self.client = anthropic.Anthropic(
            api_key=self.api_key or "EMPTY",
            base_url=self.base_url or None,
            timeout=self.timeout,
            max_retries=0,
            default_headers=self.headers or None,
        )

    # ------------------------------------------------------------------ #
    # message conversion
    # ------------------------------------------------------------------ #
    def _split_system(self, messages: Sequence[Mapping[str, Any]]) -> tuple[str, list[Mapping[str, Any]]]:
        system_parts: list[str] = []
        rest: list[Mapping[str, Any]] = []
        for message in messages:
            if message.get("role") == "system":
                system_parts.append(content_to_text(message.get("content")))
            elif message.get("role") == "exit":
                continue
            else:
                rest.append(message)
        return "\n\n".join(p for p in system_parts if p), rest

    def _convert_messages(self, messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            extra = message.get("extra") or {}
            text = content_to_text(message.get("content"))

            if role == "user":
                converted.append({"role": "user", "content": text})
            elif role == "assistant":
                blocks: list[dict[str, Any]] = []
                if text:
                    blocks.append({"type": "text", "text": text})
                for call in extra.get("tool_calls") or []:
                    if not isinstance(call, Mapping):
                        continue
                    arguments = call.get("arguments")
                    if not isinstance(arguments, dict):
                        arguments = self._parse_tool_arguments(str(call.get("raw_arguments") or ""))[0]
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": str(call.get("id", "")),
                            "name": str(call.get("name", "")),
                            "input": arguments,
                        }
                    )
                if not blocks:
                    blocks.append({"type": "text", "text": ""})
                converted.append({"role": "assistant", "content": blocks})
            elif role == "tool":
                converted.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": str(message.get("tool_call_id", "")),
                                "content": text,
                            }
                        ],
                    }
                )
        return self._merge_adjacent_tool_results(converted)

    @staticmethod
    def _merge_adjacent_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Anthropic allows several tool_result blocks in one user message."""
        merged: list[dict[str, Any]] = []
        for message in messages:
            content = message.get("content")
            is_tool_result = (
                isinstance(content, list)
                and len(content) == 1
                and isinstance(content[0], dict)
                and content[0].get("type") == "tool_result"
            )
            if is_tool_result and merged and merged[-1]["role"] == "user":
                previous = merged[-1]["content"]
                if isinstance(previous, list) and all(
                    isinstance(block, dict) and block.get("type") == "tool_result" for block in previous
                ):
                    previous.append(content[0])
                    continue
            merged.append(message)
        return merged

    def _payload(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None,
        *,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        system, rest = self._split_system(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._convert_messages(rest),
            "max_tokens": max_tokens or self.max_tokens,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [tool_schema_to_anthropic(tool) for tool in tools]
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
            return self._with_retries(lambda: self._run_stream(payload, on_event))
        return self._with_retries(lambda: self._run_blocking(payload, on_event))

    def _run_blocking(self, payload: dict[str, Any], on_event: StreamCallback | None) -> AssistantMessage:
        try:
            response = self.client.messages.create(**payload)
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in response.content or []:
            block_type = getattr(block, "type", "")
            if block_type == "text":
                text_parts.append(getattr(block, "text", ""))
            elif block_type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=block.input if isinstance(block.input, dict) else {},
                        raw_arguments=json.dumps(block.input or {}, ensure_ascii=False),
                    )
                )
        content = "".join(text_parts)
        usage = self._usage(getattr(response, "usage", None))
        if on_event is not None:
            if content:
                on_event(StreamEvent(type="text_delta", text=content))
            for call in tool_calls:
                on_event(StreamEvent(type="tool_call_end", tool_call=call))
            on_event(StreamEvent(type="usage", usage=usage))
        return AssistantMessage(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=getattr(response, "stop_reason", "") or "",
            raw=response.model_dump() if hasattr(response, "model_dump") else {"raw": str(response)},
        )

    def _run_stream(self, payload: dict[str, Any], on_event: StreamCallback | None) -> AssistantMessage:
        collected: list[str] = []
        tool_calls: list[ToolCall] = []
        current: dict[str, Any] | None = None
        partial_json: list[str] = []
        finish_reason = ""
        usage = Usage()

        try:
            stream_ctx = self.client.messages.stream(**payload)
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc

        try:
            with stream_ctx as stream:
                for event in stream:
                    event_type = getattr(event, "type", "")
                    if event_type == "content_block_start":
                        block = getattr(event, "content_block", None)
                        if getattr(block, "type", "") == "tool_use":
                            current = {"id": block.id, "name": block.name}
                            partial_json = []
                            if on_event:
                                on_event(
                                    StreamEvent(
                                        type="tool_call_start", tool_call=ToolCall(id=block.id, name=block.name)
                                    )
                                )
                    elif event_type == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        delta_type = getattr(delta, "type", "")
                        if delta_type == "text_delta":
                            text = getattr(delta, "text", "")
                            collected.append(text)
                            if on_event:
                                on_event(StreamEvent(type="text_delta", text=text))
                        elif delta_type == "input_json_delta":
                            fragment = getattr(delta, "partial_json", "") or ""
                            partial_json.append(fragment)
                            if on_event:
                                on_event(StreamEvent(type="tool_call_delta", text=fragment))
                    elif event_type == "content_block_stop":
                        if current is not None:
                            raw = "".join(partial_json)
                            arguments, _ = self._parse_tool_arguments(raw)
                            call = ToolCall(
                                id=current["id"],
                                name=current["name"],
                                arguments=arguments,
                                raw_arguments=raw,
                            )
                            tool_calls.append(call)
                            if on_event:
                                on_event(StreamEvent(type="tool_call_end", tool_call=call))
                            current = None
                            partial_json = []
                    elif event_type == "message_delta":
                        delta = getattr(event, "delta", None)
                        if getattr(delta, "stop_reason", None):
                            finish_reason = delta.stop_reason
                        usage = self._usage(getattr(event, "usage", None)) or usage
                    elif event_type == "message_start":
                        usage = self._usage(getattr(getattr(event, "message", None), "usage", None)) or usage
                final = stream.get_final_message()
                usage = self._usage(getattr(final, "usage", None)) or usage
                finish_reason = finish_reason or getattr(final, "stop_reason", "") or ""
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from exc

        if on_event:
            on_event(StreamEvent(type="usage", usage=usage))
        return AssistantMessage(
            content="".join(collected),
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish_reason,
            raw={},
        )

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _usage(usage: Any) -> Usage | None:
        if usage is None:
            return None
        return Usage(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        )

    def _map_error(self, exc: Exception) -> Exception:
        import anthropic

        message = str(exc)
        if isinstance(exc, anthropic.RateLimitError):
            return RateLimitError(message)
        if isinstance(exc, anthropic.AuthenticationError):
            return AuthenticationError(f"{message} (provider={self.name}, base_url={self.base_url})")
        if isinstance(exc, anthropic.BadRequestError):
            lowered = message.lower()
            if "prompt is too long" in lowered or "context" in lowered and "too long" in lowered:
                return ContextLengthError(message)
            return ProviderAPIError(message)
        if isinstance(exc, anthropic.APIStatusError):
            status = getattr(exc, "status_code", 0)
            if status in (401, 403):
                return AuthenticationError(message)
            if status == 429:
                return RateLimitError(message)
            if status >= 500:
                return ProviderAPIError(message)
            return ProviderAPIError(message)
        if isinstance(exc, anthropic.APIConnectionError):
            return ProviderAPIError(message)
        return ProviderAPIError(message)
