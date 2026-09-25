"""The app keeps its footing: other programs, closing dialogs, focus, busy files, slow searches.

Each test here pins down something that once went wrong.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import threading
import time
from datetime import date
from pathlib import Path

from bdbd import cli
from bdbd.budget import Budget
from bdbd.core import repo
from bdbd.tui.app import BdbdApp, MainScreen
from bdbd.tui.debt_forms import PlanScreen
from bdbd.tui.flow_form import FlowForm
from bdbd.tui.forms import ConfirmScreen, TextField
from bdbd.tui.modals import FlowCard
from bdbd.tui.scenario import Change
from bdbd.tui.views import whatif
from bdbd.tui.views.calendar import CalendarView, MonthGrid
from bdbd.tui.views.forecast import HORIZONS, ForecastView
from bdbd.tui.widgets import Panel, RowList
from bdbd.ui.theme import MINUS
from bdbd.words import fmt_month

from .conftest import SIZE, screen_text

TODAY = date(2026, 9, 24)


def _id(app: BdbdApp, name: str) -> int:
    return int(next(f for f in app.session.flows() if f.name == name).id)


def _json(path: Path, *argv: str) -> dict:
    """A JSON command's data (in-process; the app under test would capture stdout)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.suppress(SystemExit):
        cli.main(["--db", str(path), *argv])
    envelope = json.loads(out.getvalue())
    assert envelope["ok"], envelope
    return envelope["data"]


def _main(app: BdbdApp) -> MainScreen:
    main = app.main
    assert main is not None
    return main


# ── Other programs ────────────────────────────────────────────────────────────


async def test_a_card_under_a_form_waits_then_closes_when_its_flow_goes(
    make_app, budget_file
) -> None:
    """Another program deletes the flow while its card sits under the edit form: the card
    must leave the form alone (closing pops whatever is on top) and close once it's back."""
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        gym = _id(app, "Gym")
        app.open_flow_card(gym)
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, FlowForm)
        other = Budget.open(budget_file, tidy=False)
        try:
            repo.remove_flow(other.conn, gym)
        finally:
            other.close()
        app._watch()  # the two-second check for changes on disk
        await pilot.pause(1.2)  # the card checks every second
        assert isinstance(app.screen, FlowForm)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
        assert not any(isinstance(s, FlowCard) for s in app.screen_stack)


async def test_a_busy_budget_is_a_message_not_a_crash(make_app, budget_file) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.session.conn.execute("PRAGMA busy_timeout = 50")
        gym = _id(app, "Gym")
        other = sqlite3.connect(budget_file, isolation_level=None)
        other.execute("BEGIN IMMEDIATE")  # another program is writing
        try:
            app.toggle_paused(gym)
            await pilot.pause()
            assert app.toasts[-1] == (
                "The budget file is busy in another program; try again in a moment."
            )
        finally:
            other.execute("ROLLBACK")
            other.close()
        assert app.session.flow(gym).active
        app.toggle_paused(gym)  # and it works once the other program is done
        await pilot.pause()
        assert not app.session.flow(gym).active


# ── Focus ─────────────────────────────────────────────────────────────────────


async def test_closing_a_dialog_never_focuses_a_hidden_view(make_app, empty_file) -> None:
    app = make_app(empty_file)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        main = _main(app)
        for view in ("2", "5", "6", "7", "1"):
            await pilot.press(view, "b")
            await pilot.pause()
            assert app.screen is not main
            await pilot.press("escape")
            await pilot.pause()
            focused = main.focused
            assert focused is None or main.current_view in focused.ancestors, (view, focused)


async def test_moving_the_day_takes_focus_back_to_the_grid(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        view = app.screen.query_one(CalendarView)
        grid, items = view.query_one(MonthGrid), view.query_one("#items", RowList)
        for start, key, lands in (
            (date(2026, 10, 1), "right", date(2026, 10, 2)),
            (date(2026, 10, 2), "t", TODAY),
            (date(2026, 10, 1), "right_square_bracket", date(2026, 11, 1)),
        ):
            view.select(start)
            await pilot.press("enter")
            assert items.has_focus
            await pilot.press(key)
            assert grid.has_focus and view.day == lands, key


async def test_a_prefilled_field_is_replaced_by_typing(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.edit_flow(_id(app, "Gym"))
        await pilot.pause()
        form = app.screen
        assert isinstance(form, FlowForm)
        await pilot.press("tab", *"45")  # to the amount, which holds 39.00
        amount = form.field("amount")
        assert isinstance(amount, TextField) and amount.text == "45"
        assert f"{MINUS}$45.00" in str(amount.parsed.preview)


async def test_a_long_form_keeps_its_summary_and_buttons_on_screen(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.add_flow(loan=True)
        await pilot.pause()
        form = app.screen
        assert isinstance(form, FlowForm)
        for key, text in (("name", "Van"), ("amount", "300"), ("when", "monthly")):
            field = form.field(key)
            assert isinstance(field, TextField)
            field.input.value = text
        for key, text in (("balance", "9000"), ("rate", "8%")):
            field = form.field(key)
            assert isinstance(field, TextField)
            field.input.value = text
        await pilot.pause()
        await pilot.pause()
        for part in ("#summary", "#buttons"):
            region = form.query_one(part).region
            assert region.height and region.bottom <= SIZE[1] - 1, part  # above the border
        assert "Paid off" in screen_text(app) and "Cancel" in screen_text(app)


async def test_a_title_drops_its_extras_rather_than_be_cut_off(make_app) -> None:
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        top = next(line for line in screen_text(app).splitlines() if "Coming up" in line)
        assert "─  Coming up  ─" in top and "─  Debts · $36,538.89 owed  ─" in top
        assert "…" not in top
        await pilot.resize_terminal(*SIZE)  # wider: the extras come back
        await pilot.pause()
        top = next(line for line in screen_text(app).splitlines() if "Coming up" in line)
        assert "Coming up · balances include $175/week everyday" in top
        assert "Debts · $36,538.89 owed · debt-free Aug 2034" in top


# ── Clicks and keys ───────────────────────────────────────────────────────────


async def test_clicking_a_choice_in_a_border_picks_it(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        view = app.screen.query_one(ForecastView)
        # the horizon, in the headline's bottom border (right-aligned)
        y = view.query_one("#headline", Panel).region.bottom - 1
        x = screen_text(app).splitlines()[y].index(" 1y ")
        await pilot.click(offset=(x + 1, y))
        await pilot.pause()
        assert HORIZONS[view.horizon or 0][0] == "1y"
        # the two lists, in the list panel's top border (left-aligned)
        y = view.query_one("#list-panel", Panel).region.y
        x = screen_text(app).splitlines()[y].index(" Month by month ")
        await pilot.click(offset=(x + 2, y))
        await pilot.pause()
        assert view.by_month


async def test_q_quits_but_asks_first_when_a_what_if_would_go(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.session.add_change(Change("stop", "Gym", "", "2026-10-31"))
        app.refresh_views()
        await pilot.pause()
        await pilot.press("q")
        assert isinstance(app.screen, ConfirmScreen)
        assert "Quit and drop the what-if?" in screen_text(app)
        await pilot.press("escape")
        assert isinstance(app.screen, MainScreen) and app.return_code is None
        await pilot.press("q", "right", "enter")
        await pilot.pause()
    assert app.return_code == 0


async def test_q_just_quits_without_a_what_if(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
    assert app.return_code == 0


# ── The what-if ───────────────────────────────────────────────────────────────


async def test_a_plan_tried_with_the_what_if_off_counts_its_changes(make_app, budget_file) -> None:
    """Trying a plan turns the what-if on, so the plan is worked out with its changes in."""
    sale = "Car loan:13000@2026-11-01"
    with_sale = _json(budget_file, "plan", "--extra", "200", "--settle", sale)
    without = _json(budget_file, "plan", "--extra", "200")
    assert with_sale["debt_free_on"] != without["debt_free_on"]  # the sale matters
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        s = app.session
        s.add_change(Change("settle", "Car loan", "13000", "2026-11-01"))
        s.set_lens(False)
        app.refresh_views()
        await pilot.press("6", "p")
        await pilot.pause(0.4)  # the plan's debounce, then its worker
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, PlanScreen)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
        assert s.lens_on and len(s.scenario.changes) == 1
        assert s.scenario.extra == with_sale["plan_scenario"]
        free = fmt_month(date.fromisoformat(with_sale["debt_free_on"]))
        assert f"debt-free {free}" in app.toasts[-1]


async def test_one_earliest_search_at_a_time(make_app, monkeypatch) -> None:
    """New floors while a search runs wait for it; only the newest is worked out next."""
    monkeypatch.setattr(whatif.WhatIfView, "MONTHS", 2)
    real = whatif.find
    lock = threading.Lock()
    running, most, floors = 0, 0, []

    def slow(search):
        nonlocal running, most
        with lock:
            running += 1
            most = max(most, running)
        try:
            time.sleep(0.3)
            floors.append(search.floor)
            return real(search)
        finally:
            with lock:
                running -= 1

    monkeypatch.setattr(whatif, "find", slow)
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.session.add_change(Change("add_expense", "Flight", "2500", "?"))
        await pilot.press("7")
        view = app.screen.query_one(whatif.WhatIfView)
        for _ in range(200):  # until the first search is under way
            await pilot.pause(0.02)
            if running:
                break
        assert running == 1
        for floor in (50000, 100000, 150000):
            view.floor = floor
            view.refresh_view()
            await pilot.pause(0.05)
        for _ in range(400):  # a slow machine gets 20 seconds
            await pilot.pause(0.05)
            if not view.searching:
                break
        await pilot.pause()
        assert most == 1
        assert floors == [17500, 150000]
        found = view._found
        assert found is not None and found.search.floor == 150000
