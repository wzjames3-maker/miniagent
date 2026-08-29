"""E2E: a real coding task against a real model.

The fixture project (``fixtures/buggy_project``) contains three deliberate bugs.
The agent must understand the project, locate each bug, fix it, run the tests,
interpret the failures and iterate until the suite is green.

Nothing here is mocked: the agent loop, every tool, the permission system, the
session store and the context manager all run for real, and the model is a real
OpenAI-compatible endpoint.

Skipped unless credentials are present:

    MINICODE_E2E_API_KEY=... MINICODE_E2E_BASE_URL=... MINICODE_E2E_MODEL=...

The bugs (for reference only - the agent is not told about them):

1. ``monthly_total``   - annual discount uses ``months > 12`` instead of ``>= 12``
2. ``apply_coupon``    - a FLAT coupon larger than the price returns a positive
                         number instead of clamping to zero
3. ``prorate``         - off-by-one: charges ``days_used + 1``
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "fixtures" / "buggy_project"

API_KEY = os.getenv("MINICODE_E2E_API_KEY", "")
BASE_URL = os.getenv("MINICODE_E2E_BASE_URL", "")
MODEL = os.getenv("MINICODE_E2E_MODEL", "")

pytestmark = pytest.mark.e2e

requires_model = pytest.mark.skipif(
    not (API_KEY and BASE_URL and MODEL),
    reason="set MINICODE_E2E_API_KEY / MINICODE_E2E_BASE_URL / MINICODE_E2E_MODEL to run the real E2E",
)


@pytest.fixture
def workspace(tmp_path) -> Path:
    """An isolated copy of the buggy project, so the fixture is never modified."""
    target = tmp_path / "project"
    shutil.copytree(FIXTURE, target)
    return target


def run_tests(workspace: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_billing.py"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _test_env() -> dict[str, str]:
    """Make the *current* interpreter (the one that has pytest) available to `bash`.

    Otherwise the agent discovers a different python on PATH and has to install
    pytest itself - which it will happily do, but it wastes time and quota.
    """
    import os as _os

    scripts = str(Path(sys.executable).parent)
    scripts_dir = scripts if scripts.endswith("Scripts") else str(Path(sys.executable).parent / "Scripts")
    return {"PYTHON_BIN": sys.executable, "PATH": scripts + _os.pathsep + scripts_dir + _os.pathsep + _os.environ.get("PATH", "")}


def _build_agent(workspace: Path):
    from minicode.agent.core import CodingAgent
    from minicode.context.manager import ContextManager
    from minicode.permission.manager import PermissionManager, PermissionMode
    from minicode.providers.openai_compat import OpenAICompatProvider
    from minicode.session.manager import SessionManager
    from minicode.tools.registry import build_default_registry
    from minicode.ui.events import CollectingSink

    provider = OpenAICompatProvider(
        model=MODEL,
        api_key=API_KEY,
        base_url=BASE_URL,
        name="e2e",
        timeout=300,
        max_tokens=8192,
        # hosted endpoints are usually rate limited per minute - pace the loop
        # instead of burning the quota on 429s
        min_request_interval=float(os.getenv("MINICODE_E2E_MIN_INTERVAL", "6")),
        max_retries=6,
        retry_delay=10.0,
        retry_max_delay=90.0,
    )
    return (
        CodingAgent(
            provider,
            build_default_registry(cwd=str(workspace), bash_timeout=180, env=_test_env()),
            permission=PermissionManager(mode=PermissionMode.AUTO),
            context=ContextManager(),
            session=None,
            sessions=SessionManager(directory=workspace / ".sessions"),
            sink=CollectingSink(),
            cwd=str(workspace),
            stream=False,
        ),
        provider,
    )


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


@requires_model
def test_agent_fixes_all_three_real_bugs(workspace):
    before = run_tests(workspace)
    assert before.returncode != 0, "the fixture project is supposed to be broken"
    failing_before = before.stdout.count("FAILED")
    assert failing_before >= 3, f"expected several failing tests, got {failing_before}"

    agent, _ = _build_agent(workspace)
    result = agent.run(TASK)

    print("\n===== AGENT REPORT =====")
    print(result.get("submission", "(no submission)"))
    print("===== STATS =====")
    print(agent.stats())

    after = run_tests(workspace)
    print("===== FINAL TEST OUTPUT =====")
    print(after.stdout[-3000:])

    assert after.returncode == 0, (
        "the agent did not get the suite to green:\n" + after.stdout[-3000:] + after.stderr[-2000:]
    )
    assert "passed" in after.stdout

    # the tests themselves must be untouched
    assert (workspace / "test_billing.py").read_bytes() == (FIXTURE / "test_billing.py").read_bytes()

    # each of the three bugs must actually be fixed
    sys.path.insert(0, str(workspace))
    for module in list(sys.modules):
        if module == "billing":
            del sys.modules[module]
    import billing

    assert billing.monthly_total("pro", 1, months=12) == 29000, "annual discount boundary not fixed"
    assert billing.apply_coupon(2900, "FLAT50") == 0, "flat coupon clamping not fixed"
    assert billing.prorate(3000, 15, 30) == 1500, "prorate off-by-one not fixed"


@requires_model
def test_agent_uses_tools_not_guesses(workspace):
    """The agent must actually read and run things, not hallucinate a fix."""
    agent, provider = _build_agent(workspace)
    agent.run(TASK)

    tool_names = {record[0].name for record in agent.sink.tool_results}
    assert "read" in tool_names, "the agent never read a file"
    assert "bash" in tool_names, "the agent never ran the tests"
    assert any(
        "pytest" in str(record[0].arguments) for record in agent.sink.tool_results if record[0].name == "bash"
    ), "the agent never executed pytest"
