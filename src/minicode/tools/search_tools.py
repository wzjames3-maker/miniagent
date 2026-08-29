"""``glob`` and ``grep`` - code search without external binaries.

Both are implemented in pure Python (ripgrep is intentionally *not* required so
that the project stays portable and dependency-light).
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from minicode.storage.paths import is_hidden
from minicode.tools.base import BaseTool, ToolContext, ToolError, ToolResult

#: Hard caps so a greedy search can never blow up the context.
MAX_GLOB_RESULTS = 500
MAX_GREP_MATCHES = 200
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_LINE_CHARS = 500

_BINARY_SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".bz2",
    ".7z",
    ".so",
    ".dll",
    ".exe",
    ".pyc",
    ".pyd",
    ".whl",
    ".mp4",
    ".mp3",
    ".woff",
    ".woff2",
    ".ttf",
    ".lock",
    ".min.js",
}


def _walk(base: Path) -> Iterator[Path]:
    """Yield files below ``base``, skipping VCS/virtualenv/cache directories."""
    if base.is_file():
        yield base
        return
    if not base.is_dir():
        return
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(base)
        except ValueError:  # pragma: no cover - defensive
            continue
        if is_hidden(Path(rel)):
            continue
        yield path


class GlobTool(BaseTool):
    """Find files by glob pattern."""

    name = "glob"
    permission = "glob"
    description = (
        "Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts'). "
        "Results are returned newest-first and capped at 500 paths."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py' or 'src/**/*.ts'."},
            "path": {"type": "string", "description": "Directory to search in (defaults to the working directory)."},
            "limit": {"type": "integer", "description": "Maximum number of results (default 500)."},
        },
        "required": ["pattern"],
    }

    def patterns(self, args: Mapping[str, Any]) -> list[str]:
        return [str(args.get("path") or ".")]

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        pattern: str = args["pattern"]
        base = Path(ctx.resolve(args.get("path") or ctx.cwd))
        limit = int(args.get("limit", MAX_GLOB_RESULTS) or MAX_GLOB_RESULTS)

        if not base.exists():
            return ToolResult(
                title=self.name,
                output="",
                error=ToolError(
                    code="path_not_found",
                    message=f"Directory does not exist: {base}",
                    hint="Check the path argument.",
                ),
            )

        matches: list[Path] = []
        if base.is_file():
            matches = [base]
        else:
            try:
                for path in base.glob(pattern):
                    if not path.is_file():
                        continue
                    rel = path.relative_to(base)
                    if is_hidden(Path(rel)):
                        continue
                    matches.append(path)
            except (ValueError, OSError) as exc:
                return ToolResult(
                    title=self.name,
                    output="",
                    error=ToolError(code="invalid_pattern", message=f"Invalid glob pattern {pattern!r}: {exc}"),
                )

        matches.sort(key=lambda p: (p.stat().st_mtime if p.exists() else 0.0), reverse=True)
        total = len(matches)
        shown = [str(p) for p in matches[:limit]]
        output = "\n".join(shown) if shown else f"No files matched pattern {pattern!r} in {base}"
        if total > limit:
            output += f"\n\n[{total - limit} more files omitted; narrow the pattern or raise limit]"
        return ToolResult(
            title=f"glob {pattern}",
            output=output,
            metadata={"pattern": pattern, "path": str(base), "total": total, "returned": len(shown)},
        )


class GrepTool(BaseTool):
    """Search file contents with a regular expression."""

    name = "grep"
    permission = "grep"
    description = (
        "Search file contents with a regular expression. Returns 'path:line:content' matches, "
        "newest files first, capped at 200 matches. Binary files and hidden directories are skipped."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression to search for."},
            "path": {"type": "string", "description": "File or directory to search (default: working directory)."},
            "include": {"type": "string", "description": "Only search files matching this glob, e.g. '*.py'."},
            "case_sensitive": {"type": "boolean", "description": "Case sensitive search (default false)."},
            "max_matches": {"type": "integer", "description": "Maximum matches to return (default 200)."},
        },
        "required": ["pattern"],
    }

    def patterns(self, args: Mapping[str, Any]) -> list[str]:
        return [str(args.get("path") or ".")]

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        pattern: str = args["pattern"]
        base = Path(ctx.resolve(args.get("path") or ctx.cwd))
        include: str | None = args.get("include")
        case_sensitive = bool(args.get("case_sensitive", False))
        max_matches = int(args.get("max_matches", MAX_GREP_MATCHES) or MAX_GREP_MATCHES)

        if not base.exists():
            return ToolResult(
                title=self.name,
                output="",
                error=ToolError(code="path_not_found", message=f"Path does not exist: {base}"),
            )

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return ToolResult(
                title=self.name,
                output="",
                error=ToolError(code="invalid_regex", message=f"Invalid regular expression {pattern!r}: {exc}"),
            )

        files: list[Path]
        if base.is_file():
            files = [base]
        else:
            files = [p for p in _walk(base) if include is None or fnmatch.fnmatch(p.name, include)]
            files.sort(key=lambda p: (p.stat().st_mtime if p.exists() else 0.0), reverse=True)

        results: list[str] = []
        files_searched = 0
        hit_files: set[str] = set()
        truncated = False
        for path in files:
            if len(results) >= max_matches:
                truncated = True
                break
            if path.suffix.lower() in _BINARY_SKIP_SUFFIXES:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                continue
            files_searched += 1
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hit_files.add(str(path))
                    snippet = line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + "..."
                    results.append(f"{path}:{lineno}:{snippet}")
                    if len(results) >= max_matches:
                        truncated = True
                        break

        if not results:
            output = f"No matches for /{pattern}/ in {base}"
        else:
            output = "\n".join(results)
            output = f"Found {len(results)} match(es) in {len(hit_files)} file(s)\n\n{output}"
            if truncated:
                output += f"\n\n[stopped after {max_matches} matches; narrow the pattern or raise max_matches]"

        return ToolResult(
            title=f"grep /{pattern}/",
            output=output,
            metadata={
                "pattern": pattern,
                "path": str(base),
                "files_searched": files_searched,
                "files_matched": len(hit_files),
                "matches": len(results),
                "truncated": truncated,
            },
        )


__all__ = ["GlobTool", "GrepTool"]
