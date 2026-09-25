"""Paydays and pay cycles: which incomes start them, and what's left of each paycheck.

A pay cycle runs from a payday to the day before the next one. What's left of its paycheck
is the paycheck minus the cycle's bills and (by default) its everyday spending allowance;
other money coming in during the cycle is listed but not counted.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from bdbd import ask
from bdbd.budget import Budget
from bdbd.core import db, repo
from bdbd.core.errors import CashError
from bdbd.core.migrations import MIGRATIONS
from bdbd.core.models import Kind

from . import sample

# ── Which incomes are paydays ─────────────────────────────────────────────────


def _migrated(path: Path, flows: list[tuple[str, str, str | None]]) -> dict[str, bool]:
    """A budget made before paydays existed (schema v4), upgraded: each flow's payday."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for version, sql in MIGRATIONS[:4]:
        conn.executescript(
            f"BEGIN;\n{sql}\nINSERT INTO schema_version VALUES ({version}, 'then');\nCOMMIT;"
        )
    for name, kind, rrule in flows:
        conn.execute(
            "INSERT INTO flow(name, kind, amount_cents, rrule, dtstart) "
            "VALUES (?, ?, 100, ?, '2026-09-04')",
            (name, kind, rrule),
        )
    db.migrate(conn)
    try:
        return {r["name"]: bool(r["payday"]) for r in conn.execute("SELECT * FROM flow")}
    finally:
        conn.close()


def test_upgrading_flags_the_one_regular_income(tmp_path) -> None:
    got = _migrated(
        tmp_path / "a.sqlite",
        [
            ("Vanta", "income", "FREQ=WEEKLY;INTERVAL=2"),
            ("Escrow refund", "income", None),  # a one-off
            ("Bonus", "income", "FREQ=YEARLY"),
            ("Rent", "expense", "FREQ=MONTHLY;BYMONTHDAY=1"),
        ],
    )
    assert got == {"Vanta": True, "Escrow refund": False, "Bonus": False, "Rent": False}
    semimonthly = _migrated(
        tmp_path / "b.sqlite",
        [("Paycheck", "income", "FREQ=MONTHLY;BYMONTHDAY=1,15"), ("Dividends", "income",
                                                                    "FREQ=MONTHLY;INTERVAL=3")],
    )  # fmt: skip
    assert semimonthly == {"Paycheck": True, "Dividends": False}


def test_upgrading_guesses_nothing_when_two_incomes_could_be_the_paycheck(tmp_path) -> None:
    got = _migrated(
        tmp_path / "c.sqlite",
        [("Paycheck", "income", "FREQ=MONTHLY;BYMONTHDAY=1"),
         ("Rental", "income", "FREQ=MONTHLY;BYMONTHDAY=5")],
    )  # fmt: skip
    assert got == {"Paycheck": False, "Rental": False}


def test_the_first_regular_income_added_is_the_payday(conn) -> None:
    def add(name: str, kind: Kind, rrule: str | None, **kw) -> bool:
        f = repo.add_flow(
            conn, name=name, kind=kind, amount_cents=100, rrule=rrule, dtstart=date(2026, 10, 2),
            **kw,
        )  # fmt: skip
        return f.payday

    assert not add("Refund", Kind.INCOME, None)  # a one-off never is, by default
    assert not add("Rent", Kind.EXPENSE, "FREQ=MONTHLY;BYMONTHDAY=1")
    assert add("Paycheck", Kind.INCOME, "FREQ=WEEKLY;INTERVAL=2")
    assert not add("Side gig", Kind.INCOME, "FREQ=MONTHLY;BYMONTHDAY=9")  # there is one now
    assert add("Second job", Kind.INCOME, "FREQ=MONTHLY;BYMONTHDAY=20", payday=True)
    with pytest.raises(CashError, match="only an income"):
        add("Gym", Kind.EXPENSE, "FREQ=MONTHLY;BYMONTHDAY=3", payday=True)
    job = repo.resolve_flow(conn, "Second job")
    assert not repo.update_flow(conn, int(job.id), kind=Kind.EXPENSE).payday  # expenses can't


def test_payday_on_the_command_line_and_in_backups(home, tmp_path) -> None:
    assert home("show", "Paycheck")["data"]["payday"] is True  # the sample's first income
    assert home("show", "Tax refund")["data"]["payday"] is False
    d = home("add", "Side gig", "300", "monthly on the 9th", "--income", "--payday")["data"]
    assert d["payday"] is True
    assert home("edit", "Side gig", "--no-payday")["data"]["payday"] is False
    env = home("edit", "Rent", "--payday", ok=False)
    assert env["error"]["code"] == "invalid_payday"
    backup = tmp_path / "backup.json"
    home("export", "-o", str(backup))
    flows = {f["name"]: f for f in json.loads(backup.read_text())["flows"]}
    assert flows["Paycheck"]["payday"] is True and flows["Side gig"]["payday"] is False
    home("import", str(backup), "--replace")
    assert home("show", "Paycheck")["data"]["payday"] is True


# ── Pay cycles ────────────────────────────────────────────────────────────────


@pytest.fixture
def household(tmp_path, monkeypatch) -> Iterator[Budget]:
    monkeypatch.setenv("BDBD_TODAY", sample.TODAY.isoformat())
    path = tmp_path / "household.sqlite"
    conn = db.connect(path, create=True)
    sample.build(conn)
    conn.close()
    b = Budget.open(path, tidy=False)
    yield b
    b.close()


def test_a_cycle_runs_from_a_payday_to_the_day_before_the_next(household) -> None:
    pc = ask.pay_cycles(household, months=2)
    assert pc.paydays == ["Paycheck"] and pc.weekly == 17500
    first, second, third = pc.cycles[:3]
    # the cycle under way: its payday (Fri Sep 18) was before today (Thu Sep 24)
    assert (first.start, first.end, first.days) == (date(2026, 9, 18), date(2026, 10, 1), 14)
    assert first.holds(sample.TODAY) and not first.open
    assert [(i.name, i.cents) for i in first.paychecks] == [("Paycheck", 265000)]
    assert [(i.date, i.name, i.cents) for i in first.bills] == [
        (date(2026, 9, 25), "Credit card", -15000),
        (date(2026, 10, 1), "Rent", -215000),
    ]
    assert first.everyday == 35000  # 14 days at $175 a week
    assert (first.left_before_everyday, first.left) == (35000, 0)
    assert (second.start, second.end) == (date(2026, 10, 2), date(2026, 10, 15))
    assert (second.bills_total, second.left) == (69376, 160624)
    # a refund in the cycle counts: it's there to spend too
    assert [(i.name, i.cents) for i in third.money_in] == [("Tax refund", 124000)]
    assert third.money_in_total == 124000
    assert third.left == 265000 + 124000 - third.bills_total - 35000 == 279900
    assert all(c.start <= date(2026, 11, 24) for c in pc.cycles)  # two months of paydays


def test_the_cycle_under_way_counts_what_happened_before_today(household) -> None:
    repo.add_flow(
        household.conn, name="Concert", kind=Kind.EXPENSE, amount_cents=8000, rrule=None,
        dtstart=date(2026, 9, 20),
    )  # fmt: skip
    first = ask.pay_cycles(household, months=1).cycles[0]
    assert [i.name for i in first.bills] == ["Concert", "Credit card", "Rent"]
    assert first.left == -8000


def test_the_last_payday_starts_an_open_cycle(household) -> None:
    pay = repo.resolve_flow(household.conn, "Paycheck")
    repo.update_flow(household.conn, int(pay.id), until=date(2026, 10, 20))
    pc = ask.pay_cycles(household, months=2)
    last = pc.cycles[-1]
    assert last.start == date(2026, 10, 16) and last.open
    assert last.end == date(2026, 11, 24)  # where the window stops


def test_no_payday_no_cycles(home) -> None:
    home("edit", "Paycheck", "--no-payday")
    env = home("paydays")
    assert env["data"]["cycles"] == [] and env["data"]["paydays"] == []
    assert "no payday" in env["warnings"][0]


def test_bdbd_paydays(home) -> None:
    d = home("paydays", "--months", "1")["data"]
    first = d["cycles"][0]
    assert d["paydays"] == ["Paycheck"] and d["weekly_spend"] == "175.00"
    assert {k: first[k] for k in ("start", "end", "days", "current", "open")} == {
        "start": "2026-09-18",
        "end": "2026-10-01",
        "days": 14,
        "current": True,
        "open": False,
    }
    assert (first["paycheck"], first["bills_total"], first["everyday"]) == (
        "2650.00",
        "2300.00",
        "350.00",
    )
    assert (first["left"], first["left_before_everyday"]) == ("0.00", "350.00")
    assert [b["name"] for b in first["bills"]] == ["Credit card", "Rent"]
    # a what-if counts: with no rent in November, the Oct 30 paycheck isn't short any more
    before = {c["start"]: c["left"] for c in home("paydays", "--months", "2")["data"]["cycles"]}
    after = home("paydays", "--months", "2", "--stop", "Rent@2026-10-15")["data"]["cycles"]
    assert before["2026-10-30"] == "-415.76"
    assert {c["start"]: c["left"] for c in after}["2026-10-30"] == "1734.24"


# ── Spare until payday ────────────────────────────────────────────────────────


def test_spare_runs_to_the_next_payday_and_adds_money_in_on_its_day(home) -> None:
    """A refund before payday doesn't cut the count short: it's added, and it counts."""
    d = home("project", "--until", "2026-10-17", "--select", "spare")["data"]["spare"]
    assert d["next_payday"] == {"date": "2026-10-30", "name": "Paycheck", "amount": "2650.00"}
    assert d["income_before_payday"] == [
        {"date": "2026-10-20", "name": "Tax refund", "amount": "1240.00"}
    ]
    bills = sum(Decimal(b["amount"]) for b in d["committed_before_payday"])
    everyday = Decimal(d["committed_lifestyle"])
    assert [b["name"] for b in d["committed_before_payday"]][:2] == ["Internet", "Student loan"]
    assert Decimal(d["committed_total"]) == bills + everyday
    assert Decimal(d["income_total"]) == Decimal("1240.00")
    spare = Decimal(d["balance"]) + Decimal("1240.00") - bills - everyday
    assert Decimal(d["spare_balance"]) == spare


def test_without_a_payday_spare_counts_to_the_next_income(home) -> None:
    home("edit", "Paycheck", "--no-payday")
    d = home("project", "--until", "2026-10-17", "--select", "spare")["data"]["spare"]
    assert d["next_payday"]["name"] == "Tax refund"  # with no payday, any income ends it
    assert d["income_before_payday"] == []


def test_the_forecasts_spare_note_names_the_money_coming_in() -> None:
    from bdbd.tui.views.forecast import _spare_note

    spare = {
        "next_payday": {"date": "2026-10-30", "name": "Paycheck", "amount": "2650.00"},
        "committed_before_payday": [{"date": "2026-10-18", "name": "Internet", "amount": "70"}],
        "committed_lifestyle": "300.00",
        "income_before_payday": [{"date": "2026-10-20", "name": "Tax refund", "amount": "1240"}],
    }
    today = date(2026, 10, 17)
    assert _spare_note(spare, today) == (
        "until Paycheck on Fri Oct 30 · after Internet and everyday spending · with Tax refund in"
    )
    assert _spare_note(spare, today, room=70) == (
        "until Paycheck on Fri Oct 30 · after Internet and everyday spending"
    )
