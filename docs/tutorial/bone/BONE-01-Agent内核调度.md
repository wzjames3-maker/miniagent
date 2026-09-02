# BONE-01 Agent 内核调度 — `core.py:117` 为何这样排

> 这一篇只回答：`CodingAgent` 何时调模型、何时执行工具、何时停，何时不算错。

## 内核只有两条轨道

`src/minicode/agent/core.py:474` 的 `run` 判 `if self.messages`：空则 `super().run(task)`（`mini-swe-agent` 的首次建会话），非空则 `add_messages(user)` 后 `_run_loop()`。`_run_loop:498` 是去掉 `reset` 的 `DefaultAgent.run`：

```python
while True:
    try: self.step()
    except FormatError: ...  # 计数达 max_consecutive_format_errors 则 RepeatedFormatError
    except InterruptAgentFlow: add_messages(提示)  # doom-loop 注入
    if messages[-1].role=="exit": break
```

`step:457` 在下一边界查 `_interrupted`，返回 `role:exit/Interrupted` 的正常消息而非抛异常，让两层 `while` 都经 `messages[-1].role=="exit"` 正常 unwind 并 `save(output_path)`。`request_interrupt:448` 仅置位，由 `cli/app.py` 的 TUI 在 `MiniTUI.post:157` 的单点线程判断后触发。

## 问模型前的整理

`query:213` 三行：

```python
def query(self):
    self._prepare_context()
    try: message = super().query()
    except ContextLengthError:
        if self._context_retried or not self._force_compact(): raise
        message = super().query()
```

`_prepare_context:228` 调 `context.prepare`，若 `compacted` 则 `self.messages=...`、`_fingerprints.clear()`、`sync_session`、`on_compaction`。`_force_compact:246` 是兜底：先 `compact`，若 `split<=0` 则 `messages[:1]+messages[2:]` 丢最老一轮再压，仍失败则 `return False` 上抛。`_context_retried` 保只重试一次。

系统提示词在 `get_template_vars:173` 注入 `tools_list/cwd/os_name/date/project.describe()` 与 `project_instructions`（`core.py:177` 的 `project.instruction_block`，`project.py:146` 的 `20k` 截断），`prompts.py:7` 的 `EXPLORE→PLAN→VERIFY→RECOVER` 即此。

## 执行的三条分流

`execute_actions:273`:

```python
raw_calls = extra.tool_calls
if not calls: return self._finish(message)
for call in calls:
    self._check_doom_loop(call)  # core.py:400 的 sort_keys 指纹，连3次同参则 InterruptAgentFlow
    if call.name not in registry: ToolResult(unknown_tool) 不执行
    elif _tool_call_parse_error: ToolResult(parse_error) 不执行
    else: env.execute(action) → ToolRegistry:86 的 权限→执行→截断
    record_tool_call → Session; add_messages(tool observation)
```

`unknown/parse_error` 均按 OpenCode 把原始错误包装为 `role:tool` 回模型重试，而非让循环崩。`ToolCallRecord:349` 记 `duration_ms/output_chars/truncated` 供 `stats:546` 的 `cache_read/write` 展示。

## 空回答的 nudge

`_finish:366` 对推理模型 `content` 空而 `reasoning` 有值时，以 `user` 补一句“请用文字总结”，`_empty_reply_nudged` 保只补一次，仍空则 `raise Submitted` 结束。`_summarize:427` 的 `max(provider.max_tokens,4096)` 是为推理模型的 thinking 预算。

## 五个限额的归属

`CodingAgentConfig:54` 仅加 `doom_loop_threshold:3`，其余 `step_limit/cost_limit/wall_time` 与 `max_consecutive_format_errors` 复用 `DefaultAgent` 的 `query` 计数与 `cost` 累加（`add_messages:186` 的 `state.step/input/output_tokens`）。`AgentState:21` 的 `cache_read/write` 经 `core.py:192` 累加后入 `port.py:44` 的 `cache 12.3K`。

**去掉会怎样**：去掉 `_force_compact` 则 `ContextLengthError` 直接抛给用户；去掉 `doom_loop` 则模型在 `read` 同参上空转至 `step_limit`；去掉 `nudge` 则推理模型思考后直接 `Submitted` 空提交。
