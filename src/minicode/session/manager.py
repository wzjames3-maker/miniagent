"""Session persistence: create / resume / list / delete / fork."""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from minicode.errors import SessionNotFoundError
from minicode.session.models import Session, ToolCallRecord, new_session_id, session_summary
from minicode.storage.json_store import JsonDocumentStore
from minicode.storage.paths import sessions_dir

__all__ = ["SessionManager", "auto_title"]


def auto_title(text: str, *, max_length: int = 70) -> str:
    """Derive a short session title from the user's first message.

    User input is wrapped in a ``Task:`` / ``Instructions:`` template before it
    reaches the agent, so a raw title would read ``Task: read the file
    Instructions: - Treat the task above...``. Peel that template off and title
    from the actual request instead.
    """
    raw = (text or "").strip()
    # Extract the real task between the "Task:" header and the "Instructions:"
    # footer, if the message is one of our wrapped task prompts.
    match = re.search(r"^Task:\s*(.*?)(?:\n\s*\nInstructions:|\nInstructions:|\Z)", raw, re.DOTALL)
    if match:
        raw = match.group(1).strip()
    cleaned = re.sub(r"\s+", " ", raw)
    cleaned = cleaned.strip("`\"'")
    if not cleaned:
        return "New session"
    if len(cleaned) > max_length:
        cleaned = cleaned[: max_length - 3].rstrip() + "..."
    return cleaned


def _normalise_cwd(path: str | Path | None) -> str:
    """Normalise a stored/requested working directory for comparisons."""
    if not path:
        return ""
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        # A session may reference a deleted directory; fall back to a clean
        # absolute-ish form so filtering still works.
        return str(Path(path).expanduser().absolute())


class SessionManager:
    """Owns every operation on persisted sessions."""

    def __init__(self, directory: Path | str | None = None):
        self.store = JsonDocumentStore(directory or sessions_dir())

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #
    def create(
        self,
        *,
        provider: str = "",
        model: str = "",
        cwd: str = "",
        title: str = "New session",
        metadata: dict[str, Any] | None = None,
        persist: bool = True,
    ) -> Session:
        """Create a session, optionally without writing it to disk yet.

        ``persist=False`` is what the interactive app uses for the session it
        creates at start-up. Saving that one immediately is what used to fill
        the session rail with rows called "New session" -- the user had not
        said anything yet, so there was nothing to resume. The session is saved
        as soon as it earns it (first message, rename, model switch).
        """
        session = Session(
            provider=provider,
            model=model,
            cwd=cwd,
            title=title,
            metadata=dict(metadata or {}),
        )
        if persist:
            self.save(session)
        return session

    def save(self, session: Session) -> Session:
        session.updated_at = time.time()
        self.store.save(session.id, session.to_dict())
        return session

    def get(self, session_id: str) -> Session | None:
        data = self.store.load(session_id)
        if data is None:
            return None
        return Session.from_dict(data)

    def require(self, session_id: str) -> Session:
        session = self.get(session_id)
        if session is None:
            raise SessionNotFoundError(f"Session not found: {session_id}")
        return session

    def exists(self, session_id: str) -> bool:
        return self.store.exists(session_id)

    def delete(self, session_id: str) -> bool:
        return self.store.delete(session_id)

    def delete_many(self, session_ids: Iterable[str]) -> int:
        """Delete several sessions and return how many were actually removed."""
        return sum(1 for session_id in session_ids if self.delete(session_id))

    def delete_all(self, *, cwd: str | None = None) -> int:
        """Delete every session (optionally only sessions for one project)."""
        return self.delete_many(session.id for session in self.list(cwd=cwd))

    def list(self, *, limit: int | None = None, cwd: str | None = None) -> list[Session]:
        """All sessions, most recently updated first.

        ``cwd`` narrows the list to sessions whose working directory matches.
        The comparison is path-normalised so ``/a/b``, ``/a/b/`` and
        ``/a/b/.`` all mean the same project.
        """
        sessions = [Session.from_dict(data) for _, data in self.store.items()]
        if cwd is not None:
            target = _normalise_cwd(cwd)
            sessions = [s for s in sessions if _normalise_cwd(s.cwd) == target]
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions[:limit] if limit else sessions

    def summaries(self, *, limit: int | None = None, cwd: str | None = None) -> list[dict[str, Any]]:
        return [session_summary(session) for session in self.list(limit=limit, cwd=cwd)]

    # ------------------------------------------------------------------ #
    # fork / retitle
    # ------------------------------------------------------------------ #
    def fork(self, session_id: str, *, at_message: int | None = None, title: str = "") -> Session:
        """Create an independent copy of a session, optionally truncated.

        ``at_message`` caps how much history the fork inherits - handy for
        retrying from an earlier point without losing the original.
        """
        parent = self.require(session_id)
        cut = len(parent.messages) if at_message is None else max(0, min(at_message, len(parent.messages)))
        # Keep tool_calls aligned with the truncated messages.
        if at_message is None:
            forked_calls = [ToolCallRecord.from_dict(c.to_dict()) for c in parent.tool_calls]
        else:
            expected = sum(len((m.get("extra") or {}).get("tool_calls") or []) for m in parent.messages[:cut])
            forked_calls = [ToolCallRecord.from_dict(c.to_dict()) for c in parent.tool_calls[:expected]]
        forked = Session(
            id=new_session_id(),
            title=title or f"{parent.title} (fork)",
            parent_id=parent.id,
            fork_point=cut,
            provider=parent.provider,
            model=parent.model,
            cwd=parent.cwd,
            messages=[_clone_message(m) for m in parent.messages[:cut]],
            tool_calls=forked_calls,
            metadata=dict(parent.metadata),
        )
        forked.metadata["forked_from"] = parent.id
        self.save(forked)
        return forked

    def retitle(self, session_id: str, title: str) -> Session:
        session = self.require(session_id)
        session.title = title or session.title
        return self.save(session)

    # ------------------------------------------------------------------ #
    # mutation helpers used by the agent
    # ------------------------------------------------------------------ #
    def append_message(self, session: Session, message: Mapping[str, Any]) -> None:
        session.messages.append(dict(message))
        session.updated_at = time.time()

    def extend_messages(self, session: Session, messages: Iterable[Mapping[str, Any]]) -> None:
        session.messages.extend(dict(message) for message in messages)

    def record_tool_call(self, session: Session, record: ToolCallRecord) -> None:
        session.tool_calls.append(record)
        session.updated_at = time.time()

    def set_model(self, session: Session, provider: str, model: str) -> Session:
        session.provider = provider
        session.model = model
        return self.save(session)

    def maybe_auto_title(self, session: Session) -> bool:
        """Set the title from the first real user message (only once)."""
        if session.title and session.title != "New session":
            return False
        text = session.last_user_message
        if not text:
            return False
        session.title = auto_title(text)
        return True

    # ------------------------------------------------------------------ #
    # convenience
    # ------------------------------------------------------------------ #
    def most_recent(self) -> Session | None:
        sessions = self.list(limit=1)
        return sessions[0] if sessions else None

    def prune_empty(self) -> int:
        """Delete empty ``New session`` placeholder files and return how many were removed."""
        removed = 0
        for session in self.list():
            if not session.messages and session.title == "New session":
                if self.delete(session.id):
                    removed += 1
        return removed

    def repair_titles(self) -> int:
        """Re-title sessions whose title still carries the ``Task:/Instructions:`` shell.

        These were written by the old auto-titler before it learned to peel the
        wrapper, so their titles read ``Task: read the file Instructions: ...``.
        Re-derive a clean title from the last user message and persist the fix.
        """
        fixed = 0
        for session in self.list():
            if "Task:" in session.title and "Instructions:" in session.title:
                text = session.last_user_message
                if text:
                    new_title = auto_title(text)
                    if new_title and new_title != session.title:
                        session.title = new_title
                        self.save(session)
                        fixed += 1
        return fixed

    def search(self, needle: str) -> list[Session]:
        lowered = needle.lower()
        return [s for s in self.list() if lowered in s.title.lower() or lowered in s.id.lower()]

    def stats(self) -> dict[str, Any]:
        sessions = self.list()
        return {
            "count": len(sessions),
            "messages": sum(s.message_count for s in sessions),
            "tool_calls": sum(s.tool_call_count for s in sessions),
        }


def _clone_message(message: Mapping[str, Any]) -> dict[str, Any]:
    import copy

    return copy.deepcopy(dict(message))
