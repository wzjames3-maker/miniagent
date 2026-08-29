"""Escape hatch provider built on mini-swe-agent's own backend: litellm.

mini-swe-agent ships ``LitellmModel`` which talks to 100+ providers through
litellm. Rather than re-implementing that, this adapter reuses the *message
translation and streaming accumulation* of :class:`OpenAICompatProvider` (litellm
normalises every backend to the OpenAI response shape) and only swaps the
transport call. That is the whole point of the composition-first design: one new
method, one new file, 100+ new providers.
"""

from __future__ import annotations

from typing import Any

from minicode.providers.base import AuthenticationError, ContextLengthError, ProviderAPIError, RateLimitError
from minicode.providers.openai_compat import OpenAICompatProvider

__all__ = ["LiteLLMProvider"]


class LiteLLMProvider(OpenAICompatProvider):
    kind = "litellm"
    default_api_key_env = "OPENAI_API_KEY"

    def __init__(self, **kwargs: Any):
        # Skip OpenAICompatProvider.__init__ (no openai client needed); the base
        # Provider.__init__ still handles all configuration.
        super(OpenAICompatProvider, self).__init__(**kwargs)
        import litellm

        # Providers differ in which params they accept; litellm drops the rest.
        litellm.drop_params = True
        self._litellm = litellm
        self.include_usage = bool(self.options.pop("include_usage", False))

    def _create(self, payload: dict[str, Any]) -> Any:
        payload = dict(payload)
        payload.setdefault("api_key", self.api_key or None)
        if self.base_url:
            payload.setdefault("api_base", self.base_url)
        return self._litellm.completion(**payload)

    def _map_error(self, exc: Exception) -> Exception:
        message = str(exc)
        lowered = message.lower()
        if "contextwindowexceeded" in lowered.replace("_", "").replace(" ", ""):
            return ContextLengthError(message)
        name = type(exc).__name__
        if "Authentication" in name:
            return AuthenticationError(f"{message} (provider={self.name})")
        if "RateLimit" in name:
            return RateLimitError(message)
        if "ContextWindow" in name:
            return ContextLengthError(message)
        return ProviderAPIError(message)
