"""Permission enforcement: rules + interactive ``ask`` + non-interactive modes."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from minicode.permission.policy import (
    DEFAULT_ACTION,
    Action,
    Decision,
    PermissionDenied,
    PermissionRejected,
    Rule,
    evaluate,
    merge_rulesets,
    ruleset_from_config,
)

__all__ = [
    "AskReply",
    "AskRequest",
    "PermissionManager",
    "PermissionMode",
]


class AskReply(str, Enum):
    """What the user answered to an ``ask`` prompt."""

    ONCE = "once"
    ALWAYS = "always"
    REJECT = "reject"


class PermissionMode(str, Enum):
    #: rules are honoured, everything unmatched falls back to ``ask``
    DEFAULT = "default"
    #: everything that is not explicitly denied is allowed (a.k.a. yolo mode)
    AUTO = "auto"


@dataclass
class AskRequest:
    id: str
    permission: str
    patterns: list[str]
    tool: str = ""
    session_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        targets = ", ".join(self.patterns) if self.patterns else "*"
        who = f" ({self.tool})" if self.tool else ""
        return f"{self.permission}{who}: {targets}"


#: A callable that presents ``request`` to the user and returns the answer.
#: May return a bare :class:`AskReply` or a ``(reply, feedback)`` tuple.
AskCallback = Callable[[AskRequest], "AskReply | tuple[AskReply, str] | str"]


class PermissionManager:
    """Evaluates rulesets and (if needed) asks the user.

    The manager is intentionally standalone: it knows nothing about tools,
    sessions or the TUI. The TUI injects an :data:`AskCallback`.
    """

    def __init__(
        self,
        ruleset: Iterable[Rule] | Mapping[str, Any] | None = None,
        *,
        ask_callback: AskCallback | None = None,
        mode: PermissionMode | str = PermissionMode.DEFAULT,
        non_interactive: bool = False,
    ):
        if isinstance(ruleset, Mapping):
            ruleset = ruleset_from_config(ruleset)
        self.config_ruleset: list[Rule] = list(ruleset or [])
        self.approved_ruleset: list[Rule] = []
        self.ask_callback = ask_callback
        self.mode = PermissionMode(mode)
        #: set when no TTY / callback is available
        self.non_interactive = non_interactive
        self.history: list[tuple[AskRequest, AskReply]] = []

    # ------------------------------------------------------------------ #
    # configuration
    # ------------------------------------------------------------------ #
    def add_rules(self, rules: Iterable[Rule]) -> None:
        self.config_ruleset.extend(rules)

    def approve(self, permission: str, pattern: str = "*", action: Action | str = Action.ALLOW) -> None:
        """Persist an approval for the lifetime of this manager (e.g. "always")."""
        self.approved_ruleset.append(Rule(permission=permission, pattern=pattern, action=Action(action)))

    def reset_approvals(self) -> None:
        self.approved_ruleset.clear()

    @property
    def ruleset(self) -> list[Rule]:
        return merge_rulesets(self.config_ruleset, self.approved_ruleset)

    # ------------------------------------------------------------------ #
    # evaluation
    # ------------------------------------------------------------------ #
    def evaluate(self, permission: str, pattern: str) -> Rule:
        return evaluate(permission, pattern, self.config_ruleset, self.approved_ruleset)

    def check(
        self,
        permission: str,
        patterns: Sequence[str] | str,
        *,
        tool: str = "",
        metadata: dict[str, Any] | None = None,
        session_id: str = "",
    ) -> Decision:
        """Return a :class:`Decision`, raising on denial / rejection.

        Raises:
            PermissionDenied: an explicit ``deny`` rule matched.
            PermissionRejected: the user rejected, or no prompt was available.
        """
        pattern_list = [patterns] if isinstance(patterns, str) else list(patterns)
        if not pattern_list:
            pattern_list = ["*"]

        if self.mode is PermissionMode.AUTO:
            for pattern in pattern_list:
                rule = self.evaluate(permission, pattern)
                if rule.action is Action.DENY:
                    raise PermissionDenied(permission, pattern_list, [rule])
            return Decision(Action.ALLOW, permission, pattern_list)

        needs_ask = False
        denied_by: list[Rule] = []
        for pattern in pattern_list:
            rule = self.evaluate(permission, pattern)
            if rule.action is Action.DENY:
                denied_by.append(rule)
            elif rule.action is Action.ASK:
                needs_ask = True
        if denied_by:
            raise PermissionDenied(permission, pattern_list, denied_by)
        if not needs_ask:
            return Decision(Action.ALLOW, permission, pattern_list)

        request = AskRequest(
            id=secrets.token_hex(6),
            permission=permission,
            patterns=pattern_list,
            tool=tool,
            session_id=session_id,
            metadata=dict(metadata or {}),
        )
        reply, feedback = self._ask(request)
        self.history.append((request, reply))
        if reply is AskReply.REJECT:
            raise PermissionRejected(permission, pattern_list, feedback)
        if reply is AskReply.ALWAYS:
            for pattern in pattern_list:
                self.approve(permission, pattern)
        return Decision(Action.ALLOW, permission, pattern_list, feedback=feedback)

    def _ask(self, request: AskRequest) -> tuple[AskReply, str]:
        if self.ask_callback is None or self.non_interactive:
            # No way to ask -> fail closed. Callers that want unattended runs
            # should use mode="auto" (``--yolo``).
            raise PermissionRejected(
                request.permission,
                request.patterns,
                "No interactive prompt available; refusing. Re-run with --yolo to auto-approve, "
                "or add a permission rule to the config.",
            )
        raw = self.ask_callback(request)
        feedback = ""
        if isinstance(raw, tuple):
            raw, feedback = raw
        return AskReply(raw), feedback or ""

    # ------------------------------------------------------------------ #
    # serialization
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "non_interactive": self.non_interactive,
            "config": [r.to_dict() for r in self.config_ruleset],
            "approved": [r.to_dict() for r in self.approved_ruleset],
        }


def default_ruleset() -> list[Rule]:
    """Sensible defaults: reads are free, edits and shell need confirmation."""
    return [
        Rule(permission="read", pattern="*", action=Action.ALLOW),
        Rule(permission="glob", pattern="*", action=Action.ALLOW),
        Rule(permission="grep", pattern="*", action=Action.ALLOW),
        Rule(permission="edit", pattern="*", action=Action.ASK),
        Rule(permission="write", pattern="*", action=Action.ASK),
        Rule(permission="apply_patch", pattern="*", action=Action.ASK),
        Rule(permission="delete", pattern="*", action=Action.ASK),
        Rule(permission="bash", pattern="*", action=Action.ASK),
        Rule(permission="*", pattern="*", action=DEFAULT_ACTION),
    ]
