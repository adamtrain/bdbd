"""Headless screenshots of the app: an SVG (and a PNG of it) for visual checks and the README.

    uv run scripts/tui_shot.py --out DIR --name overview --size 120x36 --keys "3,],]"

It builds the made-up household from tests/sample.py in a temporary file ("today" pinned to
Thu Sep 24, 2026), runs the app headless at --size, presses --keys, and saves NAME.svg plus
NAME.svg.png (rasterized with macOS's qlmanage) in --out. Nobody's real budget is touched.

Keys are comma-separated names the way Textual's Pilot takes them: b, enter, escape, tab,
down, ], question_mark, comma … plus 'wait' (half a second), 'wait:MS', and 'type:TEXT'
(types TEXT, which can't contain a comma). --empty starts from a budget with no flows,
--no-balance from one with no recorded balance, --missing from no budget file at all, and
--what-if "KIND|TARGET|AMOUNT|WHEN" (repeatable) puts a change in the what-if sandbox first,
e.g. --what-if "settle|Car loan|13000|2026-11-01".
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["BDBD_TODAY"] = "2026-09-24"  # the sample household's "today"

from bdbd.core import db  # noqa: E402
from bdbd.tui.app import BdbdApp  # noqa: E402
from bdbd.tui.scenario import Change  # noqa: E402
from tests import sample  # noqa: E402


def build(path: Path, *, empty: bool = False, no_balance: bool = False) -> None:
    """Write the sample household (or an empty budget) to `path`."""
    conn = db.connect(path, create=True)
    try:
        if not empty:
            sample.build(conn, with_balance=not no_balance)
    finally:
        conn.close()


async def _run(
    path: Path,
    size: tuple[int, int],
    keys: list[str],
    changes: list[Change],
    out: Path,
    name: str,
) -> Path:
    app = BdbdApp(path)
    async with app.run_test(size=size, notifications=True) as pilot:
        await pilot.pause()
        if changes:
            for change in changes:
                app.session.add_change(change)
            app.refresh_views()
            await pilot.pause()
        for token in keys:
            if token == "wait":
                await pilot.pause(0.5)
            elif token.startswith("type:"):
                await pilot.press(*token.removeprefix("type:"))
            else:
                await pilot.press(token)
        await pilot.pause(0.2)
        svg = app.save_screenshot(filename=f"{name}.svg", path=str(out))
    return Path(svg)


def rasterize(svg: Path, size: int = 1600) -> Path | None:
    """NAME.svg -> NAME.svg.png next to it (macOS only; None elsewhere)."""
    if shutil.which("qlmanage") is None:
        return None
    subprocess.run(
        ["qlmanage", "-t", "-s", str(size), "-o", str(svg.parent), str(svg)],
        check=True,
        capture_output=True,
    )
    png = svg.parent / f"{svg.name}.png"
    return png if png.exists() else None


def shoot(
    name: str,
    *,
    out: Path,
    size: tuple[int, int] = (120, 36),
    keys: list[str] | None = None,
    empty: bool = False,
    no_balance: bool = False,
    missing: bool = False,
    what_if: list[Change] | None = None,
    png: bool = True,
) -> Path:
    """Build a budget, run the app, press keys, save NAME.svg (and .png); returns the SVG."""
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "budget.sqlite"
        if not missing:
            build(path, empty=empty, no_balance=no_balance)
        svg = asyncio.run(_run(path, size, keys or [], what_if or [], out, name))
    if png:
        rasterize(svg)
    return svg


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="where to save the files")
    parser.add_argument("--name", default="shot", help="the file name, without .svg")
    parser.add_argument("--size", default="120x36", help="WIDTHxHEIGHT (default 120x36)")
    parser.add_argument("--keys", default="", help="comma-separated keys to press first")
    parser.add_argument("--empty", action="store_true", help="a budget with no flows")
    parser.add_argument("--no-balance", action="store_true", help="no recorded balance")
    parser.add_argument("--missing", action="store_true", help="no budget file at all")
    parser.add_argument("--what-if", action="append", default=[], metavar="KIND|TARGET|AMOUNT|WHEN")
    parser.add_argument("--no-png", action="store_true", help="skip the PNG")
    args = parser.parse_args()
    width, _, height = args.size.partition("x")
    svg = shoot(
        args.name,
        out=args.out,
        size=(int(width), int(height)),
        keys=[k.strip() for k in args.keys.split(",") if k.strip()],
        empty=args.empty,
        no_balance=args.no_balance,
        missing=args.missing,
        what_if=[Change(*(part.strip() for part in spec.split("|"))) for spec in args.what_if],
        png=not args.no_png,
    )
    print(svg)
    png = svg.parent / f"{svg.name}.png"
    if png.exists():
        print(png)


if __name__ == "__main__":
    main()
