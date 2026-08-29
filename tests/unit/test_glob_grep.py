"""Unit tests: glob / grep - code search without external binaries."""

from __future__ import annotations

import pytest

from minicode.tools.base import ToolContext
from minicode.tools.registry import build_default_registry

pytestmark = pytest.mark.unit


@pytest.fixture
def tree(tmp_path):
    """A small project with hidden dirs, binaries and nested packages."""
    root = tmp_path / "tree"
    (root / "src").mkdir(parents=True)
    (root / "src/pkg").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "node_modules").mkdir()
    (root / "__pycache__").mkdir()

    (root / "src/main.py").write_bytes(b"def main():\n    return 1\n")
    (root / "src/pkg/util.py").write_bytes(b"def helper():\n    return 2\n")
    (root / "src/pkg/notes.txt").write_bytes(b"a note about helper\n")
    (root / "README.md").write_bytes(b"# tree\n")
    (root / ".git/config").write_bytes(b"[core]\n")
    (root / "node_modules/dep.js").write_bytes(b"module.exports = 1\n")
    (root / "__pycache__/mod.pyc").write_bytes(b"\x00\x01binary")
    (root / "logo.png").write_bytes(b"\x89PNG\x00\x01")
    return root


@pytest.fixture
def ctx(tree):
    return ToolContext(cwd=str(tree))


@pytest.fixture
def registry(tree):
    return build_default_registry(cwd=str(tree))


# --------------------------------------------------------------------------- #
# glob
# --------------------------------------------------------------------------- #
def test_glob_finds_python_files_recursively(registry, ctx):
    result = registry.execute("glob", {"pattern": "**/*.py"}, ctx)
    assert result.ok
    assert "main.py" in result.output
    assert "util.py" in result.output


def test_glob_respects_the_limit(registry, ctx):
    result = registry.execute("glob", {"pattern": "**/*", "limit": 2}, ctx)
    assert result.ok
    assert result.metadata["returned"] == 2
    assert "more files omitted" in result.output


def test_glob_reports_no_matches_clearly(registry, ctx):
    result = registry.execute("glob", {"pattern": "**/*.rs"}, ctx)
    assert result.ok
    assert "No files matched" in result.output


def test_glob_skips_hidden_and_vendor_directories(registry, ctx):
    result = registry.execute("glob", {"pattern": "**/*"}, ctx)
    output = result.output
    assert ".git" not in output
    assert "node_modules" not in output
    assert "__pycache__" not in output


def test_glob_can_search_a_subdirectory(registry, ctx):
    result = registry.execute("glob", {"pattern": "*.py", "path": "src/pkg"}, ctx)
    assert "util.py" in result.output
    assert "main.py" not in result.output


def test_glob_missing_directory_is_an_error(registry, ctx):
    result = registry.execute("glob", {"pattern": "*", "path": "nope"}, ctx)
    assert not result.ok
    assert result.error.code == "path_not_found"


def test_glob_returns_metadata_for_the_agent(registry, ctx):
    result = registry.execute("glob", {"pattern": "**/*.py"}, ctx)
    assert result.metadata["total"] >= 2
    assert result.metadata["returned"] == result.metadata["total"]


# --------------------------------------------------------------------------- #
# grep
# --------------------------------------------------------------------------- #
def test_grep_finds_matches_with_line_numbers(registry, ctx):
    result = registry.execute("grep", {"pattern": "def "}, ctx)
    assert result.ok
    assert "main.py:1:def main():" in result.output
    assert "util.py:1:def helper():" in result.output


def test_grep_is_case_insensitive_by_default(registry, ctx, tree):
    (tree / "src/Case.py").write_bytes(b"DEF UPPER():\n")
    result = registry.execute("grep", {"pattern": "def upper"}, ctx)
    assert result.ok
    assert "Case.py" in result.output


def test_grep_can_be_case_sensitive(registry, ctx, tree):
    (tree / "src/Case.py").write_bytes(b"DEF UPPER():\n")
    result = registry.execute("grep", {"pattern": "DEF UPPER", "case_sensitive": True}, ctx)
    assert "Case.py" in result.output
    result = registry.execute("grep", {"pattern": "def upper", "case_sensitive": True}, ctx)
    assert "Case.py" not in result.output


def test_grep_include_filter(registry, ctx):
    result = registry.execute("grep", {"pattern": "helper", "include": "*.py"}, ctx)
    assert "util.py" in result.output
    assert "notes.txt" not in result.output


def test_grep_skips_binary_files(registry, ctx):
    result = registry.execute("grep", {"pattern": "PNG"}, ctx)
    assert "logo.png" not in result.output


def test_grep_skips_hidden_directories(registry, ctx):
    result = registry.execute("grep", {"pattern": r"\[core\]"}, ctx)
    assert ".git" not in result.output


def test_grep_reports_no_matches_clearly(registry, ctx):
    result = registry.execute("grep", {"pattern": "zzz_no_such_thing"}, ctx)
    assert result.ok
    assert "No matches" in result.output


def test_grep_invalid_regex_is_a_structured_error(registry, ctx):
    result = registry.execute("grep", {"pattern": "([unclosed"}, ctx)
    assert not result.ok
    assert result.error.code == "invalid_regex"


def test_grep_caps_the_number_of_matches(registry, ctx, tree):
    (tree / "big.txt").write_bytes(b"match\n" * 500)
    result = registry.execute("grep", {"pattern": "match", "max_matches": 10}, ctx)
    assert result.metadata["matches"] == 10
    assert result.metadata["truncated"] is True
    assert "stopped after" in result.output


def test_grep_can_search_a_single_file(registry, ctx):
    result = registry.execute("grep", {"pattern": "helper", "path": "src/pkg/util.py"}, ctx)
    assert result.ok
    assert "util.py:1" in result.output


def test_grep_returns_search_metadata(registry, ctx):
    result = registry.execute("grep", {"pattern": "def "}, ctx)
    assert result.metadata["files_matched"] >= 2
    assert result.metadata["files_searched"] > 0


def test_grep_missing_path_is_an_error(registry, ctx):
    result = registry.execute("grep", {"pattern": "x", "path": "nope"}, ctx)
    assert not result.ok
    assert result.error.code == "path_not_found"


def test_search_tools_are_read_only_permissions():
    registry = build_default_registry()
    assert registry.get("glob").permission == "glob"
    assert registry.get("grep").permission == "grep"
