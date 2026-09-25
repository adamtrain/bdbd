"""A made-up household budget for tests, development and the README screenshots.

None of this is real data. "Today" for it is Thu Sep 24, 2026.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

from bdbd.core import balance, repo
from bdbd.core.models import Compounding, Kind, Weekend

TODAY = date(2026, 9, 24)

FLOWS = [
    # name, kind, amount (cents), rrule, dtstart, tags, weekend
    ("Paycheck", Kind.INCOME, 265000, "FREQ=WEEKLY;INTERVAL=2", date(2026, 9, 18), ["job"],
     Weekend.NONE),
    ("Tax refund", Kind.INCOME, 124000, None, date(2026, 10, 20), ["taxes"], Weekend.NONE),
    ("Rent", Kind.EXPENSE, 215000, "FREQ=MONTHLY;BYMONTHDAY=1", date(2026, 10, 1), ["housing"],
     Weekend.NEXT),
    ("Electric", Kind.EXPENSE, 9640, "FREQ=MONTHLY;BYMONTHDAY=12", date(2026, 10, 12),
     ["utilities"], Weekend.NEXT),
    ("Internet", Kind.EXPENSE, 7000, "FREQ=MONTHLY;BYMONTHDAY=18", date(2026, 10, 18),
     ["utilities"], Weekend.NONE),
    ("Phone", Kind.EXPENSE, 4500, "FREQ=MONTHLY;BYMONTHDAY=22", date(2026, 10, 22), ["utilities"],
     Weekend.NONE),
    ("Car insurance", Kind.EXPENSE, 12800, "FREQ=MONTHLY;BYMONTHDAY=15", date(2026, 10, 15),
     ["car", "insurance"], Weekend.NEXT),
    ("Gym", Kind.EXPENSE, 3900, "FREQ=MONTHLY;BYMONTHDAY=3", date(2026, 10, 3), ["fitness"],
     Weekend.NONE),
    ("Streaming", Kind.EXPENSE, 1799, "FREQ=MONTHLY;BYMONTHDAY=9", date(2026, 10, 9), ["fun"],
     Weekend.NONE),
    ("Renters insurance", Kind.EXPENSE, 18000, "FREQ=YEARLY;BYMONTH=11;BYMONTHDAY=20",
     date(2026, 11, 20), ["housing", "insurance"], Weekend.NONE),
    ("Car registration", Kind.EXPENSE, 22000, "FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=14",
     date(2027, 3, 14), ["car"], Weekend.NONE),
    ("Dentist", Kind.EXPENSE, 24000, None, date(2026, 10, 28), ["health"], Weekend.NONE),
]  # fmt: skip

DEBTS = [
    # name, payment (cents), rrule, dtstart, tags, balance, as of, rate, compounding
    ("Car loan", 41237, "FREQ=MONTHLY;BYMONTHDAY=5", date(2026, 10, 5), ["car", "debt"],
     1486000, date(2026, 9, 5), "0.0649", Compounding.SIMPLE),
    ("Student loan", 23600, "FREQ=MONTHLY;BYMONTHDAY=21", date(2026, 10, 21), ["debt"],
     1840000, date(2026, 9, 21), "0.0499", Compounding.SIMPLE),
    ("Credit card", 15000, "FREQ=MONTHLY;BYMONTHDAY=25", date(2026, 9, 25), ["debt"],
     320000, date(2026, 9, 10), "0.2299", Compounding.DAILY),
]  # fmt: skip

WEEKLY_SPEND = "175"
BALANCE = (412000, date(2026, 9, 22))


def build(conn: sqlite3.Connection, *, with_balance: bool = True) -> None:
    for name, kind, cents, rrule, dtstart, tags, weekend in FLOWS:
        repo.add_flow(
            conn,
            name=name,
            kind=kind,
            amount_cents=cents,
            rrule=rrule,
            dtstart=dtstart,
            tags=tags,
            weekend=weekend,
        )
    for name, cents, rrule, dtstart, tags, bal, as_of, rate, comp in DEBTS:
        f = repo.add_flow(
            conn,
            name=name,
            kind=Kind.EXPENSE,
            amount_cents=cents,
            rrule=rrule,
            dtstart=dtstart,
            tags=tags,
            weekend=Weekend.NEXT,
        )
        repo.set_debt(
            conn,
            int(f.id),
            balance_cents=bal,
            balance_as_of=as_of,
            annual_rate=Decimal(rate),
            compounding=comp,
            capitalize_interest=None if comp != Compounding.SIMPLE else False,
        )
    repo.config_set(conn, "weekly_spend", WEEKLY_SPEND)
    if with_balance:
        balance.record(conn, *BALANCE)
