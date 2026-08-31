# minicode

A lightweight, pure-Python **OpenCode-like coding agent core**, built on top of
[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent).

It is not a clone of OpenCode — it is the *coding agent core* of OpenCode
(multi-step loop, tools, permissions, sessions, context management) re-implemented
as a small Python project, reusing mini-swe-agent's agent loop and command
execution instead of rewriting them.

```
Requirements: Python >= 3.10, one API key. No Node.js / TypeScript / Bun.
```

---

## Table of contents

1. [Install](#1-install)
2. [Configure an API key](#2-configure-an-api-key)
3. [Providers and models](#3-providers-and-models)
4. [Start the CLI](#4-start-the-cli)
5. [Slash commands](#5-slash-commands)
6. [Sessions](#6-sessions)
7. [Permissions](#7-permissions)
8. [Tools](#8-tools)
9. [Context management](#9-context-management)
10. [Configuration reference](#10-configuration-reference)
11. [Run the tests](#11-run-the-tests)
12. [End-to-end example](#12-end-to-end-example)
13. [Architecture](#13-architecture)
14. [Related documents](#14-related-documents)

---

## 1. Install

```bash
# from a checkout
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .

# or, once published
pip install minicode
```

Verify:

```bash
python -m minicode --help
python -m minicode tools
```

> `python -m minicode` is the canonical entry point. A `minicode` console script
> is installed alongside it.

---

## 2. Configure an API key

The easiest way is the interactive provider wizard — no need to hand-edit YAML:

```bash
minicode providers login
```

It lets you pick a preset (`openai`, `deepseek`, `anthropic`, `local`) or enter
a custom OpenAI-compatible endpoint, then prompts for the API key and writes the
config file for you.

If you're already inside the TUI, you can configure it there too:

```text
/login
```

`/login` does the same interactive wizard, then reloads the provider list and
switches you to the newly configured model. `/provider` is an alias.

You can also do the same thing non-interactively:

```bash
minicode providers login openai --api-key sk-...
minicode providers list
```

If you prefer to manage the config file by hand, generate a starter config and
set the environment variable it points at:

```bash
minicode config init     # writes the global config file
minicode config path     # shows where it went
```

```bash
# macOS / Linux
export OPENAI_API_KEY=sk-...

# Windows (PowerShell)
$env:OPENAI_API_KEY = "sk-..."
```

Any OpenAI-compatible endpoint works. For example, Sensenova:

```bash
export SENSENOVA_API_KEY=sk-...
```

with this in the config file:

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

Check that everything resolves:

```bash
minicode models
# * sensenova [openai_compat, key-ok]: deepseek-v4-flash
```

Keys can also be inlined (`api_key: sk-...`), but then keep the file private.
Prefer `api_key_env`.

A `.env` file in the working directory is loaded automatically.

---

## 3. Providers and models

`type` selects the wire protocol:

| `type` | Covers |
|---|---|
| `openai_compat` | OpenAI, DeepSeek, Qwen, Sensenova, vLLM, Ollama, OpenRouter, Groq, Together … |
| `anthropic_compat` | Anthropic messages API |
| `litellm` | any of the 100+ providers LiteLLM supports (needs `pip install litellm`) |
| `my.module:MyProvider` | your own `Provider` subclass |

Declare as many as you like:

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

  ollama:                     # local, no key needed
    type: openai_compat
    base_url: http://localhost:11434/v1
    models: [qwen2.5-coder:7b]
```

Pick one for this run:

```bash
minicode -m deepseek/deepseek-chat          # provider/model
minicode -m gpt-4o-mini                     # bare model, searched across providers
```

Switch mid-session with `/model` (see below).

### Reasoning models

DeepSeek-V4/R1, o-series, QwQ and friends emit their thinking in
`reasoning_content` and often leave `content` empty. minicode handles this:

* reasoning is streamed to the terminal in a dimmed "thinking…" block,
  visually separate from the answer;
* if a reply contains only reasoning, the agent is nudged **once** to produce a
  visible answer instead of silently ending the turn.

### Rate limits

Cheap endpoints are usually limited by *requests per minute*. Options:

```yaml
providers:
  mine:
    min_request_interval: 6   # minimum seconds between requests
    max_retries: 6
    retry_delay: 10           # exponential backoff base
    retry_max_delay: 90       # backoff ceiling
```

`Retry-After` sent by the server always wins over the computed backoff.

---

## 4. Start the CLI

```bash
minicode                                   # Aider-style REPL (default)
minicode tui                               # full-screen Textual TUI (opt-in)
minicode run "fix the failing tests"       # one task, then exit
minicode --yolo run "refactor utils.py"    # auto-approve permissions
minicode --cwd /path/to/project            # work in another directory
minicode --no-stream run "..."             # no streaming output
```

There are two interactive front-ends:

* **REPL (default)** — Aider's interaction model: one input line at the bottom
  (prompt_toolkit with history), the whole conversation scrolling above it,
  slash commands, and inline permission prompts.
* **Textual TUI (opt-in, `minicode tui`)** — OpenCode-style full-screen layout:
  a session rail on the left (scoped to the current project, grouped by
  Today / Yesterday / Older), a conversation stream (user / assistant markdown /
  collapsible tool calls with diff highlighting), a live thinking panel for
  reasoning models, a status bar (cost / tokens / context / messages / model),
  a session footer (provider · model · workspace), a composer with a command
  popover, and a filterable model picker. See [`docs/TUI.md`](docs/TUI.md) for
  the full walkthrough.

**REPL keys**

| Key | Action |
|---|---|
| `enter` | send · `Esc`+`Enter` (or `Alt`+`Enter`) inserts a newline |
| `↑` / `↓` | walk back through previous prompts |
| `/` | slash commands (`/help`, `/model`, `/resume`, ...) |

**TUI keys**

| Key | Action |
|---|---|
| `/` | open the command popover, right above the composer |
| `up` / `down` (or `ctrl+p` / `ctrl+n`) | move through the popover |
| `tab` / `enter` | run the highlighted command |
| `escape` | close the popover · interrupt the running turn |
| `enter` | send · `shift+enter` inserts a newline |
| `ctrl+up` / `ctrl+down` | walk back through previous prompts |
| `ctrl+p` | command palette |
| `ctrl+n` | new session · `ctrl+l` clear transcript |
| `d` | delete the highlighted session in the left rail |
| `ctrl+t` | toggle dark / light |
| `ctrl+e` | cycle the colour palette (default / emerald / ocean / rose / minimal) |
| `ctrl+c` | quit |

Type a single `/` and every command appears, filtered as you keep typing — you
never need `/help` to find out what exists. Picking a command runs it straight
away unless it takes an argument (`/resume <id>`, `/model <spec>`, `/title
<text>`, ...); those get dropped into the composer with the caret waiting where
the argument goes. In the TUI, `/model` with no argument opens a filterable
model picker instead of dumping the registry to the transcript.

`minicode run` and piped usage only ever need the REPL path; the Textual TUI is
started explicitly with `minicode tui`.

`run` is non-interactive by default: permissions resolve from the config, and
anything left as `ask` is refused (fail-closed). Use `--yolo` to auto-approve.

---

## 5. Slash commands

Inside the interactive TUI:

| Command | What it does |
|---|---|
| `/help` | list commands |
| `/login [provider]` | add/update an API key and model interactively in the TUI |
| `/provider [provider]` | alias for `/login` |
| `/model [provider/model]` | show providers+models, or switch model (bare `/model` opens the TUI picker) |
| `/models` | list configured providers and models |
| `/session` | show the current session |
| `/sessions` | list saved sessions |
| `/resume <id>` | resume a session by id |
| `/fork [id]` | fork the current (or given) session, optionally at an earlier point |
| `/title <text>` | rename the current session |
| `/new` | start a brand new session |
| `/clear` | alias for `/new` (fresh history) |
| `/compact` | force context compaction now |
| `/tools` | list the available tools |
| `/permission` | show the active permission rules |
| `/status` | agent/session statistics |
| `/commands` / `/command` | manage custom commands |
| `/exit` | quit (`/quit`, Ctrl-D also work) |

Multi-line input: `Esc`+`Enter` (or `Alt`+`Enter`) inserts a newline.

---

## 6. Sessions

Sessions are plain JSON files; nothing about them is tied to a provider or model.

```bash
minicode sessions                              # list
minicode sessions --cwd /path/to/project       # list only this project's sessions
minicode --resume ses_ab12cd                   # continue one
minicode session show ses_ab12cd
minicode session delete ses_ab12cd
minicode session delete --all                  # delete everything (asks confirmation)
minicode session delete --all --cwd /path      # delete only this project's sessions
minicode session delete --all --yes            # non-interactive delete everything
minicode session prune                         # delete empty "New session" placeholders + fix stale titles
```

In the TUI, a session is created automatically. The left rail is scoped to the
current project and groups sessions by Today / Yesterday / Older; click one to
resume it, or press `d` on the highlighted session to delete it. `/fork` makes
an independent copy — handy for trying a different approach without losing the
original:

```
/fork                 # fork at the current point
```

```python
# programmatic
from minicode.session.manager import SessionManager

sessions = SessionManager()
session = sessions.create(provider="openai", model="gpt-4o-mini", cwd=".")
fork = sessions.fork(session.id, at_message=10)  # optional truncation point
```

A session stores: messages, tool-call history (duration, ok, error code,
truncation), current provider/model, cwd, title (auto-derived from the first
user message) and arbitrary metadata.

---

## 7. Permissions

Three actions — `allow`, `deny`, `ask` — applied at three levels:

```yaml
permission:
  read: allow                     # tool level
  glob: allow
  grep: allow
  write: ask
  edit: ask
  delete: ask
  bash:                           # command level
    "git status*": allow
    "python -m pytest*": allow
    "rm -rf **": deny
    "*": ask
  apply_patch:                    # path level
    "**/*.env": deny
    "src/**": allow
```

Pattern syntax:

| Pattern | Meaning |
|---|---|
| `*` | within one path segment (does **not** cross `/`) |
| `**` | across segments |
| `**/foo` | matches `foo` **and** `a/b/foo` (ripgrep/gitignore behaviour) |
| `?` | one character |

> **Gotcha:** because `*` stops at `/`, `rm -rf *` does not match
> `rm -rf /etc`. Use `rm -rf **` to actually block it. The shipped default
> config includes both forms.

Resolution rules:

1. **The most specific matching rule wins.** A catch-all `*: ask` at the end of
   the file does not override `read: allow` above it.
2. Ties go to the later rule, so "always allow" approvals override the config.
3. Unmatched falls back to `ask` — never to a silent allow.
4. If there is no way to ask (no TTY, `run` without `--yolo`), the operation is
   **refused**.

Destructive commands (`rm`, `rmdir`, `shred`, …) additionally require the
`delete` permission for their targets.

Non-interactive auto-approval:

```bash
minicode --yolo run "..."
```

or `mode: auto` in the config (still honours explicit `deny` rules).

---

## 8. Tools

| Tool | Purpose |
|---|---|
| `read` | read a file with line numbers; `offset`/`limit` paging |
| `write` | create or overwrite a file (creates parent dirs, shows a diff) |
| `edit` | replace an exact string; must be unique unless `replace_all` |
| `apply_patch` | multi-file add/update/delete/move in one call |
| `glob` | find files by pattern |
| `grep` | regex search over file contents |
| `bash` | run a shell command (tests, builds, git) |

All tools share one protocol:

* a JSON Schema description, sent to the model as-is;
* a `permission` key (`read`, `write`, `edit`, `bash`, …);
* a `ToolResult(title, output, metadata, error)` return value — **tools never
  raise**, failures become structured observations the agent can recover from.

Large output is truncated (line + byte caps) and the full text is written to
disk, with the path reported back so the agent can page through it.

If a model returns invalid or truncated tool-call arguments, minicode does not
execute the tool with empty arguments. Like OpenCode, the raw parser error is
fed straight back to the model so it can retry with corrected/smaller input.

Add your own:

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

## 9. Context management

Long coding sessions die from unbounded history. Three mechanisms, in
increasing aggressiveness:

1. **Truncation** — every tool result is capped; the full text goes to disk.
2. **Pruning** — old *tool outputs* are replaced by a placeholder. The tool
   *call* stays, so the history remains readable and the model still knows what
   it did. The last step is always protected.
3. **Compaction** — older turns are summarised into one message. The recent tail
   is preserved verbatim inside a token budget.

```yaml
context:
  max_tokens: 120000
  auto_compact: true
  compact_threshold: 0.85       # compact at 85% of max_tokens
  prune: true
  prune_protect_tokens: 40000
  preserve_recent_tokens: 20000 # budget for the verbatim tail
  tail_turns: null              # hard cap; null = use the budget
  tool_output_max_lines: 2000
  tool_output_max_bytes: 51200
```

Notes:

* Compaction also triggers on a provider `context length` error — the agent
  compacts and retries once instead of crashing.
* Split points are **user turns** in a multi-turn session, and **assistant
  steps** within a single long turn. Without the latter, a long single-task run
  could never be compacted — exactly the runaway-history case this exists for.
* Resuming or forking a session rebuilds history by pruning only; it never
  costs a model call.
* Token counting is a deliberately conservative heuristic (no tokenizer
  dependency). Compacting slightly early is much safer than blowing the window.

---

## 10. Configuration reference

Precedence, highest first:

```
--set key=value  >  MINICODE_* env  >  ./.minicode/config.yaml  >  global config  >  built-in defaults
```

```bash
minicode config path      # where the config files live
minicode config show      # the merged, effective config
minicode config init      # write a starter file
minicode --set agent.step_limit=50 --set ui.stream=false
minicode --set ui.theme=emerald   # TUI palette: default | emerald | ocean | rose | minimal
```

See [`config.example.yaml`](config.example.yaml) for every option with comments.
Built-in defaults live in `src/minicode/config/default.yaml`.

---

## 11. Run the tests

```bash
pip install -e ".[dev]"
pytest                      # unit + integration (E2E skips without credentials)
pytest tests/unit -q
pytest tests/integration -q
pytest --cov=minicode -q
```

E2E runs against a real model and is skipped unless you provide one:

```bash
export MINICODE_E2E_API_KEY=...
export MINICODE_E2E_BASE_URL=https://...
export MINICODE_E2E_MODEL=...
pytest tests/e2e -q -s
```

---

## 12. End-to-end example

`tests/e2e/fixtures/buggy_project` is a tiny billing library with three
deliberate bugs. A real run (see
[`docs/e2e-transcript-example.log`](docs/e2e-transcript-example.log)):

```
$ minicode --yolo run "Run the test suite and fix the bugs in billing.py so all tests pass."

→ bash   python -m pytest -q test_billing.py
  7 failed, 16 passed
→ read   billing.py
→ edit   monthly_total   : months > MONTHS_PER_YEAR  →  >=
→ edit   apply_coupon    : return amount - price     →  return 0
→ edit   prorate         : days_used + 1             →  days_used
→ bash   python -m pytest -q test_billing.py
  23 passed
```

The three bugs:

1. `monthly_total` — annual discount used `>` instead of `>=`, so a 12-month
   term was never discounted.
2. `apply_coupon` — a flat coupon larger than the price returned a positive
   value instead of clamping to zero.
3. `prorate` — an off-by-one charged `days_used + 1`.

The agent is not told what the bugs are. It reads the tests, runs them,
localises each failure and fixes the implementation.

---

## 13. Architecture

```
CLI / TUI
    ↓
Session
    ↓
Agent  ┌──────────┼──────────┐
       ↓          ↓          ↓
    Context     Tools     Provider
                              ↓
                            Model
```

```
src/minicode/
  agent/        agent loop (extends mini-swe-agent), state, prompts
  tools/        unified tool interface + registry + 7 builtins
  providers/    OpenAI / Anthropic / LiteLLM / scripted
  session/      persistence, fork, resume
  permission/   allow / deny / ask
  context/      truncation, pruning, compaction
  config/       layered settings
  project.py    repo ecosystem detection -> keeps minicode language-agnostic
  ui/           EventSink + UIPort, Rich console, Textual front-end
    console.py  Rich REPL (default)
    textual/    Textual TUI (opt-in, `minicode tui`) — app.py / widgets.py / bridge.py / modals.py / theme.py
  storage/      JSON store, platform paths
  cli/          argparse entry point
```

Both front-ends implement the same two contracts, so the core knows neither:

```
        cli/commands.py  ──UIPort──>  ui/console.py        (Rich REPL, default)
                                      ui/textual/bridge.py (Textual TUI, opt-in)
        agent  ──EventSink─────────────────^
```

Guarantees, each covered by tests:

* the agent depends only on the `Provider` abstraction;
* the agent never implements a tool — everything goes through the registry;
* tools never import the UI;
* sessions never hold a model reference;
* permissions are a standalone module;
* the core and the slash commands touch the UI only through `UIPort`;
* no prompt, tool or default rule hard-codes a programming language.

---

## 14. Related documents

| Document | Contents |
|---|---|
| [`docs/TUI.md`](docs/TUI.md) | full walkthrough of the Textual TUI: layout, keys, model picker, themes, sessions |
| [`MINISWE_DIFF.md`](MINISWE_DIFF.md) | what is reused from mini-swe-agent, what changed and why |
| [`OPENCODE_GAP.md`](OPENCODE_GAP.md) | which OpenCode features exist, which are out of scope, and why |
| [`config.example.yaml`](config.example.yaml) | fully commented configuration |
| [`docs/e2e-transcript-example.log`](docs/e2e-transcript-example.log) | a real bug-fix run transcript |

---

## License

MIT
