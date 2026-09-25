"""Regenerate the README screenshots in docs/.

    uv run scripts/screenshots.py

Each one runs the real app headless (see tui_shot.py) on the made-up household in
tests/sample.py ("today" pinned to Thu Sep 24, 2026), presses a few keys and saves the screen
as an SVG. None of the numbers are anyone's real budget.
"""

from __future__ import annotations

import sys

from tui_shot import ROOT, shoot

from bdbd.tui.scenario import Change

DOCS = ROOT / "docs"
SELL_THE_CAR = [
    Change("settle", "Car loan", "13000", "2026-11-01"),
    Change("stop_tag", "car", "", "2026-11-01"),
]

SHOTS: list[tuple[str, dict]] = [
    ("overview", {}),
    ("calendar", {"keys": ["2", "right", "right", "right", "right", "right", "right", "right"]}),
    ("forecast", {"keys": ["3", "right_square_bracket", "right_square_bracket", "wait:600"]}),
    ("budget", {"keys": ["4", "down", "down", "down", "down", "down"]}),  # the Car loan
    ("debts", {"keys": ["5"]}),
    ("plan", {"keys": ["5", "p", "wait:800"]}),
    (
        "whatif",
        {"keys": ["6", "right_square_bracket", "right_square_bracket"], "what_if": SELL_THE_CAR},
    ),
    ("add", {"keys": ["a", *"Netflix", "tab", *"15.49", "tab", *"monthly on the 12th", "tab"]}),
]


def main() -> None:
    names = set(sys.argv[1:])  # just these, or all of them
    for name, options in SHOTS:
        if not names or name in names:
            print(shoot(name, out=DOCS, png=False, **options).relative_to(ROOT))


if __name__ == "__main__":
    main()
