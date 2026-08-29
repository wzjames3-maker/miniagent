"""Large tool output truncation (a direct port of OpenCode's ``tool/truncate.ts``).

Long tool output is the #1 reason a coding agent runs out of context. Instead of
silently chopping it off, the *full* output is written to disk and the model only
sees a preview plus a pointer, so it can go back with ``read``/``grep``.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

from minicode.storage.paths import truncation_dir

__all__ = ["TruncationResult", "truncate_output", "DEFAULT_MAX_LINES", "DEFAULT_MAX_BYTES"]

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024


@dataclass
class TruncationResult:
    content: str
    truncated: bool
    output_path: str | None = None
    removed: int = 0
    unit: str = "lines"

    @property
    def hint(self) -> str:
        if not self.truncated:
            return ""
        return (
            f"Output truncated ({self.removed} {self.unit}). Full output saved to: {self.output_path}\n"
            "Use grep to search the full content, or read with offset/limit to inspect specific sections."
        )


def _write_full(text: str) -> str:
    directory: Path = truncation_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"tool_{secrets.token_hex(8)}.txt"
    path.write_bytes(text.encode("utf-8"))
    return str(path)


def truncate_output(
    text: str,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    direction: str = "head",
    tool: str = "",
) -> TruncationResult:
    """Return ``text`` unchanged if it fits, otherwise a preview + pointer to disk."""
    lines = text.split("\n")
    total_bytes = len(text.encode("utf-8"))

    if len(lines) <= max_lines and total_bytes <= max_bytes:
        return TruncationResult(content=text, truncated=False)

    out: list[str] = []
    byte_count = 0
    hit_bytes = False

    if direction == "tail":
        for i in range(len(lines) - 1, -1, -1):
            if len(out) >= max_lines:
                break
            size = len(lines[i].encode("utf-8")) + (1 if out else 0)
            if byte_count + size > max_bytes:
                hit_bytes = True
                break
            out.insert(0, lines[i])
            byte_count += size
    else:
        for i, line in enumerate(lines):
            if i >= max_lines:
                break
            size = len(line.encode("utf-8")) + (1 if i else 0)
            if byte_count + size > max_bytes:
                hit_bytes = True
                break
            out.append(line)
            byte_count += size

    removed = (total_bytes - byte_count) if hit_bytes else (len(lines) - len(out))
    unit = "bytes" if hit_bytes else "lines"
    preview = "\n".join(out)
    output_path = _write_full(text)
    prefix = f"[{tool}] " if tool else ""
    marker = f"\n\n...{removed} {unit} truncated...\n\n{prefix}The tool call succeeded but the output was truncated."
    hint = f"Full output saved to: {output_path}\nUse grep to search it, or read with offset/limit."

    content = (
        f"{preview}{marker}\n{hint}"
        if direction == "head"
        else f"...{removed} {unit} truncated...\n\n{hint}\n\n{preview}"
    )
    return TruncationResult(content=content, truncated=True, output_path=output_path, removed=removed, unit=unit)
