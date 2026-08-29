# MINISWE_DIFF — 从 mini-swe-agent 到 minicode

本文说明 minicode 是如何在 [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)
（下称 **mini**）之上演进出来的：哪些模块直接复用、哪些被修改、哪些是新增，以及每一次修改的原因。

基线版本：`mini-swe-agent 2.4.6`（本项目以 `mini-swe-agent>=2.4.0` 作为**硬依赖**安装）。

---

## 0. 总览

| 类别 | 数量 | 说明 |
|---|---|---|
| 直接复用（不改一行代码） | 3 | `DefaultAgent` 主循环、`LocalEnvironment` 命令执行、`interactive` 输入处理 |
| 继承扩展（子类 + 覆写） | 4 | Agent、Environment、Model 适配、配置模型 |
| 协议实现（鸭子类型，不继承） | 1 | `ToolEnvironment` 实现 mini 的 Environment 协议 |
| 新增模块 | 10 | tools / providers / session / permission / context / storage / ui / cli |

**核心设计决策：不 fork，不复制。** mini 作为 PyPI 依赖被 import，所有扩展通过
继承 / 组合 / 协议实现完成。因此 mini 上游的 bug 修复可以直接通过 `pip install -U` 获得。

---

## 1. 直接复用的模块

### 1.1 `minisweagent.agents.default.DefaultAgent` — Agent 主循环

**复用方式**：`CodingAgent(DefaultAgent)`，仅在必要时 `super()` 调用。

**复用的行为**：

- `run()` 的步骤循环、`step_limit` / `cost_limit` / `wall_time_limit` 三类限制
- `query()` 中的限额检查、调用计数、`cost` 累加
- jinja2 模板渲染（`StrictUndefined`）
- `serialize()` / `save()` 的 trajectory 输出
- `handle_uncaught_exception`、`FormatError` 重试计数

**为什么复用**：这是 mini 最成熟的部分。它的循环语义（限额、异常分类、退出状态）
经过 SWE-bench 大量验证，重写只会引入 bug。

**是否改变原有行为**：否。`super().run()` 的行为完全不变。

### 1.2 `minisweagent.environments.local.LocalEnvironment` — 命令执行

**复用方式**：`MiniLocalEnvironment(LocalEnvironment)` 子类，只覆写一个方法。

```python
class MiniLocalEnvironment(LocalEnvironment):
    """mini-swe-agent 的 LocalEnvironment，去掉 submit 魔术字符串。"""

    def _check_finished(self, output: dict) -> None:
        return None
```

**为什么复用**：超时处理、进程组 kill、stdout/stderr 合并、Windows 兼容性都是
容易出错的地方，mini 已经做对了。

**是否改变原有行为**：**是，且这是刻意的**。mini 用 `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`
这个魔术字符串作为"任务完成"信号；OpenCode 的语义是"模型不再调用工具即本轮结束"。
因此禁用了该检测，改由 `CodingAgent._finish()` 处理。

### 1.3 `minisweagent.agents.interactive` 的输入处理

**复用方式**：`minicode/ui/prompt.py` 参考其多行输入 UX（Esc+Enter 提交等）。
CLI 的交互式主循环自行实现（因为要支持 `/` 命令和 TUI 事件）。

---

## 2. 修改 / 扩展的模块

### 2.1 Agent：`CodingAgent(DefaultAgent)`

**文件**：`src/minicode/agent/core.py`

| 方法 | 关系 | 修改原因 |
|---|---|---|
| `run()` | 扩展 | mini 的 `run()` 每次都重置会话。交互式 TUI 需要**续跑**：有历史时走 `_run_loop()`，无历史时才 `super().run()` |
| `step()` | 继承 | 不变（`step → query → execute_actions`） |
| `query()` | 覆写 | 注入工具 schema + streaming 回调；捕获 `ContextLengthError` 触发紧急压缩后重试一次 |
| `execute_actions()` | 覆写 | mini 只执行一条 bash 命令；这里执行**多个 tool call**，逐个做权限检查、doom-loop 检测、结果记录 |
| `add_messages()` | 扩展 | 同步写入 Session、累计 token 与步数 |
| `get_template_vars()` | 扩展 | 注入 `tools_list` / `cwd` / `os_name` / `date` |
| `serialize()` | 扩展 | 额外写入 minicode 的统计信息 |
| `_finish()` | 新增 | 见下 |

**新增行为（非 mini 原有）**：

1. **Doom-loop 检测** — 连续 N 次（默认 3）完全相同的工具调用时注入中断消息，
   而不是让 agent 空转到 `step_limit`。
2. **推理模型的空回复处理** — DeepSeek-V4/R1、o-series、QwQ 这类模型经常只输出
   `reasoning_content` 而 `content` 为空。直接判定"完成"会丢掉整轮工作，
   因此 nudge 一次（仅一次）要求给出文本结论。
3. **上下文压缩** — `query()` 前调用 `ContextManager.prepare()`；
   `ContextLengthError` 时强制压缩并重试。

**是否改变原有行为**：是，且仅体现在上述新增能力上。基础的循环语义不变。

### 2.2 Environment：`ToolEnvironment`（协议实现，不继承）

**文件**：`src/minicode/agent/environment.py`

这是整个移植的**枢纽**。mini 的 agent 调 `env.execute(action)`，自己的 environment
把 `action["command"]` 当 shell 命令执行。minicode 保留协议与返回契约
（`{"output", "returncode", "exception_info", "extra"}`），但把 action 解释为
`{"tool": name, "args": {...}}` 并分发到 ToolRegistry。

```
mini:  agent → LocalEnvironment → subprocess(bash)
minicode: agent → ToolEnvironment → ToolRegistry → read/write/edit/.../bash
```

**为什么这么做**：仅需替换这一个对象，mini 的控制流（限额、异常处理、退出状态）
就完整保留，同时获得多工具能力。无需改动 mini 一行代码。

`bash` 工具本身仍然复用 mini 的 `LocalEnvironment`（见 1.2），所以两条路径
的命令执行行为一致。

### 2.3 Model：`MiniModelAdapter`

**文件**：`src/minicode/agent/core.py`

mini 的 agent 调 `model.query(messages)`。minicode 的 `Provider` 需要额外传入
工具 schema 和 streaming 回调。适配器在**不改 mini 循环**的前提下注入这些参数。

同时实现 `format_observation_messages` / `get_template_vars` / `serialize`，
完全满足 mini 的 `Model` 协议。

### 2.4 Config：`CodingAgentConfig(AgentConfig)`

```python
class CodingAgentConfig(AgentConfig):
    system_template = SYSTEM_TEMPLATE
    instance_template = INSTANCE_TEMPLATE
    doom_loop_threshold: int = 3
    confirm_on_finish: bool = False
    stream: bool = True
```

保留 mini 的全部配置字段（`step_limit`、`cost_limit`、模板等），只追加新字段。

### 2.5 配置机制

mini 用 YAML + 环境变量覆盖。minicode 保留该思路但自建 `config/settings.py`：
五层优先级（`--set` > `MINICODE_*` 环境变量 > 项目配置 > 全局配置 > 内置默认）。

**为什么不直接用 mini 的配置加载器**：mini 的配置与 SWE-bench 场景强耦合
（instance template、环境问题模板等），且没有多 provider / 权限 / 上下文这些概念。

---

## 3. 新增模块

全部位于 `src/minicode/` 下，mini 中不存在对应物。

| 模块 | 职责 | 对应 OpenCode |
|---|---|---|
| `tools/` | 统一工具接口 + Registry + 7 个内置工具 | `packages/opencode/src/tool/` |
| `providers/` | 统一 Provider 抽象（OpenAI / Anthropic / LiteLLM / scripted） | `provider/` |
| `session/` | 会话持久化：create / resume / list / delete / fork | `session/` |
| `permission/` | allow / deny / ask 三级权限（tool / command / path） | `permission/` |
| `context/` | 截断 / 剪枝 / 压缩 / 恢复重建 | `session/compaction.ts` + `tool/truncate.ts` |
| `storage/` | JSON 文档存储 + 平台路径解析 | — |
| `ui/` | EventSink 抽象 + rich TUI + 提示符 | TUI |
| `cli/` | argparse CLI + `/` 命令 | CLI |
| `agent/prompts.py` | 系统提示 / 压缩提示 | — |
| `agent/state.py` | Agent 状态记录 | — |

---

## 4. 关键差异对照表

| 能力 | mini-swe-agent | minicode |
|---|---|---|
| 工具 | 只有 bash（单命令） | 7 个结构化工具，支持一次多调用 |
| 工具协议 | shell 命令字符串 | JSON Schema + 结构化 `ToolResult` |
| 工具错误 | 非零退出码 | 结构化错误（`code` / `message` / `hint` / `details`） |
| 多 Provider | litellm 单通道 | 多 provider / 多 model，会话内切换 |
| Streaming | 有 | 有，且区分 `text_delta` 与 `reasoning_delta` |
| 推理模型 | 不区分 | 提取 `reasoning_content`，空回复时 nudge |
| 会话 | 无（单次运行） | 持久化、恢复、fork、自动标题 |
| 权限 | 无 | allow / deny / ask，tool / command / path 三级 |
| 上下文管理 | 无 | 截断 + 剪枝 + 压缩 |
| Doom loop | 无 | 连续重复调用检测 |
| 输出截断 | 有（bash 输出） | 所有工具统一，超限写盘并给路径 |
| 交互式使用 | 有（基础） | 完整 TUI + `/` 命令 |
| 提交信号 | `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` | 停止调用工具 |

---

## 5. 依赖变化

mini 的核心依赖极少。minicode 额外引入：

| 依赖 | 用途 | 必要性 |
|---|---|---|
| `openai` | OpenAI 兼容 provider | 必需（第一梯队 provider） |
| `anthropic` | Anthropic 兼容 provider | 必需（第一梯队 provider） |
| `rich` | TUI 渲染 | 必需（交互式界面） |
| `prompt_toolkit` | 输入提示符 | 必需（交互式界面） |
| `platformdirs` | 跨平台数据目录 | 必需（会话持久化） |
| `pydantic` | 配置模型 | 已有（mini 传递依赖） |
| `pyyaml` | 配置文件 | 已有 |
| `litellm` | **可选** | 仅使用 `type: litellm` 时才需要 |

纯 Python，无 Node.js / TypeScript / Bun 依赖。

---

## 6. 兼容性保证

- minicode 的 `Provider` 实现了 mini 的 `Model` 协议 → 任何 minicode provider
  可直接插入 mini 自带的 agent。
- minicode 的 `ToolEnvironment` 实现了 mini 的 `Environment` 协议 → 可插入 mini 的其他 agent。
- `CodingAgent` 是 `DefaultAgent` 的子类 → mini 的 `serialize()` / trajectory 格式完全兼容。
