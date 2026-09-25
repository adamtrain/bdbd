"""Plain English for schedules and dates.

    "monthly on the 1st"            <->  FREQ=MONTHLY;BYMONTHDAY=1
    "every 2 weeks on fri"          <->  FREQ=WEEKLY;INTERVAL=2;BYDAY=FR
    "yearly on the 3rd tue of nov"  <->  FREQ=YEARLY;BYMONTH=11;BYDAY=3TU

Schedules are stored as an RFC 5545 RRULE body plus dtstart/until, so any tool that speaks
RRULE can read them. Raw RRULE text is accepted too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from bdbd.core.dates import add_months, month_end, parse_date_rel
from bdbd.core.errors import CashError
from bdbd.core.recurrence import build_rule, validate_rrule

DAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
DAY_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DAY_LONG = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTH_LONG = ("", "January", "February", "March", "April", "May", "June", "July", "August",
              "September", "October", "November", "December")  # fmt: skip
MONTH_SHORT = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
               "Dec")  # fmt: skip

WEEKDAYS = {
    **{name.lower(): i for i, name in enumerate(DAY_LONG)},
    **{name.lower(): i for i, name in enumerate(DAY_SHORT)},
    "tues": 1, "weds": 2, "thur": 3, "thurs": 3,
}  # fmt: skip
MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9,
    "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12,
    "december": 12,
}  # fmt: skip
NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}  # fmt: skip
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "last": -1}

SCHEDULE_EXAMPLES = (
    "monthly on the 1st",
    "every 2 weeks on fri from sep 18",
    "monthly on the 15th and last day",
    "yearly on nov 20",
    "weekly on mon and thu",
    "monthly on the first friday",
    "every 3 months on the 10th",
    "once on oct 15",
)

DATE_EXAMPLES = "2026-10-15, oct 15, fri, tomorrow, in 3 weeks, +2w or eom"

_LIST_SPLIT = r"[\s,/&+]+|\band\b"


# ── Numbers and names ─────────────────────────────────────────────────────────


def ordinal(n: int) -> str:
    if n == -1:
        return "last"
    if n < 0:
        return f"{ordinal(-n)} to last"
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def join(items: list[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def _number(word: str) -> int | None:
    if word.isdigit():
        return int(word)
    return NUMBERS.get(word)


def _ordinal_value(word: str) -> int | None:
    """'1st' -> 1, 'third' -> 3, 'last' -> -1, '15' -> 15."""
    if word in ORDINALS:
        return ORDINALS[word]
    m = re.fullmatch(r"(\d{1,2})(st|nd|rd|th)?", word)
    return int(m.group(1)) if m else None


# ── Dates ─────────────────────────────────────────────────────────────────────


def _clean(text: str) -> str:
    s = text.strip().lower().replace(",", " ")
    s = re.sub(r"\b(?:the|of)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _pick_year(month: int, day: int, today: date, prefer: str) -> date:
    """Resolve a month and day without a year to the right side of today."""
    candidates = []
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        raise CashError(f"there's no {MONTH_SHORT[month]} {day}", "invalid_date")
    if prefer == "future":
        upcoming = [d for d in candidates if d >= today]
        return upcoming[0] if upcoming else candidates[-1]
    if prefer == "past":
        past = [d for d in candidates if d <= today]
        return past[-1] if past else candidates[0]
    return min(candidates, key=lambda d: (abs((d - today).days), d < today))


def _month_day(words: list[str]) -> tuple[int, int, int | None] | None:
    """['oct', '15'] / ['15th', 'october'] / ['oct', '15', '2027'] -> (month, day, year)."""
    year = None
    if len(words) == 3 and re.fullmatch(r"\d{4}", words[2]):
        year, words = int(words[2]), words[:2]
    elif len(words) == 3 and re.fullmatch(r"\d{4}", words[0]):
        year, words = int(words[0]), words[1:]
    if len(words) != 2:
        return None
    a, b = words
    if a in MONTHS and (d := _ordinal_value(b)) is not None and d > 0:
        return MONTHS[a], d, year
    if b in MONTHS and (d := _ordinal_value(a)) is not None and d > 0:
        return MONTHS[b], d, year
    return None


def _real_date(year: int, month: int, day: int, raw: str) -> date:
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise CashError(f"{raw!r} isn't a real date", "invalid_date") from exc


def parse_day(
    text: str | date, *, today: date, base: date | None = None, prefer: str = "future"
) -> date:
    """Read a date the way people write it.

    `prefer` settles dates written without a year ("oct 15") and bare weekdays ("fri"):
    "future" picks the next one (on or after today), "past" the latest one on or before today,
    "nearest" whichever is closer. Relative forms (+2w, eom, ...) count from `base`
    (default: today), like the query window flags do.
    """
    if isinstance(text, date):
        return text
    raw = str(text).strip()
    if not raw:
        raise CashError("a date is needed", "invalid_date")
    base = base or today
    try:
        return parse_date_rel(raw, base)
    except CashError:
        pass
    s = _clean(raw)
    s = re.sub(
        r"^([a-z]{3,9})(\d{1,2})$|^(\d{1,2})([a-z]{3,9})$",
        lambda m: " ".join(g for g in m.groups() if g),
        s,
    )
    words = s.split()

    # in 3 weeks / 3 weeks ago / 2 months from now
    m = re.fullmatch(r"(in )?(\w+) (day|week|month|year)s?( ago| from now)?", s)
    if m and (m.group(1) or m.group(4)) and (n := _number(m.group(2))) is not None:
        n = -n if m.group(4) == " ago" else n
        if m.group(3) == "day":
            return base + timedelta(days=n)
        if m.group(3) == "week":
            return base + timedelta(weeks=n)
        return add_months(base, n * (12 if m.group(3) == "year" else 1))
    if s in ("end of month", "end of this month", "month end"):
        return month_end(base)
    if s in ("end of year", "end of this year", "year end"):
        return date(base.year, 12, 31)

    # fri / next fri / last fri / this friday
    m = re.fullmatch(r"(next |this |last |coming )?(\w+)", s)
    if m and m.group(2) in WEEKDAYS:
        wd = WEEKDAYS[m.group(2)]
        qualifier = (m.group(1) or "").strip()
        if qualifier == "last" or (not qualifier and prefer == "past"):
            back = (today.weekday() - wd) % 7 or (7 if qualifier == "last" else 0)
            return today - timedelta(days=back)
        ahead = (wd - today.weekday()) % 7 or (7 if qualifier == "next" else 0)
        return today + timedelta(days=ahead)

    # oct 15 / 15 october / oct 15 2027
    if (md := _month_day(words)) is not None:
        month, day, year = md
        if year is not None:
            return _real_date(year, month, day, raw)
        return _pick_year(month, day, today, prefer)

    # oct / october 2027: the 1st of that month
    if len(words) in (1, 2) and words[0] in MONTHS:
        month = MONTHS[words[0]]
        if len(words) == 2:
            if not re.fullmatch(r"\d{4}", words[1]):
                raise CashError(
                    f"couldn't read {raw!r} as a date; try {DATE_EXAMPLES}", "invalid_date"
                )
            return date(int(words[1]), month, 1)
        year = today.year
        if prefer == "future" and month < today.month:
            year += 1
        elif prefer == "past" and month > today.month:
            year -= 1
        return date(year, month, 1)

    # 10/15, 10/15/2026, 10/15/26 (month first)
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?", s)
    if m and 1 <= int(m.group(1)) <= 12:
        month, day = int(m.group(1)), int(m.group(2))
        if m.group(3):
            year = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
            return _real_date(year, month, day, raw)
        return _pick_year(month, day, today, prefer)
    raise CashError(f"couldn't read {raw!r} as a date; try {DATE_EXAMPLES}", "invalid_date")


def fmt_date(d: date, today: date | None = None, *, weekday: bool = False) -> str:
    """'Oct 2' this year or within the coming months, 'Oct 2, 2029' otherwise.

    weekday=True gives 'Fri Oct 2'. Without `today`, the year is always shown.
    """
    s = f"{MONTH_SHORT[d.month]} {d.day}"
    if today is None or not (d.year == today.year or 0 <= (d - today).days < 330):
        s += f", {d.year}"
    return f"{DAY_SHORT[d.weekday()]} {s}" if weekday else s


def fmt_month(d: date) -> str:
    return f"{MONTH_SHORT[d.month]} {d.year}"


def span(months: int) -> str:
    """36 -> '3 years', 14 -> '1 year 2 months', 5 -> '5 months'."""
    years, rest = divmod(months, 12)
    parts = []
    if years:
        parts.append(f"{years} year{'s' if years != 1 else ''}")
    if rest or not years:
        parts.append(f"{rest} month{'s' if rest != 1 else ''}")
    return " ".join(parts)


def months_apart(earlier: date, later: date) -> int:
    return (
        (later.year - earlier.year) * 12 + later.month - earlier.month - (later.day < earlier.day)
    )


def relative(d: date, today: date) -> str:
    """'today', 'tomorrow', 'in 8 days', '3 weeks ago', 'in 4 years 2 months'."""
    days = (d - today).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "yesterday"
    n = abs(days)
    if n < 14:
        text = f"{n} days"
    elif n < 60:
        text = f"{round(n / 7)} weeks"
    else:
        later, earlier = (d, today) if days > 0 else (today, d)
        text = span(max(months_apart(earlier, later), 2))
    return f"in {text}" if days > 0 else f"{text} ago"


# ── Schedules: English -> RRULE ───────────────────────────────────────────────


@dataclass(frozen=True)
class Schedule:
    rrule: str | None  # None = once, on dtstart
    dtstart: date
    until: date | None = None


def _parse_weekdays(text: str) -> list[int]:
    words = [w for w in re.split(_LIST_SPLIT, text) if w]
    if words in (["weekdays"], ["weekday"]):
        return [0, 1, 2, 3, 4]
    if words in (["weekends"], ["weekend"]):
        return [5, 6]
    days = []
    for w in words:
        key = w if w in WEEKDAYS else w.removesuffix("s")
        if key not in WEEKDAYS:
            raise CashError(f"{w!r} isn't a day of the week", "invalid_schedule")
        days.append(WEEKDAYS[key])
    return sorted(set(days))


def _parse_nth_weekday(text: str) -> tuple[int, int] | None:
    """'first friday' -> (1, 4); 'last mon' -> (-1, 0); '2nd tuesday' -> (2, 1)."""
    words = text.split()
    if len(words) != 2:
        return None
    n = _ordinal_value(words[0])
    if n is None or not (n == -1 or 1 <= n <= 5) or words[1] not in WEEKDAYS:
        return None
    return n, WEEKDAYS[words[1]]


def _month_days(text: str) -> list[int]:
    """'1st and 15th' -> [1, 15]; '15th and last day' -> [15, -1]; 'day 5' -> [5]."""
    s = re.sub(r"\bof (?:each |every |the )?month\b", " ", text)
    s = re.sub(r"\bdays?\b", " ", s)
    days = []
    for p in (p for p in re.split(_LIST_SPLIT, s) if p):
        v = _ordinal_value(p)
        if v is None or not (v == -1 or 1 <= v <= 31):
            raise CashError(f"{p!r} isn't a day of the month", "invalid_schedule")
        days.append(v)
    if not days:
        raise CashError("which day of the month?", "invalid_schedule")
    return sorted(set(days), key=lambda d: (d == -1, d))


def _monthday_parts(days: list[int]) -> list[str]:
    """BYMONTHDAY; a lone 29th or 30th becomes 'that day, or the month's last day if shorter'."""
    if len(days) == 1 and days[0] in (29, 30):
        return [f"BYMONTHDAY={','.join(str(d) for d in range(28, days[0] + 1))}", "BYSETPOS=-1"]
    if days == [31]:
        return ["BYMONTHDAY=-1"]
    return [f"BYMONTHDAY={','.join(str(d) for d in days)}"]


def _take(pattern: str, s: str) -> tuple[str | None, str]:
    """Remove the first match of `pattern` from `s`; return (group 1, what's left)."""
    m = re.search(pattern, s)
    if not m:
        return None, s
    return m.group(1).strip(), re.sub(r"\s+", " ", s[: m.start()] + " " + s[m.end() :]).strip()


_STARTS = r"(?:from|starting|beginning|starts?)"
_ENDS = r"(?:until|through|thru|till|ending)"


def _split_clauses(s: str) -> tuple[str, str | None, str | None, int | None]:
    """Pull 'from DATE', 'until DATE' and 'N times' off a schedule phrase."""
    count_text, s = _take(r"\b(?:for )?(\w+) (?:times|payments|occurrences)\b", s)
    count = _number(count_text) if count_text else None
    if count_text and count is None:
        raise CashError(f"{count_text!r} isn't a number of times", "invalid_schedule")
    until, s = _take(rf"\b{_ENDS}\b (.+?)(?= \b{_STARTS}\b|$)", s)
    start, s = _take(rf"\b{_STARTS}\b (?:on )?(.+?)(?= \b{_ENDS}\b|$)", s)
    return s, start, until, count


_SYNONYMS = (
    ("one-off", "once"),
    ("one off", "once"),
    ("one time", "once"),
    ("bi-weekly", "biweekly"),
    ("semi-monthly", "semimonthly"),
    ("twice a month", "semimonthly"),
    ("every weekday", "weekly on weekdays"),
)
_EVERY = r"(?:every|each)"
_RECURRING = re.compile(
    r"\b(daily|weekly|monthly|semimonthly|yearly|annually|quarterly|biweekly|fortnightly|every"
    r"|each)\b"
)


def parse_schedule(text: str, *, today: date) -> Schedule:
    """Turn 'every 2 weeks on fri from sep 18' into an RRULE and a start date."""
    raw = text.strip()
    if not raw:
        raise CashError(
            "when does it happen? e.g. " + ", ".join(SCHEDULE_EXAMPLES[:3]), "invalid_schedule"
        )
    if "freq=" in raw.lower():
        return _parse_raw_rrule(raw, today)
    s = raw.lower().replace(",", " ")
    for a, b in _SYNONYMS:
        s = s.replace(a, b)
    s = re.sub(r"\s+", " ", s).strip()
    core, start_text, until_text, count = _split_clauses(s)
    core = re.sub(r"\s+", " ", re.sub(r"\bthe\b", " ", core)).strip()
    start = parse_day(start_text, today=today, prefer="nearest") if start_text else None
    until = parse_day(until_text, today=today, prefer="future") if until_text else None

    if not _RECURRING.search(core):
        m = re.fullmatch(r"(?:once\b\s*)?(?:on\b\s*)?(.*)", core)
        when = m.group(1) if m else core
        day = parse_day(when, today=today, prefer="future") if when else start
        if day is None:
            raise CashError("once on which date? e.g. once on oct 15", "invalid_schedule")
        if count is not None or until is not None:
            raise CashError("a one-off happens once; drop 'until' / 'times'", "invalid_schedule")
        return Schedule(None, day)

    anchor = start or today
    parts: list[str]
    if m := re.fullmatch(rf"(?:daily|{_EVERY} day|{_EVERY} (\w+) days)", core):
        interval = _interval(m.group(1), 1, "days")
        parts = ["FREQ=DAILY", *_interval_part(interval)]
        return _finish(parts, anchor, until, count, today)

    if m := re.fullmatch(
        rf"(weekly|biweekly|fortnightly|{_EVERY} week|{_EVERY} other week|{_EVERY} (\w+) weeks)"
        r"(?: on (.+))?",
        core,
    ):
        default = 2 if m.group(1) in ("biweekly", "fortnightly") or "other" in m.group(1) else 1
        parts = ["FREQ=WEEKLY", *_interval_part(_interval(m.group(2), default, "weeks"))]
        if m.group(3):
            parts.append("BYDAY=" + ",".join(DAY_CODES[d] for d in _parse_weekdays(m.group(3))))
        return _finish(parts, anchor, until, count, today)

    if m := re.fullmatch(
        rf"(monthly|semimonthly|quarterly|{_EVERY} month|{_EVERY} other month|{_EVERY} quarter"
        rf"|{_EVERY} (\w+) months)(?: on (.+))?",
        core,
    ):
        head = m.group(1)
        default = 3 if "quarter" in head else 2 if "other" in head else 1
        parts = ["FREQ=MONTHLY", *_interval_part(_interval(m.group(2), default, "months"))]
        spec = (m.group(3) or "").strip()
        if head == "semimonthly" and not spec:
            spec = "1st and 15th"
        if spec and (nth := _parse_nth_weekday(spec)):
            parts.append(f"BYDAY={nth[0]}{DAY_CODES[nth[1]]}")
        elif spec in ("last day", "last"):
            parts.append("BYMONTHDAY=-1")
        else:
            parts.extend(_monthday_parts(_month_days(spec) if spec else [anchor.day]))
        return _finish(parts, anchor, until, count, today)

    if m := re.fullmatch(
        rf"(yearly|annually|{_EVERY} year|{_EVERY} other year|{_EVERY} (\w+) years)(?: on (.+))?",
        core,
    ):
        default = 2 if "other" in m.group(1) else 1
        parts = ["FREQ=YEARLY", *_interval_part(_interval(m.group(2), default, "years"))]
        parts.extend(_yearly_parts((m.group(3) or "").strip(), anchor))
        return _finish(parts, anchor, until, count, today)

    raise CashError(
        f"couldn't read {raw!r} as a schedule; try e.g. " + "; ".join(SCHEDULE_EXAMPLES[:4]),
        "invalid_schedule",
    )


def _interval(word: str | None, default: int, unit: str) -> int:
    if word is None:
        return default
    n = _number(word)
    if n is None or n < 1:
        raise CashError(f"can't repeat every {word!r} {unit}", "invalid_schedule")
    return n


def _interval_part(n: int) -> list[str]:
    return [f"INTERVAL={n}"] if n > 1 else []


def _yearly_parts(spec: str, anchor: date) -> list[str]:
    if not spec:
        if (anchor.month, anchor.day) == (2, 29):
            return ["BYMONTH=2", "BYMONTHDAY=-1"]
        return [f"BYMONTH={anchor.month}", f"BYMONTHDAY={anchor.day}"]
    # the 3rd tuesday of november / the last day of february / the 1st of march
    m = re.fullmatch(r"(.+?) (?:of|in) (\w+)", spec)
    if m and m.group(2) in MONTHS:
        month = MONTHS[m.group(2)]
        if nth := _parse_nth_weekday(m.group(1)):
            return [f"BYMONTH={month}", f"BYDAY={nth[0]}{DAY_CODES[nth[1]]}"]
        if m.group(1) in ("last day", "last"):
            return [f"BYMONTH={month}", "BYMONTHDAY=-1"]
        days = _month_days(m.group(1))
        return [f"BYMONTH={month}", f"BYMONTHDAY={','.join(str(d) for d in days)}"]
    if (md := _month_day(spec.split())) is not None:
        month, day, _ = md
        try:
            date(2028, month, day)  # a leap year, so feb 29 counts
        except ValueError as exc:
            raise CashError(f"there's no {MONTH_SHORT[month]} {day}", "invalid_schedule") from exc
        if (month, day) == (2, 29):
            return ["BYMONTH=2", "BYMONTHDAY=-1"]
        return [f"BYMONTH={month}", f"BYMONTHDAY={day}"]
    raise CashError(
        f"couldn't read {spec!r} as a day of the year; try e.g. nov 20", "invalid_schedule"
    )


def _finish(
    parts: list[str], anchor: date, until: date | None, count: int | None, today: date
) -> Schedule:
    if count is not None:
        parts.append(f"COUNT={count}")
    rule = validate_rrule(";".join(parts))
    first = first_on_or_after(rule, anchor, anchor)
    if first is None:
        raise CashError("that schedule never happens", "invalid_schedule")
    if until is not None and until < first:
        raise CashError(
            f"it would end ({fmt_date(until, today)}) before it starts ({fmt_date(first, today)})",
            "invalid_schedule",
        )
    return Schedule(rule, first, until)


def _parse_raw_rrule(raw: str, today: date) -> Schedule:
    body, start_text, until_text, count = _split_clauses(raw.strip())
    rule = validate_rrule(body + (f";COUNT={count}" if count is not None else ""))
    anchor = parse_day(start_text, today=today, prefer="nearest") if start_text else today
    until = parse_day(until_text, today=today) if until_text else None
    return Schedule(rule, first_on_or_after(rule, anchor, anchor) or anchor, until)


def first_on_or_after(rule: str, dtstart: date, day: date) -> date | None:
    hit = build_rule(rule, dtstart).after(datetime(day.year, day.month, day.day), inc=True)
    return hit.date() if hit else None


def next_dates(
    rule: str | None, dtstart: date, until: date | None, after: date, n: int
) -> list[date]:
    """The next n nominal occurrences on or after `after` (before any weekend move)."""
    if rule is None:
        return [dtstart] if dtstart >= after else []
    r = build_rule(rule, dtstart)
    out: list[date] = []
    cursor, inc = datetime(after.year, after.month, after.day), True
    while len(out) < n:
        hit = r.after(cursor, inc=inc)
        if hit is None or (until is not None and hit.date() > until):
            break
        out.append(hit.date())
        cursor, inc = hit, False
    return out


# ── Schedules: RRULE -> English ───────────────────────────────────────────────


def _rule_parts(rule: str) -> dict[str, str]:
    out = {}
    for part in rule.upper().removeprefix("RRULE:").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


def _byday(v: str) -> list[tuple[int | None, int]]:
    out = []
    for item in v.split(","):
        m = re.fullmatch(r"([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)", item)
        if m:
            out.append((int(m.group(1)) if m.group(1) else None, DAY_CODES.index(m.group(2))))
    return out


def _days_text(days: list[int]) -> str:
    if days == [0, 1, 2, 3, 4]:
        return "weekdays"
    if days == [5, 6]:
        return "weekends"
    return join([DAY_SHORT[d] for d in days])


def _monthdays_text(v: str, setpos: str | None) -> str:
    days = [int(x) for x in v.split(",")]
    if setpos == "-1" and len(days) > 1 and days == list(range(days[0], days[-1] + 1)):
        return f"the {ordinal(days[-1])}"  # 28,29,30 + BYSETPOS=-1: the 30th, or the last day
    return "the " + join([("last day" if d == -1 else ordinal(d)) for d in days])


_KNOWN_PARTS = {"FREQ", "INTERVAL", "COUNT", "BYDAY", "BYMONTHDAY", "BYMONTH", "BYSETPOS", "WKST"}


def describe(rule: str | None, dtstart: date, until: date | None = None) -> str:
    """'Monthly on the 1st', 'Every 2 weeks on Fri', 'Once on Oct 15, 2026'.

    Anything this can't phrase comes back as the raw rule, so nothing is ever misdescribed.
    """
    if rule is None:
        return f"Once on {fmt_date(dtstart)}"
    p = _rule_parts(rule)
    unit = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year"}.get(
        p.get("FREQ", "")
    )
    if unit is None or set(p) - _KNOWN_PARTS:
        return rule
    interval = int(p.get("INTERVAL") or 1)
    if interval == 1:
        head = {"day": "Daily", "week": "Weekly", "month": "Monthly", "year": "Yearly"}[unit]
    else:
        head = f"Every {interval} {unit}s"
    byday = _byday(p["BYDAY"]) if "BYDAY" in p else []
    nth = all(n is not None for n, _ in byday) if byday else False
    on = ""
    if unit == "week":
        on = f" on {_days_text(sorted({d for _, d in byday}) if byday else [dtstart.weekday()])}"
    elif unit == "month":
        if "BYMONTHDAY" in p:
            on = " on " + _monthdays_text(p["BYMONTHDAY"], p.get("BYSETPOS"))
        elif nth:
            on = " on " + join([f"the {ordinal(n or 0)} {DAY_LONG[d]}" for n, d in byday])
        elif not byday:
            on = f" on the {ordinal(dtstart.day)}"
        else:
            return rule
    elif unit == "year":
        months = [int(x) for x in p["BYMONTH"].split(",")] if "BYMONTH" in p else [dtstart.month]
        mnames = join([MONTH_SHORT[m] for m in months])
        if "BYMONTHDAY" in p:
            days = [int(d) for d in p["BYMONTHDAY"].split(",")]
            if days == [-1]:
                on = f" on the last day of {mnames}"
            elif len(months) == 1 and len(days) == 1:
                on = f" on {mnames} {days[0]}"
            else:
                on = f" on the {join([ordinal(d) for d in days])} of {mnames}"
        elif nth:
            on = f" on {join([f'the {ordinal(n or 0)} {DAY_LONG[d]}' for n, d in byday])}"
            on += f" of {mnames}"
        elif not byday:
            on = f" on {mnames} {dtstart.day}"
        else:
            return rule
    text = head + on
    if count := p.get("COUNT"):
        text += f", {count} time{'s' if count != '1' else ''}"
    return text


def weekend_note(weekend: str) -> str:
    return {"next": "weekends → Mon", "previous": "weekends → Fri"}.get(str(weekend), "")
