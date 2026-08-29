"""Demonstrate context management on a deliberately long history.

Builds a synthetic agent transcript that would blow past a small context
budget, then shows each mechanism kicking in:

1. tool-output truncation
2. pruning of old tool outputs
3. compaction of old turns into a summary

Run:
    python scripts/context_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from minicode.context.manager import ContextConfig, ContextManager  # noqa: E402
from minicode.tools.base import ToolResult  # noqa: E402


def build_history(turns: int = 6, filler: int = 1500) -> list[dict]:
    """A transcript of `turns` agent steps, each with a big tool result."""
    messages: list[dict] = [{"role": "system", "content": "You are a coding agent.", "extra": {}}]
    for index in range(turns):
        messages.append({"role": "user", "content": f"step {index}: inspect module_{index}.py", "extra": {}})
        messages.append(
            {
                "role": "assistant",
                "content": f"Looking at module_{index}.py",
                "extra": {
                    "tool_calls": [
                        {
                            "id": f"call_{index}",
                            "name": "read",
                            "arguments": {"file_path": f"src/module_{index}.py"},
                            "raw_arguments": "{}",
                        }
                    ]
                },
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"call_{index}",
                "content": f"# module_{index}.py\n" + ("x = 1  # padding padding padding\n" * filler),
                "extra": {"tool_name": "read"},
            }
        )
    return messages


def bar(label: str, before: int, after: int, budget: int) -> None:
    width = 40
    used = min(width, int(after / budget * width))
    print(f"  {label:<12} {after:>7,} / {budget:,} tokens  |{'#' * used}{'.' * (width - used)}|")


def main() -> int:
    config = ContextConfig(
        max_tokens=20_000,
        compact_threshold=0.5,
        prune=True,
        prune_protect_tokens=4_000,
        prune_minimum_tokens=2_000,
        preserve_recent_tokens=4_000,
        tail_turns=2,
        tool_output_max_lines=500,
        tool_output_max_bytes=8_000,
    )
    manager = ContextManager(config, summarizer=lambda messages: _summary(messages))
    history = build_history()

    print("1. raw history")
    before = manager.estimate(history)
    print(f"  {len(history)} messages, {before:,} tokens (budget {config.max_tokens:,})\n")

    print("2. tool-output truncation (as it happens during the run)")
    truncated = 0
    for message in history:
        if message["role"] != "tool":
            continue
        result = ToolResult(title="read", output=message["content"])
        manager.truncate_tool_output(result)
        if result.truncated:
            truncated += 1
            message["content"] = result.output
            message["extra"]["truncated"] = True
    after_truncation = manager.estimate(history)
    print(f"  {truncated} tool result(s) truncated, full text written to disk")
    print(f"  {before:,} -> {after_truncation:,} tokens\n")

    print("3. prepare() before the next model call")
    result = manager.prepare(history)
    print(f"  pruned {result.pruned_tokens:,} tokens of old tool output")
    print(f"  compacted: {result.compacted}")
    print(f"  {after_truncation:,} -> {result.after_tokens:,} tokens\n")

    print("4. budget usage")
    bar("before", before, before, config.max_tokens)
    bar("truncated", before, after_truncation, config.max_tokens)
    bar("prepared", before, result.after_tokens, config.max_tokens)

    print(f"\n5. history shape: {len(history)} messages -> {len(result.messages)} messages")
    roles = [m["role"] for m in result.messages]
    print(f"   roles: {roles}")
    compacted = [m for m in result.messages if (m.get("extra") or {}).get("compacted")]
    if compacted:
        print("\n   injected summary message:")
        print("   " + compacted[0]["content"].replace("\n", "\n   ")[:600])

    print("\n6. what survived verbatim (the recent tail)")
    for message in result.messages[-4:]:
        content = str(message.get("content", "")).strip()
        head = content.splitlines()[0][:70] if content else "(tool result)"
        print(f"   [{message['role']}] {head}")

    assert result.after_tokens < before, "context management did not shrink anything"
    print("\nOK - the agent can keep going with the shortened history.")
    return 0


def _summary(messages: list[dict]) -> str:
    files = [
        call.get("arguments", {}).get("file_path")
        for message in messages
        for call in (message.get("extra") or {}).get("tool_calls", [])
    ]
    files = [f for f in files if f]
    return (
        "Earlier work: inspected " + ", ".join(files[:6]) + (" and others" if len(files) > 6 else "") + ".\n"
        "No files have been modified yet; the work so far has been read-only investigation."
    )


if __name__ == "__main__":
    raise SystemExit(main())
