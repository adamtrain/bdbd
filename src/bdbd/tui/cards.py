"""A flow's details as a card: the Budget view's right-hand panel and the flow card (enter on a
flow anywhere) draw the same `Card` from the same `flow_lines`, so they always agree.

    card = Card()
    card.show(flow_lines(session, flow))
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Literal

from rich.text import Text
from textual.widget import Widget

from bdbd.core.models import Flow, Kind, Weekend
from bdbd.tui.text import NEVER_PAID, event_amount, event_words, terms_words
from bdbd.tui.widgets import signed
from bdbd.ui.common import DEBT_MARK
from bdbd.ui.theme import (
    AMBER,
    DOT,
    FAINT,
    FOREGROUND,
    GREEN,
    PURPLE,
    cents_of,
    money,
    muted,
    pct,
)
from bdbd.words import describe, fmt_date, fmt_month, relative

if TYPE_CHECKING:
    from bdbd.tui.session import Session


@dataclass(frozen=True)
class Pair:
    """One line of a Card: a label and a value.

    Money (align="right") sits flush with the card's right edge, so every amount lines up;
    text (align="left") starts just after the labels and may run over several lines ("\\n").
    """

    label: str | Text
    value: Text
    align: Literal["left", "right"] = "right"


CardLine = Pair | Text | None  # a Text is a heading; None a blank line


class Card(Widget):
    """A card of FAINT labels, money flush right, headings and blank lines."""

    DEFAULT_CSS = """
    Card {
        height: auto;
        width: 1fr;
        max-width: 56;
    }
    """

    GAP = 2

    def __init__(self, *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self._lines: list[CardLine] = []

    def show(self, lines: Iterable[CardLine]) -> None:
        self._lines = list(lines)
        self.refresh(layout=True)

    def render(self) -> Text:
        width = self.size.width or 48
        texts = [_label(p.label) for p in self._lines if isinstance(p, Pair) and p.align == "left"]
        lw = max((t.cell_len for t in texts), default=0)
        out: list[Text] = []
        for line in self._lines:
            if line is None:
                out.append(Text())
            elif isinstance(line, Text):
                out.extend(_fit(part, width) for part in line.split("\n"))
            else:
                out.extend(self._pair(line, lw, width))
        return Text("\n").join(out)

    def _pair(self, p: Pair, lw: int, width: int) -> list[Text]:
        label = _label(p.label)
        if p.align == "left":  # text wraps under itself, so a narrow card still says it all
            label.truncate(lw, overflow="ellipsis", pad=True)
            head = Text.assemble(label, " " * self.GAP)
            indent = " " * head.cell_len
            room = max(width - head.cell_len, 8)
            parts: list[Text] = []
            for part in p.value.split("\n") or [Text()]:
                parts += self._wrap(part, room)
            first, *rest = parts or [Text()]
            lines = [Text.assemble(head, first)]
            lines += [Text.assemble(indent, more) for more in rest]
            return [_fit(line, width) for line in lines]
        label.truncate(max(width - p.value.cell_len - self.GAP, 1), overflow="ellipsis")
        gap = " " * max(width - label.cell_len - p.value.cell_len, self.GAP)
        return [_fit(Text.assemble(label, gap, p.value), width)]

    def _wrap(self, text: Text, room: int) -> list[Text]:
        """Lines of at most `room` cells, broken at a ' · ' when there is one (so 'in 5
        months' stays together), else between words."""
        sep = f" {DOT} "
        lines: list[Text] = []
        while text.cell_len > room:
            cut = text.plain.rfind(sep, 1, room + len(sep))
            if cut <= 0:
                return [*lines, *text.wrap(self.app.console, room)]
            lines.append(text[:cut])
            text = text[cut + len(sep) :]
        return [*lines, text]


def _label(label: str | Text) -> Text:
    return label.copy() if isinstance(label, Text) else Text(label, style=FAINT)


def _fit(text: Text, width: int) -> Text:
    text = text.copy()
    text.truncate(max(width, 1), overflow="ellipsis")
    return text


def labelled(label: str, note: str) -> Text:
    """'Owed today': a FAINT label with a dimmer note."""
    return Text.assemble((label, FAINT), (f" {note}", muted(FAINT, 0.7)))


# ── A flow ────────────────────────────────────────────────────────────────────


def flow_lines(s: Session, flow: Flow, *, dates: int = 3) -> list[CardLine]:
    """What a card says about a stored flow: money, schedule, dates and its loan.

    The same numbers as `bdbd show NAME` (never the what-if). `dates` is how many of the next
    dates to list (the first one also says how far away it is).
    """
    today = s.today
    info = s.details(flow)
    sign = 1 if flow.kind == Kind.INCOME else -1
    lines: list[CardLine] = [status(flow), None]
    lines.append(Pair("Amount", flow_money(sign * flow.amount_cents, flow)))
    if info.monthly:  # each only when it isn't the amount itself (a monthly or yearly flow)
        if info.monthly != flow.amount_cents:
            lines.append(Pair("Per month", flow_money(sign * info.monthly, flow)))
        if (yearly := s.yearly(flow)) != flow.amount_cents:
            lines.append(Pair("Per year", flow_money(sign * yearly, flow)))
    lines.append(None)
    when = Text(describe(flow.rrule, flow.dtstart, flow.until))
    if flow.weekend == Weekend.NEXT:
        when.append("\nweekend dates move to Monday", style=FAINT)
    elif flow.weekend == Weekend.PREVIOUS:
        when.append("\nweekend dates move to Friday", style=FAINT)
    if flow.rrule is not None or not info.upcoming:  # a one-off ahead just says On
        lines.append(Pair("Schedule" if flow.rrule else "When", when, align="left"))
    starts_next = bool(info.upcoming) and info.upcoming[0] == flow.dtstart
    if flow.rrule is not None and not starts_next:  # 'Starts Oct 5' would repeat 'Next Oct 5'
        label = "Started" if flow.dtstart <= today else "Starts"
        lines.append(Pair(label, Text(fmt_date(flow.dtstart, today)), align="left"))
        if flow.until is not None:
            lines.append(Pair("Ends", Text(fmt_date(flow.until, today)), align="left"))
    if info.upcoming:
        first, *more = info.upcoming
        shown = Text.assemble(
            fmt_date(first, today, weekday=True), (f" {DOT} {relative(first, today)}", FAINT)
        )
        later = more[: max(dates - 1, 0)]
        for i in range(0, len(later), 2):  # two to a line
            pair = f" {DOT} ".join(fmt_date(d, today, weekday=True) for d in later[i : i + 2])
            shown.append(f"\n{pair}", style=FAINT)
        lines.append(Pair("Next" if flow.rrule else "On", shown, align="left"))
    elif flow.active:
        lines.append(Pair("Next", Text("nothing ahead", style=FAINT), align="left"))
    if flow.tags:
        lines.append(Pair("Tags", Text(", ".join(flow.tags)), align="left"))
    if flow.notes:
        lines.append(Pair("Notes", Text(flow.notes), align="left"))
    if flow.debt is not None and info.debt is not None:
        lines += [None, *debt_lines(s, flow, info.debt)]
    return lines


def status(flow: Flow) -> Text:
    """'Money in', 'Money out · a debt ◆', 'Money out · paused' (and what pausing means)."""
    if flow.kind == Kind.INCOME:
        text = Text("Money in", style=GREEN)
    else:
        text = Text("Money out", style=FOREGROUND)
    if flow.debt is not None:
        text.append(f" {DOT} a debt ", style=FAINT)
        text.append(DEBT_MARK, style=PURPLE)
    if not flow.active:
        text.append(f" {DOT} ", style=FAINT)
        text.append("paused", style=AMBER)
        text.append("\nleft out of every projection", style=FAINT)
    return text


def flow_money(cents: int, flow: Flow) -> Text:
    """A flow's money, green with + when it comes in; faint while the flow is paused."""
    text = signed(cents)
    if not flow.active:
        text.stylize(FAINT)
    return text


def debt_lines(s: Session, flow: Flow, outlook: dict) -> list[CardLine]:
    """The loan: owed today, the rate and how interest works, when it's paid off, events.

    Interest is the app's one measure (from today), so owed + interest = all still to pay.
    """
    today = s.today
    d = flow.debt
    assert d is not None
    owed = cents_of(outlook["balance_at_as_of"])
    lines: list[CardLine] = [
        Text.assemble(("Loan ", f"bold {PURPLE}"), (DEBT_MARK, PURPLE)),
        Pair(labelled("Owed", "today"), Text(money(owed))),
        Pair("Rate", Text(pct(d.annual_rate))),
        Pair("", Text(terms_words(d, lines=True), style=FAINT), align="left"),
    ]
    if d.original_principal_cents is not None:
        lines.append(Pair("Borrowed", Text(money(d.original_principal_cents))))
    payoff = outlook.get("payoff_date")
    if payoff and date.fromisoformat(payoff) <= today:
        done = fmt_date(date.fromisoformat(payoff), today, weekday=True)
        lines.append(Pair("Paid off", Text(f"✓ {done}", style=GREEN), align="left"))
    elif payoff:
        day = date.fromisoformat(payoff)
        interest = s.interest_to_go(flow.id, baseline=True) or 0
        lines += [
            Pair("Paid off", Text.assemble(fmt_month(day), (f"\n{relative(day, today)}", FAINT)),
                 align="left"),
            Pair(labelled("Payments", "to go"), Text(str(int(outlook["payments_remaining"])))),
            Pair(labelled("Interest", "to go"), Text(money(interest), style=PURPLE)),
            Pair(labelled("In all", "still to pay"), Text(money(owed + interest))),
        ]  # fmt: skip
    else:
        lines.append(Pair("Paid off", Text(NEVER_PAID, style=AMBER), align="left"))
        grows = s.growth(flow.id, baseline=True)
        if grows:
            lines.append(Pair(labelled("Grows by", "a year"), Text(money(grows), style=AMBER)))
    if d.events:
        lines += [None, Text("Recorded", style="bold")]
        for ev in sorted(d.events, key=lambda e: e.date):
            amount = event_amount(ev)
            value = Text(amount) if amount else Text("✓", style=GREEN)
            lines.append(Pair(labelled(event_words(ev), fmt_date(ev.date, today)), value))
    return lines
