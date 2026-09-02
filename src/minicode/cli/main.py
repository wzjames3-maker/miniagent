"""Command line entry point (``python -m minicode``)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from minicode.config.settings import (
    Settings,
    load_settings,
    render_permission_yaml,
)
from minicode.session.manager import SessionManager
from minicode.storage.paths import data_dir, global_config_file, project_config_file
from minicode.ui.console import ConsoleUI

__all__ = ["build_parser", "main", "app_entry", "EXAMPLES"]


EXAMPLES = """\
examples:
  minicode                                  start the Aider-style REPL
  minicode tui                              start the full-screen Textual TUI
  minicode run "fix the failing tests"      run one task and exit
  minicode -m deepseek/deepseek-chat        start with a specific model
  minicode --yolo run "refactor utils.py"   auto-approve every permission
  minicode sessions                         list saved sessions
  minicode session delete --all --yes       delete every saved session
  minicode resume ses_ab12cd                continue a session
  minicode models                           list configured providers/models
  minicode providers login                  add/update a provider interactively
  minicode config init                      write a starter config file
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="minicode",
        description="minicode - a lightweight OpenCode-like coding agent (built on mini-swe-agent)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )
    parser.add_argument("--version", action="version", version=_version_string())
    parser.add_argument("-m", "--model", help="provider/model to use, e.g. openai/gpt-4o-mini")
    parser.add_argument("--cwd", help="working directory (defaults to the current directory)")
    parser.add_argument("--config", help="path to a config file (overrides global + project config)")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override config, repeatable")
    parser.add_argument("--yolo", action="store_true", help="auto-approve every permission (non-interactive safe)")
    parser.add_argument("--session", help="session id to open (created if missing)")
    parser.add_argument("--resume", metavar="ID", help="resume an existing session")
    parser.add_argument("--no-stream", action="store_true", help="disable streaming output")
    parser.add_argument("--quiet", action="store_true", help="suppress the banner and status lines")

    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="run a single task and exit")
    run.add_argument("task", nargs="+", help="the task description")
    run.add_argument("--save", help="save the trajectory to this path")

    sessions = sub.add_parser("sessions", help="list saved sessions")
    sessions.add_argument("--cwd", help="only list sessions for this project directory")

    sub.add_parser("models", help="list configured providers and models")
    sub.add_parser("tools", help="list the enabled tools")
    inspect = sub.add_parser("inspect", help="open the TUI trajectory/session inspector")
    inspect.add_argument("session_id", nargs="?", help="session id (defaults to the most recent session)")
    sub.add_parser("tui", help="start the full-screen Textual TUI (the default is the Aider-style REPL)")

    providers = sub.add_parser("providers", help="manage AI providers and API keys interactively")
    providers_sub = providers.add_subparsers(dest="action")
    providers_sub.add_parser("list", help="list configured providers")
    login = providers_sub.add_parser("login", help="add or update a provider interactively")
    login.add_argument("name", nargs="?", help="provider name (openai, deepseek, anthropic, local, or a custom name)")
    login.add_argument("--api-key", help="API key (omit to prompt interactively)")
    login.add_argument("--base-url", help="base URL for the API endpoint")
    login.add_argument("--models", help="comma-separated model names")
    login.add_argument("--default-model", help="default model for this provider")
    remove = providers_sub.add_parser("remove", help="remove a configured provider")
    remove.add_argument("name", help="provider name")

    session = sub.add_parser("session", help="manage sessions")
    session_sub = session.add_subparsers(dest="action")
    show = session_sub.add_parser("show", help="show one session")
    show.add_argument("id")
    delete = session_sub.add_parser("delete", help="delete one session (or --all)")
    delete_target = delete.add_mutually_exclusive_group(required=True)
    delete_target.add_argument("id", nargs="?", help="session id to delete")
    delete_target.add_argument("--all", action="store_true", help="delete every session")
    delete.add_argument("--cwd", help="when --all is used, only delete sessions for this project directory")
    delete.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    fork = session_sub.add_parser("fork", help="fork one session")
    fork.add_argument("id")
    fork.add_argument("--at", type=int, help="number of messages to inherit")
    session_sub.add_parser("prune", help="delete empty 'New session' placeholder files")

    config = sub.add_parser("config", help="inspect / create configuration")
    config_sub = config.add_subparsers(dest="action")
    config_sub.add_parser("show", help="print the effective configuration")
    config_sub.add_parser("path", help="print the config file locations")
    config_sub.add_parser("init", help="write a starter config file")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    cwd = str(Path(args.cwd).resolve()) if args.cwd else None
    try:
        settings = load_settings(cwd=cwd, config_path=args.config, overrides=args.set)
    except Exception as exc:  # noqa: BLE001 - ConfigError is a ValueError subclass via Pydantic
        # Central error handling: config problems map to exit 2.
        from minicode.errors import MinicodeError

        if isinstance(exc, (MinicodeError, ValueError)):
            print(f"error: {exc}", file=sys.stderr)
            return 2
        raise

    if args.command is None:
        return _interactive(args, settings, cwd)
    if args.command == "run":
        return _run_once(args, settings, cwd)
    if args.command == "sessions":
        return _cmd_sessions(args, settings)
    if args.command == "models":
        return _list_providers(settings)
    if args.command == "tools":
        return _cmd_tools(settings, cwd)
    if args.command == "inspect":
        return _cmd_inspect(args)
    if args.command == "tui":
        return _cmd_tui(args, settings, cwd)
    if args.command == "providers":
        return _cmd_providers(args, settings)
    if args.command == "session":
        return _cmd_session(args, settings)
    if args.command == "config":
        return _cmd_config(args, settings)
    parser.print_help()
    return 1


def app_entry() -> None:
    """Console-script entry point."""
    raise SystemExit(main())


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #
def _build_app(args: argparse.Namespace, settings: Settings, cwd: str | None, *, quiet: bool = False):
    from minicode.cli.app import InteractiveApp

    ui = ConsoleUI(settings.ui)
    return InteractiveApp(
        settings,
        cwd=cwd,
        model=args.model,
        session_id=args.resume or args.session,
        yolo=args.yolo,
        non_interactive=args.command == "run",
        stream=not args.no_stream,
        ui=ui,
    )


def _interactive(args: argparse.Namespace, settings: Settings, cwd: str | None) -> int:
    # The default front-end is the classic Rich REPL (prompt_toolkit + rich),
    # the same interaction model as Aider: one input line at the bottom, the
    # whole conversation scrolling above it, slash commands and inline
    # permission prompts. The full-screen Textual TUI is opt-in via `minicode tui`.
    return _build_app(args, settings, cwd).repl()


def _run_once(args: argparse.Namespace, settings: Settings, cwd: str | None) -> int:
    task = " ".join(args.task)
    app = _build_app(args, settings, cwd)
    if not args.quiet:
        app.ui.print_info(f"minicode {_version_string()} | {app.provider.model_id} | session {app.session.id}")
    result = app.run_task(task)
    if args.save:
        app.agent.save(Path(args.save))
        app.ui.print_info(f"trajectory saved to {args.save}")
    status = str(result.get("exit_status", ""))
    if status in {"LimitsExceeded", "TimeExceeded", "Error", "RepeatedFormatError"}:
        return 1
    return 0


def _cmd_sessions(args: argparse.Namespace, settings: Settings) -> int:
    sessions = SessionManager()
    rows = [
        row
        for row in sessions.summaries(cwd=args.cwd)
        if row.get("messages") or (row.get("title") and row["title"] != "New session")
    ]
    if not rows:
        print("no saved sessions")
        return 0
    for row in rows:
        when = datetime.fromtimestamp(row["updated_at"]).strftime("%Y-%m-%d %H:%M")
        cwd = (row.get("cwd") or "-")[:30]
        print(
            f"{row['id']}  {when}  {row['title'][:50]:<50} {row['model'] or '-':<28} "
            f"{cwd:<30} {row['messages']:>4} msg  {row['tool_calls']:>4} tools"
        )
    return 0


def _cmd_session(args: argparse.Namespace, settings: Settings) -> int:
    sessions = SessionManager()
    action = args.action or "show"
    if action == "show":
        session = sessions.get(args.id)
        if session is None:
            print(f"session not found: {args.id}", file=sys.stderr)
            return 1
        print(f"id:       {session.id}")
        print(f"title:    {session.title}")
        print(f"model:    {session.model_id or '-'}")
        print(f"cwd:      {session.cwd}")
        print(f"messages: {session.message_count}")
        print(f"tools:    {session.tool_call_count}")
        if session.parent_id:
            print(f"parent:   {session.parent_id}")
        return 0
    if action == "delete":
        if args.all:
            count = len(sessions.list(cwd=args.cwd))
            if count == 0:
                print("no sessions to delete")
                return 0
            if not args.yes:
                if not sys.stdin.isatty():
                    print("refusing to delete without confirmation; pass --yes to proceed", file=sys.stderr)
                    return 1
                answer = input(f"Delete {count} session(s)? [y/N] ").strip().lower()
                if answer not in {"y", "yes"}:
                    print("aborted")
                    return 1
            removed = sessions.delete_all(cwd=args.cwd)
            print(f"deleted {removed} session(s)")
            return 0
        return 0 if sessions.delete(args.id) else 1
    if action == "fork":
        try:
            forked = sessions.fork(args.id, at_message=args.at)
        except KeyError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(forked.id)
        return 0
    if action == "prune":
        removed = sessions.prune_empty()
        fixed = sessions.repair_titles()
        print(f"pruned {removed} empty placeholder session(s), repaired {fixed} title(s)")
        return 0
    print(f"unknown action: {action}", file=sys.stderr)
    return 1


def _cmd_tools(settings: Settings, cwd: str | None) -> int:
    from minicode.tools.registry import build_default_registry

    registry = build_default_registry(
        enabled=settings.tools.enabled or None,
        cwd=cwd or ".",
        bash_timeout=settings.tools.bash_timeout,
    )
    print(registry.describe())
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    """Open mini-swe-agent's Textual trajectory inspector on a saved session."""
    from minicode.session.manager import SessionManager
    from minicode.storage.paths import sessions_dir

    if args.session_id:
        path = sessions_dir() / f"{args.session_id}.json"
        if not path.is_file():
            print(f"session not found: {args.session_id}", file=sys.stderr)
            return 1
        files = [path]
    else:
        session = SessionManager().most_recent()
        if session is None:
            print("no saved sessions to inspect", file=sys.stderr)
            return 1
        files = [sessions_dir() / f"{session.id}.json"]

    try:
        from minisweagent.run.utilities.inspector import TrajectoryInspector
    except ImportError as exc:  # pragma: no cover
        print(f"inspector unavailable: {exc}", file=sys.stderr)
        return 1

    TrajectoryInspector(files).run()
    return 0


def _cmd_tui(args: argparse.Namespace, settings: Settings, cwd: str | None) -> int:
    """Start the full-screen Textual TUI explicitly."""
    from minicode.ui.textual import run_tui

    run_tui(
        settings,
        cwd=cwd,
        model=args.model,
        session_id=args.resume or args.session,
        yolo=args.yolo,
    )
    return 0


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# providers (interactive configuration)
# --------------------------------------------------------------------------- #
def _cmd_providers(args: argparse.Namespace, settings: Settings) -> int:
    from minicode.cli.provider_config import configure_provider, remove_provider

    action = args.action or "login"
    if action == "list":
        return _list_providers(settings)
    if action == "login":
        try:
            result = configure_provider(
                args.name,
                api_key=args.api_key,
                base_url=args.base_url,
                models=args.models,
                default_model=args.default_model,
                config_path=args.config,
            )
        except (ValueError, KeyboardInterrupt) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {result.path}")
        print(f"provider '{result.name}' configured.")
        if result.api_key_set:
            print("API key stored in the config file. Keep this file private.")
        return 0
    if action == "remove":
        from minicode.storage.paths import global_config_file

        if not remove_provider(args.name, config_path=args.config):
            print(f"provider not found: {args.name}", file=sys.stderr)
            return 1
        target = args.config or global_config_file()
        print(f"removed provider '{args.name}' from {target}")
        return 0
    print(f"unknown action: {action}", file=sys.stderr)
    return 1


def _list_providers(settings: Settings) -> int:
    from minicode.providers.registry import build_registry

    registry = build_registry(settings.model_dump())
    if not registry.provider_names():
        print("no providers configured - run `minicode providers login` to add one")
        return 0
    print(registry.describe())
    return 0


def _cmd_config(args: argparse.Namespace, settings: Settings) -> int:
    action = args.action or "show"
    if action == "path":
        print(f"data dir:   {data_dir()}")
        print(f"global:     {global_config_file()}")
        print(f"project:    {project_config_file()}")
        return 0
    if action == "show":
        import yaml

        print(yaml.safe_dump(settings.model_dump(mode="json"), sort_keys=False, allow_unicode=True))
        return 0
    if action == "init":
        target = global_config_file()
        if target.exists():
            print(f"config already exists: {target}")
            return 0
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(STARTER_CONFIG, encoding="utf-8")
        print(f"wrote {target}")
        print("Now add your API key (environment variable or `api_key:` in the file).")
        return 0
    print(f"unknown action: {action}", file=sys.stderr)
    return 1


def _version_string() -> str:
    from minisweagent import __version__ as mini_version

    import minicode

    return f"{minicode.__version__} (mini-swe-agent {mini_version})"


STARTER_CONFIG_TEMPLATE = """\
# minicode configuration - generated by `minicode config init`
#
# API keys: prefer environment variables (api_key_env). You can also inline
# `api_key: sk-...` but then keep this file private.

default_provider: openai
default_model: gpt-4o-mini

providers:
  openai:
    type: openai_compat
    api_key_env: OPENAI_API_KEY
    base_url: https://api.openai.com/v1
    models:
      - gpt-4o-mini
      - gpt-4o

  # Any OpenAI-compatible endpoint works, e.g. DeepSeek:
  deepseek:
    type: openai_compat
    api_key_env: DEEPSEEK_API_KEY
    base_url: https://api.deepseek.com
    models:
      - deepseek-chat
      - deepseek-reasoner

  anthropic:
    type: anthropic_compat
    api_key_env: ANTHROPIC_API_KEY
    models:
      - claude-sonnet-4-5
      - claude-opus-4-1

  # Local models (vLLM, Ollama, LM Studio, ...):
  local:
    type: openai_compat
    base_url: http://localhost:11434/v1
    api_key_env: OPENAI_API_KEY
    models:
      - qwen2.5-coder:32b

agent:
  step_limit: 200
  cost_limit: 10.0
  doom_loop_threshold: 3

{permission_block}

context:
  max_tokens: 120000
  auto_compact: true
  compact_threshold: 0.85

tools:
  bash_timeout: 120
"""

STARTER_CONFIG = STARTER_CONFIG_TEMPLATE.replace("{permission_block}", render_permission_yaml())
