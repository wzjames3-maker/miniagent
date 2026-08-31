"""Full-screen Textual front-end.

Imported lazily by the CLI so that ``minicode run``/piped usage keeps working on
machines without Textual installed.
"""

from minicode.ui.textual.app import MiniTUI, run_tui

__all__ = ["MiniTUI", "run_tui"]
