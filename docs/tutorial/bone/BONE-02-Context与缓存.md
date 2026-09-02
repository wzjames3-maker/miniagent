# BONE-02 Context 与缓存 — `context/manager.py:1` 的“字节级稳定”如何实现

> 这一篇只回答：长任务如何不撑爆且不打断 `prompt caching`。

## 目标

厂商对前缀做自动缓存（`openai_compat.py:303` 的 `cached_tokens` / DeepSeek 的 `prompt_cache_hit_tokens`）。历史中途改写旧消息会使后续每次请求的前缀失效，故两次压缩间必须字节级稳定。

## 三层，但何时触发已变

`manager.py:1` 的 module docstring 即纪律：**两次压缩间不碰历史**，`prepare` 不再单剪枝。

1. **截断** `truncate_tool_output:113` → `tools/truncate.py:48` 的 `2000行/51200字节` 超限写 `truncation_dir` 回 `output_path`，由 `ToolEnvironment` 经 `ctx.truncate` 统一注入。

2. **剪枝** `prune_tool_outputs:131` 倒序跳过 `prune_protect_tokens(40k)` 与末轮 `assistant`，达 `prune_minimum_tokens(20k)` 才擦为 `[tool output removed…]`，**仅由 `compact:224` 与 `rebuild:323` 调用**，`prepare:309` 在未达 `0.85*max_tokens` 时直接 `return pruned_tokens=0, after=before`。

3. **压缩** `compact:224` 以 `_select_split:196`（多轮按 `user`、单轮按 `assistant` 回退，`system_offset` 保 `keep_system_message: true`）和 `preserve_recent_tokens:20000` 予算选切点，`head` 经 `COMPACTION_TEMPLATE` 调模型或 `fallback_summary:280`，包为 `role:user` 的 `<compaction>` 插于 `system` 后。

```python
# prepare:309
if force or needs_compaction: return compact(messages)
return CompactionResult(messages, compacted=False, pruned_tokens=0, before=before, after=before)
```

## 估算

`tokens.py:29` 偏保守：`CJK 1.3 / ASCII 3.6`，`Message 4 / ToolCall 12` 额外开销，`estimate_messages_tokens:61` 累 `content + tool_calls`。早压比超窗安全，仍以 `ContextLengthError` 的 `_force_compact` 兜底。

## 80k 轨迹

`system(2k)+user(1k)+30*tool(90k)→93k` → `prepare` 达阈 `compact`，`system_offset=1`，`head` 20 条 → `summary 1.2k` → `system(2k)+compaction(1.2k)+tail(20k)=23k`，`system` 前缀持续命中 `cache_read`，后续仅 `tail` 增长。

若把 `<compaction>` 插最前或在两次压缩间单剪枝，则每轮 `system` 偏移，`cache_read` 归零。这就是为何“`system` 永远在最前且平时不碰历史”是唯一选择。

**去掉会怎样**：去掉 `keep_system_message` 则每轮压缩首条偏移；去掉 `prepare` 的“不碰历史”则隔几轮的单剪枝即打断前缀。
