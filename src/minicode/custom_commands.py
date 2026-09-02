"""Custom slash commands: markdown files that become ``/commands``.

OpenCode reads ``.opencode/command/**/*.md``; minicode reads
``.minicode/commands/**/*.md`` from the project plus ``<data>/commands/**/*.md``
from the user's data directory. A file's path *is* its name --
``commands/git/commit.md`` is ``/git/commit`` -- and its body is the prompt
handed to the agent. Writing a command therefore needs no Python, no plugin
API and no restart: drop a file in and it is in the popover.

The module owns four things and nothing else:

* :func:`load_custom_commands` -- scan both roots into :class:`CustomCommand`,
* :class:`CommandStore` -- builtins + custom as one list, which is what the
  popover filters and the dispatcher looks up,
* :func:`render_template` -- fill ``$ARGUMENTS`` / ``$1`` before the run,
* :func:`write_command` -- what ``/command new`` calls.

Execution is deliberately *not* here. A custom command is just a prompt, and
running a prompt is the agent's job (see ``InteractiveApp.run_custom``), so a
command file can never reach into the process the way a plugin could.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from minicode.cli.commands import COMMANDS, SlashCommand, match_commands
from minicode.storage import paths

__all__ = [
    "COMMANDS_DIR_NAME",
    "CustomCommand",
    "CommandStore",
    "load_custom_commands",
    "normalize_name",
    "parse_command_file",
    "render_template",
    "write_command",
]

COMMANDS_DIR_NAME = "commands"

#: Valid command names: ``review``, ``git/commit``, ``fix-tests``. No leading
#: slash (that is the trigger, not the name), no traversal, no extension.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_/-]*$")

#: ``$ARGUMENTS`` or ``$1``..``$9``.
_PLACEHOLDER_RE = re.compile(r"\$ARGUMENTS\b|\$(\d)", re.IGNORECASE)

_SCAFFOLD = """---
description: {description}
argument-hint: [args]
---

$ARGUMENTS
"""


def normalize_name(name: str) -> str:
    """Turn anything the user might type into a command name.

    ``/git/commit``, ``git/commit`` and ``git/commit.md`` are the same name;
    a leading ``/`` is the trigger, not part of the name. Raises
    :class:`ValueError` for anything that is not a safe relative path, because
    these names become file paths.
    """
    cleaned = (name or "").strip().strip("/")
    if cleaned.lower().endswith(".md"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.replace("\\", "/").strip("/")
    cleaned = re.sub(r"/{2,}", "/", cleaned)
    if not _NAME_RE.match(cleaned):
        raise ValueError(
            f"invalid command name {name!r}: use letters, digits, '-', '_' and '/' "
            "(for example 'review' or 'git/commit')"
        )
    if any(part in {".", ".."} for part in cleaned.split("/")):
        raise ValueError(f"invalid command name {name!r}: path traversal is not allowed")
    return cleaned


# --------------------------------------------------------------------------- #
# one command file
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CustomCommand:
    """A command that lives in a markdown file instead of in the registry."""

    name: str
    description: str
    template: str
    path: Path
    #: ``"project"`` or ``"user"`` -- which root the file came from.
    source: str = "project"
    argument_hint: str = ""
    model: str = ""

    @property
    def trigger(self) -> str:
        """/``name`` -- what the user types."""
        return "/" + self.name

    @property
    def title(self) -> str:
        """What the popover shows, including any argument hint."""
        return self.trigger + (f" {self.argument_hint}" if self.argument_hint else "")

    def as_command(self) -> SlashCommand:
        """The registry view of this file.

        ``handler`` stays ``None`` and ``type`` is ``"custom"``: the dispatcher
        recognises those and sends the rendered template to the agent instead of
        calling a Python function.
        """
        return SlashCommand(
            trigger=self.trigger,
            title=self.title,
            description=self.description or f"custom command ({self.source})",
            handler=None,
            keybind="",
            type="custom",
            source=self.source,
            path=self.path,
            template=self.template,
        )


def parse_command_file(path: Path, *, source: str = "project") -> CustomCommand:
    """Read one ``.md`` file into a :class:`CustomCommand`.

    A ``---`` fenced block at the top is frontmatter; everything after it is the
    prompt template. Malformed frontmatter is a warning, not a crash -- the file
    still loads as a command with just its name, because a command that refuses
    to load is worse than one whose description is missing.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    frontmatter, template = _split_frontmatter(raw)
    return CustomCommand(
        name=_name_from_path(path),
        description=str(frontmatter.get("description") or "").strip(),
        template=template.strip(),
        path=path,
        source=source,
        argument_hint=str(frontmatter.get("argument-hint") or frontmatter.get("argument_hint") or "").strip(),
        model=str(frontmatter.get("model") or "").strip(),
    )


def _name_from_path(path: Path) -> str:
    """``.../commands/git/commit.md`` -> ``git/commit`` (POSIX separators)."""
    return path.with_suffix("").as_posix().rsplit(f"/{COMMANDS_DIR_NAME}/", 1)[-1]


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a leading ``---`` block from the body. No fence, no frontmatter."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index in range(1, len(lines)):
        if lines[index].strip() in {"---", "..."}:
            try:
                data = yaml.safe_load("".join(lines[1:index])) or {}
            except yaml.YAMLError:
                data = {}
            return (data if isinstance(data, dict) else {}), "".join(lines[index + 1 :])
    return {}, text


# --------------------------------------------------------------------------- #
# scanning both roots
# --------------------------------------------------------------------------- #
def load_custom_commands(
    cwd: str | Path | None = None, *, data_dir: Path | None = None
) -> list[CustomCommand]:
    """Every command file, project first.

    Project files shadow user files of the same name: a repository gets to say
    what ``/review`` means without the user having to delete their own.
    """
    roots: tuple[tuple[str, Path], ...] = (
        ("project", Path(cwd or Path.cwd()) / paths.PROJECT_DIR_NAME / COMMANDS_DIR_NAME),
        ("user", Path(data_dir or paths.data_dir()) / COMMANDS_DIR_NAME),
    )
    found: dict[str, CustomCommand] = {}
    for source, root in roots:
        files = sorted(root.rglob("*.md")) if root.is_dir() else []
        for file in files:
            if file.name.startswith((".", "_")):
                continue
            name = _name_from_path(file)
            if name in found:
                continue
            try:
                found[name] = parse_command_file(file, source=source)
            except OSError:  # pragma: no cover - unreadable file, skip it
                continue
    return [found[name] for name in sorted(found)]


class CommandStore:
    """Builtin commands plus whatever is on disk, presented as one list.

    The popover, the dispatcher and ``/help`` all read from here, so a command
    you drop into ``.minicode/commands/`` shows up everywhere at once -- and a
    command you delete disappears without the registry noticing the difference.

    Scanning is cached on ``(mtime, size)`` per file, so :meth:`refresh` is
    cheap enough to call before every lookup; commands are edited by hand (or by
    the TUI manager) while minicode is running, and the popover has to notice.
    """

    def __init__(
        self,
        cwd: str | Path | None = None,
        *,
        data_dir: Path | None = None,
        refresh: bool = True,
    ) -> None:
        self.cwd = Path(cwd) if cwd else Path.cwd()
        self.data_dir = Path(data_dir) if data_dir else paths.data_dir()
        self._custom: list[CustomCommand] = []
        self._fingerprints: dict[Path, tuple[float, int]] = {}
        # A store built with refresh=False has never looked at the disk, and is
        # left alone until it does: builtins only, no filesystem access. That is
        # the state a test double wants.
        self._scanned = False
        if refresh:
            self.refresh()

    def _scan_if_scanned(self) -> None:
        if self._scanned:
            self.refresh()

    # ---------------------------------------------------------------- #
    @property
    def custom(self) -> list[CustomCommand]:
        """Just the file-backed commands."""
        return list(self._custom)

    @property
    def all(self) -> list[SlashCommand]:
        """Builtins first, then custom -- the order the popover shows."""
        return [*COMMANDS, *(command.as_command() for command in self._custom)]

    def refresh(self) -> list[CustomCommand]:
        """Re-scan, but only re-read files that actually changed."""
        self._scanned = True
        commands = load_custom_commands(self.cwd, data_dir=self.data_dir)
        fingerprints: dict[Path, tuple[float, int]] = {}
        for command in commands:
            try:
                stat = command.path.stat()
            except OSError:  # pragma: no cover - vanished between scan and stat
                continue
            fingerprints[command.path] = (stat.st_mtime, stat.st_size)
        if fingerprints != self._fingerprints:
            self._custom = commands
            self._fingerprints = fingerprints
        return self.custom

    def find(self, name: str) -> SlashCommand | None:
        """Look up a trigger or alias, custom commands included."""
        self._scan_if_scanned()
        needle = name.lower()
        for command in self.all:
            if command.trigger.lower() == needle:
                return command
        for command in self.all:
            if needle in {alias.lower() for alias in _aliases_of(command)}:
                return command
        return None

    def custom_for(self, trigger: str) -> CustomCommand | None:
        """The file behind a custom trigger, or ``None`` for a builtin."""
        for command in self.custom:
            if command.trigger.lower() == trigger.lower():
                return command
        return None

    def match(self, query: str) -> list[SlashCommand]:
        """Filter everything the popover should offer, best match first."""
        self._scan_if_scanned()
        return match_commands(query, self.all)


def _aliases_of(command: SlashCommand) -> Iterable[str]:
    return getattr(command, "aliases", ())


# --------------------------------------------------------------------------- #
# templating
# --------------------------------------------------------------------------- #
def render_template(template: str, args: Sequence[str] = ()) -> str:
    """Fill ``$ARGUMENTS`` and ``$1``..``$9``.

    ``$ARGUMENTS`` is the whole tail, ``$1`` the first word. Missing arguments
    collapse to an empty string rather than leaving a literal ``$2`` behind for
    the model to puzzle over.

    A template with *no* placeholder still gets the arguments appended, on its
    own paragraph: silently swallowing what the user typed after the command is
    the one failure mode that makes a command look broken.
    """
    arguments = [str(arg) for arg in args]
    joined = " ".join(arguments)

    def replace(match: re.Match[str]) -> str:
        if match.group(1):
            index = int(match.group(1)) - 1
            return arguments[index] if 0 <= index < len(arguments) else ""
        return joined

    rendered = _PLACEHOLDER_RE.sub(replace, template or "").strip()
    if arguments and not _PLACEHOLDER_RE.search(template or ""):
        rendered = f"{rendered}\n\n{joined}" if rendered else joined
    return rendered


# --------------------------------------------------------------------------- #
# writing (the TUI manager's backend)
# --------------------------------------------------------------------------- #
def command_path(name: str, *, cwd: str | Path | None = None, scope: str = "project") -> Path:
    """Where a command file would live. Does not create it."""
    root = (
        Path(cwd or Path.cwd()) / paths.PROJECT_DIR_NAME / COMMANDS_DIR_NAME
        if scope == "project"
        else paths.data_dir() / COMMANDS_DIR_NAME
    )
    return root / f"{normalize_name(name)}.md"


def write_command(
    name: str,
    template: str,
    *,
    description: str = "",
    argument_hint: str = "",
    model: str = "",
    cwd: str | Path | None = None,
    scope: str = "project",
) -> Path:
    """Write (or overwrite) a command file and return its path."""
    path = command_path(name, cwd=cwd, scope=scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(command_file_text(template, description=description, argument_hint=argument_hint, model=model), encoding="utf-8")
    return path


def command_file_text(
    template: str, *, description: str = "", argument_hint: str = "", model: str = ""
) -> str:
    """Render a command file: frontmatter (only the fields set) plus the body."""
    fields = [
        f"description: {_scalar(description)}",
        f"argument-hint: {_scalar(argument_hint)}" if argument_hint else "",
        f"model: {_scalar(model)}" if model else "",
    ]
    body = (template or "").strip() or "$ARGUMENTS"
    frontmatter = "\n".join(field for field in fields if field)
    if not frontmatter:
        return body + "\n"
    return f"---\n{frontmatter}\n---\n\n{body}\n"


def scaffold(name: str) -> str:
    """Starting text for a brand-new command, so the file is never empty."""
    return _SCAFFOLD.format(description=f"what /{normalize_name(name)} does")


def _scalar(value: str) -> str:
    """Quote a frontmatter scalar.

    ``json.dumps`` is used because every JSON scalar is also valid YAML, which
    means a description containing ``:`` or ``#`` round-trips without a
    hand-rolled escaping table.
    """
    return json.dumps(str(value), ensure_ascii=False)
