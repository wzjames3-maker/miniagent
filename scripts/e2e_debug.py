"""Run the E2E bug-fix task against a real model and dump a full transcript.

Used to debug the agent against a live endpoint without pytest swallowing the
output. Configure through the environment:

    MINICODE_E2E_API_KEY / MINICODE_E2E_BASE_URL / MINICODE_E2E_MODEL

Usage:
    python scripts/e2e_debug.py [--workspace DIR] [--task-file FILE]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURE = ROOT / "tests" / "e2e" / "fixtures" / "buggy_project"

TASK = """\
Several tests in this project are failing. Your job:

1. Run the test suite with `"$PYTHON_BIN" -m pytest -q test_billing.py` and read the failures.
2. Find the root cause of each failure in `billing.py`.
3. Fix `billing.py` so that every test passes.

Hard rules:
- Do NOT modify `test_billing.py` or the tests in any way. The tests define correct behaviour.
- Do NOT change the public function signatures.
- Fix the implementation, not the expectations.
- Run the tests again after each fix and keep going until they are all green.
- Report which functions you changed and why.
"""


class Transcript:
    """A sink that prints and records every event."""

    def __init__(self, log: Path):
        self.log = log
        self._handle = log.open("w", encoding="utf-8")

    def _write(self, text: str) -> None:
        print(text, end="", flush=True)
        self._handle.write(text)
        self._handle.flush()

    def on_stream_event(self, event) -> None:
        if event.type == "text_delta":
            self._write(event.text)
        elif event.type == "reasoning_delta":
            self._write(event.text)
        elif event.type == "tool_call_start":
            self._write(f"\n>>> {event.tool_call.name}\n")
        elif event.type == "tool_call_end":
            self._write(f"    args: {event.tool_call.arguments}\n")

    def on_assistant_message(self, message) -> None:
        self._write("\n")

    def on_tool_start(self, call) -> None:
        pass

    def on_tool_result(self, call, result) -> None:
        body = result.render()
        self._write(f"<<< {call.name} ok={result.ok}\n{body[:4000]}\n")

    def on_turn_start(self, task) -> None:
        self._write(f"\n===== TURN START =====\n{task}\n")

    def on_turn_end(self, result) -> None:
        self._write(f"\n===== TURN END: {result.exit_status} =====\n{result.submission}\n")

    def on_error(self, message, *, details=None) -> None:
        self._write(f"\n!!! ERROR: {message} {details or ''}\n")

    def on_status(self, status) -> None:
        pass

    def on_compaction(self, info) -> None:
        self._write(f"\n--- compaction: {info.get('before_tokens')} -> {info.get('after_tokens')} ---\n")

    def on_permission_denied(self, permission, patterns) -> None:
        self._write(f"\n--- permission denied: {permission} {patterns} ---\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=None, help="directory to run in (default: a fresh temp copy)")
    parser.add_argument("--task-file", default=None)
    parser.add_argument("--log", default=str(ROOT / "e2e-transcript.log"))
    args = parser.parse_args()

    api_key = os.environ["MINICODE_E2E_API_KEY"]
    base_url = os.environ["MINICODE_E2E_BASE_URL"]
    model = os.environ["MINICODE_E2E_MODEL"]

    workspace = Path(args.workspace) if args.workspace else Path(os.environ.get("TEMP", ".")) / "minicode-e2e"
    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(FIXTURE, workspace)
    print(f"workspace: {workspace}\n", flush=True)

    task = Path(args.task_file).read_text(encoding="utf-8") if args.task_file else TASK

    from minicode.agent.core import CodingAgent
    from minicode.context.manager import ContextManager
    from minicode.permission.manager import PermissionManager, PermissionMode
    from minicode.providers.openai_compat import OpenAICompatProvider
    from minicode.session.manager import SessionManager
    from minicode.tools.registry import build_default_registry

    sink = Transcript(Path(args.log))
    provider = OpenAICompatProvider(
        model=model,
        api_key=api_key,
        base_url=base_url,
        name="e2e",
        timeout=300,
        max_tokens=8192,
        min_request_interval=float(os.getenv("MINICODE_E2E_MIN_INTERVAL", "6")),
        max_retries=6,
        retry_delay=10.0,
        retry_max_delay=90.0,
    )
    scripts = str(Path(sys.executable).parent)
    env = {"PYTHON_BIN": sys.executable, "PATH": scripts + os.pathsep + os.environ.get("PATH", "")}
    agent = CodingAgent(
        provider,
        build_default_registry(cwd=str(workspace), bash_timeout=180, env=env),
        permission=PermissionManager(mode=PermissionMode.AUTO),
        context=ContextManager(),
        session=None,
        sessions=SessionManager(directory=workspace / ".sessions"),
        sink=sink,
        cwd=str(workspace),
        stream=True,
    )

    started = time.time()
    try:
        result = agent.run(task)
    except Exception as exc:  # noqa: BLE001
        import traceback

        sink._write(f"\n!!! AGENT CRASHED: {type(exc).__name__}: {exc}\n{traceback.format_exc()}\n")
        return 2

    print(f"\nelapsed: {time.time() - started:.1f}s")
    print(f"stats: {agent.stats()}")

    print("\n===== RUNNING TESTS INDEPENDENTLY =====")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_billing.py"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=300,
    )
    print(proc.stdout[-3000:])
    print(proc.stderr[-2000:])
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
