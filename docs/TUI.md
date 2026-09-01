# minicode TUI — 使用指南

`minicode tui` 启动全屏 Textual 界面。它复刻了 OpenCode 的布局，并把
pydantic-deepagents 的交互亮点（消息流、thinking 面板、tool call 折叠 +
diff 高亮、状态栏、会话脚注、模型选择弹窗、多套主题）移植了过来。

> 默认的交互前端是 Aider 式 REPL（`minicode`）；TUI 是显式 `minicode tui`
> 开启的全屏版。两者共用同一套 slash 命令和会话/权限核心。

---

## 1. 布局

```
┌──────────────────────────────────────────────────────────────┐
│ minicode — openai/gpt-4o-mini                          header │
├──────────────┬───────────────────────────────────────────────┤
│ sessions     │  you                                            │
│  ＋ New      │    hello agent                                  │
│  Today       │  assistant 14:22                                │
│  > 查看项目  │    💭 Thinking…                                 │
│  Yesterday   │    I'll check.                                  │
│  ...         │    › bash  {command: pwd}                       │
│              │      ✓ done                                     │
│              │  system                                         │
│              │    /help ...                                    │
├──────────────┼───────────────────────────────────────────────┤
│              │  $0.01  in:1.5K out:812  cache 4.1K  ▓▓░░ 25%  6 msgs  model│  status
│              │  openai · gpt-4o-mini   ~/workspace            │  footer
│              │  ↑ history  / commands  Shift+Enter  Enter  Esc│  hints
│              │  ┌─────────────────────────────────────────┐   │
│              │  │ Ask minicode anything...                │   │
│              │  └─────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
```

| 区域 | 内容 |
|---|---|
| 左侧会话栏 | 只显示当前项目的会话，按 Today / Yesterday / Older 分组，点击切换，`＋ New session` 开新会话，高亮后按 `d` 删除 |
| 消息流 | 用户消息 / 助手 Markdown / 可折叠 tool call / 系统行 |
| thinking 面板 | 推理模型的思考过程，实时显示、结束后折叠成一行 |
| 状态栏 | cost · in/out tokens · prompt-cache tokens · context 用量 · 消息数 · 模型 · 压缩次数 |
| 会话脚注 | provider · model · workspace |
| 提示条 | 输入框快捷键提示，运行中显示 Esc 可中断 |
| 输入框 | 多行输入 + `/` 命令弹窗 |

---

## 2. 快捷键

| 按键 | 作用 |
|---|---|
| `/` | 打开命令弹窗（继续打字可过滤） |
| `up` / `down`（或 `ctrl+p` / `ctrl+n`） | 在弹窗里移动 |
| `tab` / `enter` | 运行高亮的命令 |
| `escape` | 关闭弹窗 · 中断正在运行的 turn |
| `enter` | 发送 · `shift+enter` 插入换行 |
| `ctrl+up` / `ctrl+down` | 翻看之前输入的提示 |
| `ctrl+p` | 命令面板 |
| `ctrl+n` | 新会话 · `ctrl+l` 清屏 |
| `d` | 删除左侧高亮会话（需先聚焦会话栏） |
| `ctrl+t` | 深色 / 浅色 |
| `ctrl+e` | 循环切换 5 套配色 |
| `ctrl+c` | 退出 |

### 模型选择

在输入框输入 `/model` 回车，会弹出**模型选择弹窗**：

- 列出所有 `provider/model`，当前模型带 `(current)` 标记
- 输入即过滤，回车选中，`Esc` 取消
- 鼠标点击也能选

### 权限确认

当 agent 需要执行受保护操作时，输入区上方出现权限条：

```
permission  bash (bash)
   pwd && ls -la
 [y] once   [a] always   [n] reject
```

- 键盘：`y`（本次允许）/ `a`（总是允许）/ `n`（拒绝）/ `Esc`（拒绝）
- 鼠标：直接点 `[y]` / `[a]` / `[n]`

### Tool call

- 每个工具调用一行：`● 进行中` → `✓ 完成` / `✗ 失败`
- 点击该行展开/收起输出
- `edit` / `write` 返回 unified diff，预览里**新增行绿色、删除行红色、`@@` 青色**

---

## 3. 主题

5 套来自 pydantic-deepagents 的调色板，`ctrl+e` 循环：

| 名称 | 风格 |
|---|---|
| `default` | 暖琥珀 × 近黑 |
| `emerald` | 冷翡翠 |
| `ocean` | 蓝青 |
| `rose` | 玫红 |
| `minimal` | 极简单色 |

配置里 `ui.theme: <name>` 可固定默认主题（`auto` 使用 `default`）。

---

## 4. 会话

- 左侧点击任意历史会话 → 切换并回放该会话
- 左侧只显示当前项目的会话；想看全部历史用 `minicode sessions`
- 左侧高亮会话后按 `d` → 删除该会话；若删除的是当前会话，会自动开一个新会话
- `＋ New session`（或 `ctrl+n`、`/new`、`/clear`）→ 新会话
- `/resume <id>`、`/fork [id]`、`/title <text>` 同 REPL
- 空会话不会自动落盘；历史里残留的空白占位可用
  `minicode session prune` 一键清理（顺带修复旧版污染标题）

---

## 5. 提示与技巧

- 全部 slash 命令都从 `/` 弹窗可达，无需记 `/help`
- 命令带参数时（`/resume <id>`、`/title <text>`）选中后会留在输入框等你补参数
- 运行中按 `escape` 请求中断，agent 会在下一步边界停下
- `minicode run "..."` 和管道输入不需要 Textual，只走 REPL
