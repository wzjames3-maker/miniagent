"""Token estimation without any extra dependency.

A real tokenizer (tiktoken) would be more precise but adds a heavy dependency
and does not know about Anthropic/OSS models anyway. The heuristic below is
deliberately *slightly pessimistic*: over-estimating makes compaction kick in a
bit early, which is far safer than blowing past the context window.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["estimate_tokens", "estimate_messages_tokens", "content_to_text"]

#: CJK codepoints plus full-width punctuation: roughly 1 token per character.
_CJK = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef\u3000-\u303f]")

#: ASCII-ish text: ~3.6 characters per token for prose/code.
_CHARS_PER_TOKEN = 3.6
_CJK_PER_TOKEN = 1.3

#: Fixed overhead per message / per tool call (role, ids, separators).
_MESSAGE_OVERHEAD = 4
_TOOL_CALL_OVERHEAD = 12


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    rest = len(text) - cjk
    return int(cjk / _CJK_PER_TOKEN + rest / _CHARS_PER_TOKEN) + 1


def content_to_text(content: Any) -> str:
    """Flatten an OpenAI-style ``content`` field (str or list of parts) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif item.get("type") in {"input_text", "output_text"}:
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(str(item.get("content", "")))
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    return str(content)


def estimate_messages_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    """Estimate the size of a mini-swe-agent style message list."""
    total = 0
    for message in messages:
        total += _MESSAGE_OVERHEAD
        total += estimate_tokens(content_to_text(message.get("content")))
        extra = message.get("extra") or {}
        for call in extra.get("tool_calls") or []:
            total += _TOOL_CALL_OVERHEAD
            if isinstance(call, Mapping):
                total += estimate_tokens(str(call.get("name", "")))
                arguments = call.get("arguments")
                total += estimate_tokens(arguments if isinstance(arguments, str) else str(arguments or ""))
    return total
