"""Fixtures for the app's tests: the made-up household in a temp file, "today" Thu Sep 24, 2026.

A test drives the real app with Textual's Pilot and reads the screen as plain text:

    async def test_something(make_app):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("2")
            assert "Calendar" in screen_text(app)
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from textual.app import App
from textual.widget import Widget

from bdbd.core import db
from bdbd.tui.app import BdbdApp
from tests import sample

SIZE = (120, 36)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    """The sample household's today (this replaces the root conftest's Sep 16)."""
    monkeypatch.setenv("BDBD_TODAY", sample.TODAY.isoformat())
    for var in ("BDBD_DB", "BDBD_CURRENCY", "NO_COLOR"):
        monkeypatch.delenv(var, raising=False)


def build_budget(path: Path, *, empty: bool = False, with_balance: bool = True) -> Path:
    conn = db.connect(path, create=True)
    try:
        if not empty:
            sample.build(conn, with_balance=with_balance)
    finally:
        conn.close()
    return path


@pytest.fixture
def budget_file(tmp_path) -> Path:
    """The sample household, with its balance recorded on Sep 22."""
    return build_budget(tmp_path / "budget.sqlite")


@pytest.fixture
def empty_file(tmp_path) -> Path:
    """A budget with nothing in it."""
    return build_budget(tmp_path / "empty.sqlite", empty=True)


@pytest.fixture
def make_app(budget_file) -> Callable[..., BdbdApp]:
    """An app on the sample household (or on `path`)."""

    def make(path: Path | None = None, *, tidy: bool = True) -> BdbdApp:
        return BdbdApp(path or budget_file, tidy=tidy)

    return make


def screen_text(app: App) -> str:
    """Everything on the active screen, as plain text (a dialog shows without what's under it)."""
    return "\n".join(strip.text for strip in app.screen._compositor.render_strips())


def widget_text(widget: Widget) -> str:
    """The plain text inside one widget's region of the screen."""
    region = widget.region
    lines = widget.app.screen._compositor.render_strips()
    return "\n".join(
        lines[y].crop(region.x, region.right).text.rstrip()
        for y in range(region.y, region.bottom)
        if 0 <= y < len(lines)
    )
