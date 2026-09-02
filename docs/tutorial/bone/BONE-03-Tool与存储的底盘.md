# BONE-03 Tool 与存储的底盘 — `search_tools.py:1` 与 `json_store.py:18` 为何这样写

> 这一篇只回答：搜索如何快且一致，落盘如何不断电不丢。

## `grep` 的两条路径为何必须行为一致

`src/minicode/tools/search_tools.py:1` 头部即契约：优先 `ripgrep`，无 `rg` 回落纯 Python，行为一致。

- **`glob` 仍走标准库**：`pathlib` 递归已是 C 速，`ripgrep` 的 `*.py` 在 `rg` 匹配任意深度而 Python 仅顶层，切过去会静默改变模型所见，故保留。

- **`grep` 优先 `rg`**：`rg_command:82` 以 `rg --line-number --no-heading --color never --hidden --max-filesize 4M -i -g !.git/**` 等 `SKIP_DIR_NAMES:60`（`storage/paths.py:60` 的 `frozenset`）互为 `-g !` 排除，`run_rg:118` 以 `Popen` 流式读至 `max_lines=200` 即 `kill`，60s 超时，`exit 0/1/>=2` 分“命中/无命中/真错”。

- **同源**：`_rg_excludes:89` 的 `lru_cache` 与 `is_hidden:79` 同读 `SKIP_DIR_NAMES`，`MAX_FILE_BYTES 4M`/`MAX_LINE_CHARS 500` 与纯 Python 的 `_walk` 同阈，`binary` 跳过与 `MAX_GREP_MATCHES 200` 同上限。`MINICODE_NO_RIPGREP=1` 强回。

## 落盘为何要 `fsync`

`storage/json_store.py:18` 的 `atomic_write_json`：

```python
fd, tmp = mkstemp(dir=parent)
write(payload); flush(); fsync(fd); os.replace(tmp, path)
```

`mkstemp` 保唯一，`fsync` 保刷盘，`os.replace` 原子重命名，异常 `unlink(tmp)`。`JsonDocumentStore:46` 的 `Lock` 防并发 `save` 交错。`Session` 的 `fork:155` 按 `messages[:cut]` 的 `tool_calls` 数同步截断，避免消息与记录错位。

单根在 `storage/paths.py:26`，`SKIP_DIR_NAMES` 因此可被两条搜索路径共享。

## 权限的 `*` 为何必须双拒

`permission/policy.py:147` 的 `wildcard_match` 中 `*` 为 `[^/]*` 不跨 `/`，`**` 为 `.*` 跨段，`_specificity:152` 最具体优先。`default.yaml:27` 双拒 `rm -rf */**` 即因 `*` 拦不住 `rm -rf /tmp/foo`。`BashTool:34` 的 `destructive_targets` 以 `shlex` 主 + 正则兜底 `bash -c 'rm …'`，命中追加 `delete` 二次校验。

**去掉会怎样**：去掉 `fsync` 则断电半截 JSON；去掉 `SKIP` 同源则 `grep` 在有无 `rg` 时结果不一致，模型习得的搜索假设失效。
