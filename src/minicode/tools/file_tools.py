"""``read``, ``write`` and ``edit`` - the file manipulation core.

``edit`` is the workhorse: it requires ``old_string`` to match exactly once,
which is what makes agent edits safe and reviewable (no fuzzy rewriting).
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from minicode.tools.base import BaseTool, ToolContext, ToolError, ToolResult
from minicode.tools.truncate import truncate_output

# Lines shown by `read` by default (mirrors OpenCode's limit).
READ_MAX_LINES = 2000
MAX_LINE_BYTES = 2000


def _readable(path: Path) -> ToolError | None:
    if not path.exists():
        return ToolError(
            code="file_not_found",
            message=f"File does not exist: {path}",
            hint="Use glob or grep to find the correct path.",
        )
    if path.is_dir():
        return ToolError(
            code="is_directory",
            message=f"Path is a directory, not a file: {path}",
            hint="Use glob to list files inside the directory.",
        )
    return None


def _decode(raw: bytes) -> str | None:
    """Return text, or None when the file looks binary."""
    if b"\x00" in raw[:8192]:
        return None
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _write_text(path: Path, content: str) -> None:
    """Write text verbatim.

    ``Path.write_text`` opens in text mode, which translates "\n" to the platform
    line separator - on Windows that turns an already-CRLF string into "\r\r\n".
    Always write bytes so what the model asked for is what lands on disk.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))


class ReadTool(BaseTool):
    """Read a file from the local filesystem."""

    name = "read"
    permission = "read"
    description = (
        "Read a text file. Output is returned with 1-based line numbers. "
        "Large files are truncated - use offset/limit to page through them."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path, or path relative to the working directory."},
            "offset": {"type": "integer", "description": "1-based first line to read (default 1)."},
            "limit": {"type": "integer", "description": "Maximum number of lines to read (default 2000)."},
        },
        "required": ["file_path"],
    }

    def patterns(self, args: Mapping[str, Any]) -> list[str]:
        return [str(args.get("file_path", "*"))]

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        path = Path(ctx.resolve(args["file_path"]))
        if (err := _readable(path)) is not None:
            return ToolResult(title=self.name, output="", error=err)

        raw = path.read_bytes()
        text = _decode(raw)
        if text is None:
            return ToolResult(
                title=self.name,
                output="",
                error=ToolError(
                    code="binary_file",
                    message=f"Cannot read binary file: {path}",
                    hint="Use bash with a dedicated tool if you really need to inspect it.",
                ),
            )

        lines = text.splitlines()
        offset = max(1, int(args.get("offset", 1) or 1))
        limit = int(args.get("limit", READ_MAX_LINES) or READ_MAX_LINES)
        selected = lines[offset - 1 : offset - 1 + limit]

        rendered: list[str] = []
        for i, line in enumerate(selected, start=offset):
            if len(line) > MAX_LINE_BYTES:
                line = line[:MAX_LINE_BYTES] + f"... [{len(line) - MAX_LINE_BYTES} more characters]"
            rendered.append(f"{i:6d}\t{line}")

        output = "\n".join(rendered)
        metadata: dict[str, Any] = {
            "path": str(path),
            "total_lines": len(lines),
            "offset": offset,
            "returned_lines": len(selected),
        }

        if offset - 1 + limit < len(lines):
            output += (
                f"\n\n[showing lines {offset}-{offset + len(selected) - 1} of {len(lines)}; "
                f"use offset={offset + len(selected)} to continue]"
            )
            metadata["truncated"] = True
        if not selected:
            output = f"[file {path} has {len(lines)} lines; nothing to show at offset={offset}]"

        if ctx.truncate is not None:
            result = truncate_output(output, max_lines=READ_MAX_LINES, tool=self.name)
            if result.truncated:
                metadata["truncated"] = True
                metadata["output_path"] = result.output_path
            output = result.content

        return ToolResult(title=f"read {path.name}", output=output, metadata=metadata)


class WriteTool(BaseTool):
    """Create or fully overwrite a file."""

    name = "write"
    permission = "write"
    description = "Write a file to disk. Creates parent directories and overwrites existing content."
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path, or path relative to the working directory."},
            "content": {"type": "string", "description": "The full content to write."},
        },
        "required": ["file_path", "content"],
    }

    def patterns(self, args: Mapping[str, Any]) -> list[str]:
        return [str(args.get("file_path", "*"))]

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        path = Path(ctx.resolve(args["file_path"]))
        content: str = args["content"]
        existed = path.exists()
        previous = path.read_text(encoding="utf-8", errors="replace") if existed and path.is_file() else ""

        path.parent.mkdir(parents=True, exist_ok=True)
        _write_text(path, content)

        diff = ""
        if existed:
            diff = "".join(
                difflib.unified_diff(
                    previous.splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile=f"a/{path.name}",
                    tofile=f"b/{path.name}",
                    n=1,
                )
            )
        summary = f"{'Updated' if existed else 'Created'} {path} ({len(content.splitlines())} lines)"
        output = summary + (f"\n{diff}" if diff else "")
        return ToolResult(
            title=f"write {path.name}",
            output=output if len(output) < 8000 else summary,
            metadata={"path": str(path), "created": not existed, "lines": len(content.splitlines())},
        )


class EditTool(BaseTool):
    """Replace an exact string inside a file (must be unambiguous)."""

    name = "edit"
    permission = "edit"
    description = (
        "Replace an exact occurrence of old_string with new_string in a file. "
        "old_string must be unique unless replace_all is true. "
        "Use new_string='' to delete text. Always read the file first."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path, or path relative to the working directory."},
            "old_string": {"type": "string", "description": "The exact text to replace (including whitespace/indentation)."},
            "new_string": {"type": "string", "description": "The replacement text."},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)."},
        },
        "required": ["file_path", "old_string", "new_string"],
    }

    def patterns(self, args: Mapping[str, Any]) -> list[str]:
        return [str(args.get("file_path", "*"))]

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        path = Path(ctx.resolve(args["file_path"]))
        if (err := _readable(path)) is not None:
            return ToolResult(title=self.name, output="", error=err)

        raw = path.read_bytes()
        text = _decode(raw)
        if text is None:
            return ToolResult(
                title=self.name,
                output="",
                error=ToolError(code="binary_file", message=f"Cannot edit binary file: {path}"),
            )

        old = args["old_string"]
        new = args["new_string"]
        replace_all = bool(args.get("replace_all", False))
        newline = "\r\n" if "\r\n" in text else "\n"
        old = old.replace("\r\n", "\n").replace("\n", newline)
        new = new.replace("\r\n", "\n").replace("\n", newline)

        if not old:
            return ToolResult(
                title=self.name,
                output="",
                error=ToolError(code="empty_old_string", message="old_string must not be empty."),
            )

        count = text.count(old)
        if count == 0:
            return ToolResult(
                title=self.name,
                output="",
                error=ToolError(
                    code="no_match",
                    message=f"old_string not found in {path}",
                    hint="Re-read the file: whitespace, indentation or line endings may differ.",
                    details={"occurrences": 0},
                ),
            )
        if count > 1 and not replace_all:
            return ToolResult(
                title=self.name,
                output="",
                error=ToolError(
                    code="ambiguous_match",
                    message=f"old_string occurs {count} times in {path}",
                    hint="Include more context to make it unique, or set replace_all=true.",
                    details={"occurrences": count},
                ),
            )

        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        _write_text(path, updated)

        diff = "".join(
            difflib.unified_diff(
                text.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{path.name}",
                tofile=f"b/{path.name}",
                n=2,
            )
        )
        replacements = count if replace_all else 1
        output = f"Edited {path} ({replacements} replacement{'s' if replacements != 1 else ''})\n{diff}"
        return ToolResult(
            title=f"edit {path.name}",
            output=output if len(output) < 8000 else f"Edited {path} ({replacements} replacement(s))",
            metadata={"path": str(path), "replacements": replacements, "diff": diff},
        )


__all__ = ["ReadTool", "WriteTool", "EditTool"]
