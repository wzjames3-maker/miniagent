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
    "PermissionError",
    "PermissionDeniedError",
    "PermissionRejectedError",
    "SessionError",
    "SessionNotFoundError",
    "ContextError",
    "CLIError",
]


class MinicodeError(Exception):
    """Base for all domain errors."""


class ConfigError(MinicodeError):
    pass


class ProviderError(MinicodeError):
    retryable = False


class AuthenticationError(ProviderError):
    def __init__(self, message: str = "Authentication failed. Check your API key.") -> None:
        super().__init__(message)


class RateLimitError(ProviderError):
    retryable = True


class ContextLengthError(ProviderError):
    """Request exceeded the model's context window."""


class ProviderAPIError(ProviderError):
    retryable = True


class ToolError(MinicodeError):
    pass


class PermissionError(MinicodeError):
    pass


class PermissionDeniedError(PermissionError):
    pass


class PermissionRejectedError(PermissionError):
    pass


class SessionError(MinicodeError):
    pass


class SessionNotFoundError(SessionError, KeyError):
    pass


class ContextError(MinicodeError):
    pass


class CLIError(MinicodeError):
    pass
