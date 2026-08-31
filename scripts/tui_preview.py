"""Render the TUI to a self-contained HTML preview.

Not part of the app: a review aid. Feed the shell a short fake transcript and
export it, so the layout and the interactions can be checked without sitting in
front of a terminal -- including the ``/`` popover, which is the part that is
easiest to get subtly wrong.

Sessions are written to a scratch directory, never to the real one.

    python scripts/tui_preview.py            # writes docs/tui-preview.html
    python scripts/tui_preview.py --svg      # also keep the raw SVGs
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

DOCS = Path(__file__).resolve().parent.parent / "docs"
HTML = DOCS / "tui-preview.html"
SVG_POPOVER = DOCS / "tui-preview-popover.svg"
SVG_REPLAY = DOCS / "tui-preview-replay.svg"

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>minicode TUI preview</title>
<style>
  body {{
    margin: 0;
    padding: 24px;
    background: #101418;
    color: #c9d1d9;
    font: 14px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  }}
  h1 {{ font-size: 15px; font-weight: 600; margin: 0 0 4px; }}
  h2 {{ font-size: 13px; font-weight: 600; margin: 28px 0 6px; color: #e6edf3; }}
  p.caption {{ margin: 0 0 8px; color: #8b949e; font-size: 13px; }}
  figure {{ margin: 0; }}
  svg {{ width: 100%; height: auto; display: block; border-radius: 8px; }}
</style>
</head>
<body>
  <h1>minicode TUI</h1>
  <p class="caption">Session rail, transcript, live streaming strip, context meter
  and composer. Rendered headlessly from the real app.</p>

  <h2>Typing <code>/</code> opens the command popover</h2>
  <p class="caption">Filtered as you type, navigated with up/down, entered with
  enter or tab, dismissed with escape. The caret never leaves the composer.</p>
  <figure>{popover}</figure>

  <h2>Picking a session replays it</h2>
  <p class="caption">The stored messages were always on disk; nothing was
  rendering them. The rail is grouped by recency and only holds sessions that
  actually have a history.</p>
  <figure>{replay}</figure>
</body>
</html>
"""

_HISTORY = [
    ("fix the flaky test in test_session.py", 0.0),
    ("add pydantic validation to the settings loader", 1.0),
    ("port the env handling to pathlib", 9.0),
]


def _seed_history(app, *, title: str, days: float) -> object:
    """Write one finished session, as an earlier run would have left it."""
    import time

    session = app.core.sessions.create(title=title)
    app.core.sessions.append_message(session, {"role": "user", "content": title, "extra": {}})
    app.core.sessions.append_message(
        session,
        {
            "role": "assistant",
            "content": "",
            "extra": {"tool_calls": [{"name": "bash", "arguments": {"command": "pytest -q"}}]},
        },
    )
    app.core.sessions.append_message(
        session,
        {"role": "tool", "content": "9 passed in 0.41s", "extra": {"tool_name": "bash"}},
    )
    app.core.sessions.append_message(
        session, {"role": "assistant", "content": "Done - the suite is green again.", "extra": {}}
    )
    app.core.sessions.save(session)
    # ``save`` re-stamps ``updated_at``, so backdate *after* it and write
    # straight through the store -- otherwise every seeded session lands in the
    # TODAY bucket and the grouping the preview exists to show never appears.
    session.updated_at = time.time() - days * 86_400
    app.core.sessions.store.save(session.id, session.to_dict())
    return session


async def _capture(*, also_svg: bool) -> None:
    from minicode.ui.textual.app import MiniTUI

    app = MiniTUI()
    async with app.run_test(size=(150, 44)) as pilot:
        write = app.append_to_log
        older = [_seed_history(app, title=title, days=days) for title, days in _HISTORY]
        app.refresh_sessions()

        write(Text.from_markup("[bold green]you[/] fix the failing division test in test_calculator.py"))
        write(Text.from_markup("[bold yellow]\u25b8 bash[/]"))
        write(Text("python -m pytest -q test_calculator.py", style="grey62"))
        write(
            Panel(
                Text.from_markup(
                    "[red]FAILED[/] test_calculator.py::test_divide_by_zero\n"
                    "[dim]assert 0 == ZeroDivisionError(...)[/]\n"
                    "[dim]1 failed, 8 passed in 0.34s[/]"
                ),
                title="bash [red]failed[/]",
                title_align="left",
                border_style="red",
                padding=(0, 1),
                expand=False,
            )
        )
        write(Text.from_markup("[bold yellow]\u25b8 edit[/]"))
        write(Text("calculator.py: 'return a / b' -> guard against b == 0", style="grey62"))
        write(Markdown("`calculator.py` now rejects a zero divisor before dividing."))
        write(Markdown("Re-ran the suite:\n\n- `python -m pytest -q` -> **9 passed**\n- `ruff check .` -> clean"))

        app.show_stats(
            {"tokens": 41_280, "ratio": 0.34, "steps": 6, "tool_calls": 4, "cost": 0.0183, "compactions": 0}
        )
        await pilot.pause()

        DOCS.mkdir(parents=True, exist_ok=True)

        # 1. the slash popover
        await pilot.press("/")
        await pilot.pause()
        app.save_screenshot(str(SVG_POPOVER))
        await pilot.press("escape")
        await pilot.pause()

        # 2. a replayed session from the rail
        app.replay_session(older[1])
        await pilot.pause()
        app.save_screenshot(str(SVG_REPLAY))

        HTML.write_text(
            PAGE.format(
                popover=SVG_POPOVER.read_text(encoding="utf-8"),
                replay=SVG_REPLAY.read_text(encoding="utf-8"),
            ),
            encoding="utf-8",
        )
        print(f"wrote {HTML}")
        if not also_svg:
            SVG_POPOVER.unlink(missing_ok=True)
            SVG_REPLAY.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--svg", action="store_true", help="keep the raw SVGs next to the HTML")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as scratch:
        os.environ["MINICODE_DATA_DIR"] = scratch
        asyncio.run(_capture(also_svg=args.svg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
