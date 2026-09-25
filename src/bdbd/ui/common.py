"""Small pieces the app and ask.py share: next dates, flow descriptions, wording."""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

from rich.text import Text

from bdbd.budget import Start
from bdbd.core.models import Flow
from bdbd.core.recurrence import occurrences
from bdbd.ui.theme import DOT, FAINT, money
from bdbd.words import describe, fmt_date

DEBT_MARK = "◆"
LOOKAHEAD_DAYS = 800


def next_date(flow: Flow, today: date) -> date | None:
    """When the flow next actually happens (after any weekend move), from today on."""
    hits = occurrences(
        flow.rrule,
        flow.dtstart,
        flow.until,
        today,
        today + timedelta(days=LOOKAHEAD_DAYS),
        flow.weekend,
    )
    return hits[0] if hits else None


def upcoming_dates(flow: Flow, today: date, n: int) -> list[date]:
    hits = occurrences(
        flow.rrule,
        flow.dtstart,
        flow.until,
        today,
        today + timedelta(days=LOOKAHEAD_DAYS),
        flow.weekend,
    )
    return hits[:n]


def schedule_text(flow: Flow, *, weekend: bool = True) -> Text:
    """'Monthly on the 1st' plus a faint '→Mon' when weekend dates move."""
    text = Text(describe(flow.rrule, flow.dtstart, flow.until))
    if weekend and str(flow.weekend) == "next":
        text.append(" →Mon", style=FAINT)
    elif weekend and str(flow.weekend) == "previous":
        text.append(" →Fri", style=FAINT)
    if flow.until is not None and flow.rrule is not None:
        text.append(f" until {fmt_date(flow.until)}", style=FAINT)
    return text


def start_note(start: Start, today: date, items: Sequence = ()) -> Text:
    """Where a projection's starting balance came from, in a few faint words.

    The balance is always before its day's scheduled items, so a balance recorded today on a
    day with `items` (today's ledger entries) says so: 'recorded today · before today's
    Paycheck +$2,650.00' (the number you typed may have had them in it already).
    """
    rec = start.recorded
    if start.source == "given":
        return Text("as given", style=FAINT)
    if start.source == "recorded":
        when = "today" if start.as_of == today else fmt_date(start.as_of, today)
        text = Text(f"recorded {when}", style=FAINT)
        due = [e for e in items if e.date == start.as_of and e.kind != "lifestyle"]
        if start.as_of == today and len(due) == 1:
            e = due[0]
            text.append(f" {DOT} before today's {e.name} ", style=FAINT)
            text.append(money(e.delta_cents, sign=True), style=FAINT)
        elif start.as_of == today and due:
            text.append(f" {DOT} before today's {len(due)} items", style=FAINT)
        return text
    if start.source == "carried" and rec is not None:
        return Text(
            f"est. from {money(rec.amount_cents)} on {fmt_date(rec.as_of, today)}", style=FAINT
        )
    return Text("no balance recorded", style=FAINT)


def closing_balances(items: list, daily: list[tuple[date, int]]) -> list[int]:
    """The balance to show beside each item: after it, or at the end of its day if it's the
    day's last item (so lists agree with the calendar and the low point)."""
    end_of_day = dict(daily)
    shown = []
    for i, e in enumerate(items):
        last = i + 1 == len(items) or items[i + 1].date != e.date
        shown.append(
            end_of_day.get(e.date, e.balance_after_cents) if last else e.balance_after_cents
        )
    return shown


def humanize(message: str) -> str:
    """Core messages speak ISO dates and bare decimals; say them the way the views do."""
    import re

    s = re.sub(
        r"(\d{4})-(\d{2})-(\d{2})",
        lambda m: fmt_date(date(int(m[1]), int(m[2]), int(m[3]))),
        message,
    )
    s = re.sub(r"(?<![\d,$])(\d+)\.(\d{2})\b", lambda m: money(int(m[1]) * 100 + int(m[2])), s)
    return s.replace(" -> ", " → ")


def tilde(path: Path) -> str:
    """A path the way people write it: the home directory as ~."""
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home) :] if text == home or text.startswith(home + os.sep) else text
