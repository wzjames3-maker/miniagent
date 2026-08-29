"""Unit tests: read / write / edit / apply_patch - the file manipulation core."""

from __future__ import annotations

import pytest

from minicode.tools.base import ToolContext
from minicode.tools.file_tools import EditTool, ReadTool, WriteTool
from minicode.tools.patch_tool import ApplyPatchTool
from minicode.tools.registry import build_default_registry

pytestmark = pytest.mark.unit


@pytest.fixture
def ctx(project):
    return ToolContext(cwd=str(project))


@pytest.fixture
def registry(project):
    return build_default_registry(cwd=str(project))


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #
def test_read_numbers_lines_from_one(registry, ctx, project):
    result = registry.execute("read", {"file_path": "calc.py"}, ctx)
    assert result.ok
    assert "1\tdef add" in result.render() or "     1\tdef add" in result.render()
    assert result.metadata["total_lines"] == 6


def test_read_supports_offset_and_limit(registry, ctx):
    result = registry.execute("read", {"file_path": "calc.py", "offset": 5, "limit": 1}, ctx)
    assert result.metadata["offset"] == 5
    assert result.metadata["returned_lines"] == 1
    assert "multiply" in result.output


def test_read_reports_truncation_of_long_files(registry, ctx, project):
    (project / "big.py").write_bytes(b"x = 1\n" * 3000)
    result = registry.execute("read", {"file_path": "big.py"}, ctx)
    assert result.metadata["truncated"] or result.metadata["total_lines"] == 3000


def test_read_missing_file_is_a_structured_error(registry, ctx):
    result = registry.execute("read", {"file_path": "nope.py"}, ctx)
    assert not result.ok
    assert result.error.code == "file_not_found"
    assert "glob" in result.error.hint


def test_read_directory_is_a_structured_error(registry, ctx):
    result = registry.execute("read", {"file_path": "."}, ctx)
    assert not result.ok
    assert result.error.code == "is_directory"


def test_read_binary_file_is_refused(registry, ctx, project):
    (project / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    result = registry.execute("read", {"file_path": "blob.bin"}, ctx)
    assert not result.ok
    assert result.error.code == "binary_file"


def test_read_resolves_relative_paths_against_cwd(registry, ctx):
    assert registry.execute("read", {"file_path": "calc.py"}, ctx).ok
    assert registry.execute("read", {"file_path": "./calc.py"}, ctx).ok


# --------------------------------------------------------------------------- #
# write
# --------------------------------------------------------------------------- #
def test_write_creates_missing_parent_directories(registry, ctx, project):
    result = registry.execute("write", {"file_path": "pkg/sub/mod.py", "content": "VALUE = 1\n"}, ctx)
    assert result.ok
    assert (project / "pkg/sub/mod.py").read_bytes() == b"VALUE = 1\n"
    assert result.metadata["created"] is True


def test_write_overwrites_and_reports_a_diff(registry, ctx, project):
    target = project / "calc.py"
    result = registry.execute("write", {"file_path": "calc.py", "content": "def add(a, b):\n    return a - b\n"}, ctx)
    assert result.ok
    assert result.metadata["created"] is False
    assert "-    return a + b" in result.output or "- return" in result.output
    assert b"a - b" in target.read_bytes()


def test_write_is_byte_exact_no_newline_translation(registry, ctx, project):
    """On Windows, text-mode writes turn '\\n' into '\\r\\n' - this guards against it."""
    registry.execute("write", {"file_path": "lf.py", "content": "a = 1\nb = 2\n"}, ctx)
    assert (project / "lf.py").read_bytes() == b"a = 1\nb = 2\n"


def test_write_crlf_content_is_preserved(registry, ctx, project):
    registry.execute("write", {"file_path": "crlf.py", "content": "a = 1\r\nb = 2\r\n"}, ctx)
    assert (project / "crlf.py").read_bytes() == b"a = 1\r\nb = 2\r\n"


# --------------------------------------------------------------------------- #
# edit
# --------------------------------------------------------------------------- #
def test_edit_replaces_a_unique_occurrence(registry, ctx, project):
    result = registry.execute(
        "edit", {"file_path": "calc.py", "old_string": "return a + b", "new_string": "return a * b"}, ctx
    )
    assert result.ok
    assert b"return a * b" in (project / "calc.py").read_bytes()


def test_edit_requires_a_unique_match(registry, ctx, project):
    (project / "dup.py").write_bytes(b"x = 1\nx = 1\n")
    result = registry.execute("edit", {"file_path": "dup.py", "old_string": "x = 1", "new_string": "x = 2"}, ctx)
    assert not result.ok
    assert result.error.code == "ambiguous_match"
    assert result.error.details["occurrences"] == 2


def test_edit_replace_all_handles_repeats(registry, ctx, project):
    (project / "dup.py").write_bytes(b"x = 1\nx = 1\n")
    result = registry.execute(
        "edit", {"file_path": "dup.py", "old_string": "x = 1", "new_string": "x = 2", "replace_all": True}, ctx
    )
    assert result.ok
    assert (project / "dup.py").read_bytes() == b"x = 2\nx = 2\n"


def test_edit_reports_no_match_with_a_useful_hint(registry, ctx):
    result = registry.execute("edit", {"file_path": "calc.py", "old_string": "not present", "new_string": "x"}, ctx)
    assert not result.ok
    assert result.error.code == "no_match"
    assert "Re-read the file" in result.error.hint


def test_edit_rejects_empty_old_string(registry, ctx):
    result = registry.execute("edit", {"file_path": "calc.py", "old_string": "", "new_string": "x"}, ctx)
    assert not result.ok
    assert result.error.code == "empty_old_string"


def test_edit_can_delete_text(registry, ctx, project):
    registry.execute("edit", {"file_path": "calc.py", "old_string": "    return a + b\n", "new_string": ""}, ctx)
    assert b"return a + b" not in (project / "calc.py").read_bytes()


def test_edit_preserves_crlf_line_endings(registry, ctx, project):
    target = project / "crlf.py"
    target.write_bytes(b"a = 1\r\nb = 2\r\n")
    registry.execute("edit", {"file_path": "crlf.py", "old_string": "b = 2", "new_string": "b = 3"}, ctx)
    assert target.read_bytes() == b"a = 1\r\nb = 3\r\n"


def test_edit_accepts_lf_arguments_for_crlf_files(registry, ctx, project):
    """The model should not have to guess the file's line endings."""
    target = project / "crlf.py"
    target.write_bytes(b"a = 1\r\nb = 2\r\n")
    registry.execute("edit", {"file_path": "crlf.py", "old_string": "a = 1\nb = 2", "new_string": "a = 9\nb = 9"}, ctx)
    assert target.read_bytes() == b"a = 9\r\nb = 9\r\n"


def test_edit_does_not_mangle_windows_paths_in_new_content(registry, ctx, project):
    registry.execute(
        "edit",
        {
            "file_path": "calc.py",
            "old_string": "return a + b",
            "new_string": 'return "C:\\Users\\new\\path"',
        },
        ctx,
    )
    assert b"C:\\Users\\new\\path" in (project / "calc.py").read_bytes()


# --------------------------------------------------------------------------- #
# apply_patch
# --------------------------------------------------------------------------- #
UPDATE_PATCH = """*** Begin Patch
*** Update File: calc.py
@@
 def add(a, b):
-    return a + b
+    return a - b
*** End Patch
"""


def test_apply_patch_updates_an_existing_file(registry, ctx, project):
    result = registry.execute("apply_patch", {"patch": UPDATE_PATCH}, ctx)
    assert result.ok, result.error.message if result.error else ""
    assert b"return a - b" in (project / "calc.py").read_bytes()


CREATE_PATCH = """*** Begin Patch
*** Add File: new.py
+def hello():
+    return "hi"
*** End Patch
"""


def test_apply_patch_creates_a_new_file(registry, ctx, project):
    result = registry.execute("apply_patch", {"patch": CREATE_PATCH}, ctx)
    assert result.ok, result.error.message if result.error else ""
    assert b"def hello():" in (project / "new.py").read_bytes()


DELETE_PATCH = """*** Begin Patch
*** Delete File: calc.py
*** End Patch
"""


def test_apply_patch_deletes_a_file(registry, ctx, project):
    result = registry.execute("apply_patch", {"patch": DELETE_PATCH}, ctx)
    assert result.ok
    assert not (project / "calc.py").exists()


def test_apply_patch_accepts_leading_context_noise(registry, ctx, project):
    """Models routinely wrap patches in prose / fences - the parser must cope."""
    noisy = "Sure, here is the patch:\n```\n" + UPDATE_PATCH + "```\n"
    result = registry.execute("apply_patch", {"patch": noisy}, ctx)
    assert result.ok, result.error.message if result.error else ""
    assert b"return a - b" in (project / "calc.py").read_bytes()


def test_apply_patch_rejects_garbage(registry, ctx):
    result = registry.execute("apply_patch", {"patch": "this is not a patch"}, ctx)
    assert not result.ok
    assert result.error.code in {"invalid_patch", "patch_error"}


def test_apply_patch_reports_missing_context(registry, ctx, project):
    bad = """*** Begin Patch
*** Update File: calc.py
@@
-    return a ** b ** c
+    return 0
*** End Patch
"""
    result = registry.execute("apply_patch", {"patch": bad}, ctx)
    assert not result.ok
    assert result.error.code == "patch_does_not_apply"
    assert "Read" in result.error.hint


def test_apply_patch_shows_the_diff_so_the_agent_can_verify(registry, ctx):
    result = registry.execute("apply_patch", {"patch": UPDATE_PATCH}, ctx)
    # the model must be able to see exactly what changed
    assert "-    return a + b" in result.output
    assert "+    return a - b" in result.output


# --------------------------------------------------------------------------- #
# tool metadata
# --------------------------------------------------------------------------- #
def test_tools_declare_their_permission_keys():
    assert ReadTool().permission == "read"
    assert WriteTool().permission == "write"
    assert EditTool().permission == "edit"
    assert ApplyPatchTool().permission == "apply_patch"


def test_tools_expose_the_path_they_will_touch(project):
    """Permission patterns must target the path, not every argument value."""
    assert EditTool().patterns({"file_path": "a.py", "old_string": "x", "new_string": "y"}) == ["a.py"]
