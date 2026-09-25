from datetime import date
from decimal import Decimal

import pytest

from bdbd.core.errors import CashError
from bdbd.core.recurrence import occurrences, occurrences_per_year, validate_rrule

AS_OF = date(2026, 9, 16)


def test_third_tuesday_of_november():
    got = occurrences(
        "FREQ=YEARLY;BYMONTH=11;BYDAY=3TU", date(2026, 1, 1), None, AS_OF, date(2028, 12, 31)
    )
    assert got == [date(2026, 11, 17), date(2027, 11, 16), date(2028, 11, 21)]


def test_fortnightly():
    got = occurrences("FREQ=WEEKLY;INTERVAL=2", date(2026, 9, 18), None, AS_OF, date(2027, 3, 16))
    assert len(got) == 13
    assert got[0] == date(2026, 9, 18)
    assert got[-1] == date(2027, 3, 5)


def test_every_three_years():
    got = occurrences("FREQ=YEARLY;INTERVAL=3", date(2024, 6, 1), None, AS_OF, date(2036, 12, 31))
    assert got == [date(2027, 6, 1), date(2030, 6, 1), date(2033, 6, 1), date(2036, 6, 1)]


def test_feb_29():
    got = occurrences("FREQ=YEARLY", date(2024, 2, 29), None, date(2025, 1, 1), date(2033, 1, 1))
    assert got == [date(2028, 2, 29), date(2032, 2, 29)]
    got = occurrences(
        "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=-1",
        date(2024, 2, 29),
        None,
        date(2025, 1, 1),
        date(2033, 1, 1),
    )
    assert len(got) == 8
    assert date(2027, 2, 28) in got and date(2028, 2, 29) in got


def test_31st_skips_short_months():
    got = occurrences(
        "FREQ=MONTHLY;BYMONTHDAY=31", date(2026, 10, 31), None, AS_OF, date(2027, 1, 31)
    )
    assert got == [date(2026, 10, 31), date(2026, 12, 31), date(2027, 1, 31)]


def test_until_inclusive_and_one_off():
    got = occurrences(
        "FREQ=MONTHLY;BYMONTHDAY=1", date(2026, 10, 1), date(2027, 1, 1), AS_OF, date(2030, 1, 1)
    )
    assert got[-1] == date(2027, 1, 1) and len(got) == 4
    assert occurrences(None, date(2026, 12, 25), None, AS_OF, date(2027, 1, 1)) == [
        date(2026, 12, 25)
    ]
    assert occurrences(None, date(2026, 1, 1), None, AS_OF, date(2027, 1, 1)) == []


def test_count_rule():
    got = occurrences("FREQ=MONTHLY;COUNT=3", date(2026, 10, 1), None, AS_OF, date(2030, 1, 1))
    assert len(got) == 3


def test_occurrences_per_year():
    assert occurrences_per_year("FREQ=MONTHLY;BYMONTHDAY=1", date(2026, 11, 1), None, AS_OF) == 12
    assert (
        occurrences_per_year("FREQ=WEEKLY;INTERVAL=2", date(2026, 9, 18), None, AS_OF)
        == Decimal(2192) / 84
    )
    assert occurrences_per_year("FREQ=WEEKLY", date(2026, 9, 18), None, AS_OF) == Decimal(4383) / 84
    assert (
        occurrences_per_year("FREQ=YEARLY;INTERVAL=3", date(2024, 6, 1), None, AS_OF)
        == Decimal(4) / 12
    )
    assert (
        occurrences_per_year("FREQ=YEARLY;BYMONTH=11;BYDAY=3TU", date(2026, 1, 1), None, AS_OF) == 1
    )
    daily = occurrences_per_year("FREQ=DAILY", date(2026, 1, 1), None, AS_OF)
    assert daily in (Decimal(30680) / 84, Decimal(30681) / 84)  # window crosses 2100 (not leap)
    assert occurrences_per_year(None, date(2026, 1, 1), None, AS_OF) == 0
    assert occurrences_per_year("FREQ=MONTHLY", date(2020, 1, 1), date(2026, 1, 1), AS_OF) == 0


def test_validation():
    with pytest.raises(CashError) as e:
        validate_rrule("FREQ=MONTHLY;DTSTART=20260101")
    assert e.value.code == "invalid_rrule"
    with pytest.raises(CashError):
        validate_rrule("FREQ=BOGUS")
    with pytest.raises(CashError):
        validate_rrule("")
    assert validate_rrule("rrule:freq=monthly;bymonthday=1") == "FREQ=MONTHLY;BYMONTHDAY=1"
