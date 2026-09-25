"""Date helpers: ISO parsing, clamped month arithmetic, month ends."""

from __future__ import annotations

import calendar
import os
import re
from collections.abc import Iterator
from datetime import date, timedelta

from bdbd.core.errors import CashError


def today() -> date:
    """Today's date, overridable with BDBD_TODAY=YYYY-MM-DD (tests, reproducible runs)."""
    override = os.environ.get("BDBD_TODAY")
    if override:
        return parse_date(override)
    return date.today()


def month_start(d: date) -> date:
    return d.replace(day=1)


def parse_date(text: str | date) -> date:
    if isinstance(text, date):
        return text
    try:
        return date.fromisoformat(str(text).strip())
    except ValueError as exc:
        raise CashError(f"invalid date {text!r}; expected YYYY-MM-DD", "invalid_date") from exc


RELATIVE_DATE_GRAMMAR = (
    "YYYY-MM-DD, today, tomorrow, yesterday, eom (end of month), eoy (end of year), "
    "or +N/-N with a unit d/w/m/y (e.g. +28d, +4w, -1m)"
)
_RELATIVE = re.compile(r"^([+-])(\d+)([dwmy])$")


def parse_date_rel(text: str | date, base: date) -> date:
    """A date, or a relative expression resolved against `base`.

    today/tomorrow/yesterday anchor on the real today (BDBD_TODAY); eom/eoy and +N/-N
    anchor on `base` (the query's as-of date; for --as-of itself, today). Case-insensitive.
    """
    if isinstance(text, date):
        return text
    raw = str(text).strip()
    word = raw.lower()
    if word == "today":
        return today()
    if word == "tomorrow":
        return today() + timedelta(days=1)
    if word == "yesterday":
        return today() - timedelta(days=1)
    if word == "eom":
        return month_end(base)
    if word == "eoy":
        return date(base.year, 12, 31)
    m = _RELATIVE.match(word)
    if m:
        n = int(m.group(2)) * (1 if m.group(1) == "+" else -1)
        unit = m.group(3)
        if unit == "d":
            return base + timedelta(days=n)
        if unit == "w":
            return base + timedelta(weeks=n)
        if unit == "m":
            return add_months(base, n)
        return add_months(base, 12 * n)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise CashError(
            f"invalid date {text!r}; expected {RELATIVE_DATE_GRAMMAR}", "invalid_date"
        ) from exc


def iso(d: date) -> str:
    return d.isoformat()


def days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def add_months(d: date, months: int) -> date:
    """Add months, clamping the day to the target month's length (Jan 31 + 1 -> Feb 28)."""
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(d.day, days_in_month(year, month)))


def month_end(d: date) -> date:
    return date(d.year, d.month, days_in_month(d.year, d.month))


def is_month_end(d: date) -> bool:
    return d.day == days_in_month(d.year, d.month)


def daterange(start: date, end: date) -> Iterator[date]:
    """Inclusive range of dates."""
    d = start
    one = timedelta(days=1)
    while d <= end:
        yield d
        d += one


def months_between(start: date, end: date) -> int:
    """Whole-month count used for --months style windows (end - start), floor."""
    return (
        (end.year - start.year) * 12 + (end.month - start.month) - (1 if end.day < start.day else 0)
    )
