# 04 Agent 如何一轮一轮往前走

**如何决定下一步，且不在原地打转？**

> 定位：我们在全局图的 Agent 循环格，拆开 `03` 的中间一步。

## 直觉

任务交后，`Agent` 在“问模型”与“调工具”间搬运：问→`read`→回历史→再问→`edit`→回历史→再问→`bash`→回历史，直到模型只回文字。

`src/minicode/agent/core.py` 的 `CodingAgent` 继承 `mini-swe-agent` 的 `DefaultAgent`，复用限额与模板，只补三件事：怎么问、怎么执行、怎么不崩。

## 怎么问

`query` 先 `_prepare_context` 整理历史，再 `super().query()`。若模型报 `ContextLengthError`，`_force_compact` 强制压一次再重试，仅一次。

问时需同时让两套接口听懂。`mini` 侧要 `Model.query`，`minicode` 侧要 `Provider.generate` 的 `tools` 与流式回调。中间有一个很小的桥（`MiniModelAdapter` 在 `core.py`），只做塞入，其余透传，循环无需重写。

系统提示词在 `src/minicode/agent/prompts.py` 定义 `EXPLORE→PLAN→VERIFY→RECOVER→FINISH`，并注入 `{{ project }}`（探测的语言/测试命令）与 `{{ project_instructions }}`（`core.py:177` 注入的 `project.instruction_block`，`AGENTS.md`/`CLAUDE.md` 20k 截断，`project.py:146`）。模型因此在 Python 仓不会误用 `go test`，且自动遵循仓库约定。

## 怎么执行

`execute_actions` 三步：名不在表不执行、`raw_arguments` 解析失败不执行（二者均把原始错误包装为 `role: tool` 回模型）、否则经 `ToolEnvironment`→`ToolRegistry` 的“权限→执行→截断”并包为 `tool` 消息加回历史。

防呆：对 `{"tool":name,"args":args}` 的 `sort_keys` 指纹，若连续 3 次相同（`CodingAgentConfig:59` 的 `doom_loop_threshold`），`raise InterruptAgentFlow` 注入提示要求换参或总结。

空回答：推理模型把 `max_tokens` 花在 `reasoning` 上时，`_finish:366` 以 `user` 补一句“请用文字总结”，`_empty_reply_nudged` 保只补一次。

中断：`request_interrupt:448` 仅置位，`step:457` 在下一边界返回 `exit/Interrupted` 的正常消息，让两层循环经 `messages[-1].role=="exit"` 正常退出并落盘。

## 状态与缓存可见性

`AgentState:21` 的 `cache_read/write_tokens` 在 `core.py:192` 从 `usage` 累加，经 `core.py:556` 入 `stats` 供 `ui/port.py:44` 的 `cache_label`（`cache 12.3K`）展示。不影响调度，只让前缀是否命中可见。

## 记住什么

问前整理历史并注入项目与指令→校验后执行→错误回模型→循环/空回答有兜底。下一章看被调度的 `Tool`。
