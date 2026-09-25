"""Regenerate the README screenshots in docs/.

    uv run scripts/screenshots.py

Each one runs a real bdbd command, through the same code you use, against the made-up household
in tests/sample.py ("today" pinned to Thu Sep 24, 2026), and saves what it prints with rich's
SVG export. None of the numbers are anyone's real budget.
"""

from __future__ import annotations

import contextlib
import io
import os
import shlex
import sys
import tempfile
from pathlib import Path

from rich.console import Console
from rich.terminal_theme import TerminalTheme
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bdbd import cli  # noqa: E402
from bdbd.core import db  # noqa: E402
from bdbd.ui import theme  # noqa: E402
from tests import sample  # noqa: E402

DOCS = ROOT / "docs"
PROMPT = "\u276f "  # a shell-prompt chevron

THEME = TerminalTheme(
    background=(16, 18, 25),
    foreground=(226, 228, 236),
    normal=[
        (32, 34, 44),
        (242, 80, 110),
        (31, 191, 143),
        (235, 154, 18),
        (124, 131, 247),
        (168, 113, 247),
        (86, 182, 194),
        (200, 202, 212),
    ],
    bright=[
        (92, 96, 112),
        (255, 110, 136),
        (70, 214, 170),
        (250, 184, 60),
        (152, 158, 255),
        (190, 146, 255),
        (120, 208, 220),
        (255, 255, 255),
    ],
)


def terminal(width: int) -> Console:
    return Console(
        record=True,
        width=width,
        force_terminal=True,
        color_system="truecolor",
        highlight=False,
        file=io.StringIO(),
    )


def run(console: Console, command: str) -> None:
    """Draw a prompt line, then run `bdbd …` with its output going to `console`."""
    console.print(Text.assemble((PROMPT, f"bold {theme.ACCENT}"), (command, "bold")))
    cli.out = theme.out = console
    cli.err = theme.err = console
    with contextlib.suppress(SystemExit):
        cli.main(shlex.split(command)[1:])


def shot(name: str, width: int, *commands: str, title: str = "bdbd") -> None:
    console = terminal(width)
    for i, command in enumerate(commands):
        if i:
            console.print()
        run(console, command)
    console.save_svg(str(DOCS / f"{name}.svg"), title=title, theme=THEME)


def main() -> None:
    DOCS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "budget.sqlite"
        conn = db.connect(path, create=True)
        sample.build(conn)
        conn.close()
        os.environ.update(BDBD_DB=str(path), BDBD_TODAY=sample.TODAY.isoformat())
        os.environ.pop("NO_COLOR", None)

        shot("hero", 100, "bdbd")
        shot("calendar", 100, "bdbd cal oct")
        shot("plan", 100, "bdbd plan --extra 300")
        shot("whatif", 100, 'bdbd breakeven --settle "Car loan:13000@nov 1" --stop-tag "car@nov 1"')
        shot(
            "add",
            90,
            "bdbd add Netflix 15.49 monthly on the 12th --tag fun",
            "bdbd balance 3980",
        )
    for path in sorted(DOCS.glob("*.svg")):
        print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
