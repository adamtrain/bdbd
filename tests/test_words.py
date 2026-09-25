from datetime import date

import pytest

from bdbd.core.errors import CashError
from bdbd.words import describe, next_dates, ordinal, parse_day, parse_schedule, relative, span

T = date(2026, 9, 24)  # a Thursday


@pytest.mark.parametrize(
    ("text", "rrule", "start", "english"),
    [
        ("monthly on the 1st", "FREQ=MONTHLY;BYMONTHDAY=1", "2026-10-01", "Monthly on the 1st"),
        ("monthly on the 15th and last day", "FREQ=MONTHLY;BYMONTHDAY=15,-1", "2026-09-30",
         "Monthly on the 15th and last day"),
        ("every 2 weeks on fri from sep 18", "FREQ=WEEKLY;INTERVAL=2;BYDAY=FR", "2026-09-18",
         "Every 2 weeks on Fri"),
        ("biweekly from 2026-09-18", "FREQ=WEEKLY;INTERVAL=2", "2026-09-18",
         "Every 2 weeks on Fri"),
        ("every other week", "FREQ=WEEKLY;INTERVAL=2", "2026-09-24", "Every 2 weeks on Thu"),
        ("weekly on mon and thu", "FREQ=WEEKLY;BYDAY=MO,TH", "2026-09-24",
         "Weekly on Mon and Thu"),
        ("every weekday", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "2026-09-24", "Weekly on weekdays"),
        ("monthly on the first friday", "FREQ=MONTHLY;BYDAY=1FR", "2026-10-02",
         "Monthly on the 1st Friday"),
        ("monthly on the last friday", "FREQ=MONTHLY;BYDAY=-1FR", "2026-09-25",
         "Monthly on the last Friday"),
        ("every 3 months on the 10th", "FREQ=MONTHLY;INTERVAL=3;BYMONTHDAY=10", "2026-12-10",
         "Every 3 months on the 10th"),
        ("quarterly on the 1st", "FREQ=MONTHLY;INTERVAL=3;BYMONTHDAY=1", "2026-12-01",
         "Every 3 months on the 1st"),
        ("semimonthly", "FREQ=MONTHLY;BYMONTHDAY=1,15", "2026-10-01",
         "Monthly on the 1st and 15th"),
        ("yearly on nov 20", "FREQ=YEARLY;BYMONTH=11;BYMONTHDAY=20", "2026-11-20",
         "Yearly on Nov 20"),
        ("annually on the 3rd tuesday of november", "FREQ=YEARLY;BYMONTH=11;BYDAY=3TU",
         "2026-11-17", "Yearly on the 3rd Tuesday of Nov"),
        ("yearly on feb 29", "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=-1", "2027-02-28",
         "Yearly on the last day of Feb"),
        ("monthly on the 30th", "FREQ=MONTHLY;BYMONTHDAY=28,29,30;BYSETPOS=-1", "2026-09-30",
         "Monthly on the 30th"),
        ("monthly on the 31st", "FREQ=MONTHLY;BYMONTHDAY=-1", "2026-09-30",
         "Monthly on the last day"),
        ("monthly", "FREQ=MONTHLY;BYMONTHDAY=24", "2026-09-24", "Monthly on the 24th"),
        ("daily", "FREQ=DAILY", "2026-09-24", "Daily"),
        ("every 3 days", "FREQ=DAILY;INTERVAL=3", "2026-09-24", "Every 3 days"),
        ("monthly on the 1st 12 times", "FREQ=MONTHLY;BYMONTHDAY=1;COUNT=12", "2026-10-01",
         "Monthly on the 1st, 12 times"),
        ("every month on day 8 starting oct", "FREQ=MONTHLY;BYMONTHDAY=8", "2026-10-08",
         "Monthly on the 8th"),
        ("FREQ=MONTHLY;BYMONTHDAY=21", "FREQ=MONTHLY;BYMONTHDAY=21", "2026-10-21",
         "Monthly on the 21st"),
    ],
)  # fmt: skip
def test_schedules(text, rrule, start, english):
    s = parse_schedule(text, today=T)
    assert s.rrule == rrule
    assert s.dtstart.isoformat() == start
    assert describe(s.rrule, s.dtstart) == english


@pytest.mark.parametrize("text", ["once on oct 15", "oct 15", "on 2026-10-15", "one-off on oct 15"])
def test_one_offs(text):
    s = parse_schedule(text, today=T)
    assert s.rrule is None and s.dtstart == date(2026, 10, 15)
    assert describe(s.rrule, s.dtstart) == "Once on Oct 15, 2026"


def test_until_and_start_clauses():
    s = parse_schedule("monthly on the 5th until dec 31", today=T)
    assert s.until == date(2026, 12, 31) and s.dtstart == date(2026, 10, 5)
    s = parse_schedule("monthly on the 1st until dec 31 from oct 1", today=T)
    assert s.dtstart == date(2026, 10, 1) and s.until == date(2026, 12, 31)


@pytest.mark.parametrize(
    "text",
    ["", "sometimes", "monthly on the 32nd", "weekly on funday", "yearly on feb 30",
     "monthly on the 1st until 2020-01-01", "once on oct 15 until dec 1"],
)  # fmt: skip
def test_bad_schedules_explain_themselves(text):
    with pytest.raises(CashError) as e:
        parse_schedule(text, today=T)
    assert e.value.code in ("invalid_schedule", "invalid_date")


def test_english_round_trips():
    """What bdbd says about a schedule can be typed back in."""
    for text in ["monthly on the 15th and last day", "every 2 weeks on fri", "yearly on nov 20",
                 "monthly on the 1st friday", "weekly on mon and thu"]:  # fmt: skip
        first = parse_schedule(text, today=T)
        again = parse_schedule(describe(first.rrule, first.dtstart), today=T)
        assert again.rrule == first.rrule


def test_describe_falls_back_to_the_rule():
    assert describe("FREQ=MONTHLY;BYWEEKNO=3", T) == "FREQ=MONTHLY;BYWEEKNO=3"


@pytest.mark.parametrize(
    ("text", "prefer", "expected"),
    [
        ("oct 15", "future", date(2026, 10, 15)),
        ("oct 15", "past", date(2025, 10, 15)),
        ("sep 20", "future", date(2027, 9, 20)),
        ("sep 20", "past", date(2026, 9, 20)),
        ("the 15th of october", "future", date(2026, 10, 15)),
        ("dec31", "future", date(2026, 12, 31)),
        ("jan 5", "nearest", date(2027, 1, 5)),
        ("fri", "future", date(2026, 9, 25)),
        ("thu", "future", date(2026, 9, 24)),
        ("next thu", "future", date(2026, 10, 1)),
        ("last fri", "future", date(2026, 9, 18)),
        ("fri", "past", date(2026, 9, 18)),
        ("in 3 weeks", "future", date(2026, 10, 15)),
        ("2 weeks ago", "future", date(2026, 9, 10)),
        ("10/15", "future", date(2026, 10, 15)),
        ("12/25/2026", "future", date(2026, 12, 25)),
        ("oct", "future", date(2026, 10, 1)),
        ("aug", "future", date(2027, 8, 1)),
        ("eom", "future", date(2026, 9, 30)),
        ("+2w", "future", date(2026, 10, 8)),
        ("2026-12-01", "past", date(2026, 12, 1)),
    ],
)
def test_dates(text, prefer, expected):
    assert parse_day(text, today=T, prefer=prefer) == expected


@pytest.mark.parametrize("text", ["zzz", "feb 30", "13/45", ""])
def test_bad_dates(text):
    with pytest.raises(CashError):
        parse_day(text, today=T)


def test_relative_and_span():
    assert relative(T, T) == "today"
    assert relative(date(2026, 9, 25), T) == "tomorrow"
    assert relative(date(2026, 10, 2), T) == "in 8 days"
    assert relative(date(2026, 11, 5), T) == "in 6 weeks"
    assert relative(date(2031, 10, 1), T) == "in 5 years"
    assert relative(date(2026, 9, 20), T) == "4 days ago"
    assert span(14) == "1 year 2 months" and span(36) == "3 years" and span(1) == "1 month"
    assert [ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, -1)] == [
        "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "last",
    ]  # fmt: skip


def test_next_dates_respect_until():
    s = parse_schedule("monthly on the 1st until dec 31", today=T)
    assert next_dates(s.rrule, s.dtstart, s.until, T, 5) == [
        date(2026, 10, 1), date(2026, 11, 1), date(2026, 12, 1),
    ]  # fmt: skip
