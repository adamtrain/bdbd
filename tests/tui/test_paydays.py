"""Paydays (2): what's left of each paycheck after its pay cycle's bills, driven the way a
person would, and checked against `bdbd paydays`."""

from __future__ import annotations

import contextlib
import io
import json
from datetime import date
from pathlib import Path

from bdbd import cli
from bdbd.tui.app import BdbdApp, MainScreen
from bdbd.tui.flow_form import FlowForm
from bdbd.tui.forms import ChoiceField, TextField
from bdbd.tui.modals import FlowCard, PaydaysForm
from bdbd.tui.scenario import Change
from bdbd.tui.views.paydays import PaydaysView
from bdbd.tui.widgets import Panel, Row, RowList
from bdbd.ui.theme import MINUS, cents_of, money

from .conftest import SIZE, screen_text, widget_text


def _json(path: Path, *argv: str) -> dict:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.suppress(SystemExit):
        cli.main(["--db", str(path), *argv])
    envelope = json.loads(out.getvalue())
    assert envelope["ok"], envelope
    return envelope["data"]


async def _open(app: BdbdApp, pilot) -> PaydaysView:
    await pilot.pause()
    await pilot.press("2")
    await pilot.pause()
    return app.screen.query_one(PaydaysView)


def _headline(view: PaydaysView) -> str:
    return " ".join(widget_text(view.query_one("#paydays-headline")).split())


def _rows(view: PaydaysView) -> list[Row]:
    return [r for r in view.query_one("#cycles", RowList).items if isinstance(r, Row)]


def _plain(cell: object) -> str:
    return str(getattr(cell, "plain", cell)).strip()


async def test_this_pay_cycle_and_every_one_ahead(make_app, budget_file) -> None:
    data = _json(budget_file, "paydays", "--months", "12")
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await _open(app, pilot)
        text = _headline(view)
        assert "This pay cycle · Fri Sep 18 → Thu Oct 1" in text
        assert "Free to spend or save $0.00 after bills and everyday spending" in text
        assert "Paycheck +$2,650.00 Paycheck · Fri Sep 18" in text
        assert f"Bills {MINUS}$2,300.00 2 bills · Rent the biggest" in text
        assert f"Everyday {MINUS}$350.00 14 days at $175/week" in text
        assert "Before everyday $350.00 not counting everyday spending" in text
        cycles = view.query_one("#cycles", RowList)
        assert cycles.has_focus
        rows = _rows(view)  # every cycle `bdbd paydays --months 12` has, and what's left of it
        assert [r.key for r in rows] == [date.fromisoformat(c["start"]) for c in data["cycles"]]
        for row, c in zip(rows, data["cycles"], strict=True):
            assert _plain(row.cells[-1]) == money(cents_of(c["left"]))
        items = widget_text(view.query_one("#cycle-panel"))
        assert "Credit card" in items and "Everyday, 14 days" in items
        assert "$2,500.00" in items  # what's left of the paycheck after the card
        await pilot.press("down")
        text = _headline(view)
        assert "Pay cycle · Fri Oct 2 → Thu Oct 15 · in 8 days" in text
        assert "Free to spend or save $1,606.24" in text


async def test_v_and_a_click_leave_everyday_spending_out(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await _open(app, pilot)
        await pilot.press("v")
        text = _headline(view)
        assert not view.with_everyday
        assert "Free to spend or save $350.00 after bills" in text
        assert f"Everyday {MINUS}$350.00 14 days at $175/week, not counted" in text
        assert "After everyday $0.00" in text
        assert _plain(_rows(view)[0].cells[-1]) == "$350.00"
        await pilot.press("v")
        assert view.with_everyday and "$0.00 after bills and everyday" in _headline(view)
        y = view.query_one("#paydays-headline", Panel).region.bottom - 1
        x = screen_text(app).splitlines()[y].index(" without ")
        await pilot.click(offset=(x + 2, y))  # the picker in the headline's border
        await pilot.pause()
        assert not view.with_everyday


async def test_enter_shows_a_cycles_items_and_opens_their_cards(make_app) -> None:
    app = make_app()
    opened: list[int] = []
    async with app.run_test(size=SIZE) as pilot:
        view = await _open(app, pilot)
        app.open_flow_card = opened.append  # type: ignore[method-assign]
        await pilot.press("enter")
        items = view.query_one("#cycle-items", RowList)
        assert items.has_focus
        await pilot.press("enter")  # the paycheck
        paycheck = next(f.id for f in app.session.flows() if f.name == "Paycheck")
        assert opened == [paycheck]
        await pilot.press("escape")
        assert view.query_one("#cycles", RowList).has_focus


async def test_p_chooses_the_paydays(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await _open(app, pilot)
        await pilot.press("p")
        assert isinstance(app.screen, PaydaysForm)
        form = app.screen
        switch = form.field(f"payday-{_id(app, 'Paycheck')}")
        assert isinstance(switch, ChoiceField) and switch.value is True
        switch.select(False)
        await pilot.pause()
        assert "No paydays" in screen_text(app)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen) and view.has_class("-empty")
        assert "Which income is your paycheck?" in screen_text(app)
        assert app.toasts[-1] == "No paydays now, so no pay cycles"
        await pilot.press("p")  # from the empty state: it has focus, so p works there
        form = app.screen
        assert isinstance(form, PaydaysForm)
        switch = form.field(f"payday-{_id(app, 'Paycheck')}")
        assert isinstance(switch, ChoiceField)
        switch.select(True)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not view.has_class("-empty") and "This pay cycle" in _headline(view)
        assert app.toasts[-1] == "Paydays: Paycheck · each one starts a pay cycle"


def _id(app: BdbdApp, name: str) -> int:
    return int(next(f for f in app.session.flows() if f.name == name).id)


async def test_the_form_makes_the_first_regular_income_the_payday(make_app, empty_file) -> None:
    app = make_app(empty_file)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.add_flow(income=True)
        await pilot.pause()
        form = app.screen
        assert isinstance(form, FlowForm)
        payday = form.field("payday")
        assert payday.display and payday.value is False  # no schedule yet
        for key, text in (("name", "Vanta"), ("amount", "3000"), ("when", "every 2 weeks on fri")):
            field = form.field(key)
            assert isinstance(field, TextField)
            field.input.value = text
        await pilot.pause()
        assert payday.value is True  # a regular income, and the budget has no payday
        when = form.field("when")
        assert isinstance(when, TextField)
        when.input.value = "once on oct 30"
        await pilot.pause()
        assert payday.value is False  # a one-off isn't a paycheck
        when.input.value = "every 2 weeks on fri"
        await pilot.pause()
        assert isinstance(payday, ChoiceField)
        payday.select(False)  # the person says no: that sticks
        await pilot.pause()
        when.input.value = "monthly on the 1st"
        await pilot.pause()
        assert payday.value is False
        payday.select(True)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
        assert next(f for f in app.session.flows() if f.name == "Vanta").payday


async def test_an_expense_has_no_payday_and_the_card_says_payday(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.edit_flow(_id(app, "Rent"))
        await pilot.pause()
        assert isinstance(app.screen, FlowForm) and not app.screen.field("payday").display
        await pilot.press("escape")
        app.open_flow_card(_id(app, "Paycheck"))
        await pilot.pause()
        assert isinstance(app.screen, FlowCard)
        card = " ".join(widget_text(app.screen.query_one("#dialog")).split())
        assert "Money in · payday" in card and "each date starts a pay cycle" in card


async def test_the_what_if_changes_whats_left(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.session.add_change(Change("stop", "Rent", "", "2026-10-15"))
        app.refresh_views()
        view = await _open(app, pilot)
        cycles = view.query_one("#cycles", RowList)
        rows = _rows(view)
        oct30 = next(i for i, r in enumerate(rows) if r.key == date(2026, 10, 30))
        assert _plain(rows[oct30].cells[-1]) == "$1,734.24"  # no November rent
        for _ in range(oct30):
            await pilot.press("down")
        assert cycles.key == date(2026, 10, 30)
        text = _headline(view)
        assert "with the what-if" in text and "+$2,150.00 vs now" in text


async def test_an_empty_budget_says_what_it_will_show(make_app, empty_file) -> None:
    app = make_app(empty_file)
    async with app.run_test(size=SIZE) as pilot:
        view = await _open(app, pilot)
        assert view.has_class("-empty")
        text = screen_text(app)
        assert "Your paychecks show up here." in text and "add your paycheck" in text
