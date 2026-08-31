"""Visual language of the Textual TUI.

The layout follows OpenCode: a narrow session rail on the left, a scrolling
message timeline in the middle, a live streaming strip, a context meter and a
composer at the bottom.

Colours deliberately come from Textual's own design variables (``$background``,
``$panel``, ``$text-muted``, ``$accent``, ...). The framework already ships a
dark *and* a light palette and recomputes them when ``App.dark`` flips, so
keeping a second palette here would only create two copies that drift apart.

Note that Textual declares variables at the *top level* of a stylesheet
(``$name: value;``) -- the ``--name: value;`` custom-property syntax from web CSS
is not supported.
"""

from __future__ import annotations

import contextlib
from typing import Any

__all__ = [
    "CSS",
    "THEMES",
    "apply_theme",
    "available_themes",
    "context_bar",
    "context_style",
    "register_themes",
    "short_name",
]

#: Full palettes, ported from the pydantic-deepagents TUI (MIT). Keys map 1:1
#: onto Textual's ``Theme`` constructor arguments. ``default`` is the brand
#: theme (warm amber on near-black); the rest are one-key alternatives.
THEMES: dict[str, dict[str, str]] = {
    "default": {
        "primary": "#d98e48",
        "secondary": "#c2703a",
        "accent": "#f0b072",
        "foreground": "#e9e1d4",
        "background": "#0c0a07",
        "surface": "#15110c",
        "panel": "#211a12",
        "success": "#6fcf97",
        "warning": "#fbbf24",
        "error": "#ef4444",
    },
    "emerald": {
        "primary": "#10b981",
        "secondary": "#14b8a6",
        "accent": "#5eead4",
        "foreground": "#e6edeb",
        "background": "#0b0f0e",
        "surface": "#111816",
        "panel": "#172521",
        "success": "#10b981",
        "warning": "#f59e0b",
        "error": "#ef4444",
    },
    "ocean": {
        "primary": "#3b82f6",
        "secondary": "#06b6d4",
        "accent": "#7dd3fc",
        "foreground": "#e6edf3",
        "background": "#0a0f1a",
        "surface": "#111827",
        "panel": "#1b2740",
        "success": "#22c55e",
        "warning": "#f59e0b",
        "error": "#ef4444",
    },
    "rose": {
        "primary": "#f43f5e",
        "secondary": "#ec4899",
        "accent": "#fda4af",
        "foreground": "#f5e9ec",
        "background": "#140a0d",
        "surface": "#1c1117",
        "panel": "#2a1923",
        "success": "#10b981",
        "warning": "#f59e0b",
        "error": "#ef4444",
    },
    "minimal": {
        "primary": "#b4b4b4",
        "secondary": "#8a8a8a",
        "accent": "#ededed",
        "foreground": "#ededed",
        "background": "#0c0c0c",
        "surface": "#161616",
        "panel": "#202020",
        "success": "#9ca3af",
        "warning": "#d4d4d4",
        "error": "#ef4444",
    },
}

_THEME_PREFIX = "minicode-"


def _theme_name(name: str) -> str:
    """Map a short theme name to its registered Textual theme name."""
    return f"{_THEME_PREFIX}{name}"


def register_themes(app: Any) -> None:
    """Register all custom palettes with a Textual app (ported from deepagents)."""
    try:
        from textual.theme import Theme as TextualTheme
    except ImportError:  # pragma: no cover
        return

    for name, colors in THEMES.items():
        with contextlib.suppress(Exception):
            app.register_theme(
                TextualTheme(
                    name=_theme_name(name),
                    primary=colors["primary"],
                    secondary=colors.get("secondary"),
                    accent=colors.get("accent"),
                    foreground=colors.get("foreground"),
                    background=colors.get("background"),
                    surface=colors.get("surface"),
                    panel=colors.get("panel"),
                    success=colors.get("success", "#10b981"),
                    warning=colors.get("warning", "#f59e0b"),
                    error=colors.get("error", "#ef4444"),
                    dark=True,
                )
            )


def apply_theme(app: Any, theme_name: str) -> bool:
    """Apply a named theme (one of :data:`THEMES`) to the app."""
    if theme_name not in THEMES:
        return False
    try:
        app.theme = _theme_name(theme_name)
    except Exception:  # pragma: no cover - registration is best-effort
        return False
    return True


def available_themes() -> list[str]:
    """Return the short names of all registered themes."""
    return list(THEMES.keys())


def short_name(app: Any) -> str:
    """Map an app's current Textual theme name back to a short palette name."""
    name = getattr(app, "theme", "") or ""
    return name[len(_THEME_PREFIX) :] if name.startswith(_THEME_PREFIX) else ""

CSS = """
#body {
    height: 1fr;
}

#sidebar {
    width: 30;
    height: 1fr;
    border-right: solid $border;
    background: $panel;
}

.sidebar-title {
    height: 1;
    padding: 0 1;
    color: $text-muted;
    text-style: bold;
}

#new-session {
    width: 1fr;
    height: 1;
    margin: 0 1 1 1;
    padding: 0 1;
    background: $boost;
    border: none;
    color: $text;
}

#new-session:hover {
    background: $accent;
    color: $text-accent;
}

#new-session:focus {
    text-style: bold;
}

#session-list {
    height: 1fr;
    border: none;
    background: $panel;
}

#session-list .option-list--option-highlighted {
    background: $boost;
}

#main {
    height: 1fr;
}

/* The conversation stream owns the main area. The RichLog is kept in the
   layout (tests and bridge still write to it) but hidden: system output is
   mirrored into #messages so the user actually sees it. */
#messages {
    height: 1fr;
}

#log {
    display: none;
}

#stream {
    height: auto;
    max-height: 8;
    padding: 0 2;
}

#permission {
    height: auto;
    max-height: 12;
    padding: 0 1;
    background: $boost;
}

#permission-prompt {
    height: auto;
    padding: 0;
}

#permission-actions {
    height: auto;
    padding: 0 0 1 0;
}

#permission-actions Button {
    margin: 0 1 0 0;
    min-width: 10;
    padding: 0 1;
}

#context {
    height: 1;
    padding: 0 2;
    color: $text-muted;
    background: $panel;
}

#hints {
    height: 1;
    padding: 0 2;
    color: $text-muted;
    background: $panel;
}

#session-footer {
    height: 1;
    padding: 0 2;
    color: $text-muted;
    background: $panel;
    border-top: solid $border;
}

/* Command popover: sits between the context meter and the composer, so it
   reads as "attached to the input" without covering the transcript. */
#slash {
    height: auto;
    max-height: 10;
    border: round $accent;
    background: $panel;
}

#slash .option-list--option-highlighted {
    background: $boost;
}

#composer {
    height: 4;
    max-height: 12;
    border: round $border;
    background: $panel;
}

#composer:focus {
    border: round $accent;
}
"""

#: Fractions of the context window at which the usage meter changes colour.
_CONTEXT_WARN = 0.75
_CONTEXT_CRIT = 0.9
_BAR_WIDTH = 18


def context_bar(ratio: float, *, width: int = _BAR_WIDTH) -> str:
    """A context-window usage meter."""
    filled = max(0, min(width, round(ratio * width)))
    return "\u2593" * filled + "\u2591" * (width - filled)


def context_style(ratio: float) -> str:
    """Rich style for a context usage ratio.

    These must stay *real* Rich style names: Rich renders anything it does not
    recognise unstyled rather than raising, so a typo here would silently
    flatten the very warning the meter exists to give.
    """
    if ratio >= _CONTEXT_CRIT:
        return "bold red"
    if ratio >= _CONTEXT_WARN:
        return "bold dark_orange"
    return "green"
