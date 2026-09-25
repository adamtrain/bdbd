"""Relative date grammar used by the query window flags."""

from datetime import date

import pytest

from bdbd.core.dates import parse_date_rel
from bdbd.core.errors import CashError

BASE = date(2026, 10, 5)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2026-12-31", date(2026, 12, 31)),
        ("today", date(2026, 9, 16)),  # BDBD_TODAY from conftest, not BASE
        ("tomorrow", date(2026, 9, 17)),
        ("yesterday", date(2026, 9, 15)),
        ("TODAY", date(2026, 9, 16)),
        ("eom", date(2026, 10, 31)),
        ("eoy", date(2026, 12, 31)),
        ("+0d", BASE),
        ("+28d", date(2026, 11, 2)),
        ("+4w", date(2026, 11, 2)),
        ("-1d", date(2026, 10, 4)),
        ("+1m", date(2026, 11, 5)),
        ("-2m", date(2026, 8, 5)),
        ("+1y", date(2027, 10, 5)),
        (" +2w ", date(2026, 10, 19)),
    ],
)
def test_relative_forms(text, expected):
    assert parse_date_rel(text, BASE) == expected


def test_month_arithmetic_clamps():
    assert parse_date_rel("+1m", date(2026, 1, 31)) == date(2026, 2, 28)
    assert parse_date_rel("+1y", date(2028, 2, 29)) == date(2029, 2, 28)


def test_date_objects_pass_through():
    assert parse_date_rel(BASE, date(2000, 1, 1)) == BASE


@pytest.mark.parametrize("bad", ["+3", "3d", "next week", "+1x", "2026-13-01", "", "eom+1d"])
def test_rejects_unknown_forms(bad):
    with pytest.raises(CashError) as exc:
        parse_date_rel(bad, BASE)
    assert exc.value.code == "invalid_date"
    assert "+4w" in exc.value.message  # the message teaches the grammar
