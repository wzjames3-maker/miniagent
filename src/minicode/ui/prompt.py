"""Terminal input: single-line by default, multiline with a keystroke.

Uses ``prompt_toolkit`` when available (history, multiline toggle) and falls back
to ``input()`` so the CLI keeps working in dumb terminals and CI.
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = ["InputReader", "create_reader"]


class InputReader:
    """Reads user input, with optional persistent history."""

    def __init__(self, *, history_file: str | None = None, multiline_key: str = "c-o", use_prompt_toolkit: bool = True):
        self.history_file = history_file
        self.multiline_key = multiline_key
        self._session = None
        self._fallback_history: list[str] = []
        if use_prompt_toolkit:
            self._session = self._build_session()

    def _build_session(self):
        try:
            from pathlib import Path

            from prompt_toolkit import PromptSession
            from prompt_toolkit.history import FileHistory

            history = None
            if self.history_file:
                path = Path(self.history_file).expanduser()
                path.parent.mkdir(parents=True, exist_ok=True)
                history = FileHistory(str(path))
            return PromptSession(history=history, enable_history_search=True, multiline=False)
        except Exception:  # noqa: BLE001 - prompt_toolkit is optional at runtime
            return None
    def read(self, prompt: str = "> ", *, multiline: bool = False, prompt_html: str | None = None) -> str:
        if self._session is None:
            try:
                return input(prompt if not multiline else "")
            except EOFError:
                return "/exit"
        try:
            if multiline:
                return self._read_multiline()
            if prompt_html is not None:
                from prompt_toolkit.formatted_text import HTML

                return self._session.prompt(HTML(prompt_html), multiline=False)
            return self._session.prompt(prompt, multiline=False)
        except EOFError:
            return "/exit"
        except KeyboardInterrupt:
            return ""

    def _read_multiline(self) -> str:
        from prompt_toolkit import prompt as pt_prompt
        from prompt_toolkit.formatted_text import HTML

        return pt_prompt(
            HTML("<ansigreen>... </ansigreen>"),
            multiline=True,
            history=self._session.history,
        )

    def history(self) -> Iterable[str]:
        if self._session is not None and self._session.history is not None:
            return [item for item in self._session.history.get_strings()]
        return list(self._fallback_history)


def create_reader(*, history_file: str | None = None, use_prompt_toolkit: bool = True) -> InputReader:
    return InputReader(history_file=history_file, use_prompt_toolkit=use_prompt_toolkit)
