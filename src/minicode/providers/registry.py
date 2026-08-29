"""Multi-provider / multi-model registry with in-session switching.

The agent never constructs providers itself - it asks the registry, which makes
"switch model mid-session" (``/model``) a pure registry operation.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from minicode.providers.base import Provider

__all__ = ["ProviderSpec", "ProviderRegistry", "PROVIDER_KINDS"]

#: provider kind -> import path + class name
PROVIDER_KINDS: dict[str, str] = {
    "openai_compat": "minicode.providers.openai_compat:OpenAICompatProvider",
    "openai": "minicode.providers.openai_compat:OpenAICompatProvider",
    "anthropic_compat": "minicode.providers.anthropic_compat:AnthropicCompatProvider",
    "anthropic": "minicode.providers.anthropic_compat:AnthropicCompatProvider",
    "litellm": "minicode.providers.litellm_provider:LiteLLMProvider",
    "scripted": "minicode.providers.scripted:ScriptedProvider",
}


@dataclass
class ProviderSpec:
    """Configuration of one provider entry (from the ``providers:`` config block)."""

    name: str
    kind: str = "openai_compat"
    models: list[str] = field(default_factory=list)
    default_model: str = ""
    api_key_env: str = ""
    api_key: str = ""
    base_url: str = ""
    max_tokens: int = 8192
    temperature: float | None = None
    timeout: float = 180.0
    headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(cls, name: str, data: Mapping[str, Any]) -> ProviderSpec:
        known = {f for f in cls.__dataclass_fields__}
        payload = {key: value for key, value in data.items() if key in known}
        payload["name"] = name
        # The documented config key is ``type`` (it reads better in YAML than the
        # internal ``kind``); accept both, with ``type`` winning.
        declared = data.get("type") or data.get("kind")
        payload["kind"] = str(declared) if declared else "openai_compat"
        models = data.get("models") or []
        if isinstance(models, str):
            models = [models]
        payload["models"] = list(models)
        # Anything the spec does not model (max_retries, min_request_interval,
        # include_usage, ...) is forwarded to the provider constructor. Dropping
        # them silently would make documented knobs appear to do nothing.
        options = {
            key: value
            for key, value in data.items()
            if key not in known and key not in {"type", "kind", "models"}
        }
        payload["options"] = {**options, **(data.get("options") or {})}
        return cls(**payload)

    def resolved_api_key(self) -> str:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.getenv(self.api_key_env, "")
        return ""

    def available(self) -> bool:
        """Whether an API key is configured (empty key is fine for local servers)."""
        return bool(self.resolved_api_key()) or bool(self.base_url)


class ProviderRegistry:
    """Builds and caches providers from configuration."""

    def __init__(
        self,
        providers: Mapping[str, ProviderSpec] | Mapping[str, Any] | None = None,
        *,
        default_provider: str = "",
        default_model: str = "",
    ):
        self.specs: dict[str, ProviderSpec] = {}
        for name, spec in (providers or {}).items():
            self.specs[name] = spec if isinstance(spec, ProviderSpec) else ProviderSpec.from_config(name, spec)
        self.default_provider = default_provider
        self.default_model = default_model
        self._cache: dict[tuple[str, str], Provider] = {}

    # ------------------------------------------------------------------ #
    # inspection
    # ------------------------------------------------------------------ #
    def provider_names(self) -> list[str]:
        return list(self.specs)

    def models(self, provider: str) -> list[str]:
        return list(self.specs[provider].models) if provider in self.specs else []

    def list_models(self) -> list[str]:
        out: list[str] = []
        for name, spec in self.specs.items():
            for model in spec.models:
                out.append(f"{name}/{model}")
        return out

    def describe(self) -> str:
        lines: list[str] = []
        for name, spec in self.specs.items():
            marker = "*" if name == self.default_provider else " "
            models = ", ".join(spec.models) or "(no models declared)"
            key_state = "key-ok" if spec.resolved_api_key() else ("local" if spec.base_url else "no-key")
            lines.append(f"{marker} {name} [{spec.kind}, {key_state}]: {models}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # resolution
    # ------------------------------------------------------------------ #
    def _import_provider_class(self, kind: str) -> type[Provider]:
        from importlib import import_module

        target = PROVIDER_KINDS.get(kind, kind)
        if ":" in target:
            module_name, class_name = target.split(":", 1)
        else:
            module_name, class_name = target.rsplit(".", 1)
        module = import_module(module_name)
        return getattr(module, class_name)

    def create(self, provider: str | None = None, model: str | None = None) -> Provider:
        provider = provider or self.default_provider
        if provider not in self.specs:
            raise KeyError(f"Unknown provider {provider!r}. Configured: {', '.join(self.specs) or '(none)'}")
        spec = self.specs[provider]
        model = model or spec.default_model or (spec.models[0] if spec.models else "")
        if not model:
            raise ValueError(f"No model configured for provider {provider!r}")

        cache_key = (provider, model)
        if cache_key in self._cache:
            return self._cache[cache_key]

        cls = self._import_provider_class(spec.kind)
        instance = cls(
            model=model,
            name=provider,
            api_key=spec.resolved_api_key(),
            base_url=spec.base_url or None,
            max_tokens=spec.max_tokens,
            temperature=spec.temperature,
            timeout=spec.timeout,
            headers=spec.headers or None,
            extra_body=spec.extra_body or None,
            **spec.options,
        )
        self._cache[cache_key] = instance
        return instance

    def get(self, spec: str | None = None) -> Provider:
        """Resolve ``"provider/model"``, ``"model"`` or ``None`` (defaults)."""
        if not spec:
            return self.create(self.default_provider, self.default_model or None)
        if "/" in spec:
            provider, _, model = spec.partition("/")
            if provider in self.specs:
                return self.create(provider, model)
            # A qualified "provider/model" that names an unknown provider is a
            # typo, not a model to invent - say so instead of silently building
            # a provider that will fail on the first request.
            raise KeyError(f"Unknown provider {provider!r}. Configured: {', '.join(self.specs) or '(none)'}")
        if spec in self.specs:
            return self.create(spec)
        # bare model name: search every provider that declares it
        for name, provider_spec in self.specs.items():
            if spec in provider_spec.models:
                return self.create(name, spec)
        return self.create(self.default_provider, spec)

    def set_default(self, provider: str, model: str | None = None) -> Provider:
        """Switch the default provider/model (used by ``/model``)."""
        if provider not in self.specs:
            raise KeyError(f"Unknown provider {provider!r}")
        self.default_provider = provider
        spec = self.specs[provider]
        self.default_model = model or spec.default_model or (spec.models[0] if spec.models else "")
        return self.create(provider, self.default_model or None)

    def available_specs(self) -> list[ProviderSpec]:
        return [spec for spec in self.specs.values() if spec.available()]

    def first_available(self) -> Provider:
        if not self.specs:
            raise RuntimeError("No providers configured. Add a `providers:` section to your config.")
        for spec in self.available_specs():
            return self.create(spec.name)
        raise RuntimeError(
            "No provider has an API key. Set the api_key_env variable or add `api_key` to the config."
        )


def build_registry(config: Mapping[str, Any] | None) -> ProviderRegistry:
    """Build a registry from a parsed config mapping."""
    config = dict(config or {})
    providers = config.get("providers") or {}
    return ProviderRegistry(
        providers=providers,
        default_provider=str(config.get("default_provider", "") or ""),
        default_model=str(config.get("default_model", "") or ""),
    )


__all__ += ["build_registry"]
