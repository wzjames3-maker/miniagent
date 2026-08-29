"""``bash`` - shell execution, built on mini-swe-agent's LocalEnvironment.

mini-swe-agent already ships a robust local command runner (timeout handling,
process-group kill, stdout/stderr merge). We reuse it verbatim by subclassing,
and only remove the ``COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`` magic string that
mini uses as its submit signal - minicode finishes a turn the OpenCode way
(the model simply stops calling tools).
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from typing import Any

from minisweagent.environments.local import LocalEnvironment

from minicode.tools.base import BaseTool, ToolContext, ToolError, ToolResult
from minicode.tools.truncate import truncate_output

__all__ = ["BashTool", "MiniLocalEnvironment"]

#: Commands that can destroy data; they additionally require the `delete` permission.
_DESTRUCTIVE_COMMANDS = {"rm", "rmdir", "unlink", "shred", "del", "erase", "mkfs", "truncate"}


class MiniLocalEnvironment(LocalEnvironment):
    """mini-swe-agent's :class:`LocalEnvironment` without the submit magic string."""

    def _check_finished(self, output: dict) -> None:
        return None


def destructive_targets(command: str) -> list[str]:
    """Extract file targets from destructive commands like ``rm -rf /tmp/x``."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    targets: list[str] = []
    for i, token in enumerate(tokens):
        base = token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if base in _DESTRUCTIVE_COMMANDS:
            for following in tokens[i + 1 :]:
                if following.startswith("-"):
                    continue
                if following in {"&&", "||", ";", "|", ">", ">>"}:
                    break
                targets.append(following)
                break
        if token in {"-rf", "-fr", "-r", "-f"} and targets:
            continue
    return targets


class BashTool(BaseTool):
    """Execute a shell command in the working directory."""

    name = "bash"
    permission = "bash"
    description = (
        "Execute a shell command. Use it for running tests, builds, git, and any other command line work. "
        "Prefer the dedicated file tools over shell commands for editing files. "
        "Commands run with a timeout (default 60s) and output is truncated when very large."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command to execute."},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 60, max 600)."},
            "cwd": {"type": "string", "description": "Working directory (defaults to the session working directory)."},
            "description": {"type": "string", "description": "Short description of what the command does."},
        },
        "required": ["command"],
    }

    def __init__(self, *, default_timeout: int = 60, cwd: str = "", env: dict[str, str] | None = None):
        self.default_timeout = default_timeout
        self.cwd = cwd
        self.env = MiniLocalEnvironment(cwd=cwd, env=env or {}, timeout=default_timeout)

    def configure(self, *, cwd: str = "", default_timeout: int | None = None) -> None:
        if cwd:
            self.cwd = cwd
            self.env.config.cwd = cwd
        if default_timeout:
            self.default_timeout = default_timeout
            self.env.config.timeout = default_timeout

    def patterns(self, args: Mapping[str, Any]) -> list[str]:
        return [str(args.get("command", "*")).strip()]

    def required_permissions(self, args: Mapping[str, Any]) -> list[tuple[str, list[str]]]:
        """Permissions that must all pass before this call may run."""
        checks: list[tuple[str, list[str]]] = [(self.permission, self.patterns(args))]
        targets = destructive_targets(str(args.get("command", "")))
        if targets:
            checks.append(("delete", targets))
        return checks

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        command = str(args["command"])
        timeout = int(args.get("timeout") or self.default_timeout)
        timeout = max(1, min(timeout, 600))
        cwd = str(args.get("cwd") or ctx.cwd or self.cwd or ".")

        output = self.env.execute({"command": command}, cwd=cwd, timeout=timeout)
        stdout = output.get("output", "") or ""
        returncode = output.get("returncode", -1)
        exception_info = output.get("exception_info", "") or ""

        text = stdout if stdout.strip() else "[no output]"
        if exception_info:
            text = f"{text}\n\n<exception>\n{exception_info}\n</exception>"
        if returncode not in (0, None):
            text = f"{text}\n\n<returncode>{returncode}</returncode>"

        title = str(args.get("description") or command.splitlines()[0][:80] if command else "bash")
        metadata: dict[str, Any] = {"command": command, "returncode": returncode, "cwd": cwd, "timeout": timeout}

        if ctx.truncate is not None:
            result = truncate_output(text, tool=self.name, direction="head")
            if result.truncated:
                metadata["truncated"] = True
                metadata["output_path"] = result.output_path
            text = result.content

        result_obj = ToolResult(title=title, output=text, metadata=metadata)
        if returncode not in (0, None):
            result_obj.error = ToolError(
                code="nonzero_exit",
                message=f"Command exited with code {returncode}",
                hint="Read the output above, fix the problem and retry (or use a different approach).",
                details={"command": command, "returncode": returncode},
            )
        return result_obj
