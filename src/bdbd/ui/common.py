"""Pieces several views share: flow descriptions, next dates, footers."""

from __future__ import annotations

from datetime import date, timedelta

from rich.console import Console
from rich.text import Text

from bdbd.budget import Budget, Start
from bdbd.core.models import Flow, Kind
from bdbd.core.recurrence import occurrences
from bdbd.ui.theme import ACCENT, DOT, FAINT, PURPLE, money, note
from bdbd.words import describe, fmt_date, relative

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


def flow_name(flow: Flow, *, dim: bool = False) -> Text:
    text = Text(flow.name, style="dim" if dim else "")
    if flow.debt is not None:
        text.append(f" {DEBT_MARK}", style=PURPLE)
    return text


def signed(cents: int, kind: Kind | str) -> Text:
    """A flow amount with its direction: +$2,650.00 (green) or -$412.37."""
    from bdbd.ui.theme import GREEN

    if str(kind) == "income":
        return Text(money(cents, sign=True), style=GREEN)
    return Text(money(-cents))


def when_text(d: date | None, today: date) -> Text:
    if d is None:
        return Text("—", style=FAINT)
    return Text.assemble(fmt_date(d, today, weekday=True), (f"  {relative(d, today)}", FAINT))


def start_note(start: Start, today: date) -> Text:
    """Where a projection's starting balance came from, in a few faint words."""
    rec = start.recorded
    if start.source == "given":
        return Text("as given", style=FAINT)
    if start.source == "recorded":
        when = "today" if start.as_of == today else fmt_date(start.as_of, today)
        return Text(f"recorded {when}", style=FAINT)
    if start.source == "carried" and rec is not None:
        return Text(
            f"est. from {money(rec.amount_cents)} on {fmt_date(rec.as_of, today)}", style=FAINT
        )
    return Text("no balance recorded", style=FAINT)


def footer(
    console: Console,
    budget: Budget,
    *,
    warnings: list[str] | None = None,
    hints: tuple[str, ...] = (),
) -> None:
    """Warnings, what the automatic tidy-up did, and a line of suggestions."""
    from bdbd.ui.theme import AMBER, hint

    shown = False
    for w in dict.fromkeys(warnings or []):
        console.print(Text.assemble(("! ", f"bold {AMBER}"), (w, "")))
        shown = True
    for action in budget.tidied:
        note(console, Text(f"Tidied up: {humanize(action)}", style=FAINT), glyph="↺")
        shown = True
    budget.tidied = []
    if hints:
        if shown:
            console.print()
        console.print(hint(*hints))


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


def stale_warning(start: Start, today: date) -> str | None:
    rec = start.recorded
    if start.source == "carried" and rec is not None and (today - rec.as_of).days > 14:
        return (
            f"your balance was last recorded {relative(rec.as_of, today)}; "
            "update it with `bdbd balance AMOUNT` for sharper numbers"
        )
    return None


def meta(*parts: str) -> Text:
    return Text(f" {DOT} ".join(p for p in parts if p), style=FAINT)


def accent(text: str) -> Text:
    return Text(text, style=f"bold {ACCENT}")
