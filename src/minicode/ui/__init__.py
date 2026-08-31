"""Terminal user interface (rendering + input). No agent logic lives here."""

from minicode.ui.console import MINICODE_THEME, ConsoleUI
from minicode.ui.events import (
    CollectingSink,
    EventSink,
    NullSink,
    TurnResult,
)
from minicode.ui.port import UIFrontEnd, UIPort, format_status_line
from minicode.ui.prompt import InputReader, create_reader
from minicode.ui.render import clip_lines, format_arguments, render_output

__all__ = [
    "CollectingSink",
    "ConsoleUI",
    "EventSink",
    "InputReader",
    "MINICODE_THEME",
    "NullSink",
    "TurnResult",
    "UIFrontEnd",
    "UIPort",
    "clip_lines",
    "create_reader",
    "format_arguments",
    "format_status_line",
    "render_output",
]
