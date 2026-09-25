"""How lists are ordered, and what the next 12 months are.

A day's items: money in first, then money out, each largest first, then by name. Debts:
soonest paid off first, then the most owed, then by name.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from bdbd import ask
from bdbd.core import engine
from bdbd.core.models import EffectiveFlow, EffectiveModel, Kind

from .core.test_engine import loan

DAY = date(2026, 10, 1)


def _once(key: int, name: str, kind: Kind, cents: int) -> EffectiveFlow:
    return EffectiveFlow(
        key=key,
        name=name,
        kind=kind,
        tags=frozenset(),
        rrule=None,
        dtstart=DAY,
        until=None,
        base_cents=cents,
    )


def test_a_days_items_list_money_in_first_then_the_largest_then_by_name() -> None:
    car = loan(as_of=date(2026, 9, 1), dtstart=DAY)  # its payment lands among the bills
    flows = [
        _once(2, "Side gig", Kind.INCOME, 30000),
        _once(3, "Rent", Kind.EXPENSE, 215000),
        _once(4, "Gym", Kind.EXPENSE, 3900),
        _once(5, "Paycheck", Kind.INCOME, 265000),
        _once(6, "apps", Kind.EXPENSE, 3900),
        _once(7, "Bonus", Kind.INCOME, 30000),
        car,
    ]
    r = engine.run(
        EffectiveModel(flows=flows),
        as_of=date(2026, 9, 30),
        until=DAY,
        starting_balance_cents=100000,
        weekly_spend_cents=700,
    )
    day = [e for e in r.ledger if e.date == DAY]
    assert [e.name for e in day] == [
        "Paycheck", "Bonus", "Side gig",  # money in, largest first, then by name
        "Rent", "Car loan", "apps", "Gym",  # money out, the same (names ignore case)
        "Lifestyle spend (7.00/week)",  # everyday spending still closes the day
    ]  # fmt: skip
    assert [e.balance_after_cents for e in day] == [
        365000, 395000, 425000, 210000, 171334, 167434, 163534, 163434,
    ]  # fmt: skip
    assert r.daily[-1] == (DAY, 163434)
    alone = engine.run(EffectiveModel(flows=[car]), as_of=date(2026, 9, 30), until=DAY)
    assert r.debts[1].rows == alone.debts[1].rows  # listing never changes the debt math


def test_debts_list_soonest_paid_off_then_the_most_owed_then_by_name() -> None:
    def row(name: str, owed: int, paid_off: date | None) -> ask.DebtRow:
        return ask.DebtRow(
            key=name,
            name=name,
            balance=owed,
            rate=Decimal("0.05"),
            payment=10000,
            paid_off_on=paid_off,
            interest=None if paid_off is None else 0,
        )

    rows = [
        row("b", 100, date(2030, 1, 1)),
        row("A", 100, date(2030, 1, 1)),
        row("c", 500, date(2030, 1, 1)),
        row("d", 50, date(2028, 1, 1)),
        row("e", 900, None),
        row("f", 9000, None),
    ]
    assert [r.name for r in sorted(rows, key=ask.debt_order)] == ["d", "c", "A", "b", "f", "e"]


def test_the_next_12_months_end_the_day_before_the_same_date() -> None:
    assert ask.months_ahead(date(2026, 9, 24), 12) == date(2027, 9, 23)
    assert ask.months_ahead(date(2026, 1, 31), 1) == date(2026, 2, 27)  # Feb 28 is a month on
