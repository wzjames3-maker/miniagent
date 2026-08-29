"""Terminal user interface (rendering + input). No agent logic lives here."""

from minicode.ui.console import MINICODE_THEME, ConsoleUI
from minicode.ui.events import (
    CollectingSink,
    EventSink,
    NullSink,
    TurnResult,
)
from minicode.ui.prompt import InputReader, create_reader

__all__ = [
    "CollectingSink",
    "ConsoleUI",
    "EventSink",
    "InputReader",
    "MINICODE_THEME",
    "NullSink",
    "TurnResult",
    "create_reader",
]
