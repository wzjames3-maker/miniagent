"""A tiny JSON document store used for sessions and other persisted state.

Deliberately dependency-free: one file per document, atomic writes via
tmp-file + ``os.replace`` so a crash can never leave a half-written session.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, data: Any, *, indent: int = 2) -> None:
    """Write ``data`` as JSON to ``path`` atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=indent, ensure_ascii=False, default=str)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def read_json(path: Path, default: Any = None) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return default


class JsonDocumentStore:
    """Directory-backed store of JSON documents keyed by id (filename stem)."""

    def __init__(self, directory: Path | str, *, suffix: str = ".json"):
        self.directory = Path(directory)
        self.suffix = suffix
        self._lock = threading.Lock()
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, doc_id: str) -> Path:
        return self.directory / f"{doc_id}{self.suffix}"

    def save(self, doc_id: str, data: Any) -> Path:
        with self._lock:
            atomic_write_json(self._path(doc_id), data)
        return self._path(doc_id)

    def load(self, doc_id: str, default: Any = None) -> Any:
        return read_json(self._path(doc_id), default)

    def exists(self, doc_id: str) -> bool:
        return self._path(doc_id).exists()

    def delete(self, doc_id: str) -> bool:
        with self._lock:
            path = self._path(doc_id)
            if not path.exists():
                return False
            path.unlink()
            return True

    def ids(self) -> list[str]:
        if not self.directory.exists():
            return []
        return sorted(p.name[: -len(self.suffix)] for p in self.directory.glob(f"*{self.suffix}"))

    def items(self) -> Iterable[tuple[str, Any]]:
        for doc_id in self.ids():
            data = self.load(doc_id)
            if data is not None:
                yield doc_id, data

    def query(self, predicate: Callable[[str, Any], bool]) -> list[tuple[str, Any]]:
        return [(doc_id, data) for doc_id, data in self.items() if predicate(doc_id, data)]
