# minicode

一个轻量、纯 Python 的 **OpenCode 风格 Coding Agent**，基于 [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) 构建。

它不是一个 OpenCode 克隆，而是把 OpenCode 的 **Coding Agent 核心**（多步循环、工具调用、权限、会话、上下文管理）用一个小型 Python 项目重新实现，并复用 mini-swe-agent 的 Agent 循环与命令执行能力。

```text
要求：Python >= 3.10，一个 API Key。
不需要 Node.js / TypeScript / Bun。
```

---

## 目录

1. [功能特性](#1-功能特性)
2. [快速开始](#2-快速开始)
3. [安装](#3-安装)
4. [配置 API Key](#4-配置-api-key)
5. [Provider 与模型](#5-provider-与模型)
6. [启动方式](#6-启动方式)
7. [斜杠命令](#7-斜杠命令)
8. [会话管理](#8-会话管理)
9. [权限系统](#9-权限系统)
10. [内置工具](#10-内置工具)
11. [上下文管理](#11-上下文管理)
12. [配置参考](#12-配置参考)
13. [运行测试](#13-运行测试)
14. [架构简介](#14-架构简介)
15. [相关文档](#15-相关文档)

---

## 1. 功能特性

| 特性 | 说明 |
|---|---|
| 🖥️ 双前端 | 默认 Aider 风格 REPL；可选全屏 Textual TUI（`minicode tui`） |
| 🔌 多 Provider | OpenAI-compatible、Anthropic-compatible、LiteLLM、自定义 Provider |
| 🛠️ 多工具 | `read` / `write` / `edit` / `apply_patch` / `glob` / `grep` / `bash` |
| 🧠 推理模型支持 | 流式显示 thinking 块；只有思考没有回答时自动提示模型补回答 |
| 💬 会话管理 | 自动保存、恢复、fork、按项目隔离、批量删除 |
| 🔐 权限系统 | `allow` / `deny` / `ask` 三级策略；非交互环境 fail-closed |
| 📦 上下文管理 | 截断、剪枝、压缩，长任务不爆上下文 |
| ⚡ 缓存对齐 | 前缀缓存统计上状态栏（`cache 12.3K`）；两次压缩之间历史字节级稳定，保住 OpenAI / DeepSeek 自动前缀缓存 |
| 📋 项目指令 | 自动读取 `AGENTS.md` / `CLAUDE.md` 内容注入 system prompt（单文件 20k 字符截断，OpenCode 对齐） |
| ⚡ 快速搜索 | `grep` 优先走 ripgrep（原生匹配器 + `.gitignore`），无 `rg` 自动回落纯 Python |
| 🧩 可扩展 | 自定义工具、自定义 Provider、自定义命令 |
| 🧪 测试完善 | 单元 + 集成 + TUI 测试，当前 `398 passed / 2 skipped` |

---

## 2. 快速开始

```bash
# 1. 安装
python -m venv .venv
source .venv/bin/activate
pip install -e .

# 2. 配置 Provider（交互式）
minicode providers login

# 3. 启动
minicode            # Aider 风格 REPL
minicode tui        # 全屏 Textual TUI
```

也可以直接跑一次性任务：

```bash
minicode run "修复失败的测试"
```

---

## 3. 安装

```bash
# 从源码安装
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .

# 发布后也可以
pip install minicode
```

验证安装：

```bash
python -m minicode --help
python -m minicode tools
```

> `python -m minicode` 是标准入口；安装后也会有 `minicode` 命令。

---

## 4. 配置 API Key

推荐使用交互式向导，不需要手写 YAML：

```bash
minicode providers login
```

可以选择预设（`openai`、`deepseek`、`anthropic`、`local`）或自定义 OpenAI-compatible 地址，然后输入 API Key。

在 TUI 里也可以直接配置：

```text
/login
```

`/login` 与 `/provider` 等价，配置完成会自动切换模型。

非交互配置：

```bash
minicode providers login openai --api-key sk-...
minicode providers list
```

手动管理配置：

```bash
minicode config init     # 生成全局配置文件
minicode config path     # 查看配置文件位置
```

环境变量方式：

```bash
export OPENAI_API_KEY=sk-...
```

任何 OpenAI-compatible 服务都可以用，例如 Sensenova：

```bash
export SENSENOVA_API_KEY=sk-...
```

```yaml
default_provider: sensenova
default_model: deepseek-v4-flash

providers:
  sensenova:
    type: openai_compat
    api_key_env: SENSENOVA_API_KEY
    base_url: https://token.sensenova.cn/v1
    models: [deepseek-v4-flash]
```

检查配置是否生效：

```bash
minicode models
# * sensenova [openai_compat, key-ok]: deepseek-v4-flash
```

也可以把 Key 直接写在配置文件里（`api_key: sk-...`），但请确保文件权限安全。更推荐 `api_key_env`。

工作目录下的 `.env` 文件会自动加载。

---

## 5. Provider 与模型

`type` 决定线上协议：

| `type` | 适用 |
|---|---|
| `openai_compat` | OpenAI、DeepSeek、Qwen、Sensenova、vLLM、Ollama、OpenRouter、Groq、Together 等 |
| `anthropic_compat` | Anthropic Messages API |
| `litellm` | LiteLLM 支持的 100+ Provider（需 `pip install litellm`） |
| `my.module:MyProvider` | 自定义 Provider |

示例：

```yaml
providers:
  openai:
    type: openai_compat
    api_key_env: OPENAI_API_KEY
    models: [gpt-4o-mini, gpt-4o]
    default_model: gpt-4o-mini

  deepseek:
    type: openai_compat
    api_key_env: DEEPSEEK_API_KEY
    base_url: https://api.deepseek.com/v1
    models: [deepseek-chat, deepseek-reasoner]

  anthropic:
    type: anthropic_compat
    api_key_env: ANTHROPIC_API_KEY
    models: [claude-sonnet-4-5]

  ollama:
    type: openai_compat
    base_url: http://localhost:11434/v1
    models: [qwen2.5-coder:7b]
```

指定模型启动：

```bash
minicode -m deepseek/deepseek-chat
minicode -m gpt-4o-mini
```

会话内可用 `/model` 切换模型。

### 推理模型

DeepSeek-V4/R1、o-series、QwQ 等模型会在 `reasoning_content` 中输出思考过程。minicode 会：

- 在终端里用灰色 "thinking…" 块实时展示推理过程；
- 如果模型只输出了思考、没有可见回答，会自动提示模型补一次正式回答。

### 提示词缓存（Prompt Caching）

OpenAI / DeepSeek 等对请求**前缀**做自动缓存（服务端行为，无需配置）；minicode 与之对齐的两件事：

- **缓存统计可见**：状态栏显示 `cache 12.3K`（读 / 写），完整数字在 `/status`、`/session` 可查；
- **前缀保护**：两次压缩之间历史严格保持字节一致，绝不中途改写旧消息（剪枝只在压缩时发生），让自动前缀缓存持续命中。

### 速率限制

```yaml
providers:
  mine:
    min_request_interval: 6   # 最小请求间隔（秒）
    max_retries: 6
    retry_delay: 10           # 指数退避基数
    retry_max_delay: 90       # 退避上限
```

服务端返回的 `Retry-After` 优先级最高。

---

## 6. 启动方式

```bash
minicode                                   # 默认：Aider 风格 REPL
minicode tui                               # 全屏 Textual TUI（可选）
minicode run "修复失败的测试"               # 单次任务后退出
minicode --yolo run "重构 utils.py"        # 自动批准权限
minicode --cwd /path/to/project            # 指定工作目录
minicode --no-stream run "..."             # 关闭流式输出
```

### REPL（默认）

Aider 交互模型：底部一行输入，上方滚动显示完整对话，支持斜杠命令和权限确认。

| 按键 | 作用 |
|---|---|
| `enter` | 发送；`Esc`+`Enter`（或 `Alt`+`Enter`）换行 |
| `↑` / `↓` | 浏览历史输入 |
| `/` | 斜杠命令（`/help`、`/model`、`/resume` ...） |

### Textual TUI（可选）

OpenCode 风格全屏界面：

- 左侧会话栏：只显示当前项目的会话，按 Today / Yesterday / Older 分组
- 消息流：用户 / 助手 Markdown / 可折叠工具调用 / 系统消息
- thinking 面板：实时显示推理模型的思考过程
- 状态栏：cost / tokens / context / 消息数 / 模型
- 底部：provider · model · workspace
- 输入框：多行输入 + `/` 命令弹窗
- 模型选择器：`/model` 回车弹出可过滤列表

| 按键 | 作用 |
|---|---|
| `/` | 打开命令弹窗 |
| `up` / `down`（或 `ctrl+p` / `ctrl+n`） | 在弹窗中移动 |
| `tab` / `enter` | 运行高亮命令 |
| `escape` | 关闭弹窗 / 中断当前 turn |
| `enter` | 发送；`shift+enter` 换行 |
| `ctrl+up` / `ctrl+down` | 浏览历史输入 |
| `ctrl+p` | 命令面板 |
| `ctrl+n` | 新建会话；`ctrl+l` 清空 transcript |
| `d` | 删除左侧高亮会话 |
| `ctrl+t` | 切换深色 / 浅色 |
| `ctrl+e` | 循环切换 5 套配色 |
| `ctrl+c` | 退出 |

---

## 7. 斜杠命令

| 命令 | 作用 |
|---|---|
| `/help` | 显示帮助 |
| `/login [provider]` | 配置 / 更新 API Key 和模型 |
| `/provider [provider]` | `/login` 的别名 |
| `/model [provider/model]` | 查看 / 切换模型；TUI 中不带参数会打开模型选择器 |
| `/models` | 列出已配置的 Provider 和模型 |
| `/session` | 显示当前会话信息 |
| `/sessions` | 列出已保存会话 |
| `/resume <id>` | 恢复指定会话 |
| `/fork [id]` | fork 当前（或指定）会话 |
| `/title <text>` | 重命名当前会话 |
| `/new` | 新建会话 |
| `/clear` | `/new` 的别名 |
| `/compact` | 立即执行上下文压缩 |
| `/tools` | 列出可用工具 |
| `/permission` | 显示当前权限规则 |
| `/status` | 显示 Agent / 会话统计 |
| `/commands` / `/command` | 管理自定义命令 |
| `/exit` | 退出（`/quit`、Ctrl-D 也可以） |

多行输入：`Esc`+`Enter` 或 `Alt`+`Enter`。

---

## 8. 会话管理

会话是纯 JSON 文件，不绑定 Provider / Model。

```bash
minicode sessions                              # 列出全部
minicode sessions --cwd /path/to/project       # 只列出某个项目的会话
minicode --resume ses_ab12cd                   # 恢复会话
minicode session show ses_ab12cd
minicode session delete ses_ab12cd
minicode session delete --all                  # 删除全部（会确认）
minicode session delete --all --cwd /path      # 删除某个项目的全部会话
minicode session delete --all --yes            # 非交互删除全部
minicode session prune                         # 清理空 "New session" 占位
```

TUI 中的会话操作：

- 左侧只显示当前项目的会话
- 点击历史会话 → 切换并回放
- 高亮后按 `d` → 删除
- `/new`、`ctrl+n`、`/clear` 都会清空可见历史

代码方式：

```python
from minicode.session.manager import SessionManager

sessions = SessionManager()
session = sessions.create(provider="openai", model="gpt-4o-mini", cwd=".")
fork = sessions.fork(session.id, at_message=10)
```

会话保存内容：消息历史、工具调用历史（耗时 / 是否成功 / 错误码 / 是否截断）、Provider / Model、工作目录、标题、元数据。

---

## 9. 权限系统

三种动作：`allow`、`deny`、`ask`，作用在三个层级。

```yaml
permission:
  read: allow
  glob: allow
  grep: allow
  write: ask
  edit: ask
  delete: ask
  bash:
    "git status*": allow
    "python -m pytest*": allow
    "rm -rf **": deny
    "*": ask
  apply_patch:
    "**/*.env": deny
    "src/**": allow
```

匹配规则：

| 模式 | 含义 |
|---|---|
| `*` | 单个路径段内匹配（不跨 `/`） |
| `**` | 跨路径段 |
| `**/foo` | 匹配 `foo` 和 `a/b/foo` |
| `?` | 单个字符 |

> 注意：`*` 不跨 `/`，所以 `rm -rf *` 不会匹配 `rm -rf /etc`。要拦截请用 `rm -rf **`。

解析规则：

1. 最具体的规则优先。
2. 同样具体时，后面的规则优先。
3. 未匹配默认 `ask`，绝不静默放行。
4. 非交互环境无法询问时，直接拒绝（fail-closed）。

非交互自动允许：

```bash
minicode --yolo run "..."
```

或配置 `mode: auto`（显式 `deny` 仍然生效）。

---

## 10. 内置工具

| 工具 | 作用 |
|---|---|
| `read` | 带行号读取文件，支持 `offset` / `limit` 分页 |
| `write` | 创建或覆盖文件，自动创建父目录，显示 diff |
| `edit` | 精确替换文本；必须唯一匹配，除非 `replace_all` |
| `apply_patch` | 一次调用完成多文件新增 / 修改 / 删除 / 移动 |
| `glob` | 按模式查找文件（标准库实现，跳过隐藏 / 依赖目录） |
| `grep` | 正则搜索文件内容；**优先 ripgrep**（快、尊重 `.gitignore`），无 `rg` 时自动回落纯 Python 实现 |
| `bash` | 执行 Shell 命令（测试、构建、git 等） |

> `grep` 的两条实现路径行为一致（跳过隐藏 / 依赖目录、跳过二进制、新文件优先、结果上限），可放心切换。若因某种原因想强制使用纯 Python 实现，设置环境变量 `MINICODE_NO_RIPGREP=1` 即可。

所有工具遵循统一协议：

- JSON Schema 描述参数
- `permission` 权限键
- `ToolResult(title, output, metadata, error)` 返回，**工具不抛异常**

大输出会自动截断（行数 + 字节数双上限），完整内容写盘并返回路径，Agent 可以继续用 `read` / `grep` 查阅。

如果模型返回非法或截断的工具参数，minicode **不会用空参数执行工具**，而是把原始解析错误直接回传模型，让模型重试（与 OpenCode 行为一致）。

自定义工具：

```python
# mytools.py
from minicode.tools.base import BaseTool, ToolResult


class ShoutTool(BaseTool):
    name = "shout"
    permission = "read"
    description = "Shout a message."
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    def run(self, args, ctx):
        return ToolResult(title="shout", output=args["text"].upper())


def register_tools(registry):
    registry.register(ShoutTool())
```

```yaml
tools:
  extra_modules: [mytools]
```

---

## 11. 上下文管理

长会话主要通过三种机制控制上下文：

1. **截断**：每个工具输出设上限，超出部分写盘。
2. **剪枝**：删除旧工具输出，保留调用本身；最近一步始终保护。
3. **压缩**：把旧对话摘要成一条消息，最近内容按 token 预算保留原文。

**缓存纪律（OpenCode 对齐）**：两次压缩之间历史消息保持字节级一致，剪枝只在压缩发生时执行、绝不单独中途改写历史——这样 OpenAI/DeepSeek 的自动前缀缓存能持续命中（此前每次查询前都会顺手剪枝，等于隔几轮就把缓存前缀打断一次）。压缩会替换头部历史，此时前缀必然变化，属于预期内的"缓存重置点"。

```yaml
context:
  max_tokens: 120000
  auto_compact: true
  compact_threshold: 0.85       # 达到 85% 触发压缩
  prune: true
  prune_protect_tokens: 40000
  preserve_recent_tokens: 20000 # 压缩时保留的原文预算
  tail_turns: null              # 硬性保留轮数；null = 用预算
  tool_output_max_lines: 2000
  tool_output_max_bytes: 51200
```

说明：

- Provider 返回上下文超长错误时，会自动压缩并重试一次。
- 压缩切点多轮会话选 `user`，单轮长任务选 `assistant`。
- 恢复 / fork 会话时只做恢复和剪枝，不额外消耗模型调用。

---

## 12. 配置参考

配置优先级（从高到低）：

```text
--set key=value  >  MINICODE_* 环境变量  >  ./.minicode/config.yaml  >  全局配置  >  内置默认值
```

```bash
minicode config path      # 配置文件位置
minicode config show      # 查看合并后的有效配置
minicode config init      # 生成 starter 配置
minicode --set agent.step_limit=50 --set ui.stream=false
minicode --set ui.theme=emerald   # TUI 配色
```

完整配置项见 [`config.example.yaml`](config.example.yaml)。

---

## 13. 运行测试

```bash
pip install -e ".[dev]"
pytest
```

代码检查：

```bash
python -m ruff check src tests scripts
```

当前状态：

```text
398 passed, 2 skipped
```

---

## 14. 架构简介

```text
src/minicode/
  agent/        Agent 循环、状态、提示词（含 AGENTS.md 指令块、缓存计数）
  tools/        工具接口 + Registry + 内置工具 + 截断（grep 优先 ripgrep）
  providers/    Provider 抽象 + OpenAI/Anthropic/LiteLLM 实现（含缓存 token 解析）
  session/      会话持久化（create / resume / fork / delete）
  permission/   allow / deny / ask 权限策略
  context/      截断 / 剪枝 / 压缩（缓存纪律：两次压缩间历史字节一致）
  config/       分层配置
  storage/      原子 JSON 存储 + 平台路径（SKIP_DIR_NAMES 单源）
  project.py    项目生态探测 + AGENTS.md/CLAUDE.md 指令注入
  ui/           REPL + Textual TUI（状态栏含 cache 命中统计）
  cli/          argparse 入口 + 斜杠命令
```

核心保证：

- Agent 不依赖具体 Provider
- Agent 不直接实现工具
- 工具不依赖 UI
- Session 不持有 Model 引用
- Permission 是独立模块
- 核心与 UI 只通过 `UIPort` / `EventSink` 通信

---

## 15. 相关文档

| 文档 | 内容 |
|---|---|
| [`docs/TUI.md`](docs/TUI.md) | Textual TUI 完整使用指南 |
| [`docs/tutorial/`](docs/tutorial/) | 从零到改代码的 11 章教程 + advanced 专题（缓存感知、重试限流） |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 架构设计与模块职责 |
| [`OPENCODE_GAP.md`](OPENCODE_GAP.md) | 与 OpenCode 的功能边界 |
| [`MINISWE_DIFF.md`](MINISWE_DIFF.md) | 与 mini-swe-agent 的差异 |
| [`config.example.yaml`](config.example.yaml) | 完整配置示例 |
