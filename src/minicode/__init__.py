"""minicode - a lightweight OpenCode-like coding agent core in pure Python.

Built *on top of* mini-swe-agent: the agent loop, the ``Model``/``Environment``
protocols, the config utilities and the local command execution are reused from
mini-swe-agent (inheritance / composition), and the coding-agent features that
OpenCode provides (tool registry, permissions, sessions, context management,
multiple providers) are added around them.
"""

# mini-swe-agent prints a startup banner on import. We are a library/CLI that
# embeds it, so silence it before the first import happens anywhere.
import os

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

__version__ = "0.1.0"

__all__ = ["__version__"]
