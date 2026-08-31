"""Modal pickers (ported from pydantic-deepagents).

A modal is a full-screen overlay with its own focus; it returns one value via
``dismiss``. The model picker lists every provider/model pair from the registry,
filters as you type, and lets you select with keyboard or mouse.
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

__all__ = ["ModelPickerModal"]


class ModelPickerModal(ModalScreen[str | None]):
    """Select a ``provider/model`` from the configured registry."""

    DEFAULT_CSS = """
    ModelPickerModal {
        align: center middle;
    }
    ModelPickerModal > #model-container {
        width: 76;
        max-height: 30;
        border: tall $primary;
        background: $surface;
        padding: 1;
    }
    ModelPickerModal #model-title {
        height: 1;
        padding: 0 0 1 0;
    }
    ModelPickerModal #model-list {
        height: 1fr;
        max-height: 20;
        border: none;
        background: $panel;
    }
    ModelPickerModal #model-filter {
        margin: 1 0 0 0;
        height: 1;
        border: none;
        background: $panel;
    }
    ModelPickerModal #model-hint {
        height: 1;
        color: $text-disabled;
        margin: 1 0 0 0;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, models: list[str], current: str = "") -> None:
        super().__init__()
        self._models = list(models)
        self._current = current
        self._filtered: list[str] = list(models)

    def compose(self) -> ComposeResult:
        with Vertical(id="model-container"):
            yield Static("[bold]Select Model[/bold]", id="model-title")
            yield OptionList(*self._build_options(self._models), id="model-list")
            yield Input(
                placeholder="Type to filter, or enter a custom model string…",
                id="model-filter",
            )
            yield Static(
                "[dim]↑↓ navigate  Enter select  Esc cancel[/dim]",
                id="model-hint",
            )

    def on_mount(self) -> None:
        self.query_one("#model-filter", Input).focus()

    # ------------------------------------------------------------------ #
    # options
    # ------------------------------------------------------------------ #
    def _build_options(self, models: list[str]) -> list[Option]:
        options: list[Option] = []
        for model in models:
            label = model
            if model == self._current:
                label += "  [bold](current)[/bold]"
            options.append(Option(label, id=model))
        if not options:
            options.append(Option("[dim]no providers configured — use /login[/dim]", disabled=True))
        return options

    # ------------------------------------------------------------------ #
    # events
    # ------------------------------------------------------------------ #
    def on_input_changed(self, event: Input.Changed) -> None:
        query = event.value.strip().lower()
        self._filtered = [m for m in self._models if query in m.lower()] if query else list(self._models)
        listing = self.query_one("#model-list", OptionList)
        listing.clear_options()
        listing.add_options(self._build_options(self._filtered))
        if self._filtered:
            listing.highlighted = 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self.dismiss(str(event.option.id))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if "/" in text:
            self.dismiss(text)
            return
        chosen = self._highlighted_model()
        self.dismiss(chosen or text or None)

    def _highlighted_model(self) -> str | None:
        try:
            listing = self.query_one("#model-list", OptionList)
        except Exception:
            return None
        if listing.highlighted is None:
            return None
        option = listing.get_option_at_index(listing.highlighted)
        return str(option.id) if option is not None and option.id else None

    def action_cancel(self) -> None:
        self.dismiss(None)


def push_model_picker(app: Any, models: list[str], current: str, on_pick: Any) -> None:
    """Push the picker and route its result into ``on_pick(model_or_None)``."""

    def _handle(result: str | None) -> None:
        on_pick(result)

    app.push_screen(ModelPickerModal(models, current=current), _handle)
