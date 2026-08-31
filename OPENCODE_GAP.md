# OPENCODE_GAP — 与 OpenCode 的功能边界

本文逐项说明 [OpenCode](https://github.com/anomalyco/opencode) 的能力在 minicode 中的实现情况。

**设计原则**：只实现 OpenCode 的 **Coding Agent 核心**，不实现其外围产品能力。
取舍标准是"是否为编码能力所必需"，而不是"是否看起来完整"。

---

## 1. 已实现（核心能力）

### 1.1 Agent

| OpenCode 能力 | minicode | 说明 |
|---|---|---|
| 多轮 Agent Loop | ✅ | 基于 mini-swe-agent 的 `DefaultAgent` 循环 |
| Tool Calling | ✅ | 一次响应可包含多个 tool call，顺序执行 |
| Tool Result 回传 | ✅ | 结构化回传，含错误码与 hint |
| 多步骤任务执行 | ✅ | E2E 验证：9 步、13 次工具调用完成修 bug 任务 |
| 工具失败后的错误恢复 | ✅ | 结构化错误 → 模型读取 → 改参数或换策略 |
| 非法/截断工具参数 | ✅ | 不执行工具，把原始 JSON 解析错误直接回传模型（OpenCode 同款行为） |
| 最大步数限制 | ✅ | `agent.step_limit`，另有 cost / wall-time 限制 |
| Doom Loop 检测 | ✅ | 连续 N 次相同调用即注入中断消息 |
| Agent 状态记录 | ✅ | `agent/state.py`，步数 / token / 错误数 / 压缩次数 |

### 1.2 Tools

| OpenCode 工具 | minicode | 差异 |
|---|---|---|
| `read` | ✅ | 行号输出、offset/limit 分页、二进制拒绝 |
| `write` | ✅ | 自动建父目录、覆写时给出 diff |
| `edit` | ✅ | 要求唯一匹配（可 `replace_all`），与 OpenCode 一致 |
| `apply_patch` | ✅ | 同款 `*** Begin Patch` 格式，支持 add/update/delete/move |
| `glob` | ✅ | 纯 Python 实现，跳过隐藏目录与二进制 |
| `grep` | ✅ | 纯 Python 正则搜索，不依赖 ripgrep |
| `bash` | ✅ | 复用 mini 的 `LocalEnvironment`（超时 / 进程组 kill） |
| Tool Registry | ✅ | `register` / `subset` / `load_module` 扩展点 |
| 统一输入输出协议 | ✅ | JSON Schema + `ToolResult(title, output, metadata, error)` |
| 结构化错误 | ✅ | `code` / `message` / `hint` / `details` |
| 非法/截断参数处理 | ✅ | 解析失败不执行工具，原始解析错误直接回传模型 |
| 输出截断 | ✅ | 超限写盘并返回路径，模型可自行读取 |

**与 OpenCode 的差异（有意）**：
- 未实现 `list`、`todowrite`/`todoread`、`webfetch`、`task`（subagent）、`lsp`、`skill`。
  除 `list`（可用 `glob` 替代）外，均属于外围能力或本项目明确排除项。

### 1.3 Provider / Model

| 能力 | minicode | 说明 |
|---|---|---|
| OpenAI-compatible | ✅ | OpenAI / DeepSeek / Qwen / vLLM / Ollama / Sensenova … |
| Anthropic-compatible | ✅ | Anthropic messages API |
| 多 Provider | ✅ | 配置文件中任意多个 |
| 多 Model | ✅ | 每个 provider 可声明多个 model |
| 会话内切换 Model | ✅ | `/model <provider>/<model>`，registry 层操作 |
| Streaming | ✅ | SSE 增量累积 tool call |
| Tool Calling | ✅ | OpenAI 与 Anthropic 两种线格式归一化 |
| Provider/Model 配置 | ✅ | `api_key` / `api_key_env` / `base_url` / `max_tokens` / `timeout` / `headers` |
| Agent 不绑定 Provider | ✅ | Agent 只依赖 `Provider` 抽象基类 |
| 推理模型支持 | ✅ | 提取 `reasoning_content`，UI 单独展示，空回复时 nudge |
| 速率限制退避 | ✅ | 指数退避 + 遵守 `Retry-After` + 可选请求节流 |
| LiteLLM 通道 | ✅ | 可选，覆盖 100+ provider |

### 1.4 Session

| 能力 | minicode |
|---|---|
| 创建 / 恢复 / 列出 / 删除 | ✅ |
| 批量删除 / 按项目删除 | ✅ `session delete --all [--cwd <path>] [--yes]` |
| 按项目筛选列表 | ✅ `sessions --cwd <path>` |
| TUI 会话栏按项目隔离 | ✅ 左侧只显示当前项目，高亮后按 `d` 删除 |
| Session fork（可指定消息位置截断） | ✅ |
| Session 标题（含从首条消息自动生成） | ✅ |
| 消息历史 | ✅ |
| Tool Call 历史（含耗时、是否截断、错误码） | ✅ |
| 当前 Provider / Model | ✅ |
| Session 元数据 | ✅ |
| 与 Agent / Provider 解耦 | ✅ Session 只存数据，不持有 agent 或 model 引用 |

### 1.5 Permission

| 能力 | minicode |
|---|---|
| `allow` / `deny` / `ask` | ✅ |
| Tool 级权限 | ✅ `read` / `write` / `edit` / `bash` … |
| Command 级权限 | ✅ `bash: {"git *": allow, "rm -rf **": deny}` |
| Path 级权限 | ✅ `edit: {"**/*.env": deny}` |
| 高风险操作确认 | ✅ shell / 写入 / 删除 / 大范围修改默认 `ask` |
| 非交互自动允许模式 | ✅ `--yolo` / `mode: auto` |
| 无 TTY 时 fail-closed | ✅ 无法询问即拒绝（不静默放行） |
| "always" 记忆本次批准 | ✅ 仅针对该具体目标，不自动扩大到整个 permission |

### 1.6 Context Management

| 能力 | minicode |
|---|---|
| Message History 管理 | ✅ `ContextManager` 统一拥有 |
| Tool Output 截断 | ✅ 行数 + 字节数双上限，超限写盘 |
| Context Size 控制 | ✅ 启发式 token 估算（无外部依赖，略偏保守） |
| History Compaction | ✅ 摘要压缩旧历史 + 保留近期原文（tail budget 驱动） |
| 压缩后继续执行 | ✅ 压缩消息注入后 agent 无感知地继续 |
| Session 恢复后的上下文重建 | ✅ `rebuild()`：恢复 + 剪枝，**不调用模型** |

### 1.7 TUI / CLI

| 能力 | minicode |
|---|---|
| 用户输入 | ✅ `prompt_toolkit`，支持多行 |
| Agent 输出 / Streaming | ✅ rich 实时渲染 |
| Tool Call / Tool Result 展示 | ✅ 面板 + 语法高亮 |
| Permission 确认 | ✅ once / always / reject |
| 错误展示 | ✅ |
| 当前 Model / Provider 展示 | ✅ |
| `/help` `/model` `/session` `/sessions` `/resume` `/fork` `/clear` `/exit` | ✅ |
| 会话列表按项目筛选 | ✅ `minicode sessions --cwd <path>` |
| 会话批量删除 | ✅ `minicode session delete --all [--cwd <path>] [--yes]` |
| TUI 删除会话 | ✅ 左侧高亮后按 `d` |
| TUI 新建/切换清空可见历史 | ✅ `/new`、`ctrl+n`、`/clear`、切换会话均清空 transcript |
| 推理过程展示 | ✅ 灰显 thinking 块，结束后折叠为一行统计 |

---

## 2. 明确排除（本项目范围外）

| OpenCode 能力 | 是否实现 | 排除原因 | 未来如何扩展 |
|---|---|---|---|
| Web UI | ❌ | 纯终端工具，Web UI 需要整套前端与后端服务 | 可基于 `ui/events.py` 的 EventSink 增加 WebSocket sink |
| Client / Server 架构 | ❌ | 单进程直接调用，无 RPC 需求 | EventSink 已是天然的远程化边界 |
| IDE 集成 | ❌ | 需要编辑器插件生态 | 同上，通过 sink + 独立命令通道 |
| Cloud / 托管服务 | ❌ | 本地优先，不引入账号体系 | — |
| OAuth / 账号体系 | ❌ | 与"配置 API Key 即可用"冲突 | 若需要，加 provider 的 `auth_flow` 钩子 |
| Remote Session（SSH / 容器） | ❌ | 需要远程执行环境抽象 | `ToolEnvironment` 已可替换为远程实现 |
| GitHub 集成（PR / Issue） | ❌ | 产品级能力，非编码核心 | 可作为 `bash` 调用 `gh`，或注册外部工具 |
| Plugin Marketplace | ❌ | 需要分发与沙箱机制 | `registry.load_module()` 已是本地扩展点 |
| MCP（Model Context Protocol） | ❌ | 需求明确排除 | 可写 MCP→Tool 适配器注册进 registry |
| LSP | ❌ | 需求说明"暂不实现" | 可作为独立工具注册，不影响现有架构 |

---

## 3. 部分实现 / 有意简化

| 能力 | 状态 | 说明 |
|---|---|---|
| Token 计数 | 简化 | 启发式估算（CJK 与 ASCII 分开计权，略偏保守）。未引入 tiktoken：它体积大且对 Anthropic / 开源模型同样不准。压缩提前一点点发生，比撑爆上下文安全得多。 |
| `list` 工具 | 未提供 | `glob` 已能完全覆盖（`glob **/*`） |
| Subagent / Task 委派 | 未提供 | 单 agent 已完成 E2E 任务；委派需要额外的上下文隔离机制 |
| Todo 列表 | 未提供 | 模型在上下文内自行跟踪；可由用户通过自定义工具扩展 |
| Cost 追踪 | 简化 | 有 `cost_limit` 与累计，但依赖 provider 是否上报价格；未内置价格表 |
| Undo / 快照 | 未提供 | 依赖 git；agent 被明确禁止执行破坏性 git 命令 |

---

## 4. 架构约束达成情况

需求要求的解耦约束，全部满足且有测试保证：

| 约束 | 达成 | 证据 |
|---|---|---|
| Agent 不依赖具体 Provider | ✅ | `CodingAgent` 只接受 `Provider` 抽象；测试用 `ScriptedProvider` 注入 |
| Agent 不直接实现具体 Tool | ✅ | 所有工具经 `ToolRegistry` 分发 |
| Tool 不依赖 TUI | ✅ | `tools/` 不 import `ui/`（工具只返回 `ToolResult`） |
| Session 不绑定具体 Model | ✅ | Session 只存 provider/model 字符串 |
| Permission 独立 | ✅ | `permission/` 不 import tools / session / ui |
| Provider 可扩展 | ✅ | `PROVIDER_KINDS` 注册 + 自定义类路径 |
| Tool 可扩展 | ✅ | `registry.load_module()`，有单元测试覆盖 |

---

## 5. 与 OpenCode 的哲学差异

1. **纯 Python，零 Node 生态** —— OpenCode 是 TypeScript/Bun 项目；minicode 只依赖 Python。
2. **不追求功能数量** —— 排除了 Web UI、MCP、插件市场等一整个产品层。
3. **复用优于重写** —— Agent 循环与命令执行直接建立在 mini-swe-agent 之上（详见 `MINISWE_DIFF.md`）。
4. **fail-closed 的权限** —— 无法询问用户时拒绝，而不是放行。
