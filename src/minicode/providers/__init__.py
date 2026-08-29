"""Provider layer: one abstraction, many wire protocols."""

from minicode.providers.base import (
    AssistantMessage,
    AuthenticationError,
    ContextLengthError,
    Provider,
    ProviderAPIError,
    ProviderError,
    RateLimitError,
    StreamEvent,
    ToolCall,
    Usage,
    format_tool_results,
)
from minicode.providers.registry import (
    PROVIDER_KINDS,
    ProviderRegistry,
    ProviderSpec,
    build_registry,
)

__all__ = [
    "AssistantMessage",
    "AuthenticationError",
    "ContextLengthError",
    "Provider",
    "ProviderAPIError",
    "ProviderError",
    "PROVIDER_KINDS",
    "ProviderRegistry",
    "ProviderSpec",
    "RateLimitError",
    "StreamEvent",
    "ToolCall",
    "Usage",
    "build_registry",
    "format_tool_results",
]

# NOTE: the concrete providers (openai_compat / anthropic_compat / litellm /
# scripted) are imported lazily by the registry so that installing a single SDK
# is enough and importing minicode stays fast.
