"""Unit tests: context management - truncation, pruning, compaction, rebuild."""

from __future__ import annotations

import pytest

from minicode.context.manager import (
    PRUNED_PLACEHOLDER,
    CompactionResult,
    ContextConfig,
    ContextManager,
)
from minicode.context.tokens import content_to_text, estimate_messages_tokens, estimate_tokens

pytestmark = pytest.mark.unit


def _msg(role: str, content: str, **extra) -> dict:
    return {"role": role, "content": content, "extra": extra}


def _tool_msg(content: str, **extra) -> dict:
    return {"role": "tool", "tool_call_id": "c1", "content": content, "extra": extra}


# --------------------------------------------------------------------------- #
# token estimation
# --------------------------------------------------------------------------- #
def test_estimate_tokens_scales_with_length():
    short = estimate_tokens("a" * 100)
    long = estimate_tokens("a" * 1000)
    assert long > short * 5


def test_estimate_tokens_handles_empty_and_none():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0


def test_content_to_text_handles_blocks():
    # multiple text parts are paragraphs, so they are joined with newlines
    content = [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]
    assert content_to_text(content) == "hello\nworld"


def test_content_to_text_handles_mixed_shapes():
    assert content_to_text(None) == ""
    assert content_to_text("plain") == "plain"
    assert content_to_text(["a", "b"]) == "a\nb"
    assert content_to_text(42) == "42"


def test_estimate_messages_tokens_counts_every_message():
    messages = [_msg("system", "s"), _msg("user", "u" * 100), _msg("assistant", "a")]
    assert estimate_messages_tokens(messages) > estimate_tokens("u" * 100)


# --------------------------------------------------------------------------- #
# truncation
# --------------------------------------------------------------------------- #
def test_truncate_tool_output_preserves_full_text_on_disk(tmp_path, monkeypatch):
    from minicode.tools.base import ToolResult

    monkeypatch.setenv("MINICODE_DATA_DIR", str(tmp_path / "data"))
    manager = ContextManager(ContextConfig(tool_output_max_lines=5))
    result = ToolResult(title="bash", output="\n".join(f"line {i}" for i in range(100)))
    manager.truncate_tool_output(result)
    assert result.truncated
    assert result.metadata["truncated"] is True
    assert result.metadata["output_path"]
    from pathlib import Path

    assert Path(result.metadata["output_path"]).read_text(encoding="utf-8").count("line ") == 100


def test_truncate_leaves_short_output_alone():
    from minicode.tools.base import ToolResult

    manager = ContextManager()
    result = ToolResult(title="read", output="tiny")
    manager.truncate_tool_output(result)
    assert not result.truncated
    assert result.output == "tiny"


def test_truncate_counts_its_work():
    from minicode.tools.base import ToolResult

    manager = ContextManager(ContextConfig(tool_output_max_lines=2))
    manager.truncate_tool_output(ToolResult(title="x", output="a\nb\nc\nd"))
    assert manager.truncated_count == 1


# --------------------------------------------------------------------------- #
# pruning
# --------------------------------------------------------------------------- #
def test_prune_replaces_old_tool_output_with_a_placeholder():
    manager = ContextManager(ContextConfig(prune_protect_tokens=10, prune_minimum_tokens=10))
    messages = [
        _msg("user", "u"),
        _msg("assistant", "calling"),
        _tool_msg("X" * 5000),
        _msg("assistant", "again"),
        _tool_msg("Y" * 5000),
        _msg("assistant", "latest"),
        _tool_msg("Z" * 5000),
    ]
    freed = manager.prune_tool_outputs(messages)
    assert freed > 0
    pruned = [m for m in messages if (m.get("extra") or {}).get("pruned")]
    assert pruned
    assert PRUNED_PLACEHOLDER in pruned[0]["content"]
    # the tool *call* history is still intact for the model
    assert any(m["role"] == "assistant" for m in messages)


def test_prune_protects_the_most_recent_tool_output():
    manager = ContextManager(ContextConfig(prune_protect_tokens=100_000, prune_minimum_tokens=10))
    messages = [_msg("user", "u"), _msg("assistant", "a"), _tool_msg("fresh output")]
    assert manager.prune_tool_outputs(messages) == 0
    assert messages[-1]["content"] == "fresh output"


def test_prune_does_nothing_below_the_minimum_savings():
    manager = ContextManager(ContextConfig(prune_protect_tokens=0, prune_minimum_tokens=1_000_000))
    messages = [_msg("user", "u"), _msg("assistant", "a"), _tool_msg("small")]
    assert manager.prune_tool_outputs(messages) == 0
    assert manager.pruned_count == 0


def test_prune_can_be_disabled():
    manager = ContextManager(ContextConfig(prune=False, prune_protect_tokens=1, prune_minimum_tokens=1))
    messages = [_msg("user", "u"), _msg("assistant", "a"), _tool_msg("X" * 5000)]
    assert manager.prune_tool_outputs(messages) == 0


# --------------------------------------------------------------------------- #
# compaction
# --------------------------------------------------------------------------- #
def _long_history(turns: int = 4, filler: int = 4000) -> list[dict]:
    messages = [_msg("system", "You are a coding agent.")]
    for index in range(turns):
        messages.append(_msg("user", f"turn {index} " + "u" * filler))
        messages.append(_msg("assistant", f"working on {index}"))
        messages.append(_tool_msg("o" * filler))
    return messages


def test_compaction_summarises_the_old_turns_and_keeps_the_tail():
    manager = ContextManager(
        ContextConfig(preserve_recent_tokens=200, prune=False, tail_turns=1),
        summarizer=lambda messages: "SUMMARY OF THE PAST",
    )
    messages = _long_history(turns=4)
    before = manager.estimate(messages)
    result = manager.compact(messages)

    assert result.compacted
    assert result.compacted is True
    assert "SUMMARY OF THE PAST" in result.summary
    assert result.after_tokens < before
    # the summary is injected, and the last turn survives verbatim
    compacted = [m for m in result.messages if (m.get("extra") or {}).get("compacted")]
    assert len(compacted) == 1
    assert "turn 3" in result.messages[-3]["content"]
    assert manager.compaction_count == 1


def test_compaction_protects_the_system_message():
    manager = ContextManager(
        ContextConfig(preserve_recent_tokens=200, prune=False, tail_turns=1),
        summarizer=lambda m: "summary",
    )
    result = manager.compact(_long_history())
    assert result.messages[0]["role"] == "system"


def test_compaction_without_a_summarizer_uses_an_extractive_fallback():
    manager = ContextManager(ContextConfig(preserve_recent_tokens=200, prune=False, tail_turns=1))
    result = manager.compact(_long_history())
    assert result.compacted
    assert "turn 0" in result.summary or "user:" in result.summary
    assert any("extractive fallback" in note for note in result.notes)


def test_compaction_survives_a_broken_summarizer():
    def explode(_):
        raise RuntimeError("model is down")

    manager = ContextManager(ContextConfig(preserve_recent_tokens=200, prune=False, tail_turns=1), summarizer=explode)
    result = manager.compact(_long_history())
    assert result.compacted  # must not kill the run


def test_compaction_is_a_noop_with_a_single_turn():
    manager = ContextManager(summarizer=lambda m: "summary")
    messages = [_msg("user", "only one turn")]
    result = manager.compact(messages)
    assert not result.compacted


def test_needs_compaction_honours_the_threshold():
    config = ContextConfig(max_tokens=1000, compact_threshold=0.5)
    manager = ContextManager(config)
    assert not manager.needs_compaction([_msg("user", "short")])
    assert manager.needs_compaction([_msg("user", "x" * 4000)])


def test_needs_compaction_obeys_auto_compact_flag():
    manager = ContextManager(ContextConfig(max_tokens=10, auto_compact=False))
    assert not manager.needs_compaction([_msg("user", "x" * 1000)])


def test_prepare_only_compacts_when_needed():
    manager = ContextManager(ContextConfig(max_tokens=1_000_000), summarizer=lambda m: "s")
    result = manager.prepare([_msg("user", "hi")])
    assert not result.compacted


def test_prepare_compacts_when_over_budget():
    manager = ContextManager(
        ContextConfig(max_tokens=1000, compact_threshold=0.5, prune=False, tail_turns=1),
        summarizer=lambda m: "s",
    )
    result = manager.prepare(_long_history(filler=200))
    assert result.compacted


def test_prepare_force_compacts_on_demand():
    manager = ContextManager(ContextConfig(max_tokens=1_000_000, tail_turns=1), summarizer=lambda m: "s")
    result = manager.prepare(_long_history(filler=10), force=True)
    assert result.compacted


def test_prepare_keeps_history_byte_identical_between_compactions():
    """Cache discipline: below the compaction threshold, prepare() must not
    rewrite anything - pruning old tool output mid-session would invalidate
    the provider's prefix cache for every later request (OpenCode-aligned)."""
    manager = ContextManager(
        ContextConfig(
            max_tokens=1_000_000,  # far below the compaction threshold
            prune=True,
            prune_protect_tokens=10,
            prune_minimum_tokens=10,
        )
    )
    messages = [
        _msg("user", "u1"),
        _msg("assistant", "a1"),
        _tool_msg("x" * 5000),  # old, prunable tool output
        _msg("user", "u2"),
        _msg("assistant", "a2"),
        _tool_msg("y" * 5000),
    ]
    snapshot = [dict(m) for m in messages]

    result = manager.prepare(messages)

    assert not result.compacted
    assert manager.pruned_count == 0  # pruning must NOT run between compactions
    assert result.messages is messages
    assert result.messages == snapshot  # byte-identical -> cache prefix intact


def test_stats_reports_the_full_picture():
    manager = ContextManager(ContextConfig(max_tokens=1000))
    stats = manager.stats([_msg("user", "x" * 100)])
    assert stats["max_tokens"] == 1000
    assert stats["messages"] == 1
    assert 0 < stats["ratio"] < 1
    assert stats["compactions"] == 0


def test_compaction_result_saved_tokens():
    result = CompactionResult(messages=[], before_tokens=1000, after_tokens=400, pruned_tokens=100)
    assert result.saved_tokens == 700


# --------------------------------------------------------------------------- #
# rebuild (session resume / fork)
# --------------------------------------------------------------------------- #
def test_rebuild_restores_history_without_a_model_call():
    manager = ContextManager(ContextConfig(prune=False))
    history = [_msg("user", "hello"), _msg("assistant", "hi")]
    restored = manager.rebuild(history)
    assert restored == history
    # must be a copy, not the same list object
    restored.append(_msg("user", "more"))
    assert len(history) == 2


def test_rebuild_prunes_but_never_summarises():
    """Resuming must not cost a model call."""
    manager = ContextManager(ContextConfig(prune_protect_tokens=10, prune_minimum_tokens=10))
    history = [
        _msg("user", "u"),
        _msg("assistant", "a"),
        _tool_msg("X" * 5000),
        _msg("assistant", "b"),
        _tool_msg("Y" * 5000),
    ]
    restored = manager.rebuild(history)
    assert manager.compaction_count == 0
    assert any((m.get("extra") or {}).get("pruned") for m in restored)


def test_rebuild_handles_messages_without_extra():
    manager = ContextManager()
    restored = manager.rebuild([{"role": "user", "content": "hi"}])
    assert restored[0]["content"] == "hi"
    assert restored[0]["extra"] == {}
