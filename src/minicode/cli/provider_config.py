"""Interactive provider configuration, shared by the CLI and the TUI.

This is the "configure the model inside the app" path: instead of hand-editing
YAML, ``configure_provider()`` asks the user a few questions and writes the
global config file. Both ``minicode providers login`` and the in-TUI ``/login``
command use this same implementation.
"""

from __future__ import annotations

import getpass
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minicode.storage.paths import global_config_file

__all__ = [
    "PROVIDER_PRESETS",
    "ProviderConfigResult",
    "configure_provider",
    "remove_provider",
    "read_yaml_file",
    "write_yaml_file",
    "provider_config_path",
]

PROVIDER_PRESETS = {
    "openai": {
        "type": "openai_compat",
        "api_key_env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o"],
        "default_model": "gpt-4o-mini",
    },
    "deepseek": {
        "type": "openai_compat",
        "api_key_env": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "default_model": "deepseek-chat",
    },
    "anthropic": {
        "type": "anthropic_compat",
        "api_key_env": "ANTHROPIC_API_KEY",
        "models": ["claude-sonnet-4-5", "claude-opus-4-1"],
        "default_model": "claude-sonnet-4-5",
    },
    "local": {
        "type": "openai_compat",
        "base_url": "http://localhost:11434/v1",
        "models": ["qwen2.5-coder:7b"],
        "default_model": "qwen2.5-coder:7b",
    },
}


@dataclass
class ProviderConfigResult:
    name: str
    default_model: str
    path: Path
    api_key_set: bool


def provider_config_path(config_path: str | Path | None = None) -> Path:
    return Path(config_path) if config_path else global_config_file()


def read_yaml_file(path: Path) -> dict[str, Any]:
    import yaml

    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def write_yaml_file(path: Path, data: dict[str, Any]) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _prompt_provider_name(providers: dict[str, Any], input_fn: Any = input) -> str:
    print("Available presets:")
    preset_names = list(PROVIDER_PRESETS)
    for index, name in enumerate(preset_names, 1):
        print(f"  {index}) {name}")
    if providers:
        print("Already configured:")
        for name in providers:
            print(f"  - {name}")
    choice = input_fn("Provider name or number [openai]: ").strip().lower()
    if not choice:
        return "openai"
    if choice.isdigit():
        index = int(choice)
        if 1 <= index <= len(preset_names):
            return preset_names[index - 1]
    return choice


def configure_provider(
    name: str | None = None,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    models: str | list[str] | None = None,
    default_model: str | None = None,
    config_path: str | Path | None = None,
    input_fn: Any = None,
    password_fn: Any = None,
) -> ProviderConfigResult:
    """Add or update one provider. Prompts when values are missing."""
    input_fn = input_fn or input
    password_fn = password_fn or getpass.getpass
    target = provider_config_path(config_path)
    raw = read_yaml_file(target)
    providers = raw.setdefault("providers", {})
    if not isinstance(providers, dict):
        providers = {}
        raw["providers"] = providers

    chosen = (name or "").strip().lower() or _prompt_provider_name(providers, input_fn)
    preset = PROVIDER_PRESETS.get(chosen)
    existing = providers.get(chosen) or {}

    if base_url:
        provider = {**(preset or {}), **existing}
        provider["base_url"] = base_url
    elif preset is not None:
        provider = {**preset, **existing}
    else:
        provider = dict(existing)
        if not provider.get("type"):
            provider["type"] = input_fn("Provider type [openai_compat]: ").strip() or "openai_compat"
        if not provider.get("base_url"):
            provider["base_url"] = input_fn("Base URL (e.g. https://api.example.com/v1): ").strip()
        if not provider.get("models"):
            models_input = input_fn("Models (comma-separated): ").strip()
            provider["models"] = [m.strip() for m in models_input.split(",") if m.strip()]

    if models:
        provider["models"] = [
            m.strip() for m in (models if isinstance(models, list) else models.split(",")) if m.strip()
        ]

    if not provider.get("models"):
        raise ValueError("at least one model is required")

    if default_model:
        provider["default_model"] = default_model
    elif not provider.get("default_model") and provider.get("models"):
        provider["default_model"] = provider["models"][0]
    elif provider.get("default_model") not in provider["models"]:
        provider["default_model"] = provider["models"][0]

    if api_key is not None:
        api_key = api_key.strip()
    else:
        try:
            api_key = password_fn("API key (leave empty for local/no key): ").strip()
        except EOFError:
            api_key = ""

    api_key_set = bool(api_key)
    if api_key:
        provider["api_key"] = api_key
    elif not existing.get("api_key") and preset and preset.get("api_key_env") and not existing.get("api_key_env"):
        provider["api_key_env"] = preset["api_key_env"]

    providers[chosen] = provider

    if not raw.get("default_provider"):
        raw["default_provider"] = chosen
    if not raw.get("default_model"):
        raw["default_model"] = provider.get("default_model") or (provider.get("models") or [""])[0]

    write_yaml_file(target, raw)
    return ProviderConfigResult(
        name=chosen,
        default_model=provider.get("default_model") or (provider.get("models") or [""])[0],
        path=target,
        api_key_set=api_key_set,
    )


def remove_provider(name: str, *, config_path: str | Path | None = None) -> bool:
    """Remove a provider from the config file. Returns False if not found."""
    target = provider_config_path(config_path)
    raw = read_yaml_file(target)
    providers = raw.get("providers") or {}
    key = (name or "").strip().lower()
    if key not in providers:
        return False
    del providers[key]
    if raw.get("default_provider") == key:
        raw["default_provider"] = ""
        raw["default_model"] = ""
    write_yaml_file(target, raw)
    return True
