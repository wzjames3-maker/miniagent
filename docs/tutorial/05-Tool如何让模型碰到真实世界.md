# 05 Tool 如何让模型碰到真实世界

**模型只会输出 JSON，谁去改文件？**

> 定位：我们在全局图的 Tool 格。

## 为何需要 Tool

模型只能输出 `{"name":"read","arguments":{"file_path":"utils.py"}}`。需有人找到实现、真正读写、包回文字。`Tool` 即此。

## 一个 Tool 的形状

`src/minicode/tools/base.py` 统一为 `name/description/parameters/permission` + `execute→ToolResult`。`ToolResult` 永不抛异常，错误经 `render` 回模型重试；`BaseTool` 已包校验与捕获。

## 前台

`src/minicode/tools/registry.py` 的 `ToolRegistry` 是前台：一是 `register` 七工具与 `tools.extra_modules` 的 `register_tools`；二是 `schemas` 供模型；三是 `check_permissions`；四是统一截断（`2000 行/51200 字节` 超限写 `truncation_dir`）。

`grep` 与 `glob` 的排除同源于 `src/minicode/storage/paths.py:60` 的 `SKIP_DIR_NAMES`，前者走 `ripgrep` 时转 `-g !.git/**` 等（`search_tools.py:1`，`--hidden --max-filesize 4M`，60s 超时，无 `rg` 回落纯 Python，`MINICODE_NO_RIPGREP=1` 强回），后者走标准库递归与 `is_hidden`，二者行为一致：跳过隐藏/依赖/二进制、新文件优先、200 条上限。

## 三个例子

`read` 按行号分页，二进制回错；`write` 用 `write_bytes` 回 `diff`；`edit` 要求 `old_string` 唯一（`text.count`），`no_match/ambiguous_match` 提示多带上下文或 `replace_all`。

`bash` 复用 `mini-swe-agent` 的 `LocalEnvironment`（`bash_tool.py:27`），权限双检：先 `command` 再 `destructive_targets`（`shlex` 主 + 正则兜底 `bash -c 'rm …'`）查 `delete`。

## 记住什么

JSON 提需求，前台翻译成动作，`Tool` 只实现 `run`，错误回历史。
