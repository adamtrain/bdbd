"""The Session: questions with the what-if applied, changes with toasts, and staying current."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from bdbd import ask
from bdbd.budget import Budget
from bdbd.core import balance as balances
from bdbd.core import repo
from bdbd.core.errors import CashError
from bdbd.core.models import Compounding, EventType, Kind
from bdbd.tui.scenario import Change
from bdbd.tui.session import DebtTerms, FlowDraft, Session
from bdbd.ui.theme import MINUS
from tests import sample

NETFLIX = FlowDraft("Netflix", Kind.EXPENSE, 1549, "FREQ=MONTHLY;BYMONTHDAY=12", date(2026, 10, 12))


@pytest.fixture
def session(budget_file: Path):
    s = Session(budget_file)
    yield s
    s.close()


def _id(s: Session, name: str) -> int:
    return int(next(f.id for f in s.flows() if f.name == name))


# ── Questions ─────────────────────────────────────────────────────────────────


def test_overview_is_the_same_as_ask(session: Session, budget_file: Path) -> None:
    budget = Budget.open(budget_file)
    try:
        want = ask.overview(budget)
    finally:
        budget.close()
    got = session.overview()
    assert got.start == want.start
    assert got.spare == want.spare
    assert got.low_point == want.low_point
    assert (got.monthly_in, got.monthly_bills, got.monthly_everyday) == (
        want.monthly_in,
        want.monthly_bills,
        want.monthly_everyday,
    )
    assert got.debts == want.debts
    assert got.start.cents == 407000
    assert got.spare and got.spare["spare_balance"] == "1595.00"


def test_answers_are_memoized_until_the_version_moves(session: Session) -> None:
    first = session.overview()
    assert session.overview() is first
    session.bump()
    assert session.overview() is not first


def test_the_lens_applies_the_what_if(session: Session) -> None:
    before = session.debts()
    session.add_change(Change("payoff", "Credit card", when="2026-10-01"))
    assert session.lens_on
    after = {r.name: r for r in session.debts()}
    assert after["Credit card"].paid_off_on == date(2026, 10, 1)
    assert session.debts(baseline=True) == before
    session.set_lens(False)
    assert not session.lens_on
    assert session.debts() == before


def test_a_what_if_that_no_longer_fits_turns_itself_off(session: Session) -> None:
    session.add_change(Change("disable", "Gym"))
    assert session.lens_on
    session.remove_flow(session.flow(_id(session, "Gym")))
    assert not session.lens_on
    assert session.scenario_error == "scenario.disable: no flow 'Gym'"
    assert session.overview() is session.overview(baseline=True)


def test_unpinned_question_mark_keeps_the_lens_off_until_pinned(session: Session) -> None:
    session.add_change(Change("add_expense", "Flight", "650", "?"))
    assert session.scenario.unpinned and not session.lens_on
    spec = session.spec(placeholder=True)
    assert spec and spec["add_flows"][0]["on"] == "?"
    session.pin(date(2026, 10, 16))
    assert session.lens_on
    pinned = session.spec()
    assert pinned and pinned["add_flows"][0]["on"] == "2026-10-16"
    session.replace_change(0, Change("add_expense", "Flight", "700", "?"))
    assert session.scenario.pinned is None  # edits unpin


def test_check_change(session: Session) -> None:
    assert session.check_change(Change("stop", "Rent", when="2026-11-01")) is None
    assert session.check_change(Change("payoff", "Rent", when="2026-11-01")) == (
        "scenario.debt_events: flow 'Rent' has no debt record"
    )


def test_calendar_and_window_follow_the_lens(session: Session) -> None:
    oct_days, _ = session.calendar(date(2026, 10, 1))
    session.add_change(Change("add_expense", "Flight", "650", "2026-10-16"))
    days, _ = session.calendar(date(2026, 10, 1))
    names = {name for d in days for name, _, _ in d.items}
    assert "Flight" in names
    assert "Flight" not in {name for d in oct_days for name, _, _ in d.items}
    assert any(e.name == "Flight" for e in session.window(date(2026, 10, 31)).items)


def test_everyday_what_if_changes_weekly(session: Session) -> None:
    assert session.weekly() == 17500
    session.add_change(Change("everyday", amount="100"))
    assert session.weekly() == 10000
    assert session.stored_weekly() == 17500


# ── Changes ───────────────────────────────────────────────────────────────────


def test_add_update_pause_and_remove_a_flow(session: Session) -> None:
    v = session.version
    done = session.add_flow(NETFLIX)
    assert session.version > v
    assert done.message == (
        f"Added Netflix · {MINUS}$15.49 monthly on the 12th · monthly net +$1,621.04 → +$1,605.55"
    )
    fid = int(done.flow.id) if done.flow else 0
    opened = session.flow(fid)
    edited = session.update_flow(opened, replace(FlowDraft.of(opened), amount_cents=1799))
    assert edited.message.startswith(f"Saved Netflix · {MINUS}$17.99 monthly on the 12th")
    paused = session.set_active(session.flow(fid), False)
    assert paused.message == "Paused Netflix · monthly net +$1,603.05 → +$1,621.04"
    assert not session.flow(fid).active
    removed = session.remove_flow(session.flow(fid))
    assert removed.message == "Deleted Netflix · monthly net stays +$1,621.04"
    with pytest.raises(CashError):
        session.flow(fid)


def test_a_bad_draft_changes_nothing(session: Session) -> None:
    n = len(session.flows())
    with pytest.raises(CashError, match="already exists"):
        session.add_flow(replace(NETFLIX, name="Rent"))
    assert len(session.flows()) == n


def test_update_keeps_or_drops_the_loan(session: Session) -> None:
    fid = _id(session, "Car loan")
    opened = session.flow(fid)
    session.update_flow(opened, replace(FlowDraft.of(opened), notes="the blue one"))
    assert session.flow(fid).debt is not None
    opened = session.flow(fid)
    session.update_flow(opened, replace(FlowDraft.of(opened), debt=None))
    assert session.flow(fid).debt is None


def test_debts_and_events(session: Session) -> None:
    fid = _id(session, "Gym")
    terms = DebtTerms(50000, date(2026, 9, 24), Decimal("0.1"), Compounding.DAILY)
    done = session.set_debt(fid, terms)
    assert done.message.startswith("Gym is a debt · $500.00 owed at 10% · paid off ")
    card = _id(session, "Credit card")
    extra = session.add_event(card, EventType.EXTRA_PAYMENT, date(2026, 10, 1), amount_cents=100000)
    assert extra.message.startswith(
        "Recorded an extra $1,000.00 on Credit card on Oct 1 · paid off"
    )
    assert "(was Dec 2028)" in extra.message
    debt = session.flow(card).debt
    assert debt is not None and debt.events
    event_id = debt.events[-1].id
    assert event_id is not None
    assert "(was " in session.remove_event(event_id).message
    gym = session.flow(fid)
    assert session.unset_debt(gym).message == "Gym is a plain expense again · its payment stays"


def test_record_balance_says_how_far_it_drifted(session: Session) -> None:
    r = session.record_balance(398000, session.today, posted=True)
    assert session.recorded_message(r) == (
        "Recorded $3,980.00 · $90.00 behind what the budget expected"
    )
    assert session.start().cents == 398000
    assert session.forget_balances() == 2
    assert not session.start().known


def test_recording_an_older_balance_says_the_newer_one_wins(session: Session) -> None:
    r = session.record_balance(100000, date(2026, 9, 10), posted=False)
    assert session.recorded_message(r).endswith("your newer balance from Sep 22 comes first")


def test_expected(session: Session) -> None:
    exp = session.expected(session.today)
    assert exp is not None and exp.cents == 407000 and exp.previous.as_of == date(2026, 9, 22)
    assert session.expected(date(2026, 9, 1)) is None


def test_weekly_spend_and_tags(session: Session) -> None:
    assert session.set_weekly_spend(20000).message.startswith(
        "Everyday spending is $200.00 a week · monthly net +$1,621.04 →"
    )
    assert session.stored_weekly() == 20000
    assert session.rename_tag("car", "Vehicle").message == "Renamed the tag car to vehicle"
    assert "vehicle" in {t.name for t in session.tags()}
    assert session.remove_tag("vehicle").message == "Took the tag vehicle off every flow"


def test_export_and_import_round_trip(session: Session, tmp_path: Path) -> None:
    path = tmp_path / "backup.json"
    assert session.export(path) == len(sample.FLOWS) + len(sample.DEBTS)
    session.remove_flow(session.flow(_id(session, "Rent")))
    assert session.import_(path, replace=True) == len(sample.FLOWS) + len(sample.DEBTS)
    assert "Rent" in {f.name for f in session.flows()}
    assert session.recorded() is not None
    with pytest.raises(CashError, match="no file"):
        session.import_(tmp_path / "nope.json", replace=True)


def test_an_open_edit_saves_only_what_the_person_changed(session: Session, budget_file) -> None:
    """Another program changes Rent while its form is open; saving the form (tags only)
    keeps the other program's amount and notes."""
    rent = session.flow(_id(session, "Rent"))
    other = sqlite3.connect(budget_file)
    other.execute(
        "UPDATE flow SET amount_cents = 220000, notes = 'raised' WHERE id = ?", (rent.id,)
    )
    other.commit()
    other.close()
    session.reload()
    done = session.update_flow(rent, replace(FlowDraft.of(rent), tags=("home", "housing")))
    now = session.flow(int(rent.id))
    assert (now.amount_cents, now.notes, now.tags) == (220000, "raised", ("home", "housing"))
    assert "it had changed on disk too" in done.message


def test_an_open_edit_keeps_a_loan_the_tidy_up_rolled_forward(
    session: Session, budget_file
) -> None:
    loan = session.flow(_id(session, "Car loan"))
    assert loan.debt is not None
    other = sqlite3.connect(budget_file)
    other.execute(
        "UPDATE debt SET balance_cents = 1442524, balance_as_of = '2026-09-30' WHERE flow_id = ?",
        (loan.id,),
    )
    other.commit()
    other.close()
    session.reload()
    session.update_flow(loan, replace(FlowDraft.of(loan), notes="the blue one"))
    debt = session.flow(int(loan.id)).debt
    assert debt is not None and (debt.balance_cents, debt.balance_as_of) == (
        1442524,
        date(2026, 9, 30),
    )


def test_a_form_never_writes_to_a_flow_that_replaced_its_own(session: Session, budget_file) -> None:
    card = session.flow(_id(session, "Credit card"))
    session.remove_flow(card)
    session.add_flow(replace(NETFLIX, name="Streaming 2"))  # may reuse the id
    with pytest.raises(CashError) as exc:
        session.update_flow(card, replace(FlowDraft.of(card), amount_cents=100))
    assert exc.value.code in ("unknown_flow", "stale_flow")
    assert "Streaming 2" in {f.name for f in session.flows()}


def test_a_locked_file_is_a_message_not_a_crash(session: Session, budget_file) -> None:
    session.conn.execute("PRAGMA busy_timeout = 50")
    other = sqlite3.connect(budget_file, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(CashError) as exc:
            session.set_active(session.flow(_id(session, "Gym")), False)
        assert exc.value.code == "db_busy"
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert session.flow(_id(session, "Gym")).active
    session.set_active(session.flow(_id(session, "Gym")), False)  # and it works again after


def test_export_refuses_the_budget_file(session: Session, budget_file) -> None:
    with pytest.raises(CashError, match="budget file"):
        session.export(budget_file)
    assert session.flows()


def test_a_damaged_backup_changes_nothing(session: Session, tmp_path: Path) -> None:
    import json

    path = tmp_path / "backup.json"
    session.export(path)
    data = json.loads(path.read_text())
    data["balances"] = [{"as_of": "2026-13-01", "balance": "10.00"}]
    path.write_text(json.dumps(data))
    before = [f.name for f in session.flows()]
    session.remove_flow(session.flow(_id(session, "Rent")))
    with pytest.raises(CashError, match="balance 1"):
        session.import_(path, replace=True)
    assert "Rent" not in {f.name for f in session.flows()}  # nothing came back, nothing went
    assert len(session.flows()) == len(before) - 1


# ── Staying current ───────────────────────────────────────────────────────────


def test_check_disk_notices_another_process(session: Session, budget_file: Path) -> None:
    assert not session.check_disk()
    session.add_flow(NETFLIX)  # our own writes don't count
    assert not session.check_disk()
    other = sqlite3.connect(budget_file)
    other.execute("UPDATE config SET value = '200' WHERE key = 'weekly_spend'")
    other.commit()
    other.close()
    v = session.version
    assert session.check_disk()
    assert session.version > v
    assert session.stored_weekly() == 20000
    assert not session.check_disk()


def test_check_day_reopens_and_tidies(session: Session, monkeypatch) -> None:
    assert not session.check_day()
    session.pop_tidied()
    monkeypatch.setenv("BDBD_TODAY", "2026-11-02")
    assert session.check_day()
    assert session.today == date(2026, 11, 2)
    tidied = session.pop_tidied()
    assert any("Tax refund" in t for t in tidied)  # the Oct 20 one-off is gone
    assert session.pop_tidied() == []


def test_create_a_new_budget(tmp_path: Path) -> None:
    s = Session.create(tmp_path / "new" / "budget.sqlite")
    try:
        assert s.flows() == []
        assert not s.start().known
        assert repo.config_all(s.conn) == {}
        assert balances.history(s.conn) == []
    finally:
        s.close()
