"""Filesystem layout for minicode.

Everything lives under a single root so that the whole thing is trivially
inspectable / removable:

    <data_dir>/
        config.yaml
        sessions/<session-id>.json
        truncation/<tool-output-id>.txt
        logs/
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "minicode"

#: Project-local directory name (created inside the working directory).
PROJECT_DIR_NAME = ".minicode"


def data_dir() -> Path:
    """Global data directory. Overridable with ``MINICODE_DATA_DIR``."""
    root = os.getenv("MINICODE_DATA_DIR") or user_data_dir(APP_NAME)
    path = Path(root).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def sessions_dir() -> Path:
    path = data_dir() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def truncation_dir() -> Path:
    path = data_dir() / "truncation"
    path.mkdir(parents=True, exist_ok=True)
    return path


def global_config_file() -> Path:
    return data_dir() / "config.yaml"


def project_dir(cwd: str | Path | None = None) -> Path:
    """The per-project ``.minicode`` directory (config + local data)."""
    base = Path(cwd) if cwd else Path.cwd()
    return base / PROJECT_DIR_NAME


def project_config_file(cwd: str | Path | None = None) -> Path:
    return project_dir(cwd) / "config.yaml"


#: Directory names that search tools skip regardless of ``.gitignore``
#: (VCS, virtualenvs, caches, vendored trees). Mirrored into ripgrep's
#: exclude globs so the fast path and the pure-Python fallback agree.
SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".minicode",
        "dist",
        "build",
        ".tox",
        ".idea",
        ".vscode",
    }
)


def is_hidden(path: Path) -> bool:
    """Whether any path component should be skipped by search tools."""
    return any(part in SKIP_DIR_NAMES for part in path.parts)
