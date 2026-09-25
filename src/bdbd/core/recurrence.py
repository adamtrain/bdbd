"""RFC 5545 RRULE handling via dateutil. dtstart/until live outside the rule string."""

from __future__ import annotations

import functools
from datetime import date, datetime, timedelta
from decimal import Decimal

from dateutil.rrule import rrule, rrulestr

from bdbd.core.errors import CashError
from bdbd.core.models import Weekend

_FORBIDDEN = ("DTSTART", "UNTIL")


def normalize_rrule(text: str) -> str:
    s = text.strip()
    if s.upper().startswith("RRULE:"):
        s = s[6:]
    return s.upper()


def validate_rrule(text: str) -> str:
    """Return the normalized rule body or raise invalid_rrule."""
    s = normalize_rrule(text)
    if not s:
        raise CashError("rrule must not be empty (omit it for a one-off flow)", "invalid_rrule")
    for part in s.split(";"):
        key = part.split("=", 1)[0]
        if key in _FORBIDDEN:
            raise CashError(
                f"rrule must not contain {key}; use the dtstart/until fields instead",
                "invalid_rrule",
            )
    try:
        build_rule(s, date(2000, 1, 1))
    except CashError:
        raise
    except Exception as exc:  # dateutil raises ValueError/KeyError/TypeError variously
        raise CashError(f"invalid rrule {text!r}: {exc}", "invalid_rrule") from exc
    return s


def build_rule(rule_text: str, dtstart: date) -> rrule:
    start = datetime(dtstart.year, dtstart.month, dtstart.day)
    try:
        rule = rrulestr(f"RRULE:{normalize_rrule(rule_text)}", dtstart=start)
    except Exception as exc:
        raise CashError(f"invalid rrule {rule_text!r}: {exc}", "invalid_rrule") from exc
    if not isinstance(rule, rrule):
        raise CashError(f"invalid rrule {rule_text!r}", "invalid_rrule")
    return rule


def shift_weekend(d: date, weekend: Weekend | str) -> date:
    """Apply a weekend rule: Sat/Sun -> following Monday (next) or preceding Friday (previous)."""
    w = Weekend(weekend)
    if w == Weekend.NONE or d.weekday() < 5:
        return d
    if w == Weekend.NEXT:
        return d + timedelta(days=7 - d.weekday())
    return d - timedelta(days=d.weekday() - 4)


def _raw_occurrences(
    rule_text: str | None, dtstart: date, until: date | None, start: date, end: date
) -> list[date]:
    if until is not None:
        end = min(end, until)
    if end < start:
        return []
    if rule_text is None:
        return [dtstart] if start <= dtstart <= end else []
    if end < dtstart:
        return []
    rule = build_rule(rule_text, dtstart)
    lo = datetime(start.year, start.month, start.day)
    hi = datetime(end.year, end.month, end.day)
    return [dt.date() for dt in rule.between(lo, hi, inc=True)]


def occurrences(
    rule_text: str | None,
    dtstart: date,
    until: date | None,
    start: date,
    end: date,
    weekend: Weekend | str = Weekend.NONE,
) -> list[date]:
    """All occurrence dates in [start, end] inclusive, honouring dtstart/until and the
    weekend rule (the rule is applied to the nominal date; `until` bounds the nominal date)."""
    w = Weekend(weekend)
    if w == Weekend.NONE:
        return _raw_occurrences(rule_text, dtstart, until, start, end)
    pad = timedelta(days=3)
    raw = _raw_occurrences(rule_text, dtstart, until, start - pad, end + pad)
    return [s for s in (shift_weekend(d, w) for d in raw) if start <= s <= end]


def last_occurrence(rule_text: str | None, dtstart: date, until: date | None) -> date | None:
    """Final nominal occurrence for finite rules (one-off, COUNT, or until); None if unbounded."""
    if rule_text is None:
        return dtstart
    rule = build_rule(rule_text, dtstart)
    if "COUNT=" in rule_text.upper():
        dates = [dt.date() for dt in rule]
        last = dates[-1] if dates else None
        if last is not None and until is not None:
            last = min(last, until)
        return last
    if until is not None:
        prev = rule.before(datetime(until.year, until.month, until.day), inc=True)
        return prev.date() if prev else None
    return None


def first_occurrence(rule_text: str | None, dtstart: date, until: date | None) -> date | None:
    if rule_text is None:
        return dtstart
    rule = build_rule(rule_text, dtstart)
    first = rule.after(datetime(dtstart.year, dtstart.month, dtstart.day), inc=True)
    if first is None:
        return None
    d = first.date()
    if until is not None and d > until:
        return None
    return d


STEADY_WINDOW_YEARS = 84


def occurrences_per_year(
    rule_text: str | None, dtstart: date, until: date | None, as_of: date
) -> Decimal:
    """Steady-state frequency: occurrences over an 84-year window, divided by 84.

    84 years = 30,681 days = exactly 4,383 weeks (three 28-year leap cycles), so weekly rules
    count exactly, and 84 is divisible by every common yearly INTERVAL (1,2,3,4,6,7,12,14).
    Fortnightly therefore comes out at 2192/84 = 26.095 per year (true value 26.089).
    The window starts at max(as_of, dtstart). Flows that have already ended (until < as_of)
    and one-offs contribute 0.
    """
    if rule_text is None:
        return Decimal(0)
    if until is not None and until < as_of:
        return Decimal(0)
    return Decimal(_steady_count(rule_text, dtstart, max(as_of, dtstart))) / Decimal(
        STEADY_WINDOW_YEARS
    )


@functools.lru_cache(maxsize=4096)
def _steady_count(rule_text: str, dtstart: date, start: date) -> int:
    """Occurrences in the 84 years from `start` (cached: it's pure and the costly part)."""
    window_end = date(start.year + STEADY_WINDOW_YEARS, start.month, min(start.day, 28))
    # steady mode ignores a future `until` on purpose: it asks "what does this cost per year
    # while it runs", so we pass until=None here. Window is half-open: [start, window_end).
    return len(occurrences(rule_text, dtstart, None, start, window_end - timedelta(days=1)))
