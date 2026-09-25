"""The What if view and its change form, driven the way a person would.

Every number is checked against the JSON commands for the same question: `bdbd compare`
for the compare mode and `bdbd earliest --floor` for a change dated '?'.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from textual.widgets import Input

from bdbd import cli
from bdbd.tui.app import Banner, BdbdApp, MainScreen
from bdbd.tui.change_form import ChangeForm, resolve
from bdbd.tui.forms import ConfirmScreen, PromptScreen, TextField
from bdbd.tui.modals import HelpScreen
from bdbd.tui.scenario import Change
from bdbd.tui.views.whatif import WhatIfView
from bdbd.tui.widgets import Chart, KeyValues, Row, RowList
from bdbd.ui.theme import cents_of, money

from .conftest import SIZE, screen_text, widget_text

TODAY = date(2026, 9, 24)
SETTLE = Change("settle", "Car loan", "13000", "2026-11-01")
STOP_GYM = Change("stop", "Gym", "", "2026-10-31")
FLIGHT = Change("add_expense", "Flight", "2500", "?")


def run_cli(path: Path, *args: str) -> dict:
    """What a JSON command prints (the real command, in-process)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):  # the app under test captures stdout otherwise
        try:
            cli.main(["--db", str(path), *args])
        except SystemExit as exc:
            assert not exc.code
    envelope = json.loads(out.getvalue())
    assert envelope["ok"], envelope
    return envelope


def flags(*changes: Change) -> list[str]:
    """The command-line flags for these changes."""
    out: list[str] = []
    for c in changes:
        out += [f"--{c.kind.replace('_', '-')}", c.flag()]
    return out


def without_opening(data: dict) -> dict:
    """The earliest command's data less the opening balance block it adds around the query."""
    return {k: v for k, v in data.items() if k != "opening"}


def flow_rows(path: Path) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT * FROM flow ORDER BY id").fetchall()
    finally:
        conn.close()


async def open_whatif(app: BdbdApp, pilot, *changes: Change) -> WhatIfView:
    await pilot.pause()
    for c in changes:
        app.session.add_change(c)
    await pilot.press("7")
    await pilot.pause()
    assert isinstance(app.screen, MainScreen) and app.screen.switcher.current == "whatif"
    return app.screen.query_one(WhatIfView)


@pytest.fixture
def two_months(monkeypatch):
    """Earliest searches look two months ahead instead of twelve (fast tests)."""
    monkeypatch.setattr(WhatIfView, "MONTHS", 2)


def text_of(view: WhatIfView, selector: str) -> str:
    return widget_text(view.query_one(selector))


# ── The empty sandbox ─────────────────────────────────────────────────────────


async def test_the_empty_sandbox_explains_the_idea(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_whatif(app, pilot)
        assert view.has_class("-empty")
        text = screen_text(app)
        assert "Try something without changing anything." in text
        assert "Sell the car on Nov 1 for $13,000" in text
        assert "$650 flight" in text
        assert "Nothing here is saved" in text
        await pilot.press("a")  # the global a asks the view what to add
        await pilot.pause()
        assert isinstance(app.screen, ChangeForm)


async def test_w_with_nothing_to_try_opens_the_change_form(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()
        assert isinstance(app.screen, ChangeForm)
        assert app.screen_stack[-2].query_one("#views").current == "whatif"


# ── Adding a change through the form ──────────────────────────────────────────


async def test_a_change_added_through_the_form_is_tried_but_never_saved(
    make_app, budget_file
) -> None:
    before = flow_rows(budget_file)
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_whatif(app, pilot)
        await pilot.press("a")
        await pilot.pause()
        form = app.screen
        assert isinstance(form, ChangeForm)
        # About: One-offs (the default), Try: Money out, then only what a one-off needs.
        assert form.kind == "add_expense"
        assert not form.field("kind-debts").display and form.field("kind-oneoffs").display
        await pilot.press("down", "down", *"Flight", "down", *"650", "down", *"oct 9")
        await pilot.pause()
        assert "↳ Flight costs $650 on Fri Oct 9" in screen_text(app)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
        assert app.session.scenario.changes == [
            Change("add_expense", "Flight", "650.00", "2026-10-09")
        ]
        assert app.toasts[-1].startswith("Trying: Flight costs $650 on Fri Oct 9")
        rows = text_of(view, "#changes")
        assert "Flight costs $650 on Oct 9" in rows
        assert view.mode == "compare"
        assert "Compared with your budget as it is" in text_of(view, "#headline")
        assert app.session.lens_on
        assert "WHAT IF" in widget_text(app.screen.query_one(Banner))
    assert flow_rows(budget_file) == before  # nothing reached the budget file


async def test_the_form_asks_only_for_what_the_kind_needs(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await open_whatif(app, pilot)
        await pilot.press("a")
        await pilot.pause()
        form = app.screen
        assert isinstance(form, ChangeForm)
        await pilot.press("left")  # About: Debts
        assert form.kind == "payoff"
        assert form.field("target").label == "Debt"
        assert not form.field("amount").display  # paying off takes no amount
        await pilot.press("down", "right")  # Try: Sell
        assert form.kind == "settle"
        assert form.field("amount").display and form.field("amount").label == "Sale proceeds"
        for _ in range(3):
            await pilot.press("right")  # Try: Rate
        assert form.kind == "rate_change"
        assert form.field("amount").label == "New rate"
        await pilot.press("up", "right", "right", "right", "right")  # About: Everyday
        assert form.kind == "everyday"
        assert not form.field("target").display and not form.field("when").display
        assert form.field("amount").label == "Per week"
        await pilot.press("left", "down")  # About: Tags, then Try: Stop / Leave out
        assert form.kind == "stop_tag"
        assert form.field("target").label == "Tag"
        assert form.field("when").label == "After"


async def test_the_form_completes_names_and_refuses_what_the_core_would(
    make_app, two_months
) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await open_whatif(app, pilot)
        await pilot.press("a")
        await pilot.pause()
        form = app.screen
        assert isinstance(form, ChangeForm)
        await pilot.press("left", "down", "right", "down")  # Debts · Sell · the Debt box
        target = form.field("target")
        assert isinstance(target, TextField)
        target.text = "Rent"
        await pilot.pause()
        assert not target.parsed.ok
        assert "Rent isn't a debt; try" in str(target.parsed.preview)
        target.text = "car"  # the one debt it starts
        await pilot.pause()
        assert target.parsed.ok and target.parsed.value == "Car loan"
        assert "Car loan · $14,907.56 owed today at 6.49%" in str(target.parsed.preview)
        amount, when = form.field("amount"), form.field("when")
        assert isinstance(amount, TextField) and isinstance(when, TextField)
        amount.text, when.text = "lots", "2026-09-01"
        await pilot.pause()
        assert not amount.parsed.ok
        assert not when.parsed.ok and "has passed" in str(when.parsed.preview)
        await pilot.press("ctrl+s")  # refused: the bad fields show, nothing is added
        await pilot.pause()
        assert isinstance(app.screen, ChangeForm)
        assert not app.session.scenario.changes
        assert amount.has_class("-touched") and when.has_class("-touched")  # shown amber now
        amount.text, when.text = "13,000", "?"
        await pilot.pause()
        assert "let bdbd find the earliest date" in str(when.parsed.preview)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.session.scenario.changes == [Change("settle", "Car loan", "13000.00", "?")]
        await settle(app, pilot)


async def test_a_target_name_completes_inline(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await open_whatif(app, pilot)
        await pilot.press("a")
        await pilot.pause()
        form = app.screen
        assert isinstance(form, ChangeForm)
        await pilot.press("right", "down", "right", "down")  # Flows · Stop · the Flow box
        await pilot.press(*"stre")
        await pilot.pause()
        box = form.field("target").query_one(Input)
        assert box._suggestion == "Streaming"
        await pilot.press("right")  # accept the completion
        assert box.value == "Streaming"
    assert resolve("car l", ["Car insurance", "Car loan"]) == ("Car loan", [])
    assert resolve("car", ["Car insurance", "Car loan"]) == (None, ["Car insurance", "Car loan"])


# ── Compare ───────────────────────────────────────────────────────────────────


async def test_compare_says_what_bdbd_compare_says(make_app, budget_file) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_whatif(app, pilot, SETTLE, STOP_GYM)
        assert view.mode == "compare"
        data = run_cli(budget_file, "compare", *flags(SETTLE, STOP_GYM))["data"]
        be = data["breakeven"]
        assert be["status"] == "reached" and be["date"] == "2027-02-05"
        headline = text_of(view, "#headline")
        assert "CATCHES UP" in headline and "Catches up on Fri Feb 5" in headline
        numbers = text_of(view, "#numbers")
        assert money(cents_of(data["difference"]["ending"]), sign=True) in numbers
        assert money(cents_of(be["max_shortfall"]["amount"]), sign=True) in numbers
        assert money(cents_of(data["b"]["ending_balance"])) in numbers
        assert f"vs {money(cents_of(data['a']['ending_balance']))} as it is" in numbers
        assert money(cents_of(data["b"]["min_balance"]["balance"])) in numbers
        # every month, as the command gives them
        months = view.query_one("#months", RowList)
        series = data["difference"]["series"]
        assert len(months.items) == len(series)
        shown = view._shown
        assert shown is not None and shown.data["difference"]["series"] == series
        for row, r in zip(months.items, series, strict=True):
            assert isinstance(row, Row)
            cells = [str(c) for c in row.cells[1:]]
            assert cells[0].strip() == money(cents_of(r["a"]))
            assert cells[1].strip() == money(cents_of(r["b"]))
            assert cells[2].strip() == money(cents_of(r["diff"]), sign=True)
        # the chart is the difference every day
        chart = view.query_one("#chart", Chart)
        assert chart._series[-1][1] == cents_of(data["difference"]["ending"])
        # a longer horizon is `--months 60`
        await pilot.press("right_square_bracket")
        await pilot.pause()
        five = run_cli(budget_file, "compare", "--months", "60", *flags(SETTLE, STOP_GYM))
        ending = money(cents_of(five["data"]["difference"]["ending"]), sign=True)
        assert ending in text_of(view, "#numbers")
        assert "the next 5 years" in text_of(view, "#headline")


async def test_moving_through_the_months_marks_the_chart(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_whatif(app, pilot, SETTLE)
        chart = view.query_one("#chart", Chart)
        assert chart.marker is None
        view.query_one("#months", RowList).focus()
        await pilot.press("down", "down", "down")  # Today, Sep, Oct, Nov
        await pilot.pause()
        assert chart.marker == date(2026, 11, 30)
        panel = widget_text(view.query_one("#chart-panel"))
        assert "▲ Mon Nov 30" in panel and "behind" in panel


async def test_warnings_and_problems_show_under_the_answer(make_app, budget_file) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_whatif(app, pilot, Change("disable_tag", "nosuchtag"))
        assert view.mode == "compare"
        assert "No active flow has the tag nosuchtag" in text_of(view, "#warnings")
        # a change whose flow is deleted afterwards doesn't fit any more: amber, no crash
        app.session.clear_changes()
        app.session.add_change(STOP_GYM)
        gym = next(f for f in app.session.flows() if f.name == "Gym")
        app.apply(lambda: app.session.remove_flow(gym))
        await pilot.pause()
        assert view.mode == "broken"
        assert "doesn't fit your budget" in text_of(view, "#headline")
        assert "no flow 'Gym'" in text_of(view, "#note")
        assert "!" in text_of(view, "#changes")


async def test_keys_switch_remove_and_clear(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_whatif(app, pilot, SETTLE, STOP_GYM)
        s = app.session
        await pilot.press("space")  # the first change off
        await pilot.pause()
        assert not s.scenario.changes[0].enabled and s.scenario.changes[1].enabled
        assert app.toasts[-1].startswith("Switched off: Sell Car loan")
        assert "Switched off" in text_of(view, "#card")
        await pilot.press("down", "space")  # both off: nothing to compare
        await pilot.pause()
        assert view.mode == "off" and "switched off" in text_of(view, "#headline")
        await pilot.press("x")  # remove the second
        await pilot.pause()
        assert s.scenario.changes == [Change("settle", "Car loan", "13000", "2026-11-01", False)]
        await pilot.press("space")
        await pilot.pause()
        assert view.mode == "compare"
        await pilot.press("c")  # clear asks first
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("escape")
        assert s.scenario.changes
        await pilot.press("c")
        await pilot.pause()
        await pilot.click("#yes")
        await pilot.pause()
        assert not s.scenario.changes and view.has_class("-empty")


async def test_editing_a_change_keeps_its_switch(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await open_whatif(app, pilot, SETTLE, STOP_GYM)
        await pilot.press("space", "e")  # switch off, then edit the first
        await pilot.pause()
        form = app.screen
        assert isinstance(form, ChangeForm) and form.kind == "settle"
        amount = form.field("amount")
        assert isinstance(amount, TextField) and amount.text == "13,000.00"
        assert form.field("when").text == "Nov 1, 2026"
        amount.text = "14500"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        first = app.session.scenario.changes[0]
        assert first == Change("settle", "Car loan", "14500.00", "2026-11-01", enabled=False)
        assert app.toasts[-1].startswith("Now trying: Sell Car loan for $14,500")


async def test_the_card_says_what_a_change_does(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_whatif(app, pilot, SETTLE)
        card = text_of(view, "#card")
        assert "Car loan · selling it" in card
        assert "Sells for" in card and "$13,000.00" in card and "on Sun Nov 1" in card
        assert "Owed today" in card and "$14,907.56" in card
        row = next(r for r in app.session.debts(baseline=True) if r.name == "Car loan")
        assert row.paid_off_on is not None
        assert "Nov 2026" in card and f"was {row.paid_off_on:%b %Y}" in card


# ── Earliest ──────────────────────────────────────────────────────────────────


async def settle(app: BdbdApp, pilot) -> None:
    """Wait for the earliest search (it runs in a thread of its own) and the redraw after it."""
    view = app.screen.query_one(WhatIfView)
    for _ in range(400):
        await pilot.pause(0.05)
        if not view.searching:
            break
    assert not view.searching, "the earliest search took too long"
    await pilot.pause()


async def test_earliest_says_what_bdbd_earliest_says_and_pins_it(
    make_app, budget_file, two_months
) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_whatif(app, pilot, FLIGHT)
        assert view.mode == "earliest"
        await settle(app, pilot)
        data = run_cli(budget_file, "earliest", "--floor", "175", "--months", "2", *flags(FLIGHT))[
            "data"
        ]
        day = date.fromisoformat(data["date"])
        assert day == date(2026, 10, 2)
        headline = text_of(view, "#headline")
        assert "WORKS FROM" in headline and "Fri Oct 2" in headline and "in 8 days" in headline
        numbers = text_of(view, "#numbers")
        r = data["result"]
        assert money(cents_of(r["headroom"])) in numbers
        assert money(cents_of(r["min_after"]["balance"])) in numbers
        assert money(cents_of(data["floor"])) in numbers
        miss = data["last_infeasible"]["min_after"]
        assert f"would dip to {money(cents_of(miss['balance']))}" in text_of(view, "#earlier")
        # pinned: the other views show the flight on that day, and the banner says so
        assert app.session.scenario.pinned == day and app.session.lens_on
        banner = widget_text(app.screen.query_one(Banner))
        assert "Flight costs $2,500 on Oct 2 (earliest)" in banner
        await pilot.press("u")
        await pilot.pause()
        assert app.session.scenario.pinned is None and not app.session.lens_on
        assert "u pins Fri Oct 2 again" in text_of(view, "#note")
        await pilot.press("u")
        await pilot.pause()
        assert app.session.scenario.pinned == day


async def test_the_floor_and_the_measure_search_again(make_app, budget_file, two_months) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_whatif(app, pilot, FLIGHT)
        await settle(app, pilot)
        await pilot.press("f")
        await pilot.pause()
        assert isinstance(app.screen, PromptScreen)
        box = app.screen.query_one(Input)
        box.value = "1500"
        await pilot.press("enter")
        await settle(app, pilot)
        data = run_cli(budget_file, "earliest", "--floor", "1500", "--months", "2", *flags(FLIGHT))[
            "data"
        ]
        found = view._found
        assert found is not None and found.data == without_opening(data)
        assert data["date"] == "2026-10-16"  # later than with the $175 floor
        assert app.session.scenario.pinned == date(2026, 10, 16)
        assert "floor $1,500" in text_of(view, "#headline")
        await pilot.press("m")
        await settle(app, pilot)
        spare = run_cli(
            budget_file,
            "earliest",
            "--floor",
            "1500",
            "--measure",
            "spare",
            "--months",
            "2",
            *flags(FLIGHT),
        )["data"]
        found = view._found
        assert found is not None and found.data == without_opening(spare)
        if spare["status"] == "found":
            assert app.session.scenario.pinned == date.fromisoformat(spare["date"])
        else:
            assert "NO DATE WORKS" in text_of(view, "#headline")


async def test_no_balance_is_said_under_the_answer(make_app, tmp_path) -> None:
    from .conftest import build_budget

    path = build_budget(tmp_path / "nobalance.sqlite", with_balance=False)
    app = make_app(path)
    async with app.run_test(size=SIZE) as pilot:
        view = await open_whatif(app, pilot, SETTLE)
        note = text_of(view, "#note")
        assert "No balance recorded, so this starts from $0" in note and "b to record it" in note
        assert "balance" not in text_of(view, "#warnings").lower()  # said once


# ── Sizes and help ────────────────────────────────────────────────────────────


async def test_it_fits_in_80_by_24(make_app) -> None:
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        view = await open_whatif(app, pilot, SETTLE)
        assert view.has_class("-narrow") and view.has_class("-short")
        assert not view.query_one("#card").display or not view.query_one("#card").region
        assert view.query_one("#chart-panel").region.height >= 6
        assert not view.query_one("#months-panel").region
        await pilot.press("v")  # the months instead of the chart
        await pilot.pause()
        assert view.query_one("#months-panel").region and not view.query_one("#chart-panel").region
        numbers = widget_text(view.query_one("#numbers", KeyValues))
        assert "Difference" in numbers and "by Sun Sep 24, 2028" in numbers


async def test_help_lists_the_views_keys(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await open_whatif(app, pilot, SETTLE)
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        text = screen_text(app)
        for words in ("add a change", "on/off", "e  enter", "clear all", "longer horizon"):
            assert words in text
        assert "floor" not in text  # the earliest search's keys, only when it's showing
        await pilot.press("escape")
        app.session.clear_changes()
        app.session.add_change(Change("add_expense", "Flight", "2500", "?"))
        app.refresh_views()
        await pilot.pause()
        await pilot.press("question_mark")
        text = screen_text(app)
        assert "floor" in text and "balance or spare" in text and "unpin" in text
