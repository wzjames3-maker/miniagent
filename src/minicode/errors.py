"""Central error hierarchy.

Low-level modules raise these; UI/CLI decides how to present them and
Agent decides how to recover. No module prints directly.
"""

from __future__ import annotations

__all__ = [
    "MinicodeError",
    "ConfigError",
    "ProviderError",
    "AuthenticationError",
    "RateLimitError",
    "ContextLengthError",
    "ProviderAPIError",
    "ToolError",
    "PermissionDeniedError",
    "PermissionRejectedError",
    "SessionError",
    "SessionNotFoundError",
]


class MinicodeError(Exception):
    """Base for all domain errors."""


class ConfigError(MinicodeError):
    pass


class ProviderError(MinicodeError):
    pass


class AuthenticationError(ProviderError):
    def __init__(self, message: str = "Authentication failed. Check your API key.") -> None:
        super().__init__(message)


class RateLimitError(ProviderError):
    pass


class ContextLengthError(ProviderError):
    """Request exceeded the model's context window."""


class ProviderAPIError(ProviderError):
    pass


class ToolError(MinicodeError):
    pass


class PermissionDeniedError(MinicodeError):
    pass


class PermissionRejectedError(MinicodeError):
    pass


class SessionError(MinicodeError):
    pass


class SessionNotFoundError(SessionError, KeyError):
    pass
