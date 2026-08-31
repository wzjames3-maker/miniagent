# ARCHITECTURE — minicode

> `minicode` 是以 `mini-swe-agent` 为基座、参考 `opencode` 核心 Coding Agent 设计的纯 Python 终端助手。  
> 本文件在重构后更新，反映真实依赖与事件流，并标注从 `opencode` 借鉴的关键模式。

## 1. 模块职责

```
src/minicode/
  agent/        Agent 循环（继承 mini DefaultAgent）+ 状态 + 提示词
  tools/        统一 Tool 接口 + Registry + 7 内置工具 + 截断
  providers/    统一 Provider 抽象（OpenAI/Anthropic/LiteLLM/Scripted）+ 注册表
  session/      持久化 Session（create/resume/fork/delete）+ 模型无关
  permission/   独立策略层（allow/deny/ask，三级 tool/command/path）
  context/      消息历史 + Token 预算 + 截断/剪枝/压缩 + 重建
  config/       五层层叠配置（builtin < global < project < env < --set）
  storage/      原子 JSON 文档存储 + 平台路径
  project.py    仓库生态探测（语言 / 测试 / 校验命令）—— 语言无关的唯一真相源
  ui/           EventSink + UIPort 抽象 + Rich 控制台 + Textual 全屏前端
  cli/          argparse 入口 + 斜杠命令分发
  errors.py     统一错误层级（Config/Provider/Tool/Permission/Session/Context/CLI）
```

`cli/` 仅装配，`ui/` 仅渲染，`agent/` 仅协调，`tools/` 仅执行，`providers/` 仅翻译，`session/` 仅持久化，`context/` 仅构造，`permission/` 仅决策，`storage/` 仅落盘。单向依赖，禁止反向。

`ui/` 内部再分两层，前端可整体替换：

```
ui/port.py     UIPort（核心可调的契约）+ UIFrontEnd（UIPort+EventSink 的组合）
ui/events.py   EventSink：agent -> UI 的单向事件流（runtime_checkable Protocol）
ui/render.py   两个前端共用的渲染片段（参数摘要、输出截断、diff 高亮）
ui/console.py  Rich 实现：REPL（默认前端）、`minicode run`
ui/textual/    Textual 实现：全屏前端（OpenCode 布局），`minicode tui` 显式开启
   theme.py    主题调色板（5 套）+ 布局 CSS —— 只引用 Textual 自带设计变量
   widgets.py  纯展示部件：会话侧栏 / 消息流 / 状态栏 / 会话脚注 / 提示条 / 权限条 / 输入框
   modals.py   弹窗（模型选择器）
   bridge.py   TextualUI：事件 -> 部件更新（唯一同时认识 agent 与 Textual 的地方）
   app.py      MiniTUI：装配布局、管理工作线程、仲裁阻塞式提问

前端选择：默认 `InteractiveApp.repl()`（Rich REPL，Aider 交互模型）；
`minicode tui` 走 Textual。两者共用 `UIPort` + `EventSink` 契约，核心无感知。
```

## 2. 依赖关系

```
CLI (main.py:79)  --load_settings-->  Settings
 │
 ▼
InteractiveApp (app.py:33)  工厂
 ├── ProviderRegistry  --build_registry-->  Provider (base.py:221)
 ├── ToolRegistry      --build_default_registry-->  7 Tools
 ├── PermissionManager --ruleset_from_config-->  Rule[]
 ├── ContextManager    --ContextConfig-->  tokens/truncate
 ├── SessionManager    --JsonDocumentStore-->  storage/paths
 ├── ConsoleUI         --EventSink-->  ui/events
 └── CodingAgent       --Provider+Registry+Permission+Context+Session-->  ToolEnvironment
        │                         │
        ├─ContextManager.prepare() │  压缩/剪枝
        ├─ToolRegistry.execute()   │  权限→执行→截断
        └─Provider.generate()      │  统一消息←→API
              │
              ▼
           Model (API)
```

保证（由测试约束 `OPENCODE_GAP.md:144`）：
- Agent 不依赖具体 Provider（仅 `Provider` ABC）
- Agent 不直接实现 Tool（经 `ToolEnvironment` → `ToolRegistry`）
- Tool 不依赖 UI（仅返回 `ToolResult`）
- Session 不持有 Model 引用（仅 `provider/model: str`）
- Permission 不 import tools/session/ui
- Provider/Tool 可通过 `load_module` / 自定义 `type: my.mod:Cls` 扩展

## 3. Agent Loop

`CodingAgent(DefaultAgent)` `src/minicode/agent/core.py:117`

```
run(task)  # 首次 super().run()，续跑 _run_loop()
  query() -> _prepare_context() -> super().query() --ContextLengthError--> _force_compact() 重试一次
  on_assistant_message
  execute_actions(message):
    for call in tool_calls:
      _check_doom_loop() --InterruptAgentFlow--> 注入用户提示
      sink.on_tool_start
      unknown/非法参数? -> 不执行工具，原始错误直接回传模型（OpenCode 同款）
      否则 env.execute({tool, args}) -> ToolRegistry.execute() -> 权限检查 -> Tool.execute() -> ToolResult
      sink.on_tool_result
      record_tool_call -> Session
    sync_session
    add_messages(observation)  # 写回 Session + 自动标题
  _finish() # 无 tool_call 且 content 为空时 nudge 一次，否则 Submitted 退出
```

限额：`step_limit/cost_limit/wall_time` 复用 `DefaultAgent`；`doom_loop_threshold` 连续相同指纹中断；`max_consecutive_format_errors` 复用 mini。

## 4. Tool System

统一协议 `src/minicode/tools/base.py:113`

```python
class Tool(Protocol):
    name: str; description: str; parameters: dict; permission: str
    def execute(self, args, ctx) -> ToolResult: ...
    def patterns(self, args) -> list[str]: ...
class BaseTool:
    def validate(args) -> dict  # 最小 JSON-Schema 校验
    def run(args, ctx) -> ToolResult  # 子类实现
    def execute(args, ctx) -> ToolResult  # 校验+捕获 -> 永不抛异常
```

`ToolResult(title, output, metadata, error)` `truncated` 标记；`ToolRegistry` `src/minicode/tools/registry.py:22` 负责发现、Schema 导出、权限委托、执行；`build_default_registry` 装配 7 内置：`read` `write` `edit` `apply_patch` `glob` `grep` `bash`（`bash` 复用 `MiniLocalEnvironment` `src/minicode/tools/bash_tool.py:27`）。

截断：双限 `2000 lines / 51200 bytes` `src/minicode/tools/truncate.py:48` 超限写盘返回 `output_path`，由 `ContextManager.truncate_tool_output` 统一入口，`ToolEnvironment` 注入 `ctx.truncate` 供工具调用。`write/edit/apply_patch` 已统一走该路径，避免上下文撑爆。

## 5. Provider System

抽象 `src/minicode/providers/base.py:221`

```python
class Provider(ABC):
    def generate(messages, tools, *, stream, on_event, max_tokens) -> AssistantMessage
    # 同时实现 mini Model 协议: query/format_message/format_observation_messages/get_template_vars/serialize
```

归一化形状：`ToolCall` `AssistantMessage( content, tool_calls, usage, reasoning )` `StreamEvent`；`tool_schema_to_openai/anthropic` 转换；`format_tool_results` 配对工具结果。

重试/节流统一在基类 `src/minicode/providers/base.py:321`：`_throttle`（`min_request_interval`）、`_retry_after_seconds`（`Retry-After` 优先）、`_with_retries`（指数退避 `retry_delay/retry_max_delay`，`ContextLengthError/AuthenticationError` 不重试）。`OpenAICompat` `src/minicode/providers/openai_compat.py:69` 与 `AnthropicCompat` `src/minicode/providers/anthropic_compat.py:34` 共享该逻辑，差异仅 `_convert_messages/_payload/_run_blocking/_run_stream/_map_error`。

`ProviderRegistry` `src/minicode/providers/registry.py:83` 解析 `type: openai_compat|anthropic_compat|litellm|my.mod:Cls`，`get("provider/model" | "model")` 支持裸模型搜索，`set_default` 实现会话内 `/model` 切换。

## 6. Session System

模型无关 `src/minicode/session/models.py:59`：`Session(id,title,provider,model,cwd,messages,tool_calls,metadata)`；`ToolCallRecord` 含 `duration_ms/output_chars/truncated/error_code`。

`SessionManager` `src/minicode/session/manager.py:29` 基于 `JsonDocumentStore` `src/minicode/storage/json_store.py:18`（`mkstemp+fsync+os.replace` 原子写 + `Lock`），目录由 `src/minicode/storage/paths.py:6` 解析（`MINICODE_DATA_DIR` 可覆写）。支持 `list(cwd=...)` / `delete_all(cwd=...)` 按项目筛选与批量删除；CLI 暴露 `sessions --cwd`、`session delete --all [--cwd] [--yes]`。`fork(at_message)` 按消息截断时同步截断 `tool_calls`（按 `messages[:cut]` 中 `tool_calls` 数量切片），`retitle` `maybe_auto_title` 标题自动生成。TUI 左侧会话栏通过 `InteractiveApp.known_sessions(cwd=...)` 只显示当前项目。

## 7. Context System

三机制 `src/minicode/context/manager.py:1`

1. **截断** `truncate_tool_output:112` → `tools/truncate.py:48` 头尾各半取，超限写盘。
2. **剪枝** `prune_tool_outputs:131` 倒序跳过 `prune_protect_tokens(40k)` 尾部与末轮 assistant，释放达到 `prune_minimum_tokens(20k)` 才重写，标记 `pruned` / `compacted` 避免重复。
3. **压缩** `compact:224` 选切点 `_select_split:196`（多轮按 `user`，单轮长按 `assistant`），`budget preserve_recent_tokens(20k)` 或 `tail_turns` 硬截，`_summarize:272` 调模型或 `fallback_summary:281` 抽取，注入 `<compaction>` 消息。

`prepare:309` 按 `needs_compaction(0.85*max_tokens)` 阈值触发压缩否则仅剪枝；`rebuild:323` 恢复时仅剪枝不清摘要（不耗模型）；`tokens.py:29` 启发式 `CJK 1.3 / ASCII 3.6 chars/token` 略保守；`estimate_messages_tokens` 含 `tool_call 12 token` 开销。

## 8. Permission System

策略 `src/minicode/permission/policy.py:137` `wildcard_match`（`*` 不跨 `/`，`**` 跨段，` **/` 零目录），`_specificity:152` 最具体优先，平局后者胜，解决 `*:ask` 覆盖 `read:allow`。`ruleset_from_config:215` 支持 `read: allow` 与 `bash: {"git *": allow}` 双形态，`~/$HOME` 展开。

`PermissionManager` `src/minicode/permission/manager.py:66` `DEFAULT/AUTO` 双模式，`AUTO` 下 `deny` 仍拒绝其余放行，`DEFAULT` 下 `deny→ask→allow`，`ask_callback` 注入，非交互 `non_interactive` 时 fail-closed（`_ask:170` 抛 `PermissionRejected`）。`ALWAYS` 仅记忆精确 `pattern`，不扩大为 `*`。`default_ruleset:198` 与 `config/default.yaml:27` 同为读自由写需确认，`rm -rf *` 与 `rm -rf **` 双拒。

`BashTool` `src/minicode/tools/bash_tool.py:34` `destructive_targets` 双重提取（`shlex` 主 + 正则回退覆盖 `bash -c 'rm …'`），命中时追加 `delete` 权限二次校验。

## 9. TUI Event Flow

`EventSink` `src/minicode/ui/events.py:19` 抽象：

```
Agent -> sink.on_stream_event(text_delta/reasoning_delta/tool_call_*)
     -> sink.on_tool_start / on_tool_result
     -> sink.on_compaction / on_error / on_turn_start / on_turn_end
```

两个前端实现同一套契约，**核心不认识任何一个**：

| | `ConsoleUI`（Rich） | `TextualUI`（Textual） |
|---|---|---|
| 事件来源 | 主线程直出 | 工作线程经 `App.post` 回投 UI 线程 |
| 流式文本 | 逐 token 打印 | 实时写入消息流的 assistant 消息（Markdown） |
| 权限提问 | `Prompt.ask` 阻塞读 stdin | 权限条接管焦点，`y/a/n` 或鼠标点击释放工作线程 |
| 默认关系 | 默认前端（`minicode`） | 可选前端（`minicode tui`） |

`UIFrontEnd` `src/minicode/ui/port.py:43` 是两份契约的**组合**：`InteractiveApp` 用同一个对象
同时承担 `UIPort` 与 `EventSink`（`sink or ui`），只实现一半的前端在构造时不会报错，而是要等到
第一个 agent 事件才炸。把这个组合关系显式命名，就把运行期错误提前到契约层，`tests/unit/test_tui.py`
里有一个"只实现一半必须被拒"的用例守着它。为此 `EventSink` 由普通基类改为
`runtime_checkable Protocol` —— 子类仍继承 no-op 默认体，"只覆写关心的事件"的用法不受影响。

`ConsoleUI` `src/minicode/ui/console.py:42` 负责流式灰显 thinking 块（`_thinking` 折叠为 `… thought for N chars`）、工具面板（`max_output_lines` 头尾省略）、权限弹窗（`ask_permission:216` `y/a/n`）、状态行、banner。`InputReader` `src/minicode/ui/prompt.py:16` 基于 `prompt_toolkit` 历史与多行（`Ctrl+O/Alt+Enter`）。

`TextualUI` `src/minicode/ui/textual/bridge.py:32` 是唯一同时认识 agent 与 Textual 的模块。跨线程的关键约束有两处：

1. **`App.post` `app.py:157`** —— `call_from_thread` 明确禁止在 app 线程内调用（会锁死它自己要排队的那个循环），但 banner、斜杠命令回显、收尾状态行恰好都产生于 app 线程。判断"我现在在哪条线程"这件事只允许存在一个地方。
2. **阻塞式提问** —— agent 在工作线程上，答案只有 UI 线程能给出。`_PendingPrompt` 用 `threading.Event` 把工作线程挂起，**收尾统一在消息处理器里做**（`on_permission_bar_answered:222` / `on_composer_submitted:233`），而不是让线程各自清理一半——否则"无 pending 时的残留弹窗"没人关。

`handle_slash` `src/minicode/cli/commands.py:32` 分发 `/help /model /models /session /sessions /resume /fork /title /new /clear /compact /tools /permission /status`；它**只依赖 `UIPort`**，因此同一份命令表在两个前端里行为一致。`InteractiveApp` `src/minicode/cli/app.py:33` 装配链，`_switch_session` 重建 `ContextManager.rebuild` 后的 Agent。新增一个前端只需实现 `UIPort` + `EventSink`，不必改动 `cli/` 与 `agent/`。

TUI 会话栏通过 `known_sessions(cwd=...)` 按当前项目过滤；`action_clear_log` 会同时清空可见 `MessageList`，所以 `/new`、`ctrl+n`、`/clear`、切换/回放会话都不会残留上一个会话的可见历史。左侧高亮会话后按 `d` 删除。

## 10. 配置层叠

`src/minicode/config/settings.py:1`

```
builtin (default.yaml)  --文件缺失--> _FALLBACK_CONFIG_TEMPLATE（与 default.yaml 同源，仅缺失时启用）
  ← global   ~/.minicode/config.yaml
  ← project  ./.minicode/config.yaml
  ← env      MINICODE_* （__ 分层，如 MINICODE_AGENT__STEP_LIMIT）
  ← CLI      --set key=value （复用 mini _key_value_spec_to_nested_dict + recursive_merge）
```

`.env` 双路径加载；`DEFAULT_CONFIG_YAML` 以文件内容为准，文件缺失才用 `_FALLBACK_CONFIG_TEMPLATE` 渲染兜底，杜绝双源分叉；`_load_yaml` 异常转为 `ConfigError`；`ProviderSpec` `type/kind` 双兼容，`options` 透传 `max_retries/min_request_interval` 等，不静默丢弃。

## 11. 从 opencode 借鉴的关键模式

| 问题 | opencode 做法 | minicode 轻量借鉴 |
|---|---|---|
| 工具截断分散、易绕过 | `tool/tool.ts:wrap` 在注册层统一 `decode→execute→truncate.output`，工具只返回原始结果 | `ToolRegistry.execute:144` 集中调用 `ctx.truncate(result)`，工具 `run` 不再处理截断 `src/minicode/tools/file_tools.py:139` |
| 配置静默丢弃、双源分叉 | `config/config.ts` 强 schema 校验 + 兼容层 `lower` + 单源文件 | `Settings/ContextConfig:148` `extra="forbid"` 拒未知顶层键，`DEFAULT_CONFIG_YAML` 以文件为准、缺失才用模板兜底 |
| 重试/节流分散 | 基类统一 `retry + Retry-After + throttle` | `Provider._with_retries/_throttle:321` 提取到 `base.py:321`，`OpenAI/Anthropic` 共享 |
| 破坏性命令识别脆弱 | `permission/evaluate.ts` 精确匹配 + `*`/`**` 语义 | `BashTool.destructive_targets:34` 保留 `shlex` 主路径并借鉴 opencode 的正则回退覆盖 `bash -c 'rm …'` |
| 权限默认不安全 | 默认规则显式 `deny` 危险模式 | `default_ruleset:198` 追加 `rm -rf */** deny`，与 `default.yaml:42` 一致 |
| UI 与核心互相渗透 | 前端只订阅事件，核心不知道有没有屏幕 | `UIPort` + `EventSink` 双契约，`cli/commands.py` 只依赖 Protocol；Textual 前端可整包删除 |
| 工具参数非法/截断 | `runTools` 解析失败不执行，原始 error 回传模型 | `execute_actions` 检测 `raw_arguments` 解析失败后不执行工具，直接回传原始解析错误 |
| Session 管理 | 会话与项目绑定，可在 TUI 中切换/删除 | TUI 会话栏按项目过滤 + `d` 删除；CLI 支持 `sessions --cwd` 与 `session delete --all` |
| 主题需要每个部件各自适配 | 设计变量 + 单一开关 | 样式表只引用 Textual 自带 `$*` 变量，`ctrl+t` 直接复用框架 `action_toggle_dark`，项目内零调色板 |

## 12. 语言无关

minicode 不在任何地方写死 Python。仓库事实只有一个来源：`src/minicode/project.py`。

```
detect_project(root) -> ProjectProfile
    ├─ languages        由根目录 manifest / lockfile 反推（13 个生态）
    ├─ test_commands    pytest / npm test / go test ./... / cargo test / mvn test / mix test ...
    └─ lint_commands    ruff / eslint / go vet / cargo clippy / rubocop ...
```

`ProjectProfile` 同时喂给三处，避免各自演化：

1. **系统提示词** —— `agent/prompts.py` 注入 `Project` 段，并明确"不要假设语言，用这个仓库真正在用的命令"。
2. **权限白名单** —— `permission/manager.py:default_bash_rules` 由 `ECOSYSTEMS` 生成测试/校验命令模式，所以任何语言的测试命令都自动免询问。
3. **配置生成** —— `config/settings.py:render_permission_yaml` 用同一份常量渲染 `default.yaml`、内嵌兜底配置和 `minicode config init`，三份不可能分叉。

`project.py` 是**叶子模块**（不 import 任何 minicode 代码），否则 `permission → agent → context → permission` 会成环。 工厂耦合 | `Effect.Layer` 分层装配 `ToolRegistry/Config/Permission` | `InteractiveApp` 保留 ` _build_tools/_permission_rules` 等小工厂，职责已在 `cli/app.py:60` 拆分，未来可进一步提为 `cli/factory.py` |
