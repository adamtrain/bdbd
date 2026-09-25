"""The What if view (6): try something without changing anything.

Left, the changes being tried (the session's sandbox), each switched on or off. Right, the
answer, in one of two modes:

- Compare (no change dated '?'): the budget with the what-if against the budget as it is, the
  numbers `bdbd compare` gives for the same flags: a verdict from its breakeven block, the
  difference at the end, the furthest behind, the ending and lowest balances, the difference
  day by day as a chart, and month by month. The horizon runs from 6m to 10y ([ ]).
- Earliest (a change dated '?'): the first date that keeps the balance (or what's spare) at or
  above a floor for the next 12 months, the numbers `bdbd earliest --floor` gives. It takes a
  few seconds, so it runs in a thread on a snapshot; once found, the date is pinned so every
  other view shows the what-if on that day (u unpins).

Nothing here is ever written to the budget file.
"""

from __future__ import annotations

import contextlib
import json
import threading
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import ClassVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content
from textual.geometry import Size
from textual.message import Message
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

from bdbd import ask
from bdbd.budget import Start
from bdbd.core import engine
from bdbd.core.dates import add_months
from bdbd.core.errors import CashError
from bdbd.core.models import Flow, Kind
from bdbd.core.money import parse_amount, parse_rate
from bdbd.core.queries.compare import compare as compare_query
from bdbd.core.queries.earliest import earliest as earliest_query
from bdbd.core.scenario import build_effective_model, resolve_placeholders
from bdbd.tui import change_form
from bdbd.tui.modals import KeyStrip
from bdbd.tui.scenario import KINDS, Change, placeholder_offset
from bdbd.tui.session import Session, describe_flow
from bdbd.tui.text import DEBOUNCE, NEVER_PAID, SPINNER, no_balance_note, sentence
from bdbd.tui.widgets import (
    Chart,
    Column,
    EmptyState,
    HeadedList,
    Item,
    KeyValues,
    Panel,
    Picker,
    Row,
    RowList,
    View,
    bdbd,
    panel_title,
    warning_line,
)
from bdbd.ui import charts
from bdbd.ui.common import humanize
from bdbd.ui.theme import (
    ACCENT,
    AMBER,
    DARK,
    DOT,
    FAINT,
    FOREGROUND,
    GREEN,
    balance_color,
    cents_of,
    money,
    money_short,
    pct,
)
from bdbd.words import fmt_date, fmt_month, join, relative, span

HORIZONS: tuple[tuple[str, int], ...] = (
    ("6m", 6),
    ("1y", 12),
    ("2y", 24),
    ("5y", 60),
    ("10y", 120),
)
DEFAULT_HORIZON = 2  # 2y, like `bdbd compare`
EARLIEST_MONTHS = 12  # like `bdbd earliest`
MONTH_HEADS = ("As it is", "What if", "Difference")
NO_BALANCE = "no balance recorded"
HORIZON = Binding.Group("horizon")


# ── Compare ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Comparison:
    """The what-if against the budget as it is: what `bdbd compare` says, plus the days."""

    until: date
    data: dict  # compare_query's data, exactly as the JSON command gets it
    warnings: list[str]  # the what-if's own warnings (the budget's are the other views')
    diffs: list[tuple[date, int]]  # what-if minus as-it-is at the end of every day
    start: Start
    low: int  # a week of everyday spending with the what-if: the amber line for balances


def compare(session: Session, until: date) -> Comparison:
    """`bdbd compare --until UNTIL <the sandbox's flags>`, computed the way the command does:
    the stored budget against the sandbox, from the real starting balance, with the stored
    everyday spending unless the what-if changes it."""
    today = session.today
    spec = session.spec()
    flows = session.flows()
    base_model = build_effective_model(flows, None, today)
    scen_model = build_effective_model(flows, spec, today)
    start = session.start()
    weekly = session.stored_weekly()
    label = (spec or {}).get("name") or "what if"
    data, _ = compare_query(
        base_model,
        scen_model,
        as_of=today,
        until=until,
        starting_balance_cents=start.cents,
        granularity="monthly",
        include_series=True,
        labels=("baseline", label),
        weekly_spend_cents=weekly,
    )
    runs = [
        engine.run(
            model,
            as_of=today,
            until=until,
            starting_balance_cents=start.cents,
            weekly_spend_cents=_or(model.weekly_spend_cents, weekly),
        )
        for model in (base_model, scen_model)
    ]
    diffs = [(d, b - a) for (d, a), (_, b) in zip(runs[0].daily, runs[1].daily, strict=True)]
    base_warnings = set(runs[0].warnings)
    own = [w for w in dict.fromkeys(runs[1].warnings) if w not in base_warnings]
    low = _or(scen_model.weekly_spend_cents, weekly)
    return Comparison(until, data, own, diffs, start, low)


def verdict(data: dict, today: date) -> tuple[str, Text]:
    """The breakeven block in words, and its color: 'Catches up on Fri Feb 5 · in 4 months'."""
    be = data["breakeven"]
    status = be["status"]
    if status == "reached":
        d = date.fromisoformat(be["date"])
        return GREEN, Text.assemble(
            "Catches up on ",
            (fmt_date(d, today, weekday=True), "bold"),
            (f" {DOT} {relative(d, today)}", FAINT),
        )
    if status == "immediate":
        d = date.fromisoformat(be["date"])
        return GREEN, Text.assemble(
            ("Ahead from the start", "bold"),
            (f" {DOT} from {fmt_date(d, today, weekday=True)}", FAINT),
        )
    if status == "identical":
        return ACCENT, Text("No difference at all", style="bold")
    return AMBER, Text("Doesn't catch up in this window", style="bold")


# ── Earliest ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Search:
    """What an earliest search needs, taken from the session on the main thread, so the
    search can run in a thread (which must never touch the sqlite connection)."""

    today: date
    until: date
    flows: tuple[Flow, ...]
    spec: str  # the sandbox as scenario JSON, '?' dates unresolved
    start: Start
    floor: int
    measure: str  # "balance" | "spare"
    weekly: int  # the stored everyday spending (the what-if may change it per trial)

    @classmethod
    def of(cls, session: Session, *, floor: int, measure: str, months: int) -> Search:
        today = session.today
        return cls(
            today=today,
            until=add_months(today, months),
            flows=tuple(session.flows()),
            spec=json.dumps(session.spec(placeholder=True), sort_keys=True),
            start=session.start(),
            floor=floor,
            measure=measure,
            weekly=session.stored_weekly(),
        )


@dataclass(frozen=True)
class Found:
    """An earliest search's answer: what `bdbd earliest --floor` says (or why it couldn't)."""

    search: Search
    data: dict | None
    warnings: list[str]
    error: str | None = None
    daily: tuple[tuple[date, int], ...] = ()  # the balance each day, with '?' on `shown`

    @property
    def day(self) -> date | None:
        """The earliest date that works, if there is one."""
        if self.data is None or self.data["status"] != "found":
            return None
        return date.fromisoformat(self.data["date"])

    @property
    def shown(self) -> date | None:
        """The day the chart puts the what-if on: the answer, or else the closest miss."""
        best = (self.data or {}).get("best_infeasible")
        return self.day or (date.fromisoformat(best["date"]) if best else None)


class SearchDone(Message):
    """An earliest search's answer, posted from its thread."""

    def __init__(self, found: Found) -> None:
        super().__init__()
        self.found = found


def find(search: Search) -> Found:
    """`bdbd earliest --floor FLOOR --measure MEASURE <the sandbox's flags>` (seconds), and
    the balance day by day with the what-if on the day it found (as its trials run it)."""
    spec = json.loads(search.spec)
    try:
        data, warnings = earliest_query(
            list(search.flows),
            spec,
            as_of=search.today,
            until=search.until,
            starting_balance_cents=search.start.cents,
            floor_cents=search.floor,
            measure=search.measure,
            weekly_spend_cents=search.weekly,
        )
    except CashError as exc:
        return Found(search, None, [], exc.message)
    found = Found(search, data, list(warnings))
    day = found.shown
    if day is None:
        return found
    model = build_effective_model(list(search.flows), resolve_placeholders(spec, day), search.today)
    run = engine.run(
        model,
        as_of=search.today,
        until=search.until,
        starting_balance_cents=search.start.cents,
        weekly_spend_cents=_or(model.weekly_spend_cents, search.weekly),
    )
    return Found(search, data, list(warnings), daily=tuple(run.daily))


def default_floor(session: Session) -> int:
    """One week of everyday spending (the what-if's, if it changes it), in whole dollars."""
    weekly = session.stored_weekly()
    spec = session.spec(placeholder=True) or {}
    if spec.get("weekly_spend") is not None:
        weekly = parse_amount(spec["weekly_spend"])
    dollars = (Decimal(weekly) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(dollars) * 100


class DateStrip(Widget):
    """The dates an earliest search tried, one cell each (or a few): red too soon, green works."""

    DEFAULT_CSS = """
    DateStrip {
        height: auto;
        text-wrap: nowrap;
        text-overflow: clip;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._cells: list[tuple[date, bool]] = []
        self._marker: date | None = None

    def show(self, cells: list[tuple[date, bool]], marker: date | None) -> None:
        self._cells, self._marker = cells, marker
        self.refresh(layout=True)

    def render(self) -> Text:
        width = self.content_size.width or self.size.width
        if not self._cells or width < 8:
            return Text()
        return Text("\n").join(charts.strip(self._cells, width, marker=self._marker))

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return 3 if self._marker is not None else 2


# ── The view ──────────────────────────────────────────────────────────────────


class WhatIfView(View):
    """The sandbox on the left, and what it would do on the right."""

    title = "What if"

    DEFAULT_CSS = """
    WhatIfView {
        layout: horizontal;
        & #side { width: 42%; min-width: 48; max-width: 50; height: 1fr; }
        & #changes-panel { height: auto; max-height: 60%; }
        & #changes { height: auto; max-height: 14; }
        & #lens { height: auto; margin-top: 1; color: $text-muted; }
        & #card { height: auto; }
        & #card-head { height: auto; margin-bottom: 1; }
        & #facts { height: auto; }
        & #card-keys { height: auto; margin-top: 1; }
        & #answer { width: 1fr; height: 1fr; }
        & #headline { height: auto; padding: 1 2; }
        & #verdict { height: auto; margin-bottom: 1; }
        & #numbers { height: auto; }
        & #note { height: auto; margin-top: 1; color: $text-muted; }
        & #chart-panel { display: none; height: 1fr; min-height: 6; }
        & #months-panel { display: none; height: 1fr; min-height: 6; }
        & #months { height: 1fr; }
        & #strip-panel { display: none; height: auto; }
        & #earlier { height: auto; margin-top: 1; }
        & #balance-panel { display: none; height: 1fr; min-height: 6; }
        & #warnings { height: auto; padding: 0 1; }
        & #intro { display: none; }
        &.-empty > * { display: none; }
        &.-empty > #intro { display: block; }
        &.-compare #chart-panel { display: block; }
        &.-compare #months-panel { display: block; }
        &.-found #strip-panel { display: block; }
        &.-found #balance-panel { display: block; }
        &.-narrow { layout: vertical; }
        &.-narrow #side { width: 1fr; max-width: 100%; height: auto; }
        &.-narrow #changes-panel { max-height: 100%; }
        &.-narrow #changes { max-height: 5; }
        &.-narrow #lens { margin-top: 0; }
        &.-narrow #card { display: none; }
        &.-short #headline { padding: 0 2; }
        &.-short #verdict { margin-bottom: 0; }
        &.-short #note { margin-top: 0; }
        &.-short #earlier { margin-top: 0; }
        &.-short #card-keys { display: none; }
        &.-short.-narrow #lens { display: none; }
        &.-short.-narrow #changes { max-height: 3; }
        &.-short.-narrow #balance-panel { display: none; }
        &.-short.-compare #months-panel { display: none; }
        &.-short.-compare.-table #months-panel { display: block; }
        &.-short.-compare.-table #chart-panel { display: none; }
    }
    """

    BINDINGS: ClassVar = [
        Binding("a", "add", "add a change"),
        Binding("space", "switch", "on/off"),
        Binding("e", "edit", "edit", show=False),
        Binding("enter", "edit", "edit", show=False),
        Binding("x", "remove", "remove", show=False),
        Binding("delete", "remove", "remove", show=False),
        Binding("c", "clear", "clear all", show=False),
        Binding("left_square_bracket", "shorter", "shorter horizon", group=HORIZON),
        Binding("right_square_bracket", "longer", "longer horizon", group=HORIZON),
        Binding("minus", "shorter", "shorter horizon", show=False),
        Binding("plus,equals_sign", "longer", "longer horizon", show=False),
        Binding("f", "floor", "floor"),
        Binding("m", "measure", "balance or spare", show=False),
        Binding("u", "pin", "pin or unpin the date", show=False),
        Binding("v", "table", "chart or months"),
    ]

    MONTHS: ClassVar[int] = EARLIEST_MONTHS  # how far an earliest search looks (tests shorten it)

    def __init__(self, *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self.horizon = DEFAULT_HORIZON
        self.floor: int | None = None  # None: one week of everyday spending
        self.measure = "balance"
        self.mode: str | None = None  # "compare" | "earliest" | "off" | "broken"
        self._shown: Comparison | None = None
        self._found: Found | None = None  # the last earliest answer
        self._asked: Search | None = None  # the search asked for (being worked on, or next)
        self._thread: threading.Thread | None = None  # at most one search at a time
        self._pending: Search | None = None  # the newest request, for when the thread is free
        self._debounce: Timer | None = None
        self._unpinned: Search | None = None  # the search whose date u unpinned
        self._spin = 0
        self._spinner: Timer | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="side"):
            with Panel(id="changes-panel"):
                yield RowList(Column(min=1), Column(flex=True, min=10), id="changes")
                yield Static(id="lens")
            with Panel(id="card"):
                yield Static(id="card-head")
                yield KeyValues(id="facts")
                yield KeyStrip(target=self, id="card-keys")
        with Vertical(id="answer"):
            with Panel(id="headline"):
                yield Static(id="verdict")
                yield KeyValues(id="numbers")
                yield Static(id="note")
            yield Panel(Chart(color=GREEN, id="chart"), id="chart-panel")
            with Panel(id="months-panel"):
                months = HeadedList(
                    Column(flex=True, min=8),
                    *(Column(align="right") for _ in MONTH_HEADS),
                    heads=["", *MONTH_HEADS],
                    id="months",
                )
                yield months.heads
                yield months
            with Panel(id="strip-panel"):
                yield DateStrip(id="strip")
                yield Static(id="earlier")
            yield Panel(Chart(id="balance-chart"), id="balance-panel")
            yield Static(id="warnings")
        yield EmptyState(
            "Try something without changing anything.",
            "Sell the car on Nov 1 for $13,000, and see when you catch up.\n"
            "Book a $650 flight, and find the first day you can afford it.\n"
            "Nothing here is saved to your budget.",
            keys=[("a", "add a change"), ("?", "help")],
            id="intro",
        )

    def on_mount(self) -> None:
        self._spinner = self.set_interval(1 / 12, self._tick, pause=True)

    def focus_default(self) -> None:
        if self.has_class("-empty"):
            self.screen.set_focus(None)
        else:
            self.query_one("#changes", RowList).focus()

    # ── Drawing ──────────────────────────────────────────────────────────────────

    def refresh_view(self) -> None:
        s = self.session
        empty = not s.scenario.changes and s.scenario.extra is None
        was_empty = self.has_class("-empty")
        self.set_class(empty, "-empty")
        if empty:
            self._set_mode(None)
            return
        if was_empty and self.screen.focused is None:
            self.call_after_refresh(self.focus_default)
        self._changes()
        error = s.scenario_error
        if error is not None:
            self._broken(error)
        elif not s.scenario.any:
            self._off()
        elif s.scenario.placeholders:
            self._earliest()
        else:
            self._compare()

    def _set_mode(self, mode: str | None) -> None:
        for m in ("compare", "earliest", "off", "broken"):
            self.set_class(m == mode, f"-{m}")
        if mode != "earliest":
            self.set_class(False, "-found")
            self._busy(False)
        if mode != self.mode:
            self.mode = mode
            self.refresh_bindings()

    def _changes(self) -> None:
        s = self.session
        today = s.today
        rows: list[Item] = []
        on = 0
        for i, c in enumerate(s.scenario.changes):
            problem = self._problem(c)
            sentence = s.scenario.sentence(c, today, short=True)
            if c.enabled and problem:
                dot, style = Text("!", style=f"bold {AMBER}"), AMBER
            elif c.enabled:
                dot, style = Text("●", style=ACCENT), FOREGROUND
            else:
                dot, style = Text("○", style=FAINT), FAINT
            on += c.enabled
            rows.append(Row(dot, Text(sentence, style=style), key=i))
        if s.scenario.extra is not None:
            label = s.scenario.extra_label or "A payoff plan"
            if s.scenario.extra_enabled:
                on += 1
                rows.append(Row(Text("●", style=ACCENT), Text(label), key="plan"))
            else:
                rows.append(Row(Text("○", style=FAINT), Text(label, style=FAINT), key="plan"))
        self.query_one("#changes", RowList).set_rows(rows)
        total = len(rows)
        state = "all off" if not on else "all on" if on == total and total > 1 else f"{on} on"
        panel = self.query_one("#changes-panel", Panel)
        panel.fit_title("Changes", state if total > 1 or not on else "")
        note = self._lens_note()
        lens = self.query_one("#lens", Static)
        lens.update(note or "")
        lens.display = note is not None
        self._card()

    def _problem(self, change: Change) -> str | None:
        """Why a change doesn't apply to the budget (any more), or None."""
        s = self.session
        return s.cached(("whatif_check", change), lambda: s.check_change(change))

    def _card(self) -> None:
        """The selected change in full, and what it does to what it touches."""
        s = self.session
        key = self._selected()
        card = self.query_one("#card", Panel)
        head = self.query_one("#card-head", Static)
        facts = self.query_one("#facts", KeyValues)
        keys = self.query_one("#card-keys", KeyStrip)
        if key == "plan":
            on = s.scenario.extra_enabled
            card.fit_title("The payoff plan", "tried from Debts (5)")
            head.update(
                Text.assemble(
                    (s.scenario.extra_label or "A payoff plan", ""),
                    ("" if on else "\nSwitched off", FAINT),
                )
            )
            facts.set_rows([])
            keys.set_keys(
                [
                    ("space", "switch off" if on else "switch on", "switch"),
                    ("x", "remove", "remove"),
                ]
            )
            return
        if not isinstance(key, int) or key >= len(s.scenario.changes):
            card.set_title("")
            head.update("")
            facts.set_rows([])
            keys.set_keys([])
            return
        change = s.scenario.changes[key]
        problem = self._problem(change)
        card.fit_title(change.target or "Everyday spending", _CARD_TITLES.get(change.kind, ""))
        kind = Text()
        if not change.enabled:
            kind.append("Switched off", style=FAINT)
        elif problem:
            kind.append(sentence(humanize(problem)), style=AMBER)
        if KINDS[change.kind].target == "tag" and not problem:
            tagged = [f.name for f in s.flows() if change.target in f.tags and f.active]
            if kind:
                kind.append("\n")
            kind.append(_names(tagged) if tagged else "No active flow has it", style=FAINT)
        head.update(kind)
        head.display = bool(kind)
        facts.set_rows([] if problem else self._facts(change))
        keys.set_keys(
            [
                ("e", "edit", "edit"),
                ("space", "switch off" if change.enabled else "switch on", "switch"),
                ("x", "remove", "remove"),
            ]
        )

    def _facts(self, change: Change) -> list[list[str | Text]]:
        """What the change touches, as it is and with the what-if: a debt's payoff, a flow's
        money each month, a one-off's day."""
        s = self.session
        spec = KINDS[change.kind]
        flow = next((f for f in s.flows() if f.name.casefold() == change.target.casefold()), None)
        amount = _cents(change)
        rows: list[list[str | Text]] = []
        if spec.target == "debt" and flow is not None:
            before = next((r for r in s.debts(baseline=True) if r.key == flow.id), None)
            if before is None:
                return [["Paused", Text("—", style=FAINT), _faint("so this changes nothing")]]
            rows.append(["Owed today", _bold(money(before.balance)), _faint(pct(before.rate))])
            on = self._on(change)
            match change.kind:
                case "payoff":
                    rows.append(["Pays it off", _bold(on), _faint(self._relative(change))])
                case "settle":
                    rows.append(["Sells for", _bold(money(amount or 0)), _faint(f"on {on}")])
                case "extra_payment":
                    rows.append(["Extra", _bold(money(amount or 0)), _faint(f"on {on}")])
                case "set_payment":
                    now = _faint(f"now {money(before.payment)}")
                    rows.append(["Payment", _bold(money(amount or 0)), now])
                    rows.append(["From", _bold(on), _faint(self._relative(change))])
                case "rate_change":
                    now = _faint(f"now {pct(before.rate)}")
                    rows.append(["Rate", _bold(_rate(change.amount)), now])
                    rows.append(["From", _bold(on), _faint(self._relative(change))])
            after = self._debt_with(flow.id) if change.enabled else None
            if after is not None:
                paid = fmt_month(after.paid_off_on) if after.paid_off_on else "not paid off"
                was = fmt_month(before.paid_off_on) if before.paid_off_on else "never"
                rows.append(
                    [
                        "Paid off",
                        _bold(paid, GREEN if after.paid_off_on else AMBER),
                        _faint("same as it is" if paid == was else f"was {was}"),
                    ]
                )
                if after.interest is None:
                    rows.append(["Interest to go", _bold("—", AMBER), _faint(NEVER_PAID)])
                else:
                    if before.interest is None:
                        note = "as it is, it's never paid off"
                    else:
                        saved = before.interest - after.interest
                        note = (
                            f"saves {money(saved)}"
                            if saved > 0
                            else f"{money(-saved)} more"
                            if saved < 0
                            else "same as it is"
                        )
                    rows.append(["Interest to go", _bold(money(after.interest)), _faint(note)])
            return rows
        if spec.target == "name":
            cents = amount or 0
            signed = cents if change.kind == "add_income" else -cents
            rows.append(
                [
                    "Amount",
                    _bold(money(signed, sign=True), GREEN if signed > 0 else ""),
                    _faint("once"),
                ]
            )
            if change.placeholder and s.scenario.pinned is None:
                rows.append(["On", _bold("?"), _faint("earliest that works")])
            else:
                rows.append(["On", _bold(self._on(change)), _faint(self._relative(change))])
            return rows
        if change.kind == "everyday":
            now = s.stored_weekly()
            cents = amount or 0
            diff = ask.everyday_monthly(cents) - ask.everyday_monthly(now)
            rows.append(["Per week", _bold(money(cents)), _faint(f"now {money(now)}")])
            rows.append(
                [
                    "A month",
                    _bold(money(ask.everyday_monthly(cents))),
                    _faint("same as now" if not diff else f"{money(diff, sign=True)} vs now"),
                ]
            )
            return rows
        if spec.target == "tag":
            tagged = [f for f in s.flows() if change.target in f.tags and f.active]
            monthly = s.listing().monthly
            income = sum(monthly.get(f.id, 0) for f in tagged if f.kind == Kind.INCOME)
            bills = sum(monthly.get(f.id, 0) for f in tagged if f.kind != Kind.INCOME)
            if bills:
                note = self._month_note(change, income=False)
                rows.append(["A month", _bold(money(bills)), _faint(note)])
            if income:
                note = self._month_note(change, income=True)
                rows.append(["A month", _bold(money(income), GREEN), _faint(note)])
            return rows
        if flow is not None:
            cents = flow.amount_cents if flow.kind == Kind.INCOME else -flow.amount_cents
            words = describe_flow(flow).split(" ", 1)[-1]
            rows.append(["Each time", _bold(money(cents, sign=True), GREEN if cents > 0 else ""),
                         _faint(words)])  # fmt: skip
            if change.kind == "set_amount":
                new = amount or 0
                new_signed = new if flow.kind == Kind.INCOME else -new
                when = f"from {self._on(change)}" if change.when else "from today"
                rows.append(["Becomes", _bold(money(new_signed, sign=True)), _faint(when)])
                return rows
            monthly = s.listing().monthly.get(flow.id, 0)
            if monthly:
                note = self._month_note(change, income=flow.kind == Kind.INCOME)
                rows.append(["A month", _bold(money(monthly)), _faint(note)])
        return rows

    def _month_note(self, change: Change, *, income: bool) -> str:
        """What a flow's monthly money becomes: 'saved', 'saved after Dec 31', 'back in'."""
        if change.kind == "enable":
            return "back in"
        word = "less coming in" if income else "saved"
        if change.kind in ("stop", "stop_tag"):
            return f"{word} after {self._on(change, weekday=False)}"
        return word

    def _on(self, change: Change, *, weekday: bool = True) -> str:
        """'Sun Nov 1', or the pinned earliest date, or 'the earliest date'."""
        s = self.session
        day = change.day
        offset = placeholder_offset(change.when)
        if day is None and offset is not None and s.scenario.pinned is not None:
            day = s.scenario.pinned + timedelta(days=offset)
        if day is None:
            return "earliest date" if offset is not None else "today"
        return fmt_date(day, s.today, weekday=weekday)

    def _relative(self, change: Change) -> str:
        """'in 8 days', or 'the earliest · in 8 days' for a pinned '?'."""
        s = self.session
        day = change.day
        offset = placeholder_offset(change.when)
        if day is None and offset is not None and s.scenario.pinned is not None:
            day = s.scenario.pinned + timedelta(days=offset)
            return f"the earliest {DOT} {relative(day, s.today)}"
        return relative(day, s.today) if day else ""

    def _debt_with(self, key: object) -> ask.DebtRow | None:
        """A debt's row with the whole what-if applied (None when it can't be worked out)."""
        s = self.session
        if s.scenario_error or s.scenario.unpinned or not s.scenario.any:
            return None

        def compute() -> list[ask.DebtRow]:
            model = build_effective_model(s.flows(), s.spec(), s.today)
            return ask.debts(s.budget, model)

        try:
            rows = s.cached(("whatif_debts",), compute)
        except CashError:
            return None
        return next((r for r in rows if r.key == key), None)

    def _lens_note(self) -> Text | None:
        """What the other views show, when the banner doesn't already say it."""
        s = self.session
        key = f"bold {ACCENT}"
        if not s.scenario.enabled:
            return Text.assemble(
                ("The what-if is off: other views show your budget as it is. ", FAINT),
                ("w", key),
                (" turns it on.", FAINT),
            )
        if not s.scenario.any:
            return Text("Nothing here is saved to your budget.", style=FAINT)
        return None

    def _headline(
        self,
        chip: str,
        color: str,
        text: Text,
        variant: str | None,
        title: Content,
        subtitle: Content | str = "",
    ) -> None:
        self.query_one("#verdict", Static).update(
            Text.assemble((f" {chip} ", f"bold {DARK} on {color}"), "  ", text)
        )
        panel = self.query_one("#headline", Panel)
        for v in ("accent", "purple", "green", "amber"):
            panel.set_class(v == variant, f"-{v}")
        panel.set_title(title)
        panel.set_subtitle(subtitle)  # modes with controls set theirs after this

    def _numbers(self, rows: list[list[str | Text]]) -> None:
        numbers = self.query_one("#numbers", KeyValues)
        numbers.set_rows(rows)
        numbers.display = bool(rows)

    def _note(self, text: Text | None) -> None:
        note = self.query_one("#note", Static)
        note.update(text or "")
        note.display = text is not None

    def _warnings(self, lines: list[Text]) -> None:
        box = self.query_one("#warnings", Static)
        box.update(Text("\n").join(lines))
        box.display = bool(lines)

    def _broken(self, error: str) -> None:
        self._set_mode("broken")
        self._headline(
            "DOESN'T FIT",
            AMBER,
            Text("This doesn't fit your budget any more", style="bold"),
            "amber",
            panel_title("The answer"),
        )
        self._numbers([])
        self._note(
            Text.assemble(
                (f"{sentence(humanize(error))}.\n", AMBER),
                *_keys(("e", "edit it"), ("x", "remove it"), ("space", "switch it off")),
            )
        )
        self._warnings([])

    def _off(self) -> None:
        self._set_mode("off")
        self._headline(
            "ALL OFF",
            FAINT,
            Text("Every change is switched off", style="bold"),
            None,
            panel_title("The answer"),
        )
        self._numbers([])
        self._note(Text.assemble(*_keys(("space", "switches the selected one back on"))))
        self._warnings([])

    # ── Compare ──────────────────────────────────────────────────────────────────

    @property
    def until(self) -> date:
        """The last day compared."""
        return add_months(self.session.today, HORIZONS[self.horizon][1])

    def _compare(self) -> None:
        s = self.session
        until = self.until
        try:
            c = s.cached(("whatif_compare", until), lambda: compare(s, until))
        except CashError as exc:
            self._broken(exc.message)
            return
        self._set_mode("compare")
        self._shown = c
        self._compare_headline(c)
        self._chart(c)
        self._months(c)
        self._warnings([warning_line(w) for w in c.warnings])

    def _compare_headline(self, c: Comparison) -> None:
        today = self.session.today
        d = c.data
        color, words = verdict(d, today)
        variant = {GREEN: "green", AMBER: "amber"}.get(color, "accent")
        months = HORIZONS[self.horizon][1]
        self._headline(
            _CHIPS.get(d["breakeven"]["status"], "COMPARED"),
            color,
            words,
            variant,
            panel_title("Compared with your budget as it is", _next(months)),
        )
        self.query_one("#headline", Panel).set_subtitle(self._picker(), " ")
        be = d["breakeven"]
        end = cents_of(d["difference"]["ending"])
        rows: list[list[str | Text]] = [
            [
                "Difference",
                Text(money(end, sign=True), style=f"bold {_diff_color(end)}"),
                Text(f"by {fmt_date(c.until, today, weekday=True)}", style=FAINT),
            ]
        ]
        if be.get("max_shortfall"):
            ms = be["max_shortfall"]
            behind = cents_of(ms["amount"])
            rows.append(
                [
                    "Furthest behind",
                    Text(money(behind, sign=True), style=f"bold {AMBER}"),
                    Text(fmt_date(date.fromisoformat(ms["date"]), today, weekday=True), FAINT),
                ]
            )
        a, b = d["a"], d["b"]
        a_end, b_end = cents_of(a["ending_balance"]), cents_of(b["ending_balance"])
        rows.append(
            [
                "Ending balance",
                Text(money(b_end), style=f"bold {balance_color(b_end, c.low)}"),
                _versus(a_end, b_end),
            ]
        )
        a_lo, b_lo = a["min_balance"], b["min_balance"]
        lo = cents_of(b_lo["balance"])
        lo_note = _versus(cents_of(a_lo["balance"]), lo)
        day = fmt_date(date.fromisoformat(b_lo["date"]), today, weekday=True)
        dated = Text.assemble((f"{day} {DOT} ", FAINT), lo_note)
        rows.append(["Lowest", Text(money(lo), style=f"bold {balance_color(lo, c.low)}"), dated])
        if KeyValues.width_of(rows) > self._numbers_room():
            rows[-1][2] = lo_note  # the difference says more than the day
        self._numbers(rows)
        if not c.start.known:
            self._note(no_balance_note())
        elif be.get("caveat") and be["status"] == "reached":
            self._note(Text("It falls behind again later on; the chart shows when.", FAINT))
        elif be.get("extrapolated_date"):
            around = fmt_month(date.fromisoformat(be["extrapolated_date"]))
            self._note(Text(f"On its current trend, it catches up around {around}.", FAINT))
        elif be["status"] == "identical" and self.horizon < len(HORIZONS) - 1:
            self._note(
                Text.assemble(
                    (f"Nothing moves in {span(HORIZONS[self.horizon][1])}. ", FAINT),
                    *_keys(("]", "looks further ahead")),
                )
            )
        else:
            self._note(None)

    def _numbers_room(self) -> int:
        """How wide the headline's grid can be (a guess before the first layout)."""
        width = self.query_one("#numbers", KeyValues).size.width
        if width:
            return width
        side = self.query_one("#side").size.width
        answer = self.size.width - (0 if self.has_class("-narrow") else side or 50)
        return max(answer - 6, 40)

    def _picker(self) -> Picker:
        """The horizon picker in the headline's bottom border: 6m 1y 2y 5y 10y (clickable)."""
        return Picker(
            [(i, label) for i, (label, _) in enumerate(HORIZONS)],
            chosen=self.horizon,
            on_pick=self._horizon,
        )

    def _horizon(self, index: object) -> None:
        if isinstance(index, int) and index != self.horizon:
            self.horizon = index
            self.refresh_view()

    def _chart(self, c: Comparison) -> None:
        chart = self.query_one("#chart", Chart)
        chart.set_series(c.diffs, color=GREEN, marker=self._marker_day(), below=AMBER)
        panel = self.query_one("#chart-panel", Panel)
        panel.fit_title("Difference each day", "above zero, the what-if is ahead")
        self._mark()

    def _months(self, c: Comparison) -> None:
        today = self.session.today
        low_a = self.session.stored_weekly()
        rows: list[Item] = []
        for i, r in enumerate(c.data["difference"]["series"]):
            day = date.fromisoformat(r["date"])
            diff = cents_of(r["diff"])
            if i == 0:
                label = "Today"
            elif day == c.until:
                label = fmt_date(day, today)
            else:
                label = fmt_month(day)
            a, b = cents_of(r["a"]), cents_of(r["b"])
            rows.append(
                Row(
                    Text(label, style=FAINT),
                    Text(money(a), style=balance_color(a, low_a)),
                    Text(money(b), style=balance_color(b, c.low)),
                    Text(money(diff, sign=True), style=_diff_color(diff) if diff else FAINT),
                    key=("month", day),
                )
            )
        self.query_one("#months", HeadedList).set_rows(rows)
        panel = self.query_one("#months-panel", Panel)
        panel.fit_title("Month by month", "balances at each month's end")

    def _marker_day(self) -> date | None:
        """The months list's day, while it has focus (the chart marks it)."""
        months = self.query_one("#months", RowList)
        key = months.key
        if not months.has_focus or not isinstance(key, tuple):
            return None
        return key[1]

    def _mark(self) -> None:
        day = self._marker_day()
        self.query_one("#chart", Chart).marker = day
        panel = self.query_one("#chart-panel", Panel)
        c = self._shown
        diff = dict(c.diffs).get(day) if c and day else None
        if day is None or diff is None:
            panel.border_subtitle = ""
            return
        today = self.session.today
        side = "ahead" if diff > 0 else "behind" if diff < 0 else "even"
        panel.border_subtitle = Content.assemble(
            (f" ▲ {fmt_date(day, today, weekday=True)} {DOT} ", FAINT),
            (money(abs(diff)) + " " if diff else "", f"bold {_diff_color(diff)}"),
            (f"{side} ", FAINT),
        )

    # ── Earliest ─────────────────────────────────────────────────────────────────

    def _search(self) -> Search:
        s = self.session
        floor = self.floor if self.floor is not None else default_floor(s)
        return Search.of(s, floor=floor, measure=self.measure, months=self.MONTHS)

    def _earliest(self) -> None:
        self._set_mode("earliest")
        search = self._search()
        found = self._found
        if found is not None and found.search == search:
            self._asked = None
            self._busy(False)
            self._draw_found(found)
            self._keep_pinned(found)
            return
        self.set_class(False, "-found")
        self._draw_busy(search)
        self._ask(search)

    def keep_searching(self) -> None:
        """Look for an unpinned '?' date even while another view shows (the app calls this on
        every redraw), so the other views get the what-if as soon as a date is found."""
        s = self.session.scenario
        if not (s.enabled and s.unpinned) or self.session.scenario_error:
            return
        try:
            search = self._search()
        except CashError:
            return
        found = self._found
        if found is not None and found.search == search:
            self._keep_pinned(found)
            return
        self._ask(search)

    @property
    def searching(self) -> bool:
        """Whether an earliest search is asked for or under way."""
        return self._asked is not None or (self._thread is not None and self._thread.is_alive())

    @property
    def search_state(self) -> str | None:
        """What the earliest search is doing about an unpinned '?', for the banner:
        "searching", "none" (no day works), "unpinned" (u unpinned it), or None."""
        s = self.session.scenario
        if not (s.enabled and s.unpinned):
            return None
        try:
            search = self._search()
        except CashError:
            return None
        found = self._found
        if found is not None and found.search == search:
            if search == self._unpinned:
                return "unpinned"
            return "none" if found.day is None else "searching"
        return "searching"

    def _ask(self, search: Search) -> None:
        """Ask for a search: after a moment's calm, and never two at once."""
        if self._asked == search:
            return
        self._asked = search
        self._busy(True)
        if self._debounce is not None:
            self._debounce.stop()
        self._debounce = self.set_timer(DEBOUNCE, lambda: self._launch(search))

    def _launch(self, search: Search) -> None:
        """Start the search in a thread of its own, unless a newer one took its place, or wait
        for the running one to finish (it can't be interrupted) and start the newest then.

        A daemon thread rather than a Textual thread worker: those run in asyncio's executor,
        which quitting waits for. It only computes (never the sqlite connection) and posts its
        answer back to the main thread.
        """
        if search != self._asked:
            return
        if self._thread is not None and self._thread.is_alive():
            self._pending = search
            return

        def run() -> None:
            try:
                found = find(search)
            except Exception as exc:  # say so, rather than spin forever
                found = Found(search, None, [], f"couldn't work it out: {exc}")
            # post_message is thread-safe, and does nothing once the view has gone
            with contextlib.suppress(RuntimeError):  # the app's loop has closed
                self.post_message(SearchDone(found))

        self._thread = threading.Thread(target=run, name="bdbd-earliest", daemon=True)
        self._thread.start()

    def on_search_done(self, message: SearchDone) -> None:
        message.stop()
        self._arrived(message.found)

    def _arrived(self, found: Found) -> None:
        """A search's answer, on the main thread."""
        self._thread = None
        pending, self._pending = self._pending, None
        if pending is not None and pending == self._asked and pending != found.search:
            self._launch(pending)  # a newer request waited for this one
        if found.search != self._asked:
            return  # an older search, overtaken by a newer one
        self._asked = None
        self._found = found
        if self.mode == "earliest" and found.search == self._search():
            self._busy(False)
            self._draw_found(found)
        self._keep_pinned(found)
        bdbd(self).refresh_views()  # the banner, and the view showing, move on

    def _keep_pinned(self, found: Found) -> None:
        """Pin '?' to the answer, so the other views show the what-if on that day."""
        day = found.day
        if day is None or found.search == self._unpinned:
            return
        if self.session.scenario.pinned != day:
            self.call_after_refresh(self._pin, day)

    def _pin(self, day: date | None) -> None:
        s = self.session
        if s.scenario.pinned != day and (day is None or s.scenario.placeholders):
            s.pin(day)
            bdbd(self).refresh_views()

    def _busy(self, on: bool) -> None:
        if self._spinner is not None:
            if on:
                self._spinner.resume()
            else:
                self._spinner.pause()

    def _tick(self) -> None:
        if self._asked is None or self.mode != "earliest":
            self._busy(False)
            return
        self._spin = (self._spin + 1) % len(SPINNER)
        self._draw_busy(self._asked)

    def _earliest_title(self) -> Content:
        return panel_title("Earliest date", _next(self.MONTHS))

    def _controls(self, floor: int) -> list[str | Content | Picker]:
        """' f floor $175 · m  balance  spare ' in the headline's bottom border."""
        key = f"bold {ACCENT}"
        lead = Content.assemble(
            " ",
            ("f", key),
            (" floor ", FAINT),
            (money_short(floor), FOREGROUND),
            (f" {DOT} ", FAINT),
            ("m", key),
            " ",
        )
        measure = Picker(
            [("balance", "balance"), ("spare", "spare")],
            chosen=self.measure,
            on_pick=self._measure,
        )
        return [lead, measure, " "]

    def _measure(self, value: object) -> None:
        if value != self.measure:
            self.action_measure()

    def _draw_busy(self, search: Search) -> None:
        text = Text.assemble(
            (f"{SPINNER[self._spin]} ", ACCENT),
            ("Trying every day", "bold"),
            (f" of {_next(self.MONTHS)}…", FAINT),
        )
        self._headline("WORKING", ACCENT, text, "accent", self._earliest_title())
        self.query_one("#headline", Panel).set_subtitle(*self._controls(search.floor))
        self._numbers([])
        what = "what's spare" if search.measure == "spare" else "your balance"
        self._note(
            Text(
                f"Looking for the first day that keeps {what} at or above "
                f"{money_short(search.floor)}. It takes a few seconds; the rest of bdbd "
                "keeps working.",
                style=FAINT,
            )
        )
        self._warnings([])

    def _draw_found(self, found: Found) -> None:
        s = self.session
        today = s.today
        search = found.search
        title = self._earliest_title()
        if found.data is None:
            self.set_class(False, "-found")
            self._headline(
                "NO ANSWER",
                AMBER,
                Text("Couldn't look for a date", style="bold"),
                "amber",
                title,
            )
            self.query_one("#headline", Panel).set_subtitle(*self._controls(search.floor))
            self._numbers([])
            self._note(Text(sentence(humanize(found.error or "")), style=AMBER))
            self._warnings([])
            return
        d = found.data
        spare = d["measure"] == "spare"
        what = "what's spare" if spare else "your balance"
        floor = cents_of(d["floor"])
        rows: list[list[str | Text]] = []
        day = found.day
        if day is not None:
            r = d["result"]
            self._headline(
                "WORKS FROM",
                GREEN,
                Text.assemble(
                    (fmt_date(day, today, weekday=True), "bold"),
                    (f" {DOT} {relative(day, today)}", FAINT),
                ),
                "green",
                title,
            )
            m = r["min_after"]
            low = cents_of(m["spare"] if spare else m["balance"])
            rows += [
                ["Floor", _bold(money(floor)), _faint(f"{what} stays at or above it")],
                [
                    "Lowest after",
                    _bold(money(low), balance_color(low, floor)),
                    _faint(fmt_date(date.fromisoformat(m["date"]), today, weekday=True)),
                ],
                [
                    "Headroom",
                    _bold(money(cents_of(r["headroom"])), GREEN),
                    _faint("above the floor at its tightest"),
                ],
            ]
            for e in r["placeholder_entries"]:
                c = cents_of(e["delta"])
                rows.append(
                    [
                        Text.assemble(("↳ ", ACCENT), (e["name"], FAINT)),
                        Text(money(c, sign=True), style=GREEN if c > 0 else ""),
                        Text(f"on {fmt_date(date.fromisoformat(e['date']), today)}", FAINT),
                    ]
                )
            pinned = s.scenario.pinned == day
            when = fmt_date(day, today, weekday=True)
            if pinned:
                note = Text.assemble(
                    (f"Every view now shows the what-if on {when}. ", FAINT),
                    *_keys(("u", "unpins it")),
                )
            else:
                note = Text.assemble(
                    ("Other views show your budget as it is. ", FAINT),
                    *_keys(("u", f"pins {when} again")),
                )
            self._note(note)
        else:
            self._headline(
                "NO DATE WORKS",
                AMBER,
                Text.assemble(("Not ", "bold"), (_next(self.MONTHS).removeprefix("the "), "bold")),
                "amber",
                title,
            )
            rows.append(["Floor", _bold(money(floor)), _faint(f"{what} stays at or above it")])
            best = d.get("best_infeasible")
            if best:
                miss = fmt_date(date.fromisoformat(best["date"]), today, weekday=True)
                rows.append(
                    [
                        "Closest miss",
                        _bold(money(cents_of(best["shortfall"])), AMBER),
                        _faint(f"short, on {miss}"),
                    ]
                )
            self._note(
                Text.assemble(*_keys(("f", "lowers the floor"), ("m", "measures the other way")))
            )
        self.query_one("#headline", Panel).set_subtitle(*self._controls(search.floor))
        self._numbers(rows)
        self._strip(found)
        self._balance(found)
        self._warnings(self._earliest_warnings(found))

    def _balance(self, found: Found) -> None:
        """The balance each day with the what-if on the day found (or the closest miss)."""
        day, search = found.shown, found.search
        if day is None or not found.daily:
            return
        balance = search.measure == "balance"
        chart = self.query_one("#balance-chart", Chart)
        chart.set_series(found.daily, color=ACCENT, floor=search.floor if balance else None,
                         marker=day)  # fmt: skip
        when = fmt_date(day, self.session.today, weekday=True)
        on = f"on {when}" if found.day else f"on {when}, the closest miss"
        panel = self.query_one("#balance-panel", Panel)
        panel.fit_title("With the what-if", on, "red below the floor" if balance else "")

    def _strip(self, found: Found) -> None:
        d = found.data
        if d is None:
            return
        self.set_class(True, "-found")
        today = self.session.today
        cand = d["candidates"]
        first, last = date.fromisoformat(cand["from"]), date.fromisoformat(cand["before"])
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        day = found.day
        if day is not None:
            through = date.fromisoformat(d["feasible_through"])
            cells = [(x, day <= x <= through) for x in days if x <= through + timedelta(days=1)]
        else:
            cells = [(x, False) for x in days]
        self.query_one("#strip", DateStrip).show(cells, day)
        panel = self.query_one("#strip-panel", Panel)
        panel.fit_title("Days tried", "red too soon, green works")
        spare = d["measure"] == "spare"
        miss = d.get("last_infeasible")
        earlier = self.query_one("#earlier", Static)
        if day is not None and miss:
            m = miss["min_after"]
            low = cents_of(m["spare"] if spare else m["balance"])
            what = "what's spare" if spare else "your balance"
            miss_day = date.fromisoformat(miss["date"])
            dip_day = date.fromisoformat(m["date"])
            dip = (
                "that day"
                if dip_day == miss_day
                else f"on {fmt_date(dip_day, today, weekday=True)}"
            )
            earlier.update(
                Text.assemble(
                    (f"A day earlier, on {fmt_date(miss_day, today, weekday=True)}, ", FAINT),
                    (f"{what} would dip to ", FAINT),
                    (money(low), f"bold {balance_color(low, cents_of(d['floor']))}"),
                    (f" {dip}.", FAINT),
                )
            )
        elif day is not None:
            earlier.update(Text("It works from the very first day.", style=FAINT))
        else:
            earlier.update(Text("Every day tried dips below the floor later on.", style=FAINT))

    def _earliest_warnings(self, found: Found) -> list[Text]:
        d = found.data or {}
        lines: list[Text] = []
        if not found.search.start.known:
            lines.append(no_balance_note())
        day = found.day
        last = d.get("candidates", {}).get("before")
        through = d.get("feasible_through")
        if day is not None and through and last and through < last:
            back = date.fromisoformat(through) + timedelta(days=1)
            today = self.session.today
            lines.append(
                Text(
                    f"! It stops working again on {fmt_date(back, today, weekday=True)}: a later "
                    "bill lands before the next income, so the earliest day isn't a one-way door.",
                    style=AMBER,
                )
            )
        for w in found.warnings:
            if w.startswith(("feasible from", "no date between", NO_BALANCE)):
                continue  # said above, in words
            lines.append(warning_line(w))
        return lines

    # ── Keys ─────────────────────────────────────────────────────────────────────

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("shorter", "longer"):
            return self.mode == "compare"
        if action == "table":  # only a short window has to choose
            return self.mode == "compare" and self.has_class("-short")
        if action in ("floor", "measure", "pin"):
            return self.mode == "earliest"
        if action in ("edit", "switch", "remove", "clear"):
            return not self.has_class("-empty")
        return True

    def _selected(self) -> int | str | None:
        key = self.query_one("#changes", RowList).key
        return key if isinstance(key, int | str) else None

    def action_add(self) -> None:
        """a: a new change."""
        change_form.open_add(self.app)

    def action_edit(self) -> None:
        """e or enter: change the selected change."""
        key = self._selected()
        if key == "plan":
            bdbd(self).notify("That's a payoff plan: change it in Debts (5) with p.")
        elif isinstance(key, int):
            change_form.open_edit(self.app, key)

    def on_row_list_selected(self, event: RowList.Selected) -> None:
        if event.row_list.id == "changes":
            self.action_edit()

    def action_switch(self) -> None:
        """space: switch the selected change on or off (nothing else moves)."""
        s = self.session
        key = self._selected()
        if key == "plan":
            s.toggle_extra()
            verb = "Switched on" if s.scenario.extra_enabled else "Switched off"
            bdbd(self).changed(f"{verb}: {s.scenario.extra_label or 'the payoff plan'}")
            return
        if not isinstance(key, int) or key >= len(s.scenario.changes):
            return
        change = s.scenario.changes[key]
        s.toggle_change(key)
        verb = "Switched off" if change.enabled else "Switched on"
        bdbd(self).changed(f"{verb}: {s.scenario.sentence(change, s.today)}")

    def action_remove(self) -> None:
        """x: take the selected change out of the what-if (a tried plan asks first: it can
        only be made again in the payoff plan dialog)."""
        s = self.session
        key = self._selected()
        app = bdbd(self)
        if key == "plan":
            label = s.scenario.extra_label or "the payoff plan"

            def drop() -> None:
                s.set_extra(None)
                app.changed(f"Removed: {label}")

            app.confirm(
                "Remove the payoff plan?",
                f"{label} comes out of the what-if. Your budget itself isn't touched.",
                drop,
                yes="Remove",
            )
            return
        if not isinstance(key, int) or key >= len(s.scenario.changes):
            return
        words = s.scenario.sentence(s.scenario.changes[key], s.today)
        s.remove_change(key)
        app.changed(f"Removed: {words} (nothing was saved, so nothing else changes)")

    def action_clear(self) -> None:
        """c: empty the what-if (asks first)."""
        s = self.session
        n = len(s.scenario.changes) + (s.scenario.extra is not None)
        if not n:
            return

        def clear() -> None:
            s.clear_changes()
            bdbd(self).changed("Cleared the what-if · every view shows your budget as it is")

        what = "this change" if n == 1 else f"all {n} changes"
        bdbd(self).confirm(
            "Clear the what-if?",
            f"This takes out {what}. Your budget itself isn't touched.",
            clear,
            yes="Clear",
        )

    def action_shorter(self) -> None:
        """[: the next shorter horizon."""
        if self.horizon == 0:
            self.app.bell()
            return
        self.horizon -= 1
        self.refresh_view()

    def action_longer(self) -> None:
        """]: the next longer horizon."""
        if self.horizon == len(HORIZONS) - 1:
            self.app.bell()
            return
        self.horizon += 1
        self.refresh_view()

    def action_table(self) -> None:
        """v: in a short window, the chart or the months."""
        self.toggle_class("-table")

    def action_measure(self) -> None:
        """m: the balance, or what's spare (the balance minus bills due before payday)."""
        self.measure = "spare" if self.measure == "balance" else "balance"
        self._restart()

    def action_floor(self) -> None:
        """f: the floor the balance must stay at or above."""
        s = self.session
        default = default_floor(s)

        def read(text: str) -> tuple[bool, str]:
            if not text.strip():
                return True, f"Blank: one week of everyday spending, {money_short(default)}"
            try:
                cents = parse_amount(text, allow_negative=True)
            except CashError:
                return False, "An amount, e.g. 500 (or blank for a week of everyday spending)"
            what = "what's spare" if self.measure == "spare" else "your balance"
            return True, f"Keeps {what} at or above {money(cents)} from that day on"

        def chosen(text: str) -> None:
            self.floor = parse_amount(text, allow_negative=True) if text.strip() else None
            self._restart()

        current = self.floor if self.floor is not None else default
        bdbd(self).prompt(
            "The floor",
            "Keep above",
            chosen,
            value=money(current, symbol=False).removesuffix(".00"),
            preview=read,
        )

    def _restart(self) -> None:
        """The floor or measure moved: unpin the old date and look again."""
        self._unpinned = None
        if self.session.scenario.pinned is not None:
            self.session.pin(None)
            bdbd(self).refresh_views()  # draws this view too
        if self.mode == "earliest":
            self.refresh_view()

    def action_pin(self) -> None:
        """u: unpin the earliest date (other views go back to the budget as it is), or pin it."""
        s = self.session
        found = self._found
        day = found.day if found else None
        if found is None or day is None or found.search != self._search():
            self.app.bell()
            return
        if s.scenario.pinned == day:
            self._unpinned = found.search
            self._pin(None)
            bdbd(self).notify("Unpinned · other views show your budget as it is")
        else:
            self._unpinned = None
            self._pin(day)
            bdbd(self).notify(
                f"Pinned · every view shows the what-if on {fmt_date(day, s.today, weekday=True)}"
            )

    # ── Events ───────────────────────────────────────────────────────────────────

    def on_row_list_highlighted(self, event: RowList.Highlighted) -> None:
        if event.row_list.id == "months":
            self._mark()
        elif event.row_list.id == "changes":
            self._card()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        if self.mode == "compare":
            self._mark()

    def on_descendant_blur(self, event: events.DescendantBlur) -> None:
        if self.mode == "compare":
            self.call_after_refresh(self._mark)

    def on_resize(self) -> None:
        """The lowest balance's note loses its date on narrow screens; a short window offers v."""
        if self.mode == "compare" and self._shown is not None:
            self._compare_headline(self._shown)
        self.call_after_refresh(self.refresh_bindings)


# ── Pieces ────────────────────────────────────────────────────────────────────


def _next(months: int) -> str:
    """'the next year', 'the next 2 years', 'the next 6 months'."""
    return "the next year" if months == 12 else f"the next {span(months)}"


def _or(value: int | None, default: int) -> int:
    return default if value is None else value


def _keys(*pairs: tuple[str, str]) -> list[tuple[str, str]]:
    """'e edit it · x remove it' as Text.assemble parts (keys bold accent, words faint)."""
    parts: list[tuple[str, str]] = []
    for i, (key, words) in enumerate(pairs):
        if i:
            parts.append((f" {DOT} ", FAINT))
        parts += [(key, f"bold {ACCENT}"), (f" {words}", FAINT)]
    return parts


def _names(names: list[str]) -> str:
    """'Electric, Internet and Phone' (or '… and 3 more')."""
    if len(names) > 4:
        return ", ".join(names[:3]) + f" and {len(names) - 3} more"
    return join(names)


def _bold(text: str, color: str = "") -> Text:
    return Text(text, style=f"bold {color}".strip())


def _faint(text: str) -> Text:
    return Text(text, style=FAINT)


def _cents(change: Change) -> int | None:
    """A change's amount of money in cents (None for a rate, or nothing)."""
    if not change.amount or change.kind == "rate_change":
        return None
    try:
        return parse_amount(change.amount)
    except CashError:
        return None


def _rate(text: str) -> str:
    try:
        return pct(parse_rate(text))
    except CashError:
        return text


def _diff_color(cents: int) -> str:
    """Ahead is green; behind is amber (red is for balances below zero)."""
    return GREEN if cents >= 0 else AMBER


def _versus(before: int, now: int) -> Text:
    """'vs $43,520.76 as it is', or 'same as it is'."""
    if before == now:
        return Text("same as it is", style=FAINT)
    return Text(f"vs {money(before)} as it is", style=FAINT)


def _rjust(cell: Text, width: int) -> Text:
    return Text.assemble(" " * (width - cell.cell_len), cell)


_CARD_TITLES = {  # the change card's title: 'Car loan · selling it'
    "payoff": "paying it off",
    "settle": "selling it",
    "extra_payment": "an extra payment",
    "set_payment": "a new payment",
    "rate_change": "a new rate",
    "add_income": "one-off money in",
    "add_expense": "one-off money out",
    "set_amount": "a new amount",
    "stop": "stopping it",
    "stop_tag": "stopping the tag",
    "disable": "leaving it out",
    "disable_tag": "leaving the tag out",
    "enable": "bringing it back",
    "everyday": "a new weekly amount",
}

_CHIPS = {  # the compare badge states the verdict
    "reached": "CATCHES UP",
    "immediate": "AHEAD",
    "identical": "NO DIFFERENCE",
    "never_in_horizon": "DOESN'T CATCH UP",
}
