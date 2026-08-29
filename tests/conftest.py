"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _isolated_storage(tmp_path, monkeypatch):
    """Redirect every global directory into tmp_path so tests never touch $HOME."""
    monkeypatch.setenv("MINICODE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    yield


@pytest.fixture
def project(tmp_path) -> Path:
    """A scratch directory containing a tiny python project."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "calc.py").write_bytes(b"def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n")
    (root / "README.md").write_bytes(b"# demo\n")
    return root
