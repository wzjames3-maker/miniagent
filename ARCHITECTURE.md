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
  ui/           EventSink 抽象 + Rich TUI + 提示符
  cli/          argparse 入口 + 斜杠命令分发
  errors.py     统一错误层级（Config/Provider/Tool/Permission/Session/Context/CLI）
```

`cli/` 仅装配，`ui/` 仅渲染，`agent/` 仅协调，`tools/` 仅执行，`providers/` 仅翻译，`session/` 仅持久化，`context/` 仅构造，`permission/` 仅决策，`storage/` 仅落盘。单向依赖，禁止反向。

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
      env.execute({tool, args}) -> ToolRegistry.execute() -> 权限检查 -> Tool.execute() -> ToolResult
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

`SessionManager` `src/minicode/session/manager.py:29` 基于 `JsonDocumentStore` `src/minicode/storage/json_store.py:18`（`mkstemp+fsync+os.replace` 原子写 + `Lock`），目录由 `src/minicode/storage/paths.py:6` 解析（`MINICODE_DATA_DIR` 可覆写）。`fork(at_message)` 按消息截断时同步截断 `tool_calls`（按 `messages[:cut]` 中 `tool_calls` 数量切片），`retitle` `maybe_auto_title` 标题自动生成。

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

`ConsoleUI` `src/minicode/ui/console.py:42` 实现：流式灰显 thinking 块（`_thinking` 折叠为 `… thought for N chars`）、工具面板（`max_output_lines` 头尾省略）、权限弹窗（`ask_permission:216` `y/a/n`）、状态行、banner。`InputReader` `src/minicode/ui/prompt.py:16` 基于 `prompt_toolkit` 历史与多行（`Ctrl+O/Alt+Enter`）。

`handle_slash` `src/minicode/cli/commands.py:32` 分发 `/help /model /models /session /sessions /resume /fork /title /new /clear /compact /tools /permission /status`；`InteractiveApp` `src/minicode/cli/app.py:33` 装配链，`_switch_session` 重建 `ContextManager.rebuild` 后的 Agent，保持 UI 与核心零耦合：替换 `EventSink` 即可得到 CLI/Web/API/测试 Harness。

## 10. 配置层叠

`src/minicode/config/settings.py:1`

```
builtin (default.yaml)  --文件缺失--> _FALLBACK_CONFIG_YAML（与 default.yaml 同源，读文件覆写避免分叉）
  ← global   ~/.minicode/config.yaml
  ← project  ./.minicode/config.yaml
  ← env      MINICODE_* （__ 分层，如 MINICODE_AGENT__STEP_LIMIT）
  ← CLI      --set key=value （复用 mini _key_value_spec_to_nested_dict + recursive_merge）
```

`.env` 双路径加载；`DEFAULT_CONFIG_YAML` 运行时若文件存在则以文件内容覆盖嵌入串，杜绝双源分叉；`_load_yaml` 异常转为 `ConfigError`；`ProviderSpec` `type/kind` 双兼容，`options` 透传 `max_retries/min_request_interval` 等，不静默丢弃。

## 11. 从 opencode 借鉴的关键模式

| 问题 | opencode 做法 | minicode 轻量借鉴 |
|---|---|---|
| 工具截断分散、易绕过 | `tool/tool.ts:wrap` 在注册层统一 `decode→execute→truncate.output`，工具只返回原始结果 | `ToolRegistry.execute:144` 集中调用 `ctx.truncate(result)`，工具 `run` 不再处理截断 `src/minicode/tools/file_tools.py:139` |
| 配置静默丢弃、双源分叉 | `config/config.ts` 强 schema 校验 + 兼容层 `lower` + 单源文件 | `Settings/ContextConfig:148` `extra="forbid"` 拒未知顶层键，`_FALLBACK_CONFIG_YAML` 运行时覆写为文件内容 `settings.py:135` |
| 重试/节流分散 | 基类统一 `retry + Retry-After + throttle` | `Provider._with_retries/_throttle:321` 提取到 `base.py:321`，`OpenAI/Anthropic` 共享 |
| 破坏性命令识别脆弱 | `permission/evaluate.ts` 精确匹配 + `*`/`**` 语义 | `BashTool.destructive_targets:34` 保留 `shlex` 主路径并借鉴 opencode 的正则回退覆盖 `bash -c 'rm …'` |
| 权限默认不安全 | 默认规则显式 `deny` 危险模式 | `default_ruleset:198` 追加 `rm -rf */** deny`，与 `default.yaml:42` 一致 |
| 工厂耦合 | `Effect.Layer` 分层装配 `ToolRegistry/Config/Permission` | `InteractiveApp` 保留 ` _build_tools/_permission_rules` 等小工厂，职责已在 `cli/app.py:60` 拆分，未来可进一步提为 `cli/factory.py` |
