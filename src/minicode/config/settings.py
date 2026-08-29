"""Layered configuration.

Layers, later wins::

    built-in defaults
      <- ~/.minicode/config.yaml        (global)
      <- ./.minicode/config.yaml        (project)
      <- environment variables           MINICODE_*
      <- CLI overrides                   --set agent.step_limit=200

The ``key=value`` parsing and the recursive merge are *reused from
mini-swe-agent* (``minisweagent.config`` / ``minisweagent.utils.serialize``),
so ``--set`` behaves exactly like mini's own CLI.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from minicode.context.manager import ContextConfig
from minicode.storage.paths import global_config_file, project_config_file

__all__ = [
    "AgentSettings",
    "ToolsSettings",
    "UISettings",
    "Settings",
    "load_settings",
    "builtin_config_path",
    "DEFAULT_CONFIG_YAML",
]

try:  # mini-swe-agent is the base dependency, but stay importable without it
    from minisweagent.config import _key_value_spec_to_nested_dict
    from minisweagent.utils.serialize import recursive_merge
except ImportError:  # pragma: no cover

    def _key_value_spec_to_nested_dict(spec: str) -> dict:
        key, value = spec.split("=", 1)
        result: dict = {}
        current = result
        keys = key.split(".")
        for part in keys[:-1]:
            current[part] = {}
            current = current[part]
        try:
            import json

            current[keys[-1]] = json.loads(value)
        except Exception:
            current[keys[-1]] = value
        return result

    def recursive_merge(*dicts: Mapping[str, Any]) -> dict:
        merged: dict[str, Any] = {}
        for d in dicts:
            for key, value in (d or {}).items():
                if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
                    merged[key] = recursive_merge(merged[key], value)
                else:
                    merged[key] = value
        return merged


def builtin_config_path() -> Path:
    return Path(__file__).parent / "default.yaml"


_FALLBACK_CONFIG_YAML = """
# minicode default configuration (all values can be overridden per layer)
default_provider: ""
default_model: ""

providers: {}

agent:
  system_template: ""
  step_limit: 200
  cost_limit: 10.0
  wall_time_limit_seconds: 0
  max_consecutive_format_errors: 4
  doom_loop_threshold: 3
  confirm_on_finish: false

permission:
  read: allow
  glob: allow
  grep: allow
  write: ask
  edit: ask
  apply_patch: ask
  delete: ask
  bash:
    "git status*": allow
    "git diff*": allow
    "git log*": allow
    "ls*": allow
    "cat*": allow
    "python -m pytest*": allow
    "pytest*": allow
    "rm -rf *": deny
    "rm -rf **": deny
    "*": ask

context:
  max_tokens: 120000
  auto_compact: true
  compact_threshold: 0.85
  prune: true
  prune_protect_tokens: 40000
  prune_minimum_tokens: 20000
  preserve_recent_tokens: 20000
  tail_turns: null
  tool_output_max_lines: 2000
  tool_output_max_bytes: 51200

tools:
  enabled: []
  bash_timeout: 120
  extra_modules: []

ui:
  stream: true
  max_output_lines: 40
  show_tool_arguments: true
  theme: auto
""".lstrip()

# Single source: if the file exists, its content is authoritative.
try:
    _candidate = builtin_config_path()
    if _candidate.is_file():
        _file_content = _candidate.read_text(encoding="utf-8").strip()
        if _file_content:
            _FALLBACK_CONFIG_YAML = _file_content
except Exception:
    pass

DEFAULT_CONFIG_YAML = _FALLBACK_CONFIG_YAML


class AgentSettings(BaseModel):
    system_template: str = ""
    """Path to a custom system prompt template (jinja2). Empty = built-in."""
    step_limit: int = 200
    cost_limit: float = 10.0
    wall_time_limit_seconds: int = 0
    max_consecutive_format_errors: int = 4
    doom_loop_threshold: int = 3
    """Interrupt after this many identical consecutive tool calls (0 = off)."""
    confirm_on_finish: bool = False


class ToolsSettings(BaseModel):
    enabled: list[str] = Field(default_factory=list)
    """When empty, all builtin tools are enabled."""
    bash_timeout: int = 120
    extra_modules: list[str] = Field(default_factory=list)
    """Dotted module paths exposing ``register_tools(registry)``."""


class UISettings(BaseModel):
    stream: bool = True
    max_output_lines: int = 40
    show_tool_arguments: bool = True
    theme: str = "auto"


class Settings(BaseModel):
    default_provider: str = ""
    default_model: str = ""
    providers: dict[str, Any] = Field(default_factory=dict)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    permission: dict[str, Any] = Field(default_factory=dict)
    context: ContextConfig = Field(default_factory=ContextConfig)
    tools: ToolsSettings = Field(default_factory=ToolsSettings)
    ui: UISettings = Field(default_factory=UISettings)
    env: dict[str, str] = Field(default_factory=dict)
    """Extra environment variables injected into bash tool runs."""


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        from minicode.errors import ConfigError

        raise ConfigError(f"Invalid YAML in config file {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _load_dotenvs(cwd: Path) -> None:
    """Load ``.env`` files (project first, then the global config dir)."""
    try:
        import dotenv
    except ImportError:  # pragma: no cover
        return
    for candidate in (cwd / ".env", global_config_file().parent / ".env"):
        if candidate.is_file():
            dotenv.load_dotenv(dotenv_path=candidate, override=False)


def load_settings(
    *,
    cwd: str | Path | None = None,
    config_path: str | Path | None = None,
    overrides: Sequence[str] | None = None,
    load_dotenv: bool = True,
) -> Settings:
    """Build the effective :class:`Settings` from every layer."""
    cwd = Path(cwd) if cwd else Path.cwd()
    if load_dotenv:
        _load_dotenvs(cwd)

    layers: list[Mapping[str, Any]] = []
    default_path = builtin_config_path()
    layers.append(_load_yaml(default_path) if default_path.is_file() else (yaml.safe_load(DEFAULT_CONFIG_YAML) or {}))

    if config_path:
        layers.append(_load_yaml(Path(config_path)))
    else:
        layers.append(_load_yaml(global_config_file()))
        layers.append(_load_yaml(project_config_file(cwd)))

    layers.append(_from_environment())
    for spec in overrides or []:
        layers.append(_key_value_spec_to_nested_dict(spec))

    merged = recursive_merge(*layers)
    return Settings.model_validate(merged)


def _from_environment() -> dict[str, Any]:
    """Map ``MINICODE_AGENT_STEP_LIMIT=50`` onto ``{"agent": {"step_limit": 50}}``."""
    out: dict[str, Any] = {}
    prefix = "MINICODE_"
    for key, value in os.environ.items():
        if not key.startswith(prefix) or len(key) == len(prefix):
            continue
        parts = [p.lower() for p in key[len(prefix) :].split("__")]
        current = out
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = _coerce_env_value(value)
    return out


def _coerce_env_value(value: str) -> Any:
    import json

    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none", ""}:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return value
