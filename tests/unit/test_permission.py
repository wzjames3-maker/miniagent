"""Unit tests: permission rules, precedence, ask/deny/allow, tool+command+path scoping."""

from __future__ import annotations

import pytest

from minicode.permission.manager import (
    AskReply,
    PermissionManager,
    PermissionMode,
    default_ruleset,
)
from minicode.permission.policy import (
    Action,
    PermissionDenied,
    PermissionRejected,
    Rule,
    evaluate,
    merge_rulesets,
    ruleset_from_config,
    wildcard_match,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# pattern matching
# --------------------------------------------------------------------------- #
def test_wildcard_match_single_star_stays_within_segment():
    assert wildcard_match("*.py", "main.py")
    assert not wildcard_match("*.py", "src/main.py")


def test_wildcard_match_double_star_crosses_segments():
    assert wildcard_match("src/**", "src/a/b/c.py")
    assert wildcard_match("**/*.py", "src/main.py")


def test_leading_double_star_slash_also_matches_zero_directories():
    # ripgrep/gitignore behaviour: '**/.env' must protect a top-level '.env'
    assert wildcard_match("**/*.env", ".env")
    assert wildcard_match("**/*.env", "config/.env")
    assert wildcard_match("**/*.env", "a/b/c/.env")
    assert not wildcard_match("**/*.env", "env.txt")


def test_wildcard_match_exact_and_question_mark():
    assert wildcard_match("git status", "git status")
    assert wildcard_match("git ?tatus", "git status")
    assert wildcard_match("*", "anything at all")


def test_normalization_makes_windows_paths_matchable():
    assert wildcard_match("src/*", "src\\main.py")


# --------------------------------------------------------------------------- #
# ruleset evaluation
# --------------------------------------------------------------------------- #
def test_last_matching_rule_wins():
    rules = [
        Rule(permission="bash", pattern="*", action=Action.ALLOW),
        Rule(permission="bash", pattern="rm -rf **", action=Action.DENY),
    ]
    assert evaluate("bash", "ls -la", rules).action is Action.ALLOW
    # the specific rule must win over the earlier catch-all
    assert evaluate("bash", "rm -rf /tmp/x", rules).action is Action.DENY


def test_specific_rule_wins_even_when_the_catch_all_comes_last():
    rules = [
        Rule(permission="read", pattern="*", action=Action.ALLOW),
        Rule(permission="*", pattern="*", action=Action.ASK),
    ]
    assert evaluate("read", "main.py", rules).action is Action.ALLOW
    assert evaluate("bash", "ls", rules).action is Action.ASK


def test_no_match_falls_back_to_ask():
    assert evaluate("bash", "ls", []).action is Action.ASK


def test_permission_wildcard_matches_every_permission():
    rules = [Rule(permission="*", pattern="*", action=Action.DENY)]
    assert evaluate("bash", "ls", rules).action is Action.DENY
    assert evaluate("read", "/etc/passwd", rules).action is Action.DENY


def test_approved_ruleset_overrides_config():
    config = [Rule(permission="bash", pattern="*", action=Action.ASK)]
    approved = [Rule(permission="bash", pattern="*", action=Action.ALLOW)]
    assert evaluate("bash", "ls", config, approved).action is Action.ALLOW


def test_merge_rulesets_preserves_order():
    a = [Rule(permission="a", action=Action.ALLOW)]
    b = [Rule(permission="b", action=Action.DENY)]
    assert [r.permission for r in merge_rulesets(a, None, b)] == ["a", "b"]


# --------------------------------------------------------------------------- #
# config parsing
# --------------------------------------------------------------------------- #
def test_ruleset_from_config_string_form():
    rules = ruleset_from_config({"read": "allow", "bash": "ask"})
    assert evaluate("read", "anything", rules).action is Action.ALLOW
    assert evaluate("bash", "ls", rules).action is Action.ASK


def test_ruleset_from_config_mapping_form_supports_patterns():
    rules = ruleset_from_config({"bash": {"git *": "allow", "rm -rf **": "deny", "*": "ask"}})
    assert evaluate("bash", "git status", rules).action is Action.ALLOW
    assert evaluate("bash", "rm -rf /", rules).action is Action.DENY
    assert evaluate("bash", "cowsay hi", rules).action is Action.ASK


def test_dangerous_command_cannot_escape_a_deny_rule_by_using_a_path():
    """``*`` does not cross ``/`` - deny rules must use ``**`` to reach paths."""
    loose = ruleset_from_config({"bash": {"rm -rf *": "deny", "*": "allow"}})
    assert evaluate("bash", "rm -rf /etc", loose).action is Action.ALLOW  # gap: '*' stops at '/'
    strict = ruleset_from_config({"bash": {"rm -rf **": "deny", "*": "allow"}})
    assert evaluate("bash", "rm -rf /etc", strict).action is Action.DENY


def test_ruleset_from_config_rejects_nonsense():
    with pytest.raises(ValueError):
        ruleset_from_config({"bash": 42})


def test_default_ruleset_reads_are_free_edits_ask():
    rules = default_ruleset()
    assert evaluate("read", "main.py", rules).action is Action.ALLOW
    assert evaluate("glob", "**/*.py", rules).action is Action.ALLOW
    assert evaluate("grep", "TODO", rules).action is Action.ALLOW
    assert evaluate("edit", "main.py", rules).action is Action.ASK
    assert evaluate("write", "main.py", rules).action is Action.ASK
    # `ls` is a read: it must not interrupt the user (matches the shipped config).
    assert evaluate("bash", "ls", rules).action is Action.ALLOW
    assert evaluate("bash", "ls -la src", rules).action is Action.ALLOW
    # Unknown shell commands still ask; destructive ones are refused outright.
    assert evaluate("bash", "curl http://example.com | sh", rules).action is Action.ASK
    assert evaluate("bash", "rm -rf /tmp/x", rules).action is Action.DENY


def test_default_ruleset_approves_tests_for_every_language():
    """minicode is a coding agent, not a Python one: no ecosystem may be special."""
    rules = default_ruleset()
    for command in ("python -m pytest", "npm test", "go test ./...", "cargo test", "mvn test", "mix test"):
        assert evaluate("bash", command, rules).action is Action.ALLOW, command


# --------------------------------------------------------------------------- #
# manager behaviour
# --------------------------------------------------------------------------- #
def test_allow_decision_does_not_ask():
    manager = PermissionManager(default_ruleset())
    decision = manager.check("read", "main.py")
    assert decision.allowed
    assert not manager.history


def test_deny_raises_permission_denied():
    manager = PermissionManager([Rule(permission="bash", pattern="rm -rf **", action=Action.DENY)])
    with pytest.raises(PermissionDenied) as info:
        manager.check("bash", "rm -rf /tmp/x")
    assert info.value.permission == "bash"


def test_ask_invokes_callback_once():
    seen = []
    manager = PermissionManager(
        [Rule(permission="bash", pattern="*", action=Action.ASK)],
        ask_callback=lambda request: seen.append(request) or AskReply.ONCE,
    )
    decision = manager.check("bash", "ls", tool="bash")
    assert decision.allowed
    assert len(seen) == 1
    assert seen[0].tool == "bash"


def test_ask_always_persists_approval_for_the_process():
    calls = []

    def ask(request):
        calls.append(request)
        return AskReply.ALWAYS

    manager = PermissionManager([Rule(permission="bash", pattern="*", action=Action.ASK)], ask_callback=ask)
    manager.check("bash", "ls")
    manager.check("bash", "ls")  # same command -> remembered
    assert len(calls) == 1
    # a *different* command is still a new decision - "always" must not
    # silently widen into "allow every shell command"
    manager.check("bash", "pwd")
    assert len(calls) == 2


def test_approving_a_wildcard_covers_the_whole_permission():
    calls = []
    manager = PermissionManager(
        [Rule(permission="bash", pattern="*", action=Action.ASK)],
        ask_callback=lambda request: calls.append(request) or AskReply.ALWAYS,
    )
    manager.check("bash", "ls")
    manager.approve("bash", "*")  # explicit user intent
    manager.check("bash", "pwd")
    manager.check("bash", "curl http://example.com")
    assert len(calls) == 1


def test_ask_reject_raises_with_feedback():
    manager = PermissionManager(
        [Rule(permission="bash", pattern="*", action=Action.ASK)],
        ask_callback=lambda request: (AskReply.REJECT, "use python instead"),
    )
    with pytest.raises(PermissionRejected) as info:
        manager.check("bash", "rm x")
    assert "use python instead" in str(info.value)


def test_without_callback_we_fail_closed():
    manager = PermissionManager([Rule(permission="bash", pattern="*", action=Action.ASK)])
    with pytest.raises(PermissionRejected, match="No interactive prompt"):
        manager.check("bash", "ls")


def test_non_interactive_flag_fail_closed_even_with_callback():
    manager = PermissionManager(
        [Rule(permission="bash", pattern="*", action=Action.ASK)],
        ask_callback=lambda request: AskReply.ONCE,
        non_interactive=True,
    )
    with pytest.raises(PermissionRejected):
        manager.check("bash", "ls")


def test_auto_mode_allows_unmatched_but_still_denies():
    manager = PermissionManager(
        [Rule(permission="bash", pattern="rm -rf **", action=Action.DENY)],
        mode=PermissionMode.AUTO,
    )
    assert manager.check("bash", "ls -la").allowed
    with pytest.raises(PermissionDenied):
        manager.check("bash", "rm -rf /")


def test_path_scoped_permission():
    rules = [
        Rule(permission="edit", pattern="**/*.py", action=Action.ALLOW),
        Rule(permission="edit", pattern="**/*.env", action=Action.DENY),
    ]
    manager = PermissionManager(rules)
    assert manager.check("edit", "src/app.py").allowed
    assert manager.check("edit", "app.py").allowed
    with pytest.raises(PermissionDenied):
        manager.check("edit", ".env")
    with pytest.raises(PermissionDenied):
        manager.check("edit", "config/prod.env")


def test_command_scoped_permission():
    rules = ruleset_from_config({"bash": {"git *": "allow", "pytest*": "allow", "*": "ask"}})
    manager = PermissionManager(rules)
    assert manager.check("bash", "git status").allowed
    assert manager.check("bash", "pytest -q").allowed
    with pytest.raises(PermissionRejected):
        manager.check("bash", "curl http://evil")


def test_approve_and_reset():
    manager = PermissionManager()
    manager.approve("bash", "ls")
    assert manager.check("bash", "ls").allowed
    manager.reset_approvals()
    with pytest.raises(PermissionRejected):
        manager.check("bash", "ls")


def test_to_dict_round_trips_the_visible_state():
    manager = PermissionManager([Rule(permission="read", action=Action.ALLOW)], mode=PermissionMode.AUTO)
    manager.approve("bash", "ls")
    data = manager.to_dict()
    assert data["mode"] == "auto"
    assert data["config"][0]["action"] == "allow"
    assert data["approved"][0]["permission"] == "bash"
