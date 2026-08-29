"""A deterministic provider used by the test-suite.

This is a *model* stand-in, not an agent stand-in: everything else in the
integration tests (agent loop, tools, permissions, session, context) runs for
real. It exists so that the harness can be tested without spending money or
depending on network access. Production code paths never import it unless the
config explicitly selects ``type: scripted``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from minicode.providers.base import (
    AssistantMessage,
    Provider,
    StreamCallback,
    StreamEvent,
    ToolCall,
    Usage,
)

__all__ = ["ScriptedProvider"]

#: ``policy(messages, tools, call_index) -> response spec``
Policy = Callable[[list[dict[str, Any]], list[dict[str, Any]], int], Any]


def _normalize(spec: Any, call_index: int) -> AssistantMessage:
    if isinstance(spec, AssistantMessage):
        return spec
    if callable(spec):
        return _normalize(spec(call_index), call_index)
    if isinstance(spec, str):
        return AssistantMessage(content=spec, finish_reason="stop")
    if not isinstance(spec, Mapping):
        raise TypeError(f"Unsupported scripted response: {spec!r}")

    calls: list[ToolCall] = []
    for index, raw in enumerate(spec.get("tool_calls") or []):
        arguments = raw.get("arguments", {})
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        calls.append(
            ToolCall(
                id=raw.get("id", f"call_{call_index}_{index}"),
                name=raw["name"],
                **dict(zip(("arguments", "raw_arguments"), _split_args(arguments), strict=False)),
            )
        )
    return AssistantMessage(
        content=spec.get("content", ""),
        tool_calls=calls,
        finish_reason=spec.get("finish_reason", "tool_calls" if calls else "stop"),
        usage=Usage(
            input_tokens=int(spec.get("input_tokens", 10)),
            output_tokens=int(spec.get("output_tokens", 10)),
        ),
    )


def _split_args(raw: str) -> tuple[dict[str, Any], str]:
    try:
        parsed = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}, raw
    return (parsed if isinstance(parsed, dict) else {"value": parsed}), raw


class ScriptedProvider(Provider):
    """Replays scripted responses (or delegates to a policy function)."""

    kind = "scripted"

    def __init__(
        self,
        *,
        responses: Sequence[Any] | None = None,
        policy: Policy | None = None,
        model: str = "scripted-model",
        chunk_size: int = 24,
        **kwargs: Any,
    ):
        super().__init__(model=model, **kwargs)
        self.responses: list[Any] = list(responses or [])
        self.policy = policy
        self.chunk_size = max(1, chunk_size)
        self.call_index = 0
        #: every request the agent made, for assertions
        self.requests: list[dict[str, Any]] = []

    def generate(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        *,
        stream: bool = False,
        on_event: StreamCallback | None = None,
        max_tokens: int | None = None,
    ) -> AssistantMessage:
        history = [dict(message) for message in messages]
        self.requests.append({"messages": history, "tools": list(tools or []), "stream": stream})
        index = self.call_index
        self.call_index += 1

        if self.policy is not None:
            spec = self.policy(history, list(tools or []), index)
        elif self.responses:
            spec = self.responses[min(index, len(self.responses) - 1)]
        else:
            spec = {"content": "I have nothing to do."}

        result = _normalize(spec, index)
        if on_event is not None:
            for start in range(0, len(result.content), self.chunk_size):
                on_event(StreamEvent(type="text_delta", text=result.content[start : start + self.chunk_size]))
            for call in result.tool_calls:
                on_event(StreamEvent(type="tool_call_start", tool_call=call))
                on_event(StreamEvent(type="tool_call_end", tool_call=call))
            on_event(StreamEvent(type="usage", usage=result.usage))
        return result
