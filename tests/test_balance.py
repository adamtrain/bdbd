"""The remembered balance: carried forward exactly, posted items, drift, tidy-up."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta

import pytest

from bdbd.budget import Budget
from bdbd.core import balance, engine, repo

from .conftest import TODAY


@pytest.fixture
def b(budget, db_path) -> Iterator[Budget]:
    repo.config_set(budget, "weekly_spend", "70")
    opened = Budget.open(db_path, tidy=False)
    yield opened
    opened.close()


@pytest.mark.parametrize("days_later", [0, 1, 2, 5, 15, 16, 45])
def test_carrying_a_balance_forward_matches_projecting_from_it(b, days_later):
    """Starting from a balance carried to day X gives the same future as starting at its date."""
    recorded_on = TODAY
    balance.record(b.conn, 300000, recorded_on)
    later = recorded_on + timedelta(days=days_later)
    until = date(2027, 3, 16)
    start = b.start(later)
    assert start.source == ("recorded" if days_later == 0 else "carried")
    weekly = b.weekly_spend()
    direct = engine.run(b.model(as_of=recorded_on), as_of=recorded_on, until=until,
                        starting_balance_cents=300000, weekly_spend_cents=weekly)  # fmt: skip
    carried = engine.run(b.model(as_of=later), as_of=later, until=until,
                         starting_balance_cents=start.cents, weekly_spend_cents=weekly)  # fmt: skip
    assert carried.ending_balance_cents == direct.ending_balance_cents
    assert dict(carried.daily) == {d: v for d, v in direct.daily if d >= later}


def test_no_balance_starts_from_zero(b):
    s = b.start()
    assert s.source == "none" and s.cents == 0 and not s.known


def test_given_balance_wins(b):
    balance.record(b.conn, 100, TODAY)
    assert b.start(given=5000).cents == 5000


def test_posted_items_are_taken_back_out(b):
    payday = date(2026, 9, 18)  # Salary +2500 lands that day
    b.today = payday
    rec = b.record_balance(400000, payday, posted=True)
    assert [e.name for e in rec.posted] == ["Salary"]
    assert rec.balance.amount_cents == 400000 - 250000  # stored before the day's items
    assert b.start(payday).cents == 150000
    rec = b.record_balance(400000, payday, posted=False)
    assert rec.balance.amount_cents == 400000


def test_difference_from_what_the_budget_expected(b):
    b.record_balance(300000, TODAY, posted=False)
    b.today = TODAY + timedelta(days=7)
    rec = b.record_balance(290000, b.today, posted=False)
    assert rec.previous is not None and rec.previous.as_of == TODAY
    assert rec.expected_cents == 300000 - 7000 + 250000  # a week of $70 and the Sep 18 salary
    assert rec.balance.amount_cents - rec.expected_cents == 290000 - 543000


def test_recording_twice_the_same_day_replaces(b):
    b.record_balance(100, TODAY, posted=False)
    b.record_balance(200, TODAY, posted=False)
    assert [h.amount_cents for h in balance.history(b.conn)] == [200]


def test_future_balances_are_refused(b):
    from bdbd.budget import Problem

    with pytest.raises(Problem):
        b.record_balance(100, TODAY + timedelta(days=1), posted=False)


def test_tidy_forgets_old_balances_but_keeps_the_newest(b):
    balance.record(b.conn, 100, date(2026, 8, 1))
    balance.record(b.conn, 200, date(2026, 8, 20))
    b.today = date(2026, 9, 16)
    actions = b.tidy()
    assert [h.amount_cents for h in balance.history(b.conn)] == [200]
    assert any("forgot 1 balance" in a for a in actions)
    balance.record(b.conn, 300, date(2026, 9, 2))
    b.tidy()
    assert [h.amount_cents for h in balance.history(b.conn)] == [300]


def test_balance_table_leaves_the_schema_version_alone(b):
    from bdbd.core.db import current_version
    from bdbd.core.migrations import LATEST_VERSION

    balance.record(b.conn, 100, TODAY)
    assert current_version(b.conn) == LATEST_VERSION


def test_stale_balances_warn(agent, monkeypatch):
    agent("add", "Salary", "2500", "--income", "every 2 weeks from 2026-09-18")
    agent("balance", "1000")
    monkeypatch.setenv("BDBD_TODAY", "2026-10-15")
    env = agent("overview")
    assert any("last recorded" in w for w in env["warnings"])
    assert env["data"]["balance"]["source"] == "carried"
