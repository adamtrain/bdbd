"""The Calendar (2): a month at a glance, and any day in it.

A 7-column month grid (Mon first) made of one small widget per day, and a day panel answering
"how much will I have on this day?" with the numbers `bdbd project --until DAY` gives: the
balance at the end of the day, what's spare before the next income, the day's items with the
balance after each, and the rest of the month in a few lines. Days and balances come from
one simulation shared by every month shown (what `bdbd cal` shows); past days show what was
scheduled, without balances.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from bdbd import ask
from bdbd.budget import Start
from bdbd.core import engine
from bdbd.core.dates import add_months, month_end
from bdbd.core.errors import CashError
from bdbd.core.models import FlowKey, Kind
from bdbd.core.queries.project import SpareCalculator
from bdbd.core.recurrence import occurrences
from bdbd.tui.text import DEBT_KINDS, bold_balance, ledger_key, what_if_only
from bdbd.tui.widgets import (
    Column,
    Item,
    Panel,
    Row,
    RowList,
    View,
    balance,
    bdbd,
    signed,
)
from bdbd.ui.common import closing_balances
from bdbd.ui.theme import (
    ACCENT,
    AMBER,
    DARK,
    DOT,
    FAINT,
    FOREGROUND,
    GREEN,
    MINUS,
    PURPLE,
    balance_color,
    cents_of,
    compact,
    currency,
    money,
    money_short,
    muted,
)
from bdbd.words import DAY_SHORT, MONTH_LONG, MONTH_SHORT, MONTHS, fmt_date, parse_day, relative

if TYPE_CHECKING:
    from bdbd.tui.session import Session

YEARS_BACK, YEARS_AHEAD = 10, 30  # how far the cursor goes
OUTSIDE = muted(FAINT, 0.55)  # day numbers of the neighbouring months
WEEKEND = muted(FAINT, 0.7)
BAR = "▌"


# ── The month as data ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Entry:
    """Something scheduled on a day, with the balance after it (None for past days)."""

    name: str
    cents: int  # signed: money in is positive
    kind: str
    key: FlowKey | None = None  # the stored flow's id; what-if items have string keys
    after: int | None = None
    whatif: bool = False  # only in the what-if

    @property
    def debt(self) -> bool:
        return self.kind in DEBT_KINDS


@dataclass(frozen=True)
class CalDay:
    date: date
    entries: tuple[Entry, ...] = ()
    balance: int | None = None  # end of day, from today on, when the balance is known

    @property
    def net(self) -> int:
        return sum(e.cents for e in self.entries)


@dataclass(frozen=True)
class Month:
    """One month as the calendar shows it (`bdbd cal`), plus what the day panel needs."""

    first: date
    days: list[CalDay]
    start: Start
    daily: dict[date, int] = field(default_factory=dict)  # end-of-day balances, today on
    everyday: dict[date, int] = field(default_factory=dict)  # everyday spending charged
    ledger: frozenset[tuple] = frozenset()  # (date, key, kind, delta) of every item ahead

    def day(self, d: date) -> CalDay | None:
        if d.year == self.first.year and d.month == self.first.month:
            return self.days[d.day - 1]
        return None


def month_of(session: Session, first: date, *, baseline: bool = False) -> Month:
    """The month starting on `first`, with the what-if when it's on (not for baseline)."""
    lensed = session.lens_on and not baseline
    return session.cached(
        ("calendar_view.month", first, lensed), lambda: _build(session, first, lensed)
    )


RUN_MARGIN_MONTHS = 6  # a longer run is made this far past the month asked for


def _run_until(session: Session, last: date, lensed: bool) -> engine.SimResult:
    """One simulation from today, long enough for `last`, shared by every month shown.

    Paging ahead extends it (with some margin) instead of running from today again for each
    month, and only the newest run is kept, so far months stay quick and memory stays flat.
    """
    runs: dict[bool, engine.SimResult] = session.cached(("calendar_view.runs",), dict)
    run = runs.get(lensed)
    if run is None or run.until < last:
        today = session.today
        until = max(last, run.until if run else today)
        until = month_end(add_months(until, RUN_MARGIN_MONTHS))
        model = session.model(baseline=not lensed)
        run = engine.run(
            model,
            as_of=today,
            until=until,
            starting_balance_cents=session.start().cents,
            weekly_spend_cents=session.weekly(model),
        )
        runs[lensed] = run
    return run


def _build(session: Session, first: date, lensed: bool) -> Month:
    """A month like `bdbd cal`: past days show what the stored budget scheduled (the past
    isn't simulated), days from today on come from the shared run, with the balance after
    each item and at the end of each day."""
    today, last = session.today, month_end(first)
    start = session.start()
    entries: dict[date, list[Entry]] = {first + timedelta(days=i): [] for i in range(last.day)}
    end_past = min(last, today - timedelta(days=1))
    if end_past >= first:
        for f in session.flows(include_inactive=False):
            sign = 1 if f.kind == Kind.INCOME else -1
            kind = "debt_payment" if f.debt is not None else str(f.kind)
            for d in occurrences(f.rrule, f.dtstart, f.until, first, end_past, f.weekend):
                entries[d].append(Entry(f.name, sign * f.amount_cents, kind, f.id))
    daily: dict[date, int] = {}
    everyday: dict[date, int] = defaultdict(int)
    keys: frozenset[tuple] = frozenset()
    if last >= today:
        run = _run_until(session, last, lensed)
        month = [e for e in run.ledger if first <= e.date <= last]
        items = [e for e in month if e.kind != "lifestyle"]
        for e in month:
            if e.kind == "lifestyle":
                everyday[e.date] -= e.delta_cents
        base = month_of(session, first, baseline=True).ledger if lensed else None
        for e, after in zip(items, closing_balances(items, run.daily), strict=True):
            entries[e.date].append(
                Entry(
                    e.name,
                    e.delta_cents,
                    e.kind,
                    e.key,
                    after if start.known else None,
                    base is not None and what_if_only(e, base),
                )
            )
        eve = first - timedelta(days=1)  # the footer starts from the end of the day before
        daily = {d: bal for d, bal in run.daily if eve <= d <= last}
        keys = frozenset(ledger_key(e) for e in items)
    days = [
        CalDay(d, tuple(es), daily.get(d) if start.known else None)
        for d, es in sorted(entries.items())
    ]
    return Month(first, days, start, daily, dict(everyday), keys)


@dataclass(frozen=True)
class Answer:
    """What `bdbd project --until DAY` says about the end of a day."""

    balance: int
    spare: int | None  # None: no income ahead to count to
    income: tuple[str, date, int] | None  # the next income: name, date, amount


def answer(session: Session, day: date, *, baseline: bool = False) -> Answer | None:
    """The end of `day` (None for past days and when bdbd doesn't know the balance)."""
    info = month_of(session, day.replace(day=1), baseline=baseline).day(day)
    if day < session.today or info is None or info.balance is None:
        return None
    lensed = session.lens_on and not baseline
    bal = info.balance

    def compute() -> Answer:
        model = session.model(baseline=not lensed)
        weekly = session.weekly(model)
        spare = SpareCalculator(model, session.today, day, weekly).compute(day, bal)
        nxt = spare["next_income"]
        income = None
        if nxt is not None:
            income = (nxt["name"], date.fromisoformat(nxt["date"]), cents_of(nxt["amount"]))
        cents = spare["spare_balance"]
        return Answer(bal, cents_of(cents) if cents is not None else None, income)

    return session.cached(("calendar_view.answer", day, lensed), compute)


@dataclass(frozen=True)
class Totals:
    """The month footer: money in and out from `since` on, the lowest day and the end."""

    since: date | None  # the first day counted (today, or the 1st); None: the month has passed
    money_in: int
    money_out: int  # positive
    everyday: int  # positive
    starts: int | None  # the balance before `since`
    lowest: tuple[date, int] | None
    ends: tuple[date, int] | None


def totals(month: Month, today: date) -> Totals:
    """Rest of the month, like `bdbd cal`'s footer (a past month: what was scheduled in it)."""
    ahead = [d for d in month.days if d.date >= today]
    counted = ahead or month.days
    money_in = sum(e.cents for d in counted for e in d.entries if e.cents > 0)
    money_out = -sum(e.cents for d in counted for e in d.entries if e.cents < 0)
    if not ahead:
        return Totals(None, money_in, money_out, 0, None, None, None)
    since = ahead[0].date
    everyday = sum(month.everyday.get(d.date, 0) for d in ahead)
    known = [(d.date, d.balance) for d in ahead if d.balance is not None]
    starts = None
    if month.start.known:
        before = since - timedelta(days=1)
        starts = month.start.cents if since == today else month.daily.get(before)
    lowest = min(known, key=lambda t: (t[1], t[0])) if known else None
    ends = known[-1] if known else None
    return Totals(since, money_in, money_out, everyday, starts, lowest, ends)


# ── Words and numbers ─────────────────────────────────────────────────────────


def _whole(cents: int) -> str:
    """'$1,595' or '-$1,234' (a real minus): a balance in whole units for a cell."""
    v = Decimal(abs(cents)).scaleb(-2).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return (MINUS if cents < 0 else "") + f"{currency()}{v:,}"


def _net(cents: int) -> str:
    """'+2,650', '-2,150', '-39' (a real minus): a day's net in whole units."""
    v = Decimal(abs(cents)).scaleb(-2).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return ("+" if cents > 0 else MINUS if cents < 0 else "") + f"{v:,}"


def _net_compact(cents: int) -> str:
    """'+2.7k', '-2.2k': a net for narrow cells."""
    body = compact(abs(cents)).replace(currency(), "", 1)
    return ("+" if cents > 0 else MINUS if cents < 0 else "") + body


def _month_name(first: date, today: date) -> str:
    name = MONTH_LONG[first.month]
    return name if first.year == today.year else f"{name} {first.year}"


def _entry_color(e: Entry, past: bool) -> str:
    if past:
        return FAINT
    if e.debt:
        return PURPLE
    return GREEN if e.cents > 0 else FOREGROUND


# ── A day in the grid ─────────────────────────────────────────────────────────


def cell_lines(
    day: date,
    info: CalDay | None,
    width: int,
    height: int,
    *,
    today: date,
    low: int,
    selected: bool = False,
    focused: bool = False,
) -> list[Text]:
    """A day cell's lines: number and net, then item names, then the end-of-day balance.

    It adapts to its size: fewer names when short (the last becomes '+2 more'), names cut with
    an ellipsis when narrow, and compact amounts when even those don't fit. `info` None is a
    day of the neighbouring month (just a faint number).
    """
    if width < 2 or height < 1:
        return [Text() for _ in range(max(height, 0))]
    bar = Text(BAR if selected else " ", style=ACCENT if focused else muted(ACCENT, 0.5))
    inner = width - 1
    lines: list[Text] = []

    def add(text: Text) -> None:
        text.truncate(inner, overflow="ellipsis")
        lines.append(Text.assemble(bar, text))

    num = f"{day.day:>2}"
    if info is None:
        add(Text(num, style=OUTSIDE))
        return lines + [Text.assemble(bar) for _ in range(height - 1)]
    past = day < today
    if day == today:
        head = Text(f"{num} ", style=f"bold {DARK} on {ACCENT}")
    elif past:
        head = Text(num, style=FAINT)
    elif day.weekday() >= 5:
        head = Text(num, style=WEEKEND)
    else:
        head = Text(num, style=f"bold {FOREGROUND}")
    if info.entries:
        net = info.net
        color = FAINT if past else GREEN if net > 0 else FOREGROUND
        room = inner - head.cell_len - 1
        for text in (_net(net), _net_compact(net)):
            if len(text) <= room:
                head = Text.assemble(head, " " * (inner - head.cell_len - len(text)), (text, color))
                break
    add(head)
    rows = height - 1
    show_balance = info.balance is not None and rows >= 1
    if show_balance:
        rows -= 1
    entries = info.entries
    if rows > 0 and entries:
        if len(entries) <= rows:
            shown, hidden = list(entries), 0
        elif rows == 1:
            shown, hidden = [], len(entries)
        else:
            shown, hidden = list(entries[: rows - 1]), len(entries) - rows + 1
        for e in shown:
            name = Text(e.name, style=_entry_color(e, past))
            if e.whatif:
                name = Text.assemble(("↳", ACCENT), name)
            add(name)
        if hidden:
            words = f"+{hidden} more" if shown else f"{hidden} items"
            add(Text(words, style=FAINT))
    while len(lines) < height - (1 if show_balance else 0):
        add(Text())
    if show_balance and info.balance is not None:
        bal = info.balance
        text = _whole(bal) if len(_whole(bal)) <= inner else compact(bal)
        add(Text(text.rjust(inner), style=balance_color(bal, low)))
    return lines[:height]


class DayCell(Widget):
    """One day of the month grid. Clicking it selects the day (a double click opens its items)."""

    DEFAULT_CSS = """
    DayCell {
        width: 1fr;
        height: 1fr;
        text-wrap: nowrap;
        text-overflow: clip;
    }
    """

    class Pressed(Message):
        """A click on a day (`twice` for a double click)."""

        def __init__(self, day: date, twice: bool) -> None:
            super().__init__()
            self.day = day
            self.twice = twice

    def __init__(self) -> None:
        super().__init__()
        self.day = date.min
        self.info: CalDay | None = None
        self.today = date.min
        self.low = 0

    def show(self, day: date, info: CalDay | None, *, today: date, low: int) -> None:
        self.day, self.info, self.today, self.low = day, info, today, low
        self.set_class(info is None, "-outside")
        self.refresh()

    def render(self) -> Text:
        width, height = self.size
        grid = self.parent
        focused = isinstance(grid, Widget) and grid.has_focus
        lines = cell_lines(
            self.day,
            self.info,
            width,
            height,
            today=self.today,
            low=self.low,
            selected=self.has_class("-selected"),
            focused=focused,
        )
        return Text("\n").join(lines)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(self.Pressed(self.day, event.chain >= 2))


class MonthGrid(Widget, can_focus=True):
    """Six weeks of DayCells (the rows a month doesn't need are hidden). It holds the focus the
    view's day keys act on."""

    DEFAULT_CSS = """
    MonthGrid {
        layout: grid;
        grid-size: 7;
        grid-columns: 1fr;
        grid-gutter: 1 1;
        height: 1fr;
        & > DayCell { background: $surface; }
        & > DayCell.-alt { background: $panel; }
        & > DayCell.-outside { background: transparent; }
        & > DayCell:hover { background: $primary 9%; }
        & > DayCell.-selected { background: $primary 14%; }
        &:focus > DayCell.-selected { background: $primary 24%; }
        &.-tight { grid-gutter: 0 1; }
    }
    """

    def compose(self) -> ComposeResult:
        for _ in range(42):
            yield DayCell()

    def on_focus(self) -> None:
        self._repaint_selected()

    def on_blur(self) -> None:
        self._repaint_selected()

    def _repaint_selected(self) -> None:
        for cell in self.query(".-selected"):
            cell.refresh()


class WeekdayHeader(Widget):
    """Mon … Sun above the grid, in the same columns."""

    DEFAULT_CSS = """
    WeekdayHeader {
        layout: grid;
        grid-size: 7;
        grid-columns: 1fr;
        grid-gutter: 0 1;
        height: 1;
        & > Static { height: 1; padding-left: 1; }
    }
    """

    def compose(self) -> ComposeResult:
        for i, name in enumerate(DAY_SHORT):
            yield Static(Text(name, style=FAINT if i < 5 else WEEKEND))


# ── The day panel's figures ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Fig:
    """A figures row: a faint label, an optional faint note, and a value at the right edge."""

    label: str
    value: Text
    note: str = ""


class Figures(Widget):
    """Rows of label · note · value with every value right-aligned at the right edge, and plain
    lines (Text) in between. Labels and notes give way (with an ellipsis) before values do."""

    DEFAULT_CSS = """
    Figures {
        height: auto;
        width: 1fr;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._rows: list[Fig | Text] = []

    def set_rows(self, rows: list[Fig | Text]) -> None:
        self._rows = rows
        self.refresh(layout=True)

    def render(self) -> Text:
        width = self.size.width or 40
        label_w = max((len(r.label) for r in self._rows if isinstance(r, Fig)), default=0)
        lines: list[Text] = []
        for row in self._rows:
            if isinstance(row, Text):
                line = row.copy()
                line.truncate(width, overflow="ellipsis")
                if row.justify == "right":
                    line = Text.assemble(" " * (width - line.cell_len), line)
                lines.append(line)
                continue
            left = Text(row.label.ljust(label_w), style=FAINT)
            if row.note:
                left.append(f"  {row.note}", style=FAINT)
            left.truncate(max(0, width - row.value.cell_len - 2), overflow="ellipsis")
            gap = " " * max(1, width - left.cell_len - row.value.cell_len)
            lines.append(Text.assemble(left, gap, row.value))
        return Text("\n").join(lines)


# ── The view ──────────────────────────────────────────────────────────────────


class CalendarView(View):
    """A month at a glance, and any day in it."""

    title = "Calendar"

    DEFAULT_CSS = """
    CalendarView {
        layout: horizontal;
        & #month-panel { width: 1fr; height: 1fr; padding: 0 1; }
        & WeekdayHeader { margin-bottom: 1; }
        & #day-panel { width: 38; height: 1fr; padding: 0; }
        & #day-body { height: 1fr; }
        & #figures-box { height: auto; }
        & #figures { margin-top: 1; }
        & Figures, .section { padding: 0 1; }
        & #items-box { height: 1fr; margin-top: 1; }
        & .section { height: 1; }
        & #items { height: 1fr; }
        & #month-box { height: auto; margin-bottom: 1; }
        &.-short WeekdayHeader { margin-bottom: 0; }
        &.-short #figures { margin-top: 0; }
        &.-short #month-box { margin-bottom: 0; }
        &.-narrow { layout: vertical; }
        &.-narrow #month-panel { padding: 0; }
        &.-narrow #day-panel { width: 1fr; height: 8; }
        &.-narrow #day-body { layout: horizontal; }
        &.-narrow #figures-box { width: 1fr; height: auto; }
        &.-narrow #figures { margin-top: 0; }
        &.-narrow #items-box { width: 1fr; margin: 0 0 0 3; }
        &.-narrow #month-box { display: none; }
    }
    """

    BINDINGS: ClassVar = [
        Binding("left,h", "move(-1)", "previous day", show=False),
        Binding("right,l", "move(1)", "next day", show=False),
        Binding("up,k", "move(-7)", "a week earlier", show=False),
        Binding("down,j", "move(7)", "a week later", show=False),
        Binding(
            "left_square_bracket",
            "month(-1)",
            "previous month",
            group=Binding.Group("month"),
        ),
        Binding(
            "right_square_bracket",
            "month(1)",
            "next month",
            group=Binding.Group("month"),
        ),
        Binding("pageup", "month(-1)", "previous month", show=False),
        Binding("pagedown", "month(1)", "next month", show=False),
        Binding("t", "today", "today", show=False),
        Binding("g", "go", "go to a date"),
        Binding("enter", "items", "the day's items", show=False),
        Binding("escape", "back", "back to the month"),
    ]

    def __init__(self, *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self.day = date.min  # the selected day (set on the first draw)
        self.month = date.min  # the 1st of the month shown
        self._want = 0  # the day of the month [ and ] try to keep

    def compose(self) -> ComposeResult:
        with Panel(id="month-panel"):
            yield WeekdayHeader()
            yield MonthGrid(id="grid")
        with Panel(id="day-panel"), Vertical(id="day-body"):
            with Vertical(id="figures-box"):
                yield Figures(id="figures")
            with Vertical(id="items-box"):
                yield Static(id="items-head", classes="section")
                yield RowList(
                    Column(flex=True, min=6),
                    Column(align="right"),
                    Column(align="right"),
                    gap=1,
                    empty="Nothing scheduled.",
                    id="items",
                )
            with Vertical(id="month-box"):
                yield Static(id="month-head", classes="section")
                yield Figures(id="month-figures")

    # drawing ---------------------------------------------------------------------------

    def refresh_view(self) -> None:
        today = self.session.today
        if self.day == date.min:
            self.day, self._want = today, today.day
        self.day = self._clamp(self.day)
        self.month = self.day.replace(day=1)
        self._draw_grid()
        self._draw_day()

    def _clamp(self, day: date) -> date:
        today = self.session.today
        lo = add_months(today, -12 * YEARS_BACK).replace(day=1)
        hi = month_end(add_months(today, 12 * YEARS_AHEAD))
        return min(max(day, lo), hi)

    @property
    def grid(self) -> MonthGrid:
        return self.query_one("#grid", MonthGrid)

    def _draw_grid(self) -> None:
        s = self.session
        today, low = s.today, s.weekly()
        month = month_of(s, self.month)
        first = self.month
        start = first - timedelta(days=first.weekday())
        weeks = (first.weekday() + month_end(first).day + 6) // 7
        for i, cell in enumerate(self.grid.query(DayCell)):
            d = start + timedelta(days=i)
            cell.display = i < weeks * 7
            if cell.display:
                cell.show(d, month.day(d), today=today, low=low)
                cell.set_class(d == self.day, "-selected")
        extra = ["balances at the end of each day" if month.start.known else ""]
        if s.lens_on:
            extra.append("with the what-if")
        title = f"{MONTH_LONG[first.month]} {first.year}"
        panel = self.query_one("#month-panel", Panel)
        panel.fit_title(title, *extra)
        self._fit()

    def _select_cell(self) -> None:
        for cell in self.grid.query(DayCell):
            selected = cell.day == self.day and cell.info is not None
            if cell.has_class("-selected") != selected:
                cell.set_class(selected, "-selected")
                cell.refresh()

    def _draw_day(self) -> None:
        s = self.session
        today, low, day = s.today, s.weekly(), self.day
        month = month_of(s, self.month)
        info = month.day(day) or CalDay(day)
        past = day < today
        panel = self.query_one("#day-panel", Panel)
        when = "today" if day == today else relative(day, today)
        panel.fit_title(fmt_date(day, weekday=True), when)
        self.query_one("#figures", Figures).set_rows(self._figures(info, low))
        # the day's items
        rows: list[Item] = []
        for i, e in enumerate(info.entries):
            name = Text(e.name, style=FAINT if past else "")
            if e.debt:
                name.append(" ◆", style=FAINT if past else PURPLE)
            if e.whatif:
                name = Text.assemble(("↳ ", ACCENT), name)
            amount = Text(money(e.cents, sign=True), style=FAINT) if past else signed(e.cents)
            after = balance(e.after, low) if e.after is not None else ""
            rows.append(Row(name, amount, after, key=(e.key, day, i)))
        items = self.query_one("#items", RowList)
        if not s.flows():
            items.empty = "No flows yet. Press a to add one on this day."
        else:
            items.empty = "Nothing was scheduled." if past else "Nothing scheduled."
        items.set_rows(rows)
        count = len(info.entries)
        head = Text("Scheduled" if past else "On the day", style="bold")
        if count:
            head.append(f"  {count} item{'s' if count != 1 else ''}", style=FAINT)
        self.query_one("#items-head", Static).update(head)
        self._draw_month(month)

    def _figures(self, info: CalDay, low: int) -> list[Fig | Text]:
        s = self.session
        today, day = s.today, info.date
        if day < today:
            return [
                Text("This day has passed.", style=FAINT),
                Text("bdbd doesn't simulate past days.", style=FAINT),
            ]
        a = answer(s, day)
        if a is None:
            return [
                Fig("At the end of the day", Text("unknown", style=f"bold {AMBER}")),
                Text.assemble(
                    ("press ", AMBER), ("b", f"bold {ACCENT}"), (" to record your balance", AMBER)
                ),
            ]
        cramped = self.has_class("-narrow") and self.has_class("-short")
        base = answer(s, day, baseline=True) if s.lens_on and not cramped else None
        rows: list[Fig | Text] = [Fig("At the end of the day", _bold(a.balance, low))]
        if base is not None and base.balance != a.balance:
            rows.append(_delta(a.balance, base.balance))
        if a.spare is not None and a.income is not None:
            rows.append(Fig("Spare", _bold(a.spare, low)))
            if base is not None and base.spare is not None and base.spare != a.spare:
                rows.append(_delta(a.spare, base.spare))
            name, on, _ = a.income
            when = f" on {fmt_date(on, today, weekday=True)}"
            rows.append(Text.assemble(("before ", FAINT), name, (when, FAINT)))
        else:
            rows.append(Fig("Spare", Text("—", style=FAINT)))
            rows.append(Text("no income ahead to count to", style=FAINT))
        return rows

    def _draw_month(self, month: Month) -> None:
        s = self.session
        today, low = s.today, s.weekly()
        t = totals(month, today)
        name = _month_name(month.first, today)
        if t.since is None:
            head = Text.assemble((name, "bold"), (f" {DOT} what was scheduled", FAINT))
        elif t.since == month.first:
            head = Text(name, style="bold")
        else:
            head = Text(f"Rest of {name}", style="bold")
        self.query_one("#month-head", Static).update(head)
        short = self.has_class("-short")
        rows: list[Fig | Text] = []
        if t.starts is not None and t.since is not None and not short:
            note = "today" if t.since == today else _on(t.since)
            rows.append(Fig("Starts at", bold_balance(t.starts, low), note))
        rows.append(Fig("Money in", signed(t.money_in) if t.money_in else _none()))
        rows.append(Fig("Money out", signed(-t.money_out) if t.money_out else _none()))
        if t.everyday and not short:
            weekly = f"{money_short(s.weekly())}/week"
            rows.append(Fig("Everyday", Text(money(-t.everyday)), weekly))
        if t.ends is not None:
            rows.append(Fig("Ends at", _bold(t.ends[1], low), _on(t.ends[0])))
        if t.lowest is not None:
            lo_day, lo = t.lowest
            rows.append(Fig("Lowest", Text(money(lo), style=balance_color(lo, low)), _on(lo_day)))
        self.query_one("#month-figures", Figures).set_rows(rows)
        self.query_one("#month-panel", Panel).border_subtitle = (
            self._summary(t, name) if self.has_class("-narrow") else ""
        )

    def _summary(self, t: Totals, name: str) -> Text:
        """The month in one line, for the grid's bottom border when the day panel is below."""
        width = self.query_one("#month-panel", Panel).outer_size.width - 6
        if t.since is None:
            scope = f"{name} (past)"
        elif t.since == self.month:
            scope = name
        else:
            scope = f"Rest of {name}"
        text = Text()
        for fmt, short_scope in ((money, scope), (_whole_money, scope), (_whole_money, "")):
            parts: list[tuple[str, str]] = []
            if short_scope:
                parts.append((f"{short_scope}  ", FAINT))
            parts += [(fmt(t.money_in, sign=True), GREEN if t.money_in else FAINT)]
            parts += [(" in · ", FAINT)]
            parts += [(fmt(-t.money_out), FOREGROUND), (" out", FAINT)]
            if t.lowest is not None:
                parts += [(" · lowest ", FAINT), (fmt(t.lowest[1]), FOREGROUND)]
                parts += [(f" {fmt_date(t.lowest[0], self.session.today)}", FAINT)]
            if t.ends is not None:
                parts += [(" · ends ", FAINT), (fmt(t.ends[1]), FOREGROUND)]
            text = Text.assemble(" ", *parts, " ")
            if text.cell_len <= width:
                return text
        return text

    def _fit(self) -> None:
        """Row gutters only when there's room; below the grid, the day panel takes what's left."""
        narrow, short = self.has_class("-narrow"), self.has_class("-short")
        height = self.size.height
        weeks = sum(1 for c in self.grid.query(DayCell) if c.display) // 7 or 6
        chrome = 3 if short else 4  # the grid's borders, the weekdays (and a line under them)
        room = height - chrome
        tight = room < weeks * 3 + (weeks - 1)
        panel = self.query_one("#day-panel")
        if narrow:  # the day panel sits below and takes what the weeks leave (at least 5 rows)
            want = 5 if short else 7
            gap = 0 if room - want < weeks * 3 + (weeks - 1) else 1
            tight = gap == 0
            per_week = max(1, (room - want - gap * (weeks - 1)) // weeks)
            panel.styles.height = max(want, room - per_week * weeks - gap * (weeks - 1))
        elif panel.styles.clear_rule("height"):  # back beside the grid, as tall as it
            panel.refresh(layout=True)
        self.grid.set_class(tight, "-tight")
        for i, cell in enumerate(self.grid.query(DayCell)):  # tiles that touch: alternate weeks
            cell.set_class(tight and (i // 7) % 2 == 1 and cell.info is not None, "-alt")

    def on_resize(self) -> None:
        """The layout follows the size: gutters, the day panel's height, the month summary."""
        if self.drawn == self.session.version and self.month != date.min:
            self._fit()
            self._draw_day()

    # moving ----------------------------------------------------------------------------

    def select(self, day: date, *, keep: bool = False) -> None:
        """Move the cursor to `day`, turning the page when it's in another month."""
        day = self._clamp(day)
        if not keep:
            self._want = day.day
        if day == self.day:
            return
        self.day = day
        if day.replace(day=1) != self.month:
            self.month = day.replace(day=1)
            self._draw_grid()
        else:
            self._select_cell()
        self._draw_day()

    def _to_grid(self) -> None:
        """Moving the day takes focus back to the grid (the day's items list is left behind)."""
        if not self.grid.has_focus:
            self.grid.focus()

    def action_move(self, days: int) -> None:
        self._to_grid()
        self.select(self.day + timedelta(days=days))

    def action_month(self, step: int) -> None:
        self._to_grid()
        target = add_months(self.month, step)
        last = month_end(target).day
        self.select(target.replace(day=min(self._want or self.day.day, last)), keep=True)

    def action_today(self) -> None:
        self._to_grid()
        self.select(self.session.today)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """'esc back to the month' only while the day's items have focus."""
        if action == "back":
            return self.query_one("#items", RowList).has_focus
        return True

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        self.refresh_bindings()  # the footer offers esc only from the day's items

    def action_go(self) -> None:
        today = self.session.today

        def preview(text: str) -> tuple[bool, str]:
            day = self._read_day(text)
            if day is None:
                return False, "Try dec 12, fri, +3m, next or 2027-01-15"
            if self._clamp(day) != day:
                return False, f"{fmt_date(day, weekday=True)} is further than the calendar goes"
            a = answer(self.session, day)
            when = fmt_date(day, today, weekday=True)
            if a is not None:  # the answer, right in the prompt
                return True, f"{when} {DOT} ends at {money(a.balance)}"
            return True, f"{when} {DOT} {relative(day, today)}"

        def go(text: str) -> None:
            day = self._read_day(text)
            if day is not None:
                self.select(day)
                self.grid.focus()

        bdbd(self).prompt("Go to a date", "Date", go, preview=preview)

    def _read_day(self, text: str) -> date | None:
        """Date words ('dec 12', '+3m', 'fri') or a month ('oct', 'next', '2027-01', '+2').

        A day without a year is the nearer one ('sep 18' is last week's), a month on its own
        the next one.
        """
        today = self.session.today
        words = text.strip().lower()
        if not words:
            return None
        if words not in MONTHS:
            try:
                return parse_day(words, today=today, prefer="nearest")
            except CashError:
                pass
        try:
            return ask.month_of(words, today)
        except CashError:
            return None

    def action_items(self) -> None:
        items = self.query_one("#items", RowList)
        if items.current is None:
            self.app.bell()
            return
        items.focus()

    def action_back(self) -> None:
        self.grid.focus()

    def action_add(self) -> None:
        """a: a new flow, starting on the selected day."""
        bdbd(self).add_flow(starts=self.day)

    def on_day_cell_pressed(self, event: DayCell.Pressed) -> None:
        self.select(event.day)
        if event.twice:
            self.action_items()
        else:
            self.grid.focus()

    def on_row_list_selected(self, event: RowList.Selected) -> None:
        key = event.key
        flow = key[0] if isinstance(key, tuple) else key
        if isinstance(flow, int):
            bdbd(self).open_flow_card(flow)
        else:
            bdbd(self).notify("That's part of the what-if, not your budget.")


# ── Pieces ────────────────────────────────────────────────────────────────────


def _bold(cents: int, low: int) -> Text:
    return Text(money(cents), style=f"bold {balance_color(cents, low)}")


def _delta(now: int, before: int) -> Text:
    """'+$120.00 vs now', right-aligned under the number the what-if moved."""
    return Text(f"{money(now - before, sign=True)} vs now", style=FAINT, justify="right")


def _none() -> Text:
    return Text(money(0), style=FAINT)


def _on(day: date) -> str:
    return f"{DAY_SHORT[day.weekday()]} {MONTH_SHORT[day.month]} {day.day}"


def _whole_money(cents: int, *, sign: bool = False) -> str:
    text = _whole(cents)
    return ("+" + text) if sign and cents > 0 else text
