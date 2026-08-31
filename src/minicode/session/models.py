"""Session data model.

A session is *pure data*: it stores provider/model as plain strings, never as
live objects. That is what makes it possible to persist, fork and resume a
session without dragging the agent or a provider along.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

SESSION_SCHEMA_VERSION = 1

__all__ = ["Session", "ToolCallRecord", "new_session_id", "session_summary"]


def new_session_id() -> str:
    return "ses_" + secrets.token_hex(6)


@dataclass
class ToolCallRecord:
    """One executed tool call, kept for ``/session`` and debugging."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    error_code: str = ""
    duration_ms: int = 0
    output_chars: int = 0
    truncated: bool = False
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
            "ok": self.ok,
            "error_code": self.error_code,
            "duration_ms": self.duration_ms,
            "output_chars": self.output_chars,
            "truncated": self.truncated,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ToolCallRecord:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Session:
    id: str = field(default_factory=new_session_id)
    title: str = "New session"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    #: id of the session this one was forked from
    parent_id: str | None = None
    #: number of messages inherited from the parent at fork time
    fork_point: int | None = None
    provider: str = ""
    model: str = ""
    cwd: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SESSION_SCHEMA_VERSION

    # -- derived --------------------------------------------------------- #
    @property
    def message_count(self) -> int:
        return len(self.messages)

    @property
    def tool_call_count(self) -> int:
        return len(self.tool_calls)

    @property
    def last_user_message(self) -> str:
        for message in reversed(self.messages):
            if message.get("role") == "user":
                content = message.get("content")
                return content if isinstance(content, str) else str(content or "")
        return ""

    @property
    def model_id(self) -> str:
        return f"{self.provider}/{self.model}" if self.provider else self.model

    # -- (de)serialization ------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "schema_version": self.schema_version,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "parent_id": self.parent_id,
            "fork_point": self.fork_point,
            "provider": self.provider,
            "model": self.model,
            "cwd": self.cwd,
            "messages": self.messages,
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Session:
        session = cls(
            id=str(data.get("id") or new_session_id()),
            title=str(data.get("title") or "New session"),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            parent_id=data.get("parent_id"),
            fork_point=data.get("fork_point"),
            provider=str(data.get("provider") or ""),
            model=str(data.get("model") or ""),
            cwd=str(data.get("cwd") or ""),
            messages=list(data.get("messages") or []),
            metadata=dict(data.get("metadata") or {}),
            schema_version=int(data.get("schema_version") or SESSION_SCHEMA_VERSION),
        )
        session.tool_calls = [ToolCallRecord.from_dict(item) for item in (data.get("tool_calls") or [])]
        return session


def session_summary(session: Session) -> dict[str, Any]:
    """Compact view used by ``/sessions``."""
    return {
        "id": session.id,
        "title": session.title,
        "model": session.model_id,
        "cwd": session.cwd,
        "messages": session.message_count,
        "tool_calls": session.tool_call_count,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "parent_id": session.parent_id,
    }
