"""The Forecast view, driven the way a person would, checked against `bdbd project`."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
from datetime import date
from pathlib import Path

from textual.content import Content
from textual.widget import Widget
from textual.widgets import Input

from bdbd import cli
from bdbd.core import balance as balances
from bdbd.core.errors import CashError
from bdbd.core.models import Kind
from bdbd.tui.app import BdbdApp, MainScreen
from bdbd.tui.forms import PromptScreen
from bdbd.tui.modals import HelpScreen
from bdbd.tui.scenario import Change
from bdbd.tui.session import FlowDraft
from bdbd.tui.views.forecast import ForecastView, read_until
from bdbd.tui.widgets import Chart, Panel, Row, RowList
from bdbd.ui.theme import DOT, cents_of, money

from .conftest import SIZE, build_budget, screen_text, widget_text

TODAY = date(2026, 9, 24)
SETTLE = Change("settle", "Car loan", "13000", "2026-11-01")
FLIGHT = Change("add_expense", "Flight", "650", "2026-10-09")


def project(path: Path, until: date, *flags: str) -> dict:
    """What `bdbd project --until UNTIL` prints (the real JSON command)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):  # the app under test captures stdout otherwise
        try:
            cli.main(["--db", str(path), "project", "--until", until.isoformat(), *flags])
        except SystemExit as exc:
            assert not exc.code
    envelope = json.loads(out.getvalue())
    assert envelope["ok"], envelope
    return envelope


async def open_forecast(app: BdbdApp, pilot) -> ForecastView:
    await pilot.pause()
    await pilot.press("4")
    await pilot.pause()
    view = app.screen.query_one(ForecastView)
    assert isinstance(app.screen, MainScreen) and app.screen.switcher.current == "forecast"
    return view


async def settle(app: BdbdApp, pilot) -> None:
    """Wait for the thread that works out long windows, and for the redraw after it."""
    await app.workers.wait_for_complete()
    await pilot.pause()


def headline(view: ForecastView) -> str:
    return widget_text(view.query_one("#headline"))


def rows_of(view: ForecastView, list_id: str) -> list[Row]:
    return [it for it in view.query_one(list_id, RowList).items if isinstance(it, Row)]


def border(widget: Widget, *, top: bool = False) -> str:
    """A widget's border subtitle (or title) as plain text."""
    markup = widget.border_title if top else widget.border_subtitle
    return Content.from_markup(str(markup or "")).plain


def day_of(row: Row) -> date:
    key = row.key
    assert isinstance(key, tuple) and isinstance(key[1], date)
    return key[1]


def plain(row: Row) -> list[str]:
    return [str(getattr(c, "plain", c)) for c in row.cells]


def assert_headline_matches(text: str, data: dict) -> None:
    totals = data["totals"]
    for value in (
        data["starting_balance"],
        data["opening"]["balance"],
        data["ending_balance"],
        data["spare_balance"],
        data["min_balance"]["balance"],
        totals["expense"],
        data["lifestyle_total"],
        totals["interest_paid"],
        totals["principal_paid"],
        data["total_debt_at_until"],
    ):
        assert money(cents_of(value)) in text, value
    assert money(cents_of(totals["income"]), sign=True) in text
    assert money(cents_of(totals["net"]), sign=True) in text


# ── The numbers ───────────────────────────────────────────────────────────────


async def test_the_headline_is_what_bdbd_project_says(make_app, budget_file) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_forecast(app, pilot)
        assert view.until == date(2026, 12, 24)  # 3 months by default
        data = project(budget_file, view.until)["data"]
        text = headline(view)
        assert_headline_matches(text, data)
        assert "Thu Sep 24 → Thu Dec 24" in text and "3 months" in text
        nxt = data["spare"]["next_payday"]
        assert f"until {nxt['name']} on Fri Dec 25" in text  # what the spare counts to
        assert "Thu Oct 1" in text  # the lowest day
        await pilot.press("right_square_bracket", "right_square_bracket", "right_square_bracket")
        await pilot.press("right_square_bracket")
        assert view.until == date(2031, 9, 24)
        await settle(app, pilot)  # five years are worked out in a thread
        data = project(budget_file, view.until)["data"]
        text = headline(view)
        assert "working it out" not in text
        assert_headline_matches(text, data)
        paid = sorted((d["paid_off_on"], d["name"]) for d in data["debts"] if d["paid_off_on"])
        assert [name for _, name in paid] == ["Credit card", "Car loan"]
        assert "✓ Paid off Credit card Dec 2028 · Car loan Feb 2030" in text


async def test_what_if_numbers_match_the_flags(make_app, budget_file) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        app.session.add_change(SETTLE)
        app.session.add_change(FLIGHT)
        view = await open_forecast(app, pilot)
        await pilot.press("right_square_bracket")  # 6 months
        flags = ("--settle", "Car loan:13000@2026-11-01", "--add-expense", "Flight:650@2026-10-09")
        data = project(budget_file, view.until, *flags)["data"]
        base = project(budget_file, view.until)["data"]
        text = headline(view)
        assert_headline_matches(text, data)
        assert "with the what-if" in text
        moved = cents_of(data["ending_balance"]) - cents_of(base["ending_balance"])
        assert f"{money(moved, sign=True)} vs now" in text
        assert "✓ Car loan paid off Nov 2026" in text
        names = {plain(r)[1] for r in rows_of(view, "#transactions")}
        assert "↳ Flight" in names
        assert "↳ Car loan ◆  ✓ paid off" in names  # the sale clears the loan
        assert "Car loan ◆" in names  # the payments before it are the budget's own


async def test_transactions_are_the_ledger_with_closing_balances(make_app, budget_file) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_forecast(app, pilot)
        data = project(budget_file, view.until, "--ledger", "--daily")["data"]
        ledger = data["ledger"]
        end_of_day = {r["date"]: cents_of(r["balance"]) for r in data["series"]}
        rows = rows_of(view, "#transactions")
        assert len(rows) == len(ledger)
        for i, (row, entry) in enumerate(zip(rows, ledger, strict=True)):
            _, name, amount, after = plain(row)
            assert name.split(" ◆")[0] == entry["name"]
            assert amount == money(cents_of(entry["delta"]), sign=True)
            last = i + 1 == len(ledger) or ledger[i + 1]["date"] != entry["date"]
            want = end_of_day[entry["date"]] if last else cents_of(entry["balance_after"])
            assert after == money(want)  # what the calendar shows for that day
        items = view.query_one("#transactions", RowList).items
        assert None in items  # weeks are set apart by a blank line
        weeks = [day_of(r).isocalendar()[:2] for r in rows]
        assert items.count(None) == len(set(weeks)) - 1


async def test_month_by_month(make_app, budget_file) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_forecast(app, pilot)
        await pilot.press("v")
        assert view.by_month and view.query_one("#months", RowList).has_focus
        assert not view.query_one("#transactions").display
        series = project(budget_file, view.until)["data"]["series"]
        rows = rows_of(view, "#months")
        assert [plain(r)[0] for r in rows] == [
            "Today",
            "Sep 25\u201330",
            "Oct 2026",
            "Nov 2026",
            "Dec 1\u201324",
        ]
        for row, point in zip(rows, series, strict=True):
            _, inc, out, net, bal, spare = (c.strip() for c in plain(row))
            income, expense = cents_of(point["income"]), cents_of(point["expense"])
            assert inc == (money(income, sign=True) if income else "—")
            assert out == (money(-expense) if expense else "—")
            assert net == (
                money(cents_of(point["net"]), sign=True) if point["net"] != "0.00" else "—"
            )
            assert bal == money(cents_of(point["balance"]))
            assert spare == money(cents_of(point["spare"]))
        heads = widget_text(view.query_one("#months-heads"))
        assert heads.split() == ["In", "Out", "Net", "Balance", "Spare"]
        # Enter on a month shows its transactions, from the first one
        await pilot.press("down", "down", "down", "enter")
        assert not view.by_month
        transactions = view.query_one("#transactions", RowList)
        assert transactions.has_focus
        key = transactions.key
        assert isinstance(key, tuple) and key[1] == date(2026, 11, 2)  # Rent, moved to Monday
        await pilot.press("v", "v")
        assert transactions.key == key  # each list keeps its place


# ── Keys ──────────────────────────────────────────────────────────────────────


async def test_horizon_keys(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_forecast(app, pilot)
        picker = view.query_one("#headline", Panel)
        assert view.horizon == 1
        await pilot.press("left_square_bracket")
        assert view.until == date(2026, 10, 24) and "1 month" in headline(view)
        await pilot.press("minus")  # already the shortest: nothing changes
        assert view.horizon == 0
        await pilot.press("plus", "equals_sign", "right_square_bracket")
        assert view.until == date(2027, 9, 24) and "1 year" in headline(view)
        await pilot.press("right_square_bracket", "right_square_bracket", "right_square_bracket")
        assert view.horizon == 5 and "5 years" in headline(view)  # at once, numbers to follow
        await settle(app, pilot)
        assert "Wed Sep 24, 2031" in headline(view) and "working it out" not in headline(view)
        assert "5y" in border(picker)


async def test_a_custom_end_date(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_forecast(app, pilot)
        await pilot.press("g")
        assert isinstance(app.screen, PromptScreen)
        field = app.screen.query_one(Input)
        await pilot.press(*"-1d", "enter")
        assert isinstance(app.screen, PromptScreen)  # refused
        assert "Pick a day after today" in screen_text(app)
        field.value = "mar 14 2027"
        await pilot.pause()
        assert "Sun Mar 14 · in 5 months" in screen_text(app)  # the live preview
        await pilot.press("enter")
        assert isinstance(app.screen, MainScreen)
        assert view.until == date(2027, 3, 14) and view.horizon is None
        text = headline(view)
        assert "Thu Sep 24 → Sun Mar 14" in text
        assert "Mar 14" in border(view.query_one("#headline"))
        await pilot.press("left_square_bracket")  # the nearest shorter preset
        assert view.until == date(2026, 12, 24) and view.horizon == 1
        view.set_until(date(2027, 3, 14))
        await pilot.press("right_square_bracket")  # the nearest longer one
        assert view.until == date(2027, 3, 24) and view.horizon == 2
        view.set_until(date(2027, 9, 24))  # a preset's own end picks the preset
        assert view.horizon == 3 and view.custom is None


def test_reading_an_end_date() -> None:
    assert read_until("+18m", TODAY) == date(2028, 3, 24)
    assert read_until("dec 12", TODAY) == date(2026, 12, 12)
    for text, problem in [
        ("", "A day after today"),
        ("soon", "Couldn't read 'soon' as a date"),
        ("today", "Pick a day after today"),
        ("+11y", "Pick a day within ten years"),
    ]:
        try:
            read_until(text, TODAY)
        except CashError as exc:
            assert problem in str(exc)
        else:
            raise AssertionError(text)


async def test_the_cursor_moves_the_chart_marker(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_forecast(app, pilot)
        chart = view.query_one("#chart", Chart)
        rows = view.query_one("#transactions", RowList)
        assert rows.has_focus
        assert chart.marker == date(2026, 9, 25)  # the first item: Credit card
        subtitle = border(view.query_one("#chart-panel"))
        assert "Fri Sep 25" in subtitle and "ends the day at $3,895.00" in subtitle
        await pilot.press("down")  # past the blank line between weeks
        assert chart.marker == date(2026, 10, 1)
        assert "ends the day at $1,595.00" in border(view.query_one("#chart-panel"))
        assert "▲ Oct 1" in widget_text(chart)
        await pilot.press("v")
        assert chart.marker == TODAY  # the month rows start with today
        await pilot.press("down")
        assert chart.marker == date(2026, 9, 30)


async def test_enter_opens_the_flow_card(make_app) -> None:
    app = make_app()
    opened: list[int] = []
    async with app.run_test(size=SIZE) as pilot:
        app.session.add_change(FLIGHT)
        view = await open_forecast(app, pilot)
        app.open_flow_card = opened.append  # type: ignore[method-assign]
        await pilot.press("down", "enter")  # Rent
        rent = next(int(f.id) for f in app.session.flows() if f.name == "Rent")
        assert opened == [rent]
        rows = view.query_one("#transactions", RowList)
        flight = next(r.key for r in rows_of(view, "#transactions") if "Flight" in plain(r)[1])
        rows.select_key(flight)
        await pilot.press("enter")
        assert opened == [rent]
        assert app.toasts[-1] == "That's part of the what-if, not your budget."


async def test_add_and_help(make_app) -> None:
    app = make_app()
    added: list[bool] = []
    async with app.run_test(size=SIZE) as pilot:
        await open_forecast(app, pilot)
        app.add_flow = lambda **kw: added.append(True)  # type: ignore[method-assign]
        await pilot.press("a")
        assert added == [True]
        await pilot.press("question_mark")
        assert isinstance(app.screen, HelpScreen)
        text = screen_text(app)
        for words in ("shorter horizon", "longer horizon", "month by month", "until a date"):
            assert words in text
        assert "[  -" in text and "]  +  =" in text


# ── States ────────────────────────────────────────────────────────────────────


async def test_empty_budget(make_app, empty_file: Path) -> None:
    app = make_app(empty_file)
    async with app.run_test(size=SIZE) as pilot:
        view = await open_forecast(app, pilot)
        assert view.has_class("-empty")
        text = screen_text(app)
        assert "Nothing to forecast yet." in text and "add your paycheck" in text
        await pilot.press("right_square_bracket", "m")  # nothing to move; nothing breaks
        assert view.has_class("-empty")
        paycheck = FlowDraft("Paycheck", Kind.INCOME, 265000, "FREQ=WEEKLY;INTERVAL=2", TODAY)
        app.apply(lambda: app.session.add_flow(paycheck))
        await pilot.pause()
        assert not view.has_class("-empty")
        rows = view.query_one("#transactions", RowList)
        assert rows.has_focus and plain(rows_of(view, "#transactions")[0])[1] == "Paycheck"
    conn = sqlite3.connect(empty_file)
    try:
        assert conn.execute("SELECT name FROM flow").fetchall() == [("Paycheck",)]
    finally:
        conn.close()


async def test_without_a_balance(make_app, tmp_path: Path) -> None:
    path = build_budget(tmp_path / "nb.sqlite", with_balance=False)
    app = make_app(path)
    async with app.run_test(size=SIZE) as pilot:
        view = await open_forecast(app, pilot)
        envelope = project(path, view.until)
        assert envelope["warnings"] == ["no balance recorded: this projection starts from 0"]
        assert_headline_matches(headline(view), envelope["data"])
        # said once, beside Starts at (as much of it as fits), not again as a warning
        assert "balance" not in widget_text(view.query_one("#warnings")).lower()
        assert f"Starts at         $0.00   no balance recorded {DOT} b" in headline(view)


async def test_small_screen(make_app) -> None:
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        view = await open_forecast(app, pilot)
        assert view.has_class("-narrow") and view.has_class("-short")
        text = headline(view)
        assert "Starts at" in text and "Debt left" in text
        assert "$4,070.00" in text and "est. from Sep 22" in text
        assert "principal" not in text  # no room for notes beside the totals
        assert view.query_one("#chart", Chart).region.height >= 3
        assert view.query_one("#transactions", RowList).region.height >= 3
        assert "…" not in text.split("Money in")[0]  # the balances' notes fit
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_forecast(app, pilot)
        assert "$1,779.88 principal" in headline(view)
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert "principal" not in headline(view)  # the headline follows the width


# ── A change made in the app ──────────────────────────────────────────────────


async def test_recording_a_balance_moves_the_forecast(make_app, budget_file) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_forecast(app, pilot)
        assert "$4,070.00" in headline(view)
        await pilot.press("b", *"5000", "enter", "enter")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
        assert app.screen.switcher.current == "forecast"
        data = project(budget_file, view.until)["data"]
        assert data["starting_balance"] == "5000.00"
        text = headline(view)
        assert "$5,000.00" in text and "recorded today" in text
        assert_headline_matches(text, data)
    conn = sqlite3.connect(budget_file)
    conn.row_factory = sqlite3.Row
    try:
        latest = balances.latest(conn)
    finally:
        conn.close()
    assert latest is not None and (latest.as_of, latest.amount_cents) == (TODAY, 500000)
