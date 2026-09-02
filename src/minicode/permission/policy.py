"""Permission rule model + evaluation (mirrors OpenCode's ``permission`` module).

A ruleset is a flat list of rules; the *last* matching rule wins, and when no
rule matches the default action is ``ask``. Evaluation is::

    evaluate(permission="bash", pattern="git status", ruleset)

Rules can come from three places (in increasing precedence):

1. the config file  (``permission:`` section)
2. rules approved for the current process/session ("always allow")
3. CLI overrides (``--permission bash=allow``)

Permission keys are tool-level (``read``, ``edit``, ``bash``, ``glob``, ...) and
patterns are values within that permission: a path for file tools, the command
line for ``bash``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

from minicode.errors import PermissionDeniedError, PermissionRejectedError

__all__ = [
    "Action",
    "Rule",
    "DEFAULT_ACTION",
    "PermissionDenied",
    "PermissionDeniedError",
    "PermissionRejected",
    "PermissionRejectedError",
    "wildcard_match",
    "normalize_pattern",
    "evaluate",
    "ruleset_from_config",
    "merge_rulesets",
]


class Action(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


DEFAULT_ACTION = Action.ASK


class PermissionDenied(PermissionDeniedError):
    """Raised when a rule explicitly denies an operation."""

    def __init__(self, permission: str, patterns: Sequence[str], matching: Sequence[Rule] = ()):
        super().__init__(f"Permission denied for {permission}: {', '.join(patterns) or '*'}")
        self.permission = permission
        self.patterns = list(patterns)
        self.matching = list(matching)


class PermissionRejected(PermissionRejectedError):
    """Raised when the user rejects an ``ask`` prompt (optionally with feedback)."""

    def __init__(self, permission: str, patterns: Sequence[str], feedback: str = ""):
        msg = f"User rejected {permission}: {', '.join(patterns) or '*'}"
        if feedback:
            msg += f"\nUser feedback: {feedback}"
        super().__init__(msg)
        self.permission = permission
        self.patterns = list(patterns)
        self.feedback = feedback


@dataclass(frozen=True)
class Rule:
    permission: str
    pattern: str = "*"
    action: Action = DEFAULT_ACTION

    def __post_init__(self) -> None:
        if isinstance(self.action, str):
            object.__setattr__(self, "action", Action(self.action))

    def to_dict(self) -> dict[str, str]:
        return {"permission": self.permission, "pattern": self.pattern, "action": self.action.value}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Rule:
        return cls(
            permission=str(data["permission"]), pattern=str(data.get("pattern", "*")), action=Action(data["action"])
        )


# --------------------------------------------------------------------------- #
# pattern matching
# --------------------------------------------------------------------------- #


def normalize_pattern(value: str) -> str:
    """Normalize a value so that rules work across platforms."""
    return str(value).replace("\\", "/").strip()


@lru_cache(maxsize=4096)
def _compile(pattern: str) -> re.Pattern[str]:
    """Translate a wildcard pattern into a regex.

    ``**`` crosses path separators, ``*`` does not (standard glob semantics).
    A leading ``**/`` additionally matches *zero* directories, so ``**/*.env``
    matches ``.env`` in the current directory as well as ``a/b/.env`` - the
    behaviour ripgrep/gitignore users already expect.
    """
    out: list[str] = ["^"]
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i + 1 : i + 2] == "*":
                if pattern[i + 2 : i + 3] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def wildcard_match(pattern: str, value: str) -> bool:
    """Case-sensitive wildcard match.

    ``*`` matches within a path segment, ``**`` across segments, and a leading
    ``**/`` also matches zero directories. Because ``*`` does not cross ``/``,
    command rules that must reach into paths need ``**`` (``rm -rf **``), and
    anything unmatched falls back to ``ask`` - never to a silent allow.
    """
    if pattern == "*" or pattern == "**":
        return True
    if pattern == value:
        return True
    return _compile(normalize_pattern(pattern)).match(normalize_pattern(value)) is not None


def _specificity(rule: Rule) -> tuple[int, int, int, int]:
    """How specific a rule is - higher wins.

    Plain "last match wins" is wrong for permission rules: a catch-all
    ``*: ask`` sitting at the end of the ruleset (which every sane config has)
    would silently override ``read: allow`` and force a prompt for every file
    read. Most-specific-wins is what OpenCode does, and it is what users expect:
    a narrow rule should beat a broad one no matter the order.
    """

    def score(value: str) -> int:
        if value in {"*", "**"}:
            return 0
        return 1 if "*" in value or "?" in value else 2

    normalized = normalize_pattern(rule.pattern)
    return (score(rule.permission), score(rule.pattern), len(normalized), len(rule.permission))


def evaluate(permission: str, pattern: str, *rulesets: Iterable[Rule] | None) -> Rule:
    """Return the effective rule for ``permission`` / ``pattern``.

    Every matching rule is considered; the most specific one wins, and ties are
    broken by order (later rulesets / later rules override earlier ones), which
    is how "always allow" approvals override the config.
    """
    match: Rule | None = None
    match_score: tuple[int, int, int, int] | None = None
    for ruleset in rulesets:
        if not ruleset:
            continue
        for rule in ruleset:
            if not wildcard_match(rule.permission, permission):
                continue
            if not wildcard_match(rule.pattern, pattern):
                continue
            score = _specificity(rule)
            # ``>=`` keeps the last rule on ties -> later rulesets still win
            if match_score is None or score >= match_score:
                match, match_score = rule, score
    if match is None:
        return Rule(permission=permission, pattern=pattern, action=DEFAULT_ACTION)
    return match


def merge_rulesets(*rulesets: Iterable[Rule] | None) -> list[Rule]:
    out: list[Rule] = []
    for ruleset in rulesets:
        if ruleset:
            out.extend(ruleset)
    return out


def _expand_home(pattern: str) -> str:
    """Expand a leading ``~/`` or ``$HOME/`` in a config pattern to the home dir."""
    home = str(Path.home()).replace("\\", "/")
    if pattern in {"~", "$HOME"}:
        return home
    for prefix in ("~/", "$HOME/"):
        if pattern.startswith(prefix):
            return home + pattern[len(prefix) - 1 :]
    return pattern


def ruleset_from_config(config: Mapping[str, Any] | None) -> list[Rule]:
    """Build a ruleset from the ``permission:`` section of the config file.

    Two shapes are supported, exactly like OpenCode::

        permission:
          read: allow                    # permission -> action, pattern "*"
          bash:
            "git *": allow               # permission -> {pattern: action}
            "rm -rf *": deny
            "*": ask
          "*": ask
    """
    rules: list[Rule] = []
    if not config:
        return rules
    for key, value in config.items():
        permission = str(key)
        if isinstance(value, str):
            rules.append(Rule(permission=permission, pattern="*", action=Action(value)))
            continue
        if isinstance(value, Mapping):
            for pattern, action in value.items():
                rules.append(Rule(permission=permission, pattern=_expand_home(str(pattern)), action=Action(action)))
            continue
        raise ValueError(f"Invalid permission config for {permission!r}: {value!r}")
    return rules


@dataclass
class Decision:
    """Outcome of a permission check."""

    action: Action
    permission: str
    patterns: list[str] = field(default_factory=list)
    feedback: str = ""

    @property
    def allowed(self) -> bool:
        return self.action is Action.ALLOW
