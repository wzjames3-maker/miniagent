# BONE-04 CLI 与测试 — `cli/main.py:79` 如何分发，`scripted` 如何让测试离线

> 这一篇只回答：命令从敲下到 `Agent.run` 如何分发，测试如何在无 Key 时仍过 `312 passed`。

## 分发：`build_parser` 到 `handle_slash`

`src/minicode/cli/main.py:79` 的 `build_parser` 定义 8 组子命令：`run` / `sessions` / `session` / `config` / `providers` / `models` / `tools` / `tui`，辅以 `--model/--cwd/--config/--set/--yolo/--resume/--no-stream`。`main:106` 按 `args.command` 分发，无命令则 `_interactive` → `InteractiveApp.repl()`，有则 `_run_once/_cmd_sessions/_cmd_tui/...`。

`InteractiveApp` 在 `cli/app.py:33` 装配链：`ProviderRegistry:83` 解析 `type: openai_compat|my.mod:Cls`，`ToolRegistry:22` 装 7 工具，`PermissionManager:66` 按 `--yolo` 选 `AUTO/DEFAULT`，`ContextManager:72` 持预算，`SessionManager:29` 持 `JsonDocumentStore`。`InteractiveApp:33` 用同一对象兼任 `UIPort` 与 `EventSink`，斜杠分发在 `cli/commands.py:32` 仅依赖 `UIPort`，故同表在 `ConsoleUI` 与 `TextualUI` 下行为一致。

`/login` 的交互在 `cli/provider_config.py` 经 `input_fn/password_fn` 注入，便于 `TextualUI:32` 的 `Composer` 以 `Text` 回填而 `ConsoleUI:42` 以 `Prompt.ask` 阻塞。

## 配置五层与双源防范

`src/minicode/config/settings.py:72` 的 `DEFAULT_CONFIG_YAML` 以 `builtin/default.yaml` 文件内容为准，仅缺失时用 `_FALLBACK_CONFIG_TEMPLATE` 渲染，避免 `OPENCODE_GAP:8` 的双源分叉。`Settings:148` 的 `extra="forbid"` 拒未知顶层键，`_load_yaml` 异常转 `ConfigError`。

`load_settings:214` 的层叠 `builtin←global←project←env←--set` 复用 `mini-swe-agent` 的 `recursive_merge`，`_load_dotenvs` 同时读 `cwd/.env` 与 `global/.env` 且 `override=False`，`ProviderSpec:63` 对 `max_retries/min_request_interval` 等未建模键透传至 `Provider.options`，不静默丢弃。

## 测试：`scripted` 如何离线

`src/minicode/providers/scripted.py` 的 `ScriptedProvider` 按脚本返回固定 `AssistantMessage`，不触网。`tests/unit/test_provider.py:11` 断言 DeepSeek 的 `prompt_cache_hit_tokens` 双兼容，`test_context.py:30` 断言 `prepare` 不单剪枝的缓存纪律，`test_tui.py:13` 断言半实现 `UIPort+EventSink` 被拒。`README.md` 的 `312 passed` 即此链的出口。

**去掉会怎样**：去掉 `scripted` 则 CI 需真实 Key；去掉 `DEFAULT_CONFIG_YAML` 的文件优先则内置与磁盘分叉；去掉 `commands.py` 的 `UIPort` 依赖则新增前端需改 `cli`。
