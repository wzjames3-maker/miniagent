"""``glob`` and ``grep`` - code search, ripgrep-accelerated.

**grep** runs `ripgrep <https://github.com/BurntSushi/ripgrep>`_ when it is on
the ``PATH`` (the same choice pydantic-ai and opencode make: a native matcher
is orders of magnitude faster on big trees and respects ``.gitignore``), and
falls back to a pure-Python implementation when it is not - so the project
stays portable and dependency-light. Set ``MINICODE_NO_RIPGREP=1`` to force
the fallback. Both paths obey the *same* contract: hidden dirs and vendor
trees skipped (see :data:`minicode.storage.paths.SKIP_DIR_NAMES`), binary
files skipped, newest files first, capped results.

**glob** stays on the stdlib walker: pydantic-ai's own glob shim makes the
same call - ``pathlib`` recursion is already C-speed, the cost is the mtime
sort, and ripgrep's glob language differs from Python's (``*.py`` matches at
any depth in rg but only the top level here, so a switch would silently change
what the model sees).
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from minicode.storage.paths import SKIP_DIR_NAMES, is_hidden
from minicode.tools.base import BaseTool, ToolContext, ToolError, ToolResult

#: Hard caps so a greedy search can never blow up the context.
MAX_GLOB_RESULTS = 500
MAX_GREP_MATCHES = 200
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_LINE_CHARS = 500

#: How long a ripgrep run may take before falling back to the Python search.
RG_TIMEOUT = 60.0

#: rg's exit codes: 0 = matches found, 1 = no matches, >=2 = real error.
_RG_NO_MATCH_EXIT = 1

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

#: ``path:line:text`` - non-greedy so a ":" inside the path (Windows drive
#: letters, colon-heavy names) does not split the wrong colon.
_RG_LINE_RE = re.compile(r"^(.+?):(\d+):(.*)$")


def _abs_path(base: Path, rel: str) -> str:
    """Rebuild an absolute path from rg's cwd-relative output (``./x/y.py``)."""
    if rel.startswith("./"):
        rel = rel[2:]
    if os.path.isabs(rel):
        return rel
    return str(base / rel)


def _ripgrep_available() -> bool:
    """Whether the ripgrep fast path may run."""
    if os.environ.get("MINICODE_NO_RIPGREP") == "1":
        return False
    return shutil.which("rg") is not None


@lru_cache(maxsize=1)
def _rg_excludes() -> tuple[str, ...]:
    """Exclude globs mirroring the Python walker's skip rules exactly."""
    dirs = tuple(f"!{name}/**" for name in SKIP_DIR_NAMES)
    files = tuple(f"!*{suffix}" for suffix in _BINARY_SKIP_SUFFIXES)
    return dirs + files


def rg_command(
    pattern: str,
    base: Path,
    *,
    include: str | None = None,
    case_sensitive: bool = False,
) -> list[str]:
    """Build the ripgrep argv.

    rg matches ``-g`` globs against paths **relative to the cwd**, so the
    caller must run it with ``cwd=base`` and search ``.`` (rg_command alone has
    no way to know; ``run_rg`` takes the cwd). ``--hidden`` because the Python
    walker only skips the *listed* names (``SKIP_DIR_NAMES``), not every
    dot-file; the negated globs below then apply the same skip list.
    """
    cmd = [
        "rg",
        "--line-number",
        "--no-heading",
        "--color",
        "never",
        "--hidden",
        "--max-filesize",
        f"{MAX_FILE_BYTES // (1024 * 1024)}M",
    ]
    if not case_sensitive:
        cmd.append("-i")
    for exclude in _rg_excludes():
        cmd += ["-g", exclude]
    if include:
        cmd += ["-g", include]
    cmd += ["-e", pattern, "--", "."]
    return cmd


def run_rg(
    args: Sequence[str], max_lines: int, timeout: float = RG_TIMEOUT, cwd: str | Path | None = None
) -> tuple[list[str], int, str, bool]:
    """Stream ripgrep, stopping after ``max_lines`` matches.

    Returns ``(lines, exit_code, stderr, timed_out)``; ``exit_code`` is ``-1``
    when the timeout killed the process. ``FileNotFoundError`` propagates -
    the caller treats it as "no ripgrep" and falls back.
    """
    proc = subprocess.Popen(
        list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=str(cwd) if cwd is not None else None,
    )
    lines: list[str] = []
    deadline = time.monotonic() + timeout
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            lines.append(raw.rstrip("\n"))
            if len(lines) >= max_lines:
                proc.kill()
                break
        remaining = max(0.0, deadline - time.monotonic())
        proc.wait(timeout=remaining)
        stderr = proc.stderr.read() if proc.stderr is not None else ""
        return lines, proc.returncode or 0, stderr, False
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return lines, -1, "", True


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

        matches.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)
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
        "newest files first, capped at 200 matches. Binary files, hidden and vendored directories "
        "are skipped. Ripgrep is used when available (fast on large repositories)."
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

        # Ripgrep fast path (OpenCode / pydantic-ai-aligned); the pure-Python
        # implementation below is the portable fallback.
        if _ripgrep_available() and base.is_dir():
            result = self._run_with_ripgrep(
                pattern, base, include=include, case_sensitive=case_sensitive, max_matches=max_matches
            )
            if result is not None:
                return result
        return self._run_python(pattern, base, include=include, case_sensitive=case_sensitive, max_matches=max_matches)

    # ------------------------------------------------------------------ #
    # fast path: ripgrep
    # ------------------------------------------------------------------ #
    def _run_with_ripgrep(
        self,
        pattern: str,
        base: Path,
        *,
        include: str | None,
        case_sensitive: bool,
        max_matches: int,
    ) -> ToolResult | None:
        """Run ripgrep and shape its output like the Python path.

        Returns ``None`` when rg failed in a way the fallback can do better
        (timeout, vanished binary, non-regex error) - the caller retries in
        pure Python.
        """
        args = rg_command(pattern, base, include=include, case_sensitive=case_sensitive)
        try:
            lines, exit_code, stderr, timed_out = run_rg(args, max_matches, cwd=base)
        except FileNotFoundError:
            return None

        if exit_code >= 2 or timed_out:
            if "regex parse error" in stderr:
                detail = stderr.strip().splitlines()[0] if stderr.strip() else stderr
                return ToolResult(
                    title=self.name,
                    output="",
                    error=ToolError(
                        code="invalid_regex", message=f"Invalid regular expression {pattern!r}: {detail}"
                    ),
                )
            return None  # anything else: let the Python implementation try

        if exit_code == _RG_NO_MATCH_EXIT:
            return ToolResult(
                title=f"grep /{pattern}/",
                output=f"No matches for /{pattern}/ in {base}",
                metadata={
                    "pattern": pattern,
                    "path": str(base),
                    "engine": "ripgrep",
                    "files_searched": 0,
                    "files_matched": 0,
                    "matches": 0,
                    "truncated": False,
                },
            )

        hit_files: set[str] = set()
        parsed: list[tuple[str, int, str]] = []
        for line in lines:
            match = _RG_LINE_RE.match(line)
            if match is None:  # pragma: no cover - defensive
                continue
            # rg prints paths relative to its cwd (which is `base`), usually
            # with a leading "./"; rebuild the absolute path for the caller.
            abs_path = _abs_path(base, match.group(1))
            hit_files.add(abs_path)
            parsed.append((abs_path, int(match.group(2)), match.group(3)))

        # Newest files first, then line order - same contract as the Python path.
        mtimes: dict[str, float] = {}

        def _mtime(path: str) -> float:
            if path not in mtimes:
                try:
                    mtimes[path] = os.stat(path).st_mtime
                except OSError:
                    mtimes[path] = 0.0
            return mtimes[path]

        parsed.sort(key=lambda item: (-_mtime(item[0]), item[0], item[1]))

        results: list[str] = []
        truncated = len(parsed) >= max_matches
        for path, lineno, raw in parsed:
            if len(results) >= max_matches:
                break
            snippet = raw if len(raw) <= MAX_LINE_CHARS else raw[:MAX_LINE_CHARS] + "..."
            results.append(f"{path}:{lineno}:{snippet}")

        if not results:
            output = f"No matches for /{pattern}/ in {base}"
        else:
            output = f"Found {len(results)} match(es) in {len(hit_files)} file(s)\n\n" + "\n".join(results)
            if truncated:
                output += f"\n\n[stopped after {max_matches} matches; narrow the pattern or raise max_matches]"

        return ToolResult(
            title=f"grep /{pattern}/",
            output=output,
            metadata={
                "pattern": pattern,
                "path": str(base),
                "engine": "ripgrep",
                # rg does not report how many files it searched; matched files
                # is the closest honest number we can produce without a second pass.
                "files_searched": len(hit_files),
                "files_matched": len(hit_files),
                "matches": len(results),
                "truncated": truncated,
            },
        )

    # ------------------------------------------------------------------ #
    # fallback: pure Python (portable, no external binary)
    # ------------------------------------------------------------------ #
    def _run_python(
        self,
        pattern: str,
        base: Path,
        *,
        include: str | None,
        case_sensitive: bool,
        max_matches: int,
    ) -> ToolResult:
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
            files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)

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
                "engine": "python",
                "files_searched": files_searched,
                "files_matched": len(hit_files),
                "matches": len(results),
                "truncated": truncated,
            },
        )


__all__ = ["GlobTool", "GrepTool", "rg_command", "run_rg"]
