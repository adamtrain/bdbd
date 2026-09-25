"""The Calendar view, driven the way a person would (Textual's Pilot).

Every number it shows is checked against the JSON command that answers the same question:
`bdbd cal` for the days and balances, `bdbd project --until DAY` for the day panel.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from textual.widgets import Input

from bdbd import cli
from bdbd.core import db, repo
from bdbd.core.models import Kind
from bdbd.tui.app import BdbdApp, MainScreen
from bdbd.tui.forms import FormScreen, PromptScreen
from bdbd.tui.modals import HelpScreen
from bdbd.tui.scenario import Change
from bdbd.tui.views.calendar import (
    CalDay,
    CalendarView,
    DayCell,
    Entry,
    Figures,
    MonthGrid,
    answer,
    cell_lines,
    month_of,
    totals,
)
from bdbd.tui.widgets import Panel, RowList
from bdbd.ui.theme import MINUS, cents_of, money

from .conftest import SIZE, build_budget, screen_text, widget_text

TODAY = date(2026, 9, 24)


def _json(path: Path, capsys: pytest.CaptureFixture[str], *argv: str) -> dict:
    """Run a real bdbd command against `path` and return its data."""
    capsys.readouterr()
    try:
        cli.main(["--db", str(path), *argv])
    except SystemExit as exc:
        assert not exc.code, exc
    env = json.loads(capsys.readouterr().out)
    assert env["ok"], env
    return env["data"]


async def _open(pilot, app: BdbdApp) -> CalendarView:
    await pilot.pause()
    await pilot.press("3")
    await pilot.pause()
    return app.screen.query_one(CalendarView)


def _cell(view: CalendarView, day: date) -> DayCell:
    return next(c for c in view.query(DayCell) if c.day == day and c.info is not None)


def _figures(view: CalendarView) -> str:
    return widget_text(view.query_one("#figures", Figures))


# ── Showing a month ───────────────────────────────────────────────────────────


async def test_it_opens_on_today(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await _open(pilot, app)
        assert view.day == TODAY and view.month == date(2026, 9, 1)
        assert view.query_one(MonthGrid).has_focus
        text = screen_text(app)
        assert "September 2026" in text and "balances at the end of each day" in text
        assert "Thu Sep 24, 2026 · today" in text
        today = _cell(view, TODAY)
        assert today.has_class("-selected")
        assert "$4,070" in widget_text(today)  # the end of today, in whole dollars
        figures = _figures(view)
        assert "At the end of the day" in figures and "$4,070.00" in figures
        assert "Spare" in figures and "$1,595.00" in figures
        assert "before Paycheck on Fri Oct 2" in figures


async def test_days_and_balances_are_bdbd_cal(make_app, budget_file, capsys) -> None:
    cal = _json(budget_file, capsys, "cal", "oct")
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await _open(pilot, app)
        await pilot.press("right_square_bracket")
        month = month_of(app.session, date(2026, 10, 1))
        for want, got in zip(cal["days"], month.days, strict=True):
            assert got.date.isoformat() == want["date"]
            assert got.balance == cents_of(want["balance"])
            assert [(e.name, e.cents, e.kind) for e in got.entries] == [
                (i["name"], cents_of(i["amount"]), i["kind"]) for i in want["items"]
            ]
        rent = widget_text(_cell(view, date(2026, 10, 1)))
        assert f"{MINUS}2,150" in rent and "Rent" in rent and "$1,595" in rent
        paycheck = widget_text(_cell(view, date(2026, 10, 2)))
        assert "+2,650" in paycheck and "Paycheck" in paycheck and "$4,220" in paycheck


async def test_the_day_panel_is_bdbd_project(make_app, budget_file, capsys) -> None:
    days = [date(2026, 9, 24), date(2026, 10, 1), date(2026, 10, 24), date(2026, 12, 12)]
    projects = {d: _json(budget_file, capsys, "project", "--until", d.isoformat()) for d in days}
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await _open(pilot, app)
        for day, data in projects.items():
            view.select(day)
            await pilot.pause()
            a = answer(app.session, day)
            assert a is not None
            assert a.balance == cents_of(data["ending_balance"])
            assert a.spare == cents_of(data["spare_balance"])
            nxt = data["spare"]["next_payday"]
            payday = (nxt["name"], date.fromisoformat(nxt["date"]), cents_of(nxt["amount"]))
            assert a.payday == payday
            figures = _figures(view)
            assert money(a.balance) in figures and money(a.spare) in figures


async def test_the_month_footer_is_the_cal_footer(make_app, budget_file, capsys) -> None:
    days = _json(budget_file, capsys, "cal", "oct")["days"]
    ahead = [d for d in days if d["date"] >= TODAY.isoformat()]
    money_in = sum(
        cents_of(i["amount"]) for d in ahead for i in d["items"] if i["amount"][0] != "-"
    )
    money_out = -sum(
        cents_of(i["amount"]) for d in ahead for i in d["items"] if i["amount"][0] == "-"
    )
    lowest = min(ahead, key=lambda d: (cents_of(d["balance"]), d["date"]))
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await _open(pilot, app)
        await pilot.press("right_square_bracket")
        t = totals(month_of(app.session, date(2026, 10, 1)), TODAY)
        assert (t.money_in, t.money_out) == (money_in, money_out)
        assert t.lowest == (date.fromisoformat(lowest["date"]), cents_of(lowest["balance"]))
        assert t.ends == (date(2026, 10, 31), cents_of(days[-1]["balance"]))
        assert t.starts is not None and t.ends is not None  # and it adds up
        assert t.starts + t.money_in - t.money_out - t.everyday == t.ends[1]
        text = widget_text(view.query_one("#month-box"))
        assert "October" in text and "+$9,190.00" in text and f"{MINUS}$3,584.76" in text
        assert "Lowest" in text and "Thu Oct 1" in text and "$1,595.00" in text
        assert "Ends at" in text and "$8,600.24" in text


async def test_past_days_show_what_was_scheduled(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await _open(pilot, app)
        payday = date(2026, 9, 18)
        cell = widget_text(_cell(view, payday))
        assert "Paycheck" in cell and "$" not in cell  # scheduled, but no balance
        view.select(payday)
        await pilot.pause()
        assert "This day has passed." in _figures(view)
        items = widget_text(view.query_one("#items", RowList))
        assert "Paycheck" in items and "+$2,650.00" in items and items.count("$") == 1


# ── Moving around ─────────────────────────────────────────────────────────────


async def test_a_days_items_list_money_in_first_then_the_largest(make_app, budget_file) -> None:
    """Past days (worked out flow by flow) list the same way as the days ahead (the engine)."""
    conn = db.connect(budget_file)
    try:
        for name, cents in (("Apps", 500), ("Bills", 5000)):
            repo.add_flow(conn, name=name, kind=Kind.EXPENSE, amount_cents=cents,
                          rrule="FREQ=MONTHLY;BYMONTHDAY=3", dtstart=date(2026, 9, 3))  # fmt: skip
        repo.add_flow(conn, name="Refund", kind=Kind.INCOME, amount_cents=2000, rrule=None,
                      dtstart=date(2026, 9, 3))  # fmt: skip
    finally:
        conn.close()
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await _open(pilot, app)
        mine = {"Refund", "Bills", "Gym", "Apps"}
        for day, want in (
            (date(2026, 9, 3), ["Refund", "Bills", "Apps"]),  # past: what was scheduled
            (date(2026, 10, 3), ["Bills", "Gym", "Apps"]),  # ahead: the projection
        ):
            info = month_of(app.session, day.replace(day=1)).day(day)
            assert info is not None
            names = [e.name for e in info.entries if e.name in mine]
            assert [n for n in names if n in want] == want, day


async def test_keys_move_the_day_across_months(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await _open(pilot, app)
        await pilot.press("right", "l")
        assert view.day == date(2026, 9, 26)
        await pilot.press("down", "j")  # into October: the page turns
        assert view.day == date(2026, 10, 10) and view.month == date(2026, 10, 1)
        assert "October 2026" in screen_text(app)
        await pilot.press("up", "k", "h", "left")
        assert view.day == date(2026, 9, 24) and view.month == date(2026, 9, 1)
        await pilot.press("pagedown")
        assert view.day == date(2026, 10, 24)
        await pilot.press("pageup", "left_square_bracket")
        assert view.day == date(2026, 8, 24)
        await pilot.press("t")
        assert view.day == TODAY


async def test_month_keys_keep_the_day_of_the_month(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await _open(pilot, app)
        view.select(date(2027, 1, 31))
        await pilot.press("right_square_bracket")
        assert view.day == date(2027, 2, 28)  # February is short
        await pilot.press("right_square_bracket")
        assert view.day == date(2027, 3, 31)  # and the 31st comes back


async def test_go_to_a_date(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await _open(pilot, app)
        await pilot.press("g")
        assert isinstance(app.screen, PromptScreen)
        await pilot.press(*"dec 12")
        await pilot.pause()
        assert "Sat Dec 12 · ends at $9,356.12" in screen_text(app)  # the answer, live
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
        assert view.day == date(2026, 12, 12) and view.query_one(MonthGrid).has_focus
        await pilot.press("g", "x", "enter")
        assert isinstance(app.screen, PromptScreen)  # refused
        assert "Try dec 12" in screen_text(app)
        await pilot.press("escape")
        assert view.day == date(2026, 12, 12)
        for words, day in (("sep 18", date(2026, 9, 18)), ("feb", date(2027, 2, 1))):
            view.action_go()  # a day without a year is the nearer one, a month the next one
            await pilot.pause()
            prompt = app.screen
            assert isinstance(prompt, PromptScreen)
            prompt.query_one(Input).value = words
            await pilot.press("enter")
            assert view.day == day


async def test_clicking_a_day_selects_it(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await _open(pilot, app)
        await pilot.click(_cell(view, date(2026, 9, 10)))
        assert view.day == date(2026, 9, 10)
        outside = next(c for c in view.query(DayCell) if c.day == date(2026, 10, 2))
        await pilot.click(outside)  # a day of the next month turns the page
        assert view.day == date(2026, 10, 2) and view.month == date(2026, 10, 1)


async def test_enter_opens_the_days_items_then_the_flow_card(make_app) -> None:
    app = make_app()
    opened: list[int] = []
    async with app.run_test(size=SIZE) as pilot:
        view = await _open(pilot, app)
        app.open_flow_card = opened.append  # type: ignore[method-assign]
        view.select(date(2026, 10, 1))
        await pilot.press("enter")
        items = view.query_one("#items", RowList)
        assert items.has_focus
        text = widget_text(items)
        assert "Rent" in text and f"{MINUS}$2,150.00" in text and "$1,595.00" in text
        await pilot.press("enter")
        rent = next(int(f.id) for f in app.session.flows() if f.name == "Rent")
        assert opened == [rent]
        await pilot.press("escape")
        assert view.query_one(MonthGrid).has_focus
        view.select(date(2026, 10, 6))  # nothing scheduled: enter stays on the grid
        await pilot.press("enter")
        assert view.query_one(MonthGrid).has_focus


async def test_a_day_with_several_items_shows_the_balance_after_each(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await _open(pilot, app)
        view.select(date(2027, 2, 5))  # Paycheck and Car loan
        await pilot.pause()
        info = month_of(app.session, date(2027, 2, 1)).day(date(2027, 2, 5))
        assert info is not None and len(info.entries) == 2
        paycheck, loan = info.entries
        assert loan.after == info.balance  # the day's last item shows the end of the day,
        assert paycheck.after is not None and info.balance is not None  # so it includes the
        assert paycheck.after + loan.cents - info.balance == 2500  # day's $25 of everyday
        text = widget_text(view.query_one("#items", RowList))
        assert money(paycheck.after) in text and money(loan.after or 0) in text
        assert "Car loan ◆" in text and "2 items" in widget_text(view.query_one("#items-head"))


# ── Changing things ───────────────────────────────────────────────────────────


async def test_a_adds_a_flow_starting_on_the_selected_day(make_app, budget_file) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await _open(pilot, app)
        view.select(date(2026, 10, 13))
        await pilot.press("a")
        await pilot.pause()
        form = app.screen
        assert isinstance(form, FormScreen)
        assert form.field("starts").text == "Oct 13, 2026"  # filled in with the day
        for key, text in (("name", "Gift"), ("amount", "80"), ("when", "once")):
            form.field(key).focus_field()
            await pilot.press(*text)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen), screen_text(app)
        assert app.toasts[-1].startswith("Added Gift")
        cell = widget_text(_cell(view, date(2026, 10, 13)))
        assert "Gift" in cell and f"{MINUS}80" in cell
    conn = sqlite3.connect(budget_file)
    conn.row_factory = sqlite3.Row
    try:
        gift = repo.resolve_flow(conn, "Gift")
    finally:
        conn.close()
    assert gift.dtstart == date(2026, 10, 13) and gift.rrule is None
    assert gift.amount_cents == 8000


async def test_the_what_if_lens(make_app, budget_file, capsys) -> None:
    day = date(2026, 10, 26)
    data = _json(
        budget_file,
        capsys,
        "project",
        "--until",
        day.isoformat(),
        "--settle",
        "Car loan:13000@2026-10-26",
        "--add-expense",
        "Flight:650@2026-10-26",
    )
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await _open(pilot, app)
        app.session.add_change(Change("settle", "Car loan", "13000", day.isoformat()))
        app.session.add_change(Change("add_expense", "Flight", "650", day.isoformat()))
        app.refresh_views()
        view.select(day)
        await pilot.pause()
        a = answer(app.session, day)
        assert a is not None
        assert a.balance == cents_of(data["ending_balance"])
        assert a.spare == cents_of(data["spare_balance"])
        assert "with the what-if" in screen_text(app)
        cell = widget_text(_cell(view, day))
        assert "↳Flight" in cell and "Credit" in cell
        items = widget_text(view.query_one("#items", RowList))
        assert "↳ Flight" in items and "↳ Car loan" in items and "↳ Credit" not in items
        base = answer(app.session, day, baseline=True)
        assert base is not None
        assert f"{money(a.balance - base.balance, sign=True)} vs now" in _figures(view)
        await pilot.press("w")  # the lens off: the budget as it is
        await pilot.pause()
        assert "↳" not in widget_text(_cell(view, day))
        assert "vs now" not in _figures(view)


# ── Sizes and empty states ────────────────────────────────────────────────────


async def test_months_take_the_rows_they_need(make_app) -> None:
    months = {
        date(2027, 2, 1): 4,  # starts on a Monday, 28 days
        date(2026, 10, 1): 5,
        date(2026, 11, 1): 6,  # starts on a Sunday
        date(2028, 2, 1): 5,  # a leap February
    }
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await _open(pilot, app)
        for month, weeks in months.items():
            view.select(month)
            await pilot.pause()
            shown = [c for c in view.query(DayCell) if c.display]
            assert len(shown) == weeks * 7
            inside = [c for c in shown if c.info is not None]
            assert [c.day.day for c in inside] == list(range(1, len(inside) + 1))
            assert shown[0].day.weekday() == 0 and len({c.region.height for c in inside}) <= 2


async def test_narrow_puts_the_day_below(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await _open(pilot, app)
        await pilot.press("right_square_bracket")
        assert not view.query_one("#month-panel", Panel).border_subtitle
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert view.has_class("-narrow")
        grid, day = view.query_one("#month-panel"), view.query_one("#day-panel")
        assert day.region.y >= grid.region.bottom  # below, full width
        assert not view.query_one("#month-box").display
        summary = widget_text(grid).splitlines()[-1]  # the month, in the bottom border
        assert "+$9,190 in" in summary and "ends $8,600" in summary
        assert "$1,595" in widget_text(_cell(view, date(2026, 10, 1)))
        assert "At the end of the day" in _figures(view)
        await pilot.resize_terminal(*SIZE)  # and back
        await pilot.pause()
        assert view.query_one("#month-box").display and view.query_one(MonthGrid).region.height
        assert view.query_one("#day-panel").region.height == view.region.height
        assert not view.query_one("#month-panel", Panel).border_subtitle


async def test_without_a_balance(make_app, tmp_path: Path) -> None:
    path = build_budget(tmp_path / "nb.sqlite", with_balance=False)
    app = make_app(path)
    async with app.run_test(size=SIZE) as pilot:
        view = await _open(pilot, app)
        await pilot.press("right_square_bracket")
        figures = _figures(view)
        assert "unknown" in figures and "press b to record your balance" in figures
        rent = widget_text(_cell(view, date(2026, 10, 1)))
        assert "Rent" in rent and "$" not in rent
        assert "balances at the end" not in screen_text(app)


async def test_an_empty_budget(make_app, empty_file: Path) -> None:
    app = make_app(empty_file)
    async with app.run_test(size=SIZE) as pilot:
        view = await _open(pilot, app)
        assert "No flows yet. Press a to add one" in widget_text(view.query_one("#items"))
        assert "September 2026" in screen_text(app)


async def test_help_lists_the_calendar_keys(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await _open(pilot, app)
        await pilot.press("question_mark")
        assert isinstance(app.screen, HelpScreen)
        text = screen_text(app)
        assert "Calendar" in text and "previous month" in text and "go to a date" in text
        assert "the day's items" in text


# ── A cell on its own ─────────────────────────────────────────────────────────


def _day(*names: str, balance: int | None = 150000) -> CalDay:
    entries = tuple(Entry(n, -1000 * (i + 1), "expense") for i, n in enumerate(names))
    return CalDay(date(2026, 10, 12), entries, balance)


def _plain(lines) -> list[str]:
    return [line.plain for line in lines]


def _lines(info: CalDay, width: int, height: int) -> list[str]:
    return _plain(cell_lines(info.date, info, width, height, today=TODAY, low=0))


def test_a_cell_fits_its_size() -> None:
    day = _day("Electric", "Car insurance", "Gym", "Streaming")
    roomy = _lines(day, 12, 7)
    assert roomy[0].startswith(" 12") and roomy[0].endswith(f"{MINUS}100")
    assert roomy[1:5] == [" Electric", " Car insura…", " Gym", " Streaming"]
    assert roomy[-1].endswith("$1,500")
    short = _lines(day, 12, 4)
    assert short[1:3] == [" Electric", " +3 more"] and short[3].endswith("$1,500")
    assert _lines(day, 12, 3)[1] == " 4 items"
    rent = CalDay(day.date, (Entry("Rent", -215000, "expense"),), 150000)
    assert _lines(rent, 10, 2)[0] == f" 12 {MINUS}2,150"
    assert _lines(rent, 9, 2)[0] == f" 12 {MINUS}2.2k"  # compact first
    assert _lines(rent, 7, 2)[0] == " 12"  # then it gives way
    one = _lines(day, 12, 1)
    assert len(one) == 1 and one[0].startswith(" 12")


def test_a_selected_cell_has_the_accent_bar() -> None:
    day = _day("Rent")
    lines = cell_lines(day.date, day, 10, 3, today=TODAY, low=0, selected=True, focused=True)
    assert all(line.plain.startswith("▌") for line in lines)
    outside = cell_lines(date(2026, 11, 1), None, 10, 3, today=TODAY, low=0)
    assert _plain(outside)[0] == "  1" and _plain(outside)[1].strip() == ""
