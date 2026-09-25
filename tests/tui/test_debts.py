"""The Debts view and its dialogs (record an event, the payoff plan), driven through Pilot."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from bdbd import agent, ask
from bdbd.core import repo
from bdbd.core.dates import add_months
from bdbd.core.models import EventType
from bdbd.core.queries.debt_schedule import debt_schedule
from bdbd.core.queries.plan import plan as plan_query
from bdbd.tui.app import BdbdApp, MainScreen
from bdbd.tui.debt_forms import EventForm, PlanScreen
from bdbd.tui.flow_form import FlowForm
from bdbd.tui.forms import ConfirmScreen, TextField
from bdbd.tui.modals import HelpScreen
from bdbd.tui.scenario import Change
from bdbd.tui.views.debts import DebtsView, HeadedList
from bdbd.tui.widgets import Row
from bdbd.ui.theme import cents_of, money, pct
from bdbd.words import fmt_date, fmt_month

from .conftest import SIZE, screen_text, widget_text

TODAY = date(2026, 9, 24)


def _view(app: BdbdApp) -> DebtsView:
    return app.screen.query_one(DebtsView)


def _text(app: BdbdApp, selector: str) -> str:
    return widget_text(app.screen.query_one(selector))


def _events(app: BdbdApp, name: str) -> tuple:
    """The events recorded on a debt, straight from the database."""
    debt = repo.resolve_flow(app.session.conn, name).debt
    assert debt is not None
    return debt.events


def _debts_list(app: BdbdApp) -> HeadedList:
    return app.screen.query_one("#debts-list", HeadedList)


async def _settle(app: BdbdApp, pilot) -> None:
    """Let the plan's debounce fire and its worker finish."""
    await pilot.pause(0.4)
    await app.workers.wait_for_complete()
    await pilot.pause()


# ── What it shows ─────────────────────────────────────────────────────────────


async def test_headline_and_list_are_bdbd_debts(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("5")
        want = agent.debts_json(ask.debts(app.session.budget))
        head = _text(app, "#debts-headline")
        assert money(cents_of(want["total_owed"])) in head
        assert f"{money(cents_of(want['monthly_payments']))} a month in payments" in head
        assert money(cents_of(want["interest_remaining"])) in head
        assert fmt_month(date.fromisoformat(want["debt_free_on"])) in head
        lines = _text(app, "#debts-list-panel").splitlines()
        rows = [line for line in lines if any(d["name"] in line for d in want["debts"])]
        assert len(rows) == len(want["debts"])
        for line, d in zip(rows, want["debts"], strict=True):  # soonest paid off first
            assert d["name"] in line
            for cell in (
                money(cents_of(d["balance"])),
                pct(d["annual_rate"]),
                money(cents_of(d["payment"])),
                fmt_month(date.fromisoformat(d["paid_off_on"])),
            ):
                assert cell in line
        assert "Owed" in lines[1] and "Rate" in lines[1] and "Paid off" in lines[1]


async def test_the_selected_debt_is_bdbd_show_and_its_schedule(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("5")
        budget = app.session.budget
        model = budget.model(include_inactive=True)
        for i, row in enumerate(ask.debts(budget)):
            if i:
                await pilot.press("down")
            flow = next(f for f in budget.flows() if f.id == row.key)
            outlook = ask.details(budget, flow).debt  # what `bdbd show NAME` reports
            assert outlook is not None
            panel = _text(app, "#debt-panel")
            assert flow.name in panel
            assert money(cents_of(outlook["balance_at_as_of"])) in panel
            payoff = date.fromisoformat(outlook["payoff_date"])
            assert fmt_date(payoff, TODAY) in panel
            assert f"{outlook['payments_remaining']} payments to go" in panel
            # interest to go from today (`bdbd debts`), so owed + interest = all still to pay
            owed = cents_of(outlook["balance_at_as_of"])
            assert row.interest is not None and money(row.interest) in panel
            assert f"to go, of {money(owed + row.interest)} in all" in panel
            want, _ = debt_schedule(  # `bdbd debt schedule NAME --all`
                model, row.key, as_of=TODAY, until=add_months(TODAY, 600), max_rows=None
            )
            rows = app.screen.query_one("#debt-schedule", HeadedList).items
            assert len(rows) == len(want["rows"])
            for got, w in ((rows[0], want["rows"][0]), (rows[-1], want["rows"][-1])):
                assert isinstance(got, Row)
                assert [str(c) for c in got.cells[:6]] == [
                    str(w["n"]),
                    fmt_date(date.fromisoformat(w["date"]), TODAY),
                    money(cents_of(w["payment"])),
                    money(cents_of(w["interest"])),
                    money(cents_of(w["principal"])),
                    money(cents_of(w["balance"])),
                ]


async def test_the_cursor_moves_the_detail_and_enter_opens_the_schedule(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("5")
        debts = _debts_list(app)
        assert app.screen.focused is debts
        assert "Credit card" in _text(app, "#debt-panel")
        await pilot.press("down")
        assert "Car loan" in _text(app, "#debt-panel")
        await pilot.press("down", "down")  # the cursor stops at the last debt
        assert debts.key == ask.debts(app.session.budget)[-1].key
        await pilot.press("up")
        assert "Car loan" in _text(app, "#debt-panel")
        await pilot.press("enter")
        schedule = app.screen.query_one("#debt-schedule", HeadedList)
        assert app.screen.focused is schedule
        await pilot.press("down", "down")
        assert schedule.key == 3
        await pilot.press("escape")
        assert app.screen.focused is debts
        assert "Car loan" in _text(app, "#debt-panel")  # still the same debt


async def test_small_screens_swap_the_schedule_in(make_app) -> None:
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("5")
        view = _view(app)
        assert view.has_class("-short") and view.has_class("-cramped")
        assert not _text(app, "#schedule-panel")
        assert "enter every payment" in _text(app, "#debt-panel")
        await pilot.press("enter")
        assert view.has_class("-schedule")
        assert "Every payment" in _text(app, "#schedule-panel")
        await pilot.press("escape")
        assert not view.has_class("-schedule")
        assert "Owed today" in _text(app, "#debt-panel")


async def test_no_debts_says_so_and_offers_to_add_one(empty_file, make_app) -> None:
    app = make_app(empty_file)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("5")
        assert _view(app).has_class("-empty")
        text = screen_text(app)
        assert "No debts. Lovely." in text and "add a debt" in text
        await pilot.press("r", "p", "x", "u")  # nothing to act on: nothing happens
        assert isinstance(app.screen, MainScreen)
        await pilot.press("a")
        assert isinstance(app.screen, FlowForm)


async def test_the_what_if_lens_shows_in_the_numbers(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("5", "down")
        app.session.add_change(Change("extra_payment", "Car loan", "2000", "2026-11-01"))
        app.refresh_views()
        await pilot.pause()
        want = agent.debts_json(ask.debts(app.session.budget, app.session.model()))
        head = _text(app, "#debts-headline")
        assert "with the what-if" in head
        assert money(cents_of(want["interest_remaining"])) in head and "vs now" in head
        car = next(d for d in want["debts"] if d["name"] == "Car loan")
        assert fmt_month(date.fromisoformat(car["paid_off_on"])) in _text(app, "#debts-list-panel")
        schedule = _text(app, "#schedule-panel")
        assert "with the what-if" in schedule and "$2,000.00" in schedule and "extra" in schedule


# ── Changing things ───────────────────────────────────────────────────────────


async def test_recording_an_extra_payment_lands_in_the_budget(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("5", "down", "r")  # Car loan
        form = app.screen
        assert isinstance(form, EventForm)
        await pilot.press(*"500")
        assert "Paid off Feb 2030 →" in widget_text(form.query_one("#summary"))
        await pilot.press("ctrl+s")
        assert isinstance(app.screen, MainScreen)
        flow = repo.resolve_flow(app.session.conn, "Car loan")
        assert flow.debt is not None
        [ev] = flow.debt.events
        assert (ev.type, ev.date, ev.amount_cents) == (EventType.EXTRA_PAYMENT, TODAY, 50000)
        assert app.toasts[-1].startswith("Recorded an extra $500.00 on Car loan")
        assert "(was Feb 2030)" in app.toasts[-1]
        events = _text(app, "#debt-panel")
        assert "Extra payment" in events and "$500.00" in events


async def test_recording_a_new_rate_from_a_date(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("5", "down", "down", "r")  # Student loan
        form = app.screen
        assert isinstance(form, EventForm)
        await pilot.press("shift+tab", "right")  # What happened: Rate
        assert form.field("rate").display and not form.field("extra").display
        await pilot.press("tab", *"3.9%", "tab")
        on, notes = form.field("on"), form.field("notes")
        assert isinstance(on, TextField) and isinstance(notes, TextField)
        on.text, notes.text = "nov 1", "refinanced"
        await pilot.press("ctrl+s")
        assert isinstance(app.screen, MainScreen)
        flow = repo.resolve_flow(app.session.conn, "Student loan")
        assert flow.debt is not None
        [ev] = flow.debt.events
        assert (ev.type, ev.rate, ev.date, ev.notes) == (
            EventType.RATE_CHANGE,
            Decimal("0.039"),
            date(2026, 11, 1),
            "refinanced",
        )
        assert "Student loan's rate is 3.9% from Nov 1" in app.toasts[-1]


async def test_an_event_before_the_balance_date_is_refused(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("5", "down", "r")
        form = app.screen
        assert isinstance(form, EventForm)
        await pilot.press(*"100")
        on = form.field("on")
        assert isinstance(on, TextField)
        on.text = "sep 1"  # the Car loan's balance is from Sep 5
        await pilot.pause()
        assert not on.parsed.ok and "before the balance you gave" in str(on.parsed.preview)
        await pilot.press("ctrl+s")
        assert app.screen is form  # refused
        await pilot.press("escape")
        assert not _events(app, "Car loan")


async def test_deleting_an_event_asks_first(budget_file, make_app) -> None:
    from bdbd.core import db

    conn = db.connect(budget_file)
    car = repo.resolve_flow(conn, "Car loan")
    repo.add_event(conn, int(car.id), type=EventType.EXTRA_PAYMENT, date=TODAY, amount_cents=25000)
    conn.commit()
    conn.close()
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("5", "down")
        assert "x deletes it" in _text(app, "#debt-panel")
        await pilot.press("x")
        assert isinstance(app.screen, ConfirmScreen)
        assert "$250.00" in screen_text(app)
        await pilot.press("escape")  # keep it
        assert _events(app, "Car loan")
        await pilot.press("x", "right", "enter")  # Delete
        assert isinstance(app.screen, MainScreen)
        assert not _events(app, "Car loan")
        assert app.toasts[-1].startswith("Deleted the event on Car loan")
        assert "Nothing recorded yet" in _text(app, "#debt-panel")


async def test_stop_tracking_keeps_the_payment(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("5")  # Credit card
        await pilot.press("u")
        assert isinstance(app.screen, ConfirmScreen)
        assert "stays as a plain expense" in screen_text(app)
        await pilot.press("right", "enter")
        flow = repo.resolve_flow(app.session.conn, "Credit card")
        assert flow.debt is None and flow.amount_cents == 15000
        assert "Credit card is a plain expense again" in app.toasts[-1]
        assert "Credit card" not in _text(app, "#debts-list-panel")
        assert "Car loan" in _text(app, "#debt-panel")  # the cursor moved on


async def test_a_adds_a_debt_and_e_edits_its_terms(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("5", "a")
        form = app.screen
        assert isinstance(form, FlowForm) and form.initial["loan"]
        await pilot.press("escape")
        await pilot.press("down", "e")
        form = app.screen
        assert isinstance(form, FlowForm)
        assert form.flow is not None and form.flow.name == "Car loan"


async def test_help_lists_the_views_keys(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("5", "question_mark")
        assert isinstance(app.screen, HelpScreen)
        text = screen_text(app)
        for words in (
            "add a debt",
            "record an event",
            "payoff plan",
            "edit the loan terms",
            "delete the selected event",
            "stop tracking as a debt",
            "every payment",
        ):
            assert words in text


# ── The payoff plan ───────────────────────────────────────────────────────────


def _plan(app: BdbdApp, extra: int, strategy: str = "avalanche", start: date | None = None):
    """`bdbd plan --extra … --strategy … [--from …]` on the app's budget."""
    s = app.session
    budget = s.budget
    begin = budget.start(TODAY)
    model = budget.model()
    return plan_query(
        model,
        as_of=TODAY,
        until=add_months(TODAY, 600),
        extra_cents=extra,
        start=start,
        strategy=strategy,
        starting_balance_cents=begin.cents,
        weekly_spend_cents=budget.weekly_for(model),
    )[0]


async def test_the_plan_is_bdbd_plan(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("5", "p")
        plan = app.screen
        assert isinstance(plan, PlanScreen)
        await _settle(app, pilot)
        want = _plan(app, 20000)
        text = screen_text(app)
        free = fmt_month(date.fromisoformat(want["debt_free_on"]))
        assert f"Debt-free {free}" in text and "4 years 7 months sooner" in text
        assert money(cents_of(want["monthly_outlay"]["with_extra"])) in text
        assert money(cents_of(want["interest_saved"])) in text
        low = want["cash_check"]["min_balance_after_start"]
        assert money(cents_of(low["balance"])) in text and "the plan fits your budget" in text
        for step in want["steps"]:
            line = next(ln for ln in text.splitlines() if step["name"] in ln and "%" in ln)
            assert money(cents_of(step["payment_during"])) in line
            assert fmt_month(date.fromisoformat(step["paid_off_on"])) in line
        assert "Credit card → Car loan → Student loan" in text

        # a new amount and the other order, typed: worked out again after a pause
        extra = plan.field("extra")
        assert isinstance(extra, TextField)
        extra.focus_field()
        await pilot.press("backspace", "backspace", "backspace", *"450", "tab", "right")
        await _settle(app, pilot)
        want = _plan(app, 45000, "snowball")
        text = screen_text(app)
        assert f"Debt-free {fmt_month(date.fromisoformat(want['debt_free_on']))}" in text
        assert money(cents_of(want["interest_saved"])) in text


async def test_the_plan_can_start_later_and_says_when_it_cant(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("5", "p")
        plan = app.screen
        assert isinstance(plan, PlanScreen)
        start = plan.field("start")
        assert isinstance(start, TextField)
        start.text = "nov 1"
        await _settle(app, pilot)
        want = _plan(app, 20000, start=date(2026, 11, 1))
        assert money(cents_of(want["interest_saved"])) in screen_text(app)
        start.text = "yesterday"
        await _settle(app, pilot)
        assert not start.parsed.ok
        assert "Fix the fields above" in screen_text(app)


async def test_trying_the_plan_puts_it_in_the_what_if(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("5", "p")
        await _settle(app, pilot)
        plan = app.screen
        assert isinstance(plan, PlanScreen)
        await pilot.press("ctrl+s")
        assert isinstance(app.screen, MainScreen)
        s = app.session
        want = _plan(app, 20000)
        assert s.scenario.extra == want["plan_scenario"]
        assert s.scenario.extra_label == "Put $200 extra a month toward debts, highest rate first"
        assert s.lens_on
        assert app.toasts[-1].startswith("Trying the payoff plan · debt-free Jan 2030")
        assert "WHAT IF" in screen_text(app)
        head = _text(app, "#debts-headline")
        assert "with the what-if" in head and "Jan 2030" in head
        # planning again replaces the plan in the sandbox rather than stacking on it
        await pilot.press("p")
        await _settle(app, pilot)
        assert "with the what-if" not in widget_text(app.screen.query_one("#dialog"))
        assert f"Debt-free {fmt_month(date(2030, 1, 1))}" in screen_text(app)


async def test_the_plan_without_a_balance_skips_the_cash_check(tmp_path) -> None:
    from .conftest import build_budget

    path = build_budget(tmp_path / "nobalance.sqlite", with_balance=False)
    app = BdbdApp(path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("5", "p")
        await _settle(app, pilot)
        text = screen_text(app)
        assert "Lowest balance" in text and "unknown" in text
        assert "record your balance" in text
