"""``apply_patch`` - multi-file edits in one call.

Implements OpenCode's patch format::

    *** Begin Patch
    *** Add File: src/new_file.py
    +def hello():
    +    return "hi"
    *** Update File: src/existing.py
    @@ def foo():
     unchanged line
    -old line
    +new line
    *** Delete File: src/obsolete.py
    *** End Patch

Legend for hunk lines: `` `` (context), ``-`` (remove), ``+`` (add), ``@@`` (start
a new hunk / optional section heading).

Matching is exact first and whitespace-tolerant as a fallback, which keeps the
tool robust against models that re-indent context lines.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from minicode.tools.base import BaseTool, ToolContext, ToolError, ToolResult
from minicode.tools.file_tools import _write_text

__all__ = ["ApplyPatchTool", "PatchParseError", "PatchApplyError", "parse_patch", "apply_patch_to_text"]


class PatchParseError(ValueError):
    """The patch text is malformed."""


class PatchApplyError(ValueError):
    """The patch parses but does not apply to the file."""


@dataclass
class Hunk:
    lines: list[tuple[str, str]] = field(default_factory=list)  # (op, text)
    heading: str = ""


@dataclass
class PatchOp:
    kind: str  # add | update | delete
    path: str
    move_to: str | None = None
    hunks: list[Hunk] = field(default_factory=list)


@dataclass
class ParsedPatch:
    ops: list[PatchOp] = field(default_factory=list)


_BEGIN = "*** Begin Patch"
_END = "*** End Patch"


def parse_patch(text: str) -> ParsedPatch:
    """Parse an OpenCode-style patch into :class:`ParsedPatch`."""
    lines = text.splitlines()
    if not any(line.strip() == _BEGIN for line in lines):
        raise PatchParseError("Patch must start with '*** Begin Patch'")
    if not any(line.strip() == _END for line in lines):
        raise PatchParseError("Patch must end with '*** End Patch'")

    parsed = ParsedPatch()
    current: PatchOp | None = None
    hunk: Hunk | None = None
    in_patch = False

    for raw in lines:
        line = raw.rstrip("\r")
        stripped = line.strip()
        if stripped == _BEGIN:
            in_patch = True
            continue
        if stripped == _END:
            in_patch = False
            break
        if not in_patch:
            continue

        if stripped.startswith("*** "):
            directive = stripped[4:]
            keyword, _, argument = directive.partition(":")
            keyword = keyword.strip().lower()
            argument = argument.strip()
            if keyword in {"add file", "update file", "delete file"}:
                if hunk is not None and current is not None:
                    current.hunks.append(hunk)
                    hunk = None
                if current is not None:
                    parsed.ops.append(current)
                kind = {"add file": "add", "update file": "update", "delete file": "delete"}[keyword]
                current = PatchOp(kind=kind, path=argument)
                hunk = Hunk() if kind == "update" else None
                continue
            if keyword == "move to":
                if current is None:
                    raise PatchParseError("'*** Move to' outside of a file section")
                current.move_to = argument
                continue
            raise PatchParseError(f"Unknown patch directive: {stripped!r}")

        if current is None:
            continue

        if current.kind == "update":
            if stripped == "@@" or stripped.startswith("@@ "):
                if hunk is not None and (hunk.lines or hunk.heading):
                    current.hunks.append(hunk)
                hunk = Hunk(heading=stripped[2:].strip())
                continue
            if hunk is None:
                hunk = Hunk()
            if line.startswith("+"):
                hunk.lines.append(("+", line[1:]))
            elif line.startswith("-"):
                hunk.lines.append(("-", line[1:]))
            elif line.startswith(" ") or line == "":
                hunk.lines.append((" ", line[1:] if line else ""))
            elif line.startswith("\\"):
                continue  # "\ No newline at end of file"
            else:
                raise PatchParseError(f"Unexpected line in patch: {raw!r}")
        elif current.kind == "add":
            if line.startswith("+"):
                current.hunks = current.hunks or [Hunk()]
                current.hunks[0].lines.append(("+", line[1:]))
            elif line.startswith("\\"):
                continue
            elif line.strip() == "":
                current.hunks = current.hunks or [Hunk()]
                current.hunks[0].lines.append(("+", ""))
            else:
                raise PatchParseError(f"Unexpected line in 'Add File' section: {raw!r}")

    if current is not None:
        if hunk is not None and (hunk.lines or hunk.heading):
            current.hunks.append(hunk)
        parsed.ops.append(current)

    if not parsed.ops:
        raise PatchParseError("Patch contained no file operations")
    return parsed


def _match_seq(file_lines: list[str], index: int, expected: list[str], *, strict: bool) -> bool:
    for offset, expected_line in enumerate(expected):
        actual = file_lines[index + offset]
        a = actual.rstrip("\r\n")
        e = expected_line.rstrip()
        if strict:
            if a != e:
                return False
        elif a.strip() != e.strip():
            return False
    return True


def _locate(file_lines: list[str], hunk: Hunk, start: int) -> int:
    old_side = [text for op, text in hunk.lines if op in (" ", "-")]
    if not old_side:
        return max(0, min(start, len(file_lines)))
    n = len(old_side)
    if n > len(file_lines):
        return -1
    for strict in (True, False):
        for begin in list(range(start, len(file_lines) - n + 1)) + list(range(0, min(start, len(file_lines) - n + 1))):
            if _match_seq(file_lines, begin, old_side, strict=strict):
                return begin
    return -1


def apply_patch_to_text(content: str, hunks: list[Hunk]) -> str:
    """Apply update-hunks to ``content`` and return the new text."""
    newline = "\r\n" if "\r\n" in content else "\n"
    file_lines = content.splitlines(keepends=True)
    ends_with_newline = content.endswith(("\n", "\r"))

    cursor = 0
    for hunk in hunks:
        index = _locate(file_lines, hunk, cursor)
        if index < 0:
            snippet = "\n".join(t for op, t in hunk.lines if op in (" ", "-"))[:400]
            raise PatchApplyError(f"Could not find the target block:\n{snippet}")
        produced: list[str] = []
        pointer = index
        for op, text in hunk.lines:
            if op == " ":
                produced.append(file_lines[pointer])
                pointer += 1
            elif op == "-":
                pointer += 1
            else:
                produced.append(f"{text}{newline}")
        file_lines[index:pointer] = produced
        cursor = index + len([1 for op, _ in hunk.lines if op in ("+", " ")])

    result = "".join(file_lines)
    if not ends_with_newline:
        result = result.rstrip("\n").rstrip("\r")
    return result


class ApplyPatchTool(BaseTool):
    """Apply a multi-file patch (add / update / delete / move)."""

    name = "apply_patch"
    permission = "apply_patch"
    description = (
        "Apply a patch that can create, modify, delete or move files in one operation. "
        "The patch must be wrapped in '*** Begin Patch' / '*** End Patch'. "
        "Use it for larger, multi-file changes; use edit for single, surgical replacements."
    )
    parameters = {
        "type": "object",
        "properties": {
            "patch": {
                "type": "string",
                "description": "The full patch text, including *** Begin Patch / *** End Patch.",
            }
        },
        "required": ["patch"],
    }

    def patterns(self, args: Mapping[str, Any]) -> list[str]:
        try:
            return [op.path for op in parse_patch(str(args.get("patch", ""))).ops]
        except PatchParseError:
            return ["*"]

    def required_permissions(self, args: Mapping[str, Any]) -> list[tuple[str, list[str]]]:
        checks: list[tuple[str, list[str]]] = []
        paths: list[str] = []
        try:
            ops = parse_patch(str(args.get("patch", ""))).ops
        except PatchParseError:
            return [(self.permission, ["*"])]
        for op in ops:
            paths.append(op.path)
            if op.move_to:
                paths.append(op.move_to)
        if paths:
            checks.append((self.permission, paths))
            checks.append(("edit", paths))
        for op in ops:
            if op.kind == "delete":
                checks.append(("delete", [op.path]))
        return checks or [(self.permission, ["*"])]

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        patch_text = str(args["patch"])
        try:
            parsed = parse_patch(patch_text)
        except PatchParseError as exc:
            return ToolResult(
                title=self.name,
                output="",
                error=ToolError(
                    code="invalid_patch",
                    message=str(exc),
                    hint="Emit '*** Begin Patch', then '*** Add File: p' / '*** Update File: p' / "
                    "'*** Delete File: p' sections with ' ', '-' and '+' lines, then '*** End Patch'.",
                ),
            )

        applied: list[str] = []
        diffs: list[str] = []
        for op in parsed.ops:
            target = Path(ctx.resolve(op.path))
            if op.kind == "add":
                target.parent.mkdir(parents=True, exist_ok=True)
                content = "".join(f"{text}\n" for h in op.hunks for o, text in h.lines if o == "+")
                _write_text(target, content)
                applied.append(f"created {target}")
                diffs.append(
                    f"--- /dev/null\n+++ b/{op.path}\n" + "".join(f"+{line}\n" for line in content.splitlines())
                )
                continue

            if op.kind == "delete":
                if not target.exists():
                    return ToolResult(
                        title=self.name,
                        output="",
                        error=ToolError(code="file_not_found", message=f"Cannot delete missing file {target}"),
                    )
                target.unlink()
                applied.append(f"deleted {target}")
                continue

            # update
            if not target.exists() or not target.is_file():
                return ToolResult(
                    title=self.name,
                    output="",
                    error=ToolError(
                        code="file_not_found",
                        message=f"Cannot update missing file {target}",
                        hint="Use '*** Add File' to create it, or check the path.",
                    ),
                )
            before = target.read_text(encoding="utf-8")
            try:
                after = apply_patch_to_text(before, op.hunks)
            except PatchApplyError as exc:
                return ToolResult(
                    title=self.name,
                    output="",
                    error=ToolError(
                        code="patch_does_not_apply",
                        message=f"{exc}",
                        hint=f"Read {op.path} again and regenerate the patch against the current content.",
                    ),
                )
            destination = Path(ctx.resolve(op.move_to)) if op.move_to else target
            if destination != target:
                destination.parent.mkdir(parents=True, exist_ok=True)
                target.unlink()
            _write_text(destination, after)
            applied.append(f"updated {destination}" + (f" (moved from {target})" if destination != target else ""))
            diffs.append(
                "".join(
                    difflib.unified_diff(
                        before.splitlines(keepends=True),
                        after.splitlines(keepends=True),
                        fromfile=f"a/{op.path}",
                        tofile=f"b/{op.move_to or op.path}",
                        n=2,
                    )
                )
            )

        output = "Patch applied:\n" + "\n".join(f"  - {line}" for line in applied)
        diff_text = "\n".join(d for d in diffs if d)
        # Diff is helpful for review but unbounded — let truncation decide.
        if diff_text:
            output += f"\n\n{diff_text}"
        result = ToolResult(
            title="apply_patch",
            output=output,
            metadata={"files": applied, "operations": [op.kind for op in parsed.ops]},
        )
        if ctx.truncate is not None:
            ctx.truncate(result)
        else:
            from minicode.tools.truncate import truncate_output

            truncated = truncate_output(result.output, tool=self.name)
            if truncated.truncated:
                result.output = truncated.content
                result.metadata["truncated"] = True
                result.metadata["output_path"] = truncated.output_path
        return result
