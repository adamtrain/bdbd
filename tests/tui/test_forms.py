"""The flow form, the flow card and settings, driven the way a person would.

Every change goes through the UI and is checked in the database, and where the JSON commands
can make the same change (`bdbd add`, `bdbd edit`, `bdbd debt set`) the result is compared with
theirs on a twin of the budget.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from textual.pilot import Pilot

from bdbd import ask, cli
from bdbd.budget import Budget
from bdbd.core import repo
from bdbd.core.models import Compounding, DayCount, Flow, Kind, Weekend
from bdbd.tui.app import BdbdApp, MainScreen
from bdbd.tui.cards import Card, flow_lines
from bdbd.tui.flow_form import FlowForm, TagSuggester
from bdbd.tui.forms import ChoiceField, ConfirmScreen, PromptScreen, TextField
from bdbd.tui.modals import FlowCard, ImportScreen, SettingsScreen
from bdbd.ui.theme import MINUS

from .conftest import SIZE, build_budget, screen_text, widget_text

# ── Helpers ───────────────────────────────────────────────────────────────────


def _flows(path: Path) -> dict[str, Flow]:
    b = Budget.open(path, tidy=False)
    try:
        return {f.name: f for f in b.flows()}
    finally:
        b.close()


def _cli(path: Path, *argv: str, capsys: pytest.CaptureFixture[str]) -> dict:
    """Run a JSON command on `path` and return its envelope."""
    with contextlib.suppress(SystemExit):
        cli.main(["--db", str(path), *argv])
    env = json.loads(capsys.readouterr().out)
    assert env["ok"], env
    return env


def _form(app: BdbdApp) -> FlowForm:
    assert isinstance(app.screen, FlowForm), app.screen
    return app.screen


def _type(form: FlowForm, key: str, text: str) -> None:
    field = form.field(key)
    assert isinstance(field, TextField)
    field.input.value = text


def _preview(form: FlowForm, key: str) -> str:
    """A field's preview line(s) as plain words (even when it's scrolled out of view)."""
    preview = form.field(key).parsed.preview
    return " ".join(str(preview).split())


def _summary(form: FlowForm) -> str:
    return " ".join(widget_text(form.query_one("#summary")).split())


async def _open_add(
    app: BdbdApp,
    pilot: Pilot,
    *,
    income: bool = False,
    loan: bool = False,
    starts: date | None = None,
) -> FlowForm:
    await pilot.pause()
    app.add_flow(income=income, loan=loan, starts=starts)
    await pilot.pause()
    return _form(app)


def _choose(form: FlowForm, key: str, value: object) -> None:
    """Pick a choice (as left/right or a click would)."""
    field = form.field(key)
    assert isinstance(field, ChoiceField)
    field.select(value)


def _id(app: BdbdApp, name: str) -> int:
    return int(next(f for f in app.session.flows() if f.name == name).id)


# ── Adding ────────────────────────────────────────────────────────────────────


async def test_adding_netflix_by_typing_lands_in_the_budget(make_app, budget_file) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("a")
        form = _form(app)
        assert form.focused is form.field("name").query_one("Input")  # name first, not kind
        await pilot.press(*"Netflix", "tab", *"15.49", "tab", *"monthly on the 12th")
        await pilot.press("tab", "tab", "tab", "tab", *"fu", "right")  # completes 'fun'
        assert form.field("tags").text == "fun"
        assert f"{MINUS}$15.49 each time" in _preview(form, "amount")
        when = _preview(form, "when")
        assert "Monthly on the 12th" in when
        assert "next Mon Oct 12, Thu Nov 12, Sat Dec 12" in when
        assert "Monthly net +$1,621.04 → +$1,605.55" in _summary(form)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
    netflix = _flows(budget_file)["Netflix"]
    assert netflix.kind == Kind.EXPENSE and netflix.amount_cents == 1549
    assert netflix.rrule == "FREQ=MONTHLY;BYMONTHDAY=12"
    assert netflix.dtstart == date(2026, 10, 12) and netflix.tags == ("fun",)
    assert app.toasts[-1] == (
        f"Added Netflix · {MINUS}$15.49 monthly on the 12th · next Mon Oct 12 · "
        "monthly net +$1,621.04 → +$1,605.55"
    )


async def test_the_monthly_net_matches_the_json_after_saving(
    make_app, budget_file, tmp_path, capsys
) -> None:
    """The form's "after" is what `bdbd overview` says once it's saved (no saving to preview)."""
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        form = await _open_add(app, pilot, income=True)
        _type(form, "name", "Side gig")
        _type(form, "amount", "420")
        _type(form, "when", "every 2 weeks on sat")
        await pilot.pause()
        summary = _summary(form)
        assert _flows(budget_file).get("Side gig") is None  # nothing saved yet
        await pilot.press("ctrl+s")
        await pilot.pause()
    env = _cli(budget_file, "overview", capsys=capsys)
    net = int(Decimal(env["data"]["monthly"]["net"]) * 100)
    from bdbd.ui.theme import money

    assert f"→ {money(net, sign=True)}" in summary
    assert money(net, sign=True) in app.toasts[-1]


async def test_add_mirrors_bdbd_add_with_starts_and_ends(make_app, budget_file, capsys) -> None:
    twin = build_budget(budget_file.with_name("twin.sqlite"))
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        form = await _open_add(app, pilot)
        _type(form, "name", "Lessons")
        _type(form, "amount", "60")
        _type(form, "when", "every 2 weeks on tue")
        _type(form, "starts", "oct 6")
        _type(form, "ends", "dec 31")
        _choose(form, "weekend", Weekend.NEXT)
        _type(form, "tags", "Kids, fun")
        _type(form, "notes", "Piano")
        await pilot.pause()
        assert "until Dec 31" in _preview(form, "when")
        await pilot.press("ctrl+s")
        await pilot.pause()
    _cli(
        twin,
        "add",
        "Lessons",
        "60",
        "every 2 weeks on tue",
        "--from",
        "oct 6",
        "--until",
        "dec 31",
        "--ach",
        "--tag",
        "Kids, fun",
        "--notes",
        "Piano",
        capsys=capsys,
    )
    ours, theirs = _flows(budget_file)["Lessons"], _flows(twin)["Lessons"]
    for attr in ("kind", "amount_cents", "rrule", "dtstart", "until", "weekend", "tags", "notes"):
        assert getattr(ours, attr) == getattr(theirs, attr), attr
    assert ours.weekend == Weekend.NEXT


async def test_starts_preset_and_once(make_app, budget_file) -> None:
    """The Calendar's a: Starts is the selected day, and 'once' is enough."""
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        form = await _open_add(app, pilot, starts=date(2026, 12, 12))
        assert form.field("starts").text == "Dec 12, 2026"
        _type(form, "name", "Gift")
        _type(form, "amount", "80")
        _type(form, "when", "once")
        await pilot.pause()
        assert "Once on Dec 12, 2026" in _preview(form, "when")
        await pilot.press("ctrl+s")
        await pilot.pause()
    gift = _flows(budget_file)["Gift"]
    assert gift.rrule is None and gift.dtstart == date(2026, 12, 12)


async def test_problems_show_with_an_example_and_block_saving(make_app, budget_file) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        form = await _open_add(app, pilot)
        _type(form, "name", "rent")  # names are unique regardless of case
        _type(form, "amount", "12x")
        _type(form, "when", "every blue moon")
        _type(form, "starts", "someday")
        await pilot.pause()
        assert "There's already a flow called Rent" in _preview(form, "name")
        assert "try 15.49" in _preview(form, "amount")
        assert "try monthly on the 1st" in _preview(form, "when")
        assert "Try oct 15, fri, +2w or 2026-10-15" in _preview(form, "starts")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is form  # refused
        assert form.focused is form.field("name").query_one("Input")
        assert form.field("amount").has_class("-touched")  # the problems now show in amber
        _type(form, "name", "Rent 2")
        _type(form, "amount", "10")
        _type(form, "when", "monthly on the 3rd")
        _type(form, "starts", "")
        _type(form, "ends", "2026-09-01")  # before it would start
        await pilot.pause()
        assert "before it starts" in _preview(form, "when")
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
    assert "Rent 2" not in _flows(budget_file)


async def test_income_preset_hides_the_loan_switch(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        form = await _open_add(app, pilot, income=True)
        assert "Add an income" in screen_text(app)
        assert not form.field("loan").display
        kind = form.field("kind")
        assert isinstance(kind, ChoiceField)
        kind.focus()
        await pilot.press("left")  # money out
        assert form.field("loan").display
        assert "Add an expense" in screen_text(app)


def test_tag_suggestions_complete_the_last_tag() -> None:
    import asyncio

    tags = TagSuggester(["car", "insurance", "fun", "fitness"])
    get = tags.get_suggestion
    assert asyncio.run(get("f")) == "fun"
    assert asyncio.run(get("car, in")) == "car, insurance"
    assert asyncio.run(get("fun, f")) == "fun, fitness"  # skips one already there
    assert asyncio.run(get("car, ")) is None
    assert asyncio.run(get("zzz")) is None


async def test_adding_in_an_empty_budget(make_app, empty_file) -> None:
    app = make_app(empty_file)
    async with app.run_test(size=SIZE) as pilot:
        form = await _open_add(app, pilot, income=True)
        _type(form, "name", "Paycheck")
        _type(form, "amount", "2650")
        _type(form, "when", "every 2 weeks on fri from sep 18")
        await pilot.pause()
        assert "Monthly net $0.00 → +$5,760.07" in _summary(form)  # as `bdbd overview` says
        await pilot.press("ctrl+s")
        await pilot.pause()
    pay = _flows(empty_file)["Paycheck"]
    assert pay.kind == Kind.INCOME and pay.dtstart == date(2026, 9, 18)


# ── Editing ───────────────────────────────────────────────────────────────────


async def test_an_edit_keeps_the_pay_week_when_when_is_untouched(
    make_app, budget_file, capsys
) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.edit_flow(_id(app, "Paycheck"))
        await pilot.pause()
        form = _form(app)
        assert form.field("when").text == "Every 2 weeks on Fri"
        assert form.field("starts").text == "Sep 18, 2026"
        _type(form, "amount", "2,700.00")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
    pay = _flows(budget_file)["Paycheck"]
    assert pay.rrule == "FREQ=WEEKLY;INTERVAL=2" and pay.dtstart == date(2026, 9, 18)
    assert pay.amount_cents == 270000
    net = _cli(budget_file, "overview", capsys=capsys)["data"]["monthly"]["net"]
    assert net == "1729.72"
    assert app.toasts[-1] == (
        "Updated Paycheck · amount $2,650.00 → $2,700.00 · monthly net +$1,621.04 → +$1,729.72"
    )


async def test_an_edit_mirrors_bdbd_edit(make_app, budget_file, capsys) -> None:
    """New words for When (with the Starts it shows), a cleared end, new tags and notes."""
    twin = build_budget(budget_file.with_name("twin.sqlite"))
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.edit_flow(_id(app, "Rent"))
        await pilot.pause()
        form = _form(app)
        _type(form, "when", "monthly on the 5th")
        _type(form, "tags", "housing, home")
        _type(form, "notes", "Landlord: Sam")
        _choose(form, "weekend", Weekend.NONE)
        await pilot.pause()
        assert "next Mon Oct 5, Thu Nov 5, Sat Dec 5" in _preview(form, "when")
        await pilot.press("ctrl+s")
        await pilot.pause()
    _cli(
        twin,
        "edit",
        "Rent",
        "--when",
        "monthly on the 5th",
        "--from",
        "Oct 1, 2026",
        "--tags",
        "housing, home",
        "--notes",
        "Landlord: Sam",
        "--weekend",
        "none",
        capsys=capsys,
    )
    ours, theirs = _flows(budget_file)["Rent"], _flows(twin)["Rent"]
    for attr in ("amount_cents", "rrule", "dtstart", "until", "weekend", "tags", "notes"):
        assert getattr(ours, attr) == getattr(theirs, attr), attr
    toast = app.toasts[-1]
    assert toast.startswith("Updated Rent · schedule Monthly on the 1st → Monthly on the 5th")
    assert "tags housing → home, housing" in toast and "notes updated" in toast


async def test_clearing_ends_is_no_until(make_app, budget_file) -> None:
    conn = Budget.open(budget_file, tidy=False)
    repo.update_flow(conn.conn, _flow_id(budget_file, "Gym"), until=date(2027, 6, 3))
    conn.close()
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.edit_flow(_id(app, "Gym"))
        await pilot.pause()
        form = _form(app)
        assert form.field("ends").text == "Jun 3, 2027"
        _type(form, "ends", "")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
    gym = _flows(budget_file)["Gym"]
    assert gym.until is None and gym.dtstart == date(2026, 10, 3)
    assert "ends Jun 3, 2027 → never" in app.toasts[-1]


def _flow_id(path: Path, name: str) -> int:
    return int(_flows(path)[name].id)


async def test_saving_an_untouched_edit_changes_nothing(make_app, budget_file) -> None:
    before = _flows(budget_file)["Car loan"]
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.edit_flow(_id(app, "Car loan"))
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
    assert app.toasts[-1] == "Nothing changed in Car loan"
    assert _flows(budget_file)["Car loan"] == before


async def test_pausing_from_the_form(make_app, budget_file) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.edit_flow(_id(app, "Gym"))
        await pilot.pause()
        form = _form(app)
        _choose(form, "active", False)
        await pilot.pause()
        assert "Monthly net +$1,621.04 → +$1,660.04" in _summary(form)
        await pilot.press("ctrl+s")
        await pilot.pause()
    assert not _flows(budget_file)["Gym"].active
    assert "paused" in app.toasts[-1]


# ── Loans ─────────────────────────────────────────────────────────────────────


async def test_adding_a_debt_mirrors_debt_set(make_app, budget_file, capsys) -> None:
    twin = build_budget(budget_file.with_name("twin.sqlite"))
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        form = await _open_add(app, pilot, loan=True)
        assert "Add a debt" in screen_text(app)
        assert form.query_one("#loan").display
        _type(form, "name", "Boat loan")
        _type(form, "amount", "310")
        _type(form, "when", "monthly on the 20th")
        _type(form, "balance", "12,000")
        _type(form, "as_of", "sep 20")
        _type(form, "rate", "7.25%")
        _choose(form, "compounding", Compounding.DAILY)
        await pilot.pause()
        summary = _summary(form)
        assert "Paid off" in summary and "interest to go" in summary
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
    _cli(twin, "add", "Boat loan", "310", "monthly on the 20th", capsys=capsys)
    _cli(
        twin,
        "debt",
        "set",
        "Boat loan",
        "--balance",
        "12000",
        "--as-of",
        "sep 20",
        "--rate",
        "7.25%",
        "--compounding",
        "daily",
        capsys=capsys,
    )
    ours, theirs = _flows(budget_file)["Boat loan"].debt, _flows(twin)["Boat loan"].debt
    assert ours is not None and theirs is not None
    assert ours == replace(theirs, flow_id=ours.flow_id)
    assert ours.capitalize_interest  # daily: unpaid interest joins the balance by default
    shown = _cli(twin, "show", "Boat loan", capsys=capsys)["data"]["debt_outlook"]
    from bdbd.words import fmt_month

    payoff = fmt_month(date.fromisoformat(shown["payoff_date"]))
    assert f"Paid off {payoff}" in summary
    assert f"{shown['payments_remaining']} payments" in summary
    assert f"paid off {payoff}" in app.toasts[-1]


async def test_simple_interest_cannot_capitalize(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        form = await _open_add(app, pilot, loan=True)
        await pilot.click("#more-toggle")
        await pilot.pause()
        cap = form.field("capitalize")
        assert cap.display and "Usual: simple interest never joins" in _preview(form, "capitalize")
        _choose(form, "capitalize", True)
        await pilot.pause()
        assert "Simple interest never capitalizes" in _preview(form, "capitalize")
        _choose(form, "compounding", Compounding.MONTHLY)  # fine, and a posting day shows
        await pilot.pause()
        assert cap.parsed.ok and form.field("posting_day").display


async def test_edit_loan_terms_keeps_an_exact_rate(make_app, budget_file) -> None:
    """d on a card opens the loan section; '6.49%' is prefilled exactly, and a change saves."""
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.edit_flow(_id(app, "Car loan"), loan=True)
        await pilot.pause()
        form = _form(app)
        await pilot.pause()
        assert form.focused is form.field("balance").query_one("Input")
        assert form.field("rate").text == "6.49%"
        _type(form, "rate", "5.99%")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
    debt = _flows(budget_file)["Car loan"].debt
    assert debt is not None and debt.annual_rate == Decimal("0.0599")
    assert debt.balance_cents == 1486000 and debt.posting_day == 5
    assert debt.compounding == Compounding.SIMPLE and not debt.capitalize_interest
    assert "rate 6.49% → 5.99%" in app.toasts[-1] and "paid off Feb 2030 → " in app.toasts[-1]


async def test_turning_the_loan_off_asks_first(make_app, budget_file) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.edit_flow(_id(app, "Credit card"))
        await pilot.pause()
        form = _form(app)
        _choose(form, "loan", False)
        await pilot.pause()
        assert not form.query_one("#loan").display
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert "Stop tracking Credit card as a debt?" in screen_text(app)
        await pilot.press("escape")  # keep
        await pilot.pause()
        assert app.screen is form
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.click("#yes")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
    card = _flows(budget_file)["Credit card"]
    assert card.debt is None and card.amount_cents == 15000
    assert "no longer a debt" in app.toasts[-1]


# ── The flow card ─────────────────────────────────────────────────────────────


async def test_the_card_shows_what_bdbd_show_says(make_app, budget_file, capsys) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.open_flow_card(_id(app, "Car loan"))
        await pilot.pause()
        assert isinstance(app.screen, FlowCard)
        title = app.screen.query_one("#dialog").border_title
        text = " ".join(widget_text(app.screen.query_one(Card)).split())
    shown = _cli(budget_file, "show", "Car loan", capsys=capsys)["data"]
    debts = _cli(budget_file, "debts", capsys=capsys)["data"]["debts"]
    assert "Money out · a debt ◆" in text and "Car loan" in str(title)
    assert f"{MINUS}$412.37" in text and "weekend dates move to Monday" in text
    for day in ("Mon Oct 5", "Thu Nov 5", "Mon Dec 7", "Tue Jan 5", "Fri Feb 5", "Fri Mar 5"):
        assert day in text  # shown["upcoming"], after the weekend moves
    assert [date.fromisoformat(d).day for d in shown["upcoming"]] == [5, 5, 7, 5, 5, 5]
    assert f"Per year {MINUS}$4,948.44" in text  # 12 x shown["monthly"] 412.37
    outlook = shown["debt_outlook"]
    assert outlook["balance_at_as_of"] == "14907.56" and "$14,907.56" in text
    assert "simple interest, accrued daily never compounds" in text
    assert outlook["payoff_date"] == "2030-02-05" and "Feb 2030" in text
    assert outlook["payments_remaining"] == 41 and "Payments to go 41" in text
    # interest to go is measured from today, like `bdbd debts`, so owed + interest = in all
    interest = next(d for d in debts if d["name"] == "Car loan")["interest_remaining"]
    assert interest == "1667.92" and "Interest to go $1,667.92" in text
    assert "In all still to pay $16,575.48" in text  # 14,907.56 + 1,667.92


async def test_one_card_serves_the_budget_panel_and_the_flow_card(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        card = Card()
        await app.screen.mount(card)
        await pilot.pause()
        s = app.session
        card.show(flow_lines(s, s.flow(_id(app, "Tax refund"))))
        await pilot.pause()
        text = widget_text(card)
        assert "Money in" in text and "+$1,240.00" in text
        assert "On" in text and "Tue Oct 20" in text and "Per month" not in text
        card.show(flow_lines(s, s.flow(_id(app, "Car registration"))))
        await pilot.pause()
        text = " ".join(widget_text(card).split())
        # `bdbd summary` says monthly 18.33; a yearly flow's year is its amount, said once
        assert f"Per month {MINUS}$18.33" in text and "Per year" not in text
        card.show(flow_lines(s, s.flow(_id(app, "Paycheck"))))
        await pilot.pause()
        text = " ".join(widget_text(card).split())
        assert "Per month +$5,760.07" in text and "Per year +$69,120.83" in text


async def test_card_keys_pause_edit_and_delete(make_app, budget_file) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        gym = _id(app, "Gym")
        app.open_flow_card(gym)
        await pilot.pause()
        card = app.screen
        await pilot.press("space")
        await pilot.pause()
        assert not app.session.flow(gym).active and "paused" in screen_text(app)
        assert "space resume" in screen_text(app)
        await pilot.press("space")
        await pilot.pause()
        assert app.session.flow(gym).active and "paused" not in screen_text(app)
        await pilot.press("e")
        await pilot.pause()
        form = _form(app)
        _type(form, "name", "Gym & pool")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is card and "Gym & pool" in screen_text(app)  # redrawn
        await pilot.press("x")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.click("#yes")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)  # the card closed with its flow
    assert "Gym & pool" not in _flows(budget_file)


async def test_card_d_makes_an_expense_a_debt_and_not_an_income(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.open_flow_card(_id(app, "Paycheck"))
        await pilot.pause()
        card = widget_text(app.screen.query_one("#dialog"))
        assert "loan terms" not in card and "debt" not in card
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, FlowCard)
        await pilot.press("escape")
        app.open_flow_card(_id(app, "Phone"))
        await pilot.pause()
        assert "d make it a debt" in widget_text(app.screen.query_one("#dialog"))
        await pilot.press("d")
        await pilot.pause()
        form = _form(app)
        assert form.query_one("#loan").display and form.field("loan").value is True


async def test_settings_everyday_spending(make_app, budget_file) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("comma")
        assert isinstance(app.screen, SettingsScreen)
        form = app.screen
        assert "$175.00 a week · about $760.94 a month" in screen_text(app)
        assert budget_file.name in screen_text(app)
        field = form.field("weekly")
        assert isinstance(field, TextField)
        field.input.value = "200"
        await pilot.pause()
        text = screen_text(app)
        assert "$200.00 a week · about $869.64 a month" in text
        assert "Monthly net +$1,621.04 → +$1,512.34" in text
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
    b = Budget.open(budget_file, tidy=False)
    assert b.weekly_spend() == 20000 and ask.monthly_net(b) == 151234
    b.close()
    assert app.toasts[-1] == (
        "Everyday spending is $200.00 a week · monthly net +$1,621.04 → +$1,512.34"
    )


async def test_settings_export_then_import_replacing(make_app, budget_file, tmp_path) -> None:
    backup = tmp_path / "backup.json"
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("comma")
        await pilot.click("#export")
        await pilot.pause()
        assert isinstance(app.screen, PromptScreen)
        prompt = app.screen.query_one(TextField)
        assert prompt.text == "~/bdbd-backup-2026-09-24.json"
        prompt.input.value = str(backup)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen) and backup.exists()
        payload = json.loads(backup.read_text())
        assert len(payload["flows"]) == 15 and payload["balances"]
        app.session.remove_flow(
            app.session.flow(_id(app, "Gym"))
        )  # so the import has something to restore
        await pilot.click("#import")
        await pilot.pause()
        assert isinstance(app.screen, ImportScreen)
        imp = app.screen
        file = imp.field("file")
        assert isinstance(file, TextField)
        file.input.value = str(backup)
        await pilot.pause()
        assert "15 flows and 1 balance, backed up Thu Sep 24" in screen_text(app)
        assert imp.field("replace").value is True
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.click("#yes")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
    assert "Gym" in _flows(budget_file)
    assert app.toasts[-1] == "Restored 15 flows from backup.json"


async def test_import_without_replace_refuses_a_full_budget(make_app, tmp_path) -> None:
    backup = tmp_path / "b.json"
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.session.export(backup)
        app.push_screen(ImportScreen())
        await pilot.pause()
        imp = app.screen
        assert isinstance(imp, ImportScreen)
        file = imp.field("file")
        assert isinstance(file, TextField)
        file.input.value = str(tmp_path / "nope.json")
        await pilot.pause()
        assert "There's no file at" in screen_text(app)
        file.input.value = str(backup)
        replace_ = imp.field("replace")
        assert isinstance(replace_, ChoiceField)
        replace_.select(False)
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is imp
        assert "switch Replace on" in screen_text(app)


async def test_loan_terms_under_a_closed_more_still_count(make_app, budget_file) -> None:
    b = Budget.open(budget_file, tidy=False)
    repo.set_debt(
        b.conn,
        _flow_id(budget_file, "Credit card"),
        balance_cents=320000,
        balance_as_of=date(2026, 9, 10),
        annual_rate=Decimal("0.2299"),
        compounding=Compounding.DAILY,
        day_count=DayCount.ACT_360,
    )
    b.close()
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.edit_flow(_id(app, "Credit card"), loan=True)
        await pilot.pause()
        form = _form(app)
        assert form.query_one("#more").has_class("-open")  # it has a term that isn't usual
        await pilot.click("#more-toggle")  # close it: the terms still count
        _type(form, "amount", "175")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
    card = _flows(budget_file)["Credit card"]
    assert card.amount_cents == 17500 and card.debt is not None
    assert card.debt.day_count == DayCount.ACT_360


async def test_a_bad_term_under_a_closed_more_opens_it(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        form = await _open_add(app, pilot, loan=True)
        for key, text in [("name", "Van"), ("amount", "300"), ("when", "monthly")]:
            _type(form, key, text)
        _type(form, "balance", "9000")
        _type(form, "rate", "8%")
        await pilot.click("#more-toggle")
        await pilot.pause()  # opening it scrolls the form to show it
        _type(form, "principal", "lots")
        await pilot.click("#more-toggle")
        await pilot.pause()
        assert not form.query_one("#more").has_class("-open")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen is form and form.query_one("#more").has_class("-open")
        assert form.focused is form.field("principal").query_one("Input")
