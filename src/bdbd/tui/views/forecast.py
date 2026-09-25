"""The Forecast (3): the balance over the months ahead.

The same numbers as `bdbd project --until END`: where the balance starts and ends, what's spare
then, the lowest point, money in and out, interest paid and the debt left at the end. A chart of
every day's closing balance carries a marker at the selected row's date; below it, every
scheduled item with the balance after it, or the months one by one (m). The horizon runs from
1m to 5y ([ ] or - +), or to any day (g).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from functools import cached_property
from typing import ClassVar

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.widgets import Static
from textual.worker import Worker, WorkerState

from bdbd.budget import Start
from bdbd.core import engine
from bdbd.core.dates import add_months, month_end
from bdbd.core.engine import LedgerEntry
from bdbd.core.errors import CashError
from bdbd.core.models import EffectiveModel
from bdbd.core.queries.project import SpareCalculator, extremes
from bdbd.core.queries.project import project as project_query
from bdbd.tui.session import Session
from bdbd.tui.text import (
    DEBT_KINDS,
    STALE_DAYS,
    bold_balance,
    ledger_key,
    no_balance_note,
    what_if_only,
)
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
    signed,
    warning_line,
)
from bdbd.ui.common import closing_balances, start_note
from bdbd.ui.theme import (
    ACCENT,
    AMBER,
    DOT,
    FAINT,
    FOREGROUND,
    GREEN,
    PURPLE,
    balance_color,
    cents_of,
    money,
    money_short,
)
from bdbd.words import MONTH_SHORT, fmt_date, fmt_month, join, parse_day, relative, span

HORIZONS: tuple[tuple[str, int], ...] = (
    ("1m", 1),
    ("3m", 3),
    ("6m", 6),
    ("1y", 12),
    ("2y", 24),
    ("5y", 60),
)
DEFAULT_HORIZON = 1  # 3m
MAX_MONTHS = 120  # a custom end date can be up to ten years out
NO_BALANCE = "no balance recorded: this projection starts from 0"
NO_INCOME = "no income found within"  # the Spare row says it in words already
MONTH_HEADS = ("In", "Out", "Net", "Balance", "Spare")
DATE_EXAMPLES = "dec 12, +18m, mar 2028 or in 2 years"
WORKER_DAYS = 800  # windows longer than this are worked out in a thread (5y takes ~0.1 s)
EN_DASH = "\u2013"
NOTE_ROOM = 29  # what the balances' notes need ('est. from $4,120.00 on Sep 22')
HORIZON = Binding.Group("horizon")


# ── The numbers ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Forecast:
    """One projection: what `bdbd project --until` says, and the day-by-day run behind it."""

    until: date
    data: dict  # project_query's data, exactly as the JSON command gets it
    warnings: list[str]
    run: engine.SimResult
    start: Start
    weekly: int  # everyday spending per week (the what-if can change it)
    items: list[LedgerEntry]  # every scheduled item, without the everyday spending
    after: list[int]  # the balance shown beside each item (as the calendar shows it)

    @cached_property
    def closing(self) -> dict[date, int]:
        """The balance at the end of each day in the window."""
        return dict(self.run.daily)


@dataclass(frozen=True)
class Baseline:
    """The budget as it is over the same days, to measure a what-if against.

    The same computations `bdbd project` makes without what-if flags, only the ones the view
    compares: the ending balance, what's spare then, the lowest balance, and the ledger.
    """

    ending: int
    spare: int | None
    lowest: int
    ledger: frozenset[tuple]


@dataclass(frozen=True)
class Inputs:
    """What a forecast needs, taken from the session on the main thread, so the numbers can
    be worked out in a thread (which must never touch the sqlite connection)."""

    today: date
    start: Start
    model: EffectiveModel  # with the what-if when its lens is on
    weekly: int
    base_model: EffectiveModel | None  # the budget as it is, while the lens is on
    base_weekly: int

    @classmethod
    def of(cls, session: Session) -> Inputs:
        model = session.model()
        base = session.model(baseline=True) if session.lens_on else None
        return cls(
            today=session.today,
            start=session.start(),
            model=model,
            weekly=session.weekly(model),
            base_model=base,
            base_weekly=session.weekly(base) if base else 0,
        )


Answer = tuple[Forecast, Baseline | None]


def forecast(inputs: Inputs, until: date) -> Forecast:
    """`bdbd project --until UNTIL` from today, computed the way the project command does it:
    its model (what-if aware), its starting balance and its everyday spending."""
    today, start, model, weekly = inputs.today, inputs.start, inputs.model, inputs.weekly
    data, warnings = project_query(
        model,
        as_of=today,
        until=until,
        starting_balance_cents=start.cents,
        granularity="monthly",
        weekly_spend_cents=weekly,
    )
    if not start.known:
        warnings.append(NO_BALANCE)
    run = engine.run(
        model,
        as_of=today,
        until=until,
        starting_balance_cents=start.cents,
        weekly_spend_cents=weekly,
    )
    items = [e for e in run.ledger if e.kind != "lifestyle"]
    return Forecast(
        until=until,
        data=data,
        warnings=list(dict.fromkeys(warnings)),
        run=run,
        start=start,
        weekly=weekly,
        items=items,
        after=closing_balances(items, run.daily),
    )


def baseline(inputs: Inputs, until: date) -> Baseline | None:
    """The as-it-is numbers a what-if is measured against (None while the lens is off)."""
    model, weekly, today = inputs.base_model, inputs.base_weekly, inputs.today
    if model is None:
        return None
    run = engine.run(
        model,
        as_of=today,
        until=until,
        starting_balance_cents=inputs.start.cents,
        weekly_spend_cents=weekly,
    )
    spare = SpareCalculator(model, today, until, weekly).compute(until, run.ending_balance_cents)[
        "spare_balance"
    ]
    lowest, _ = extremes(run)
    return Baseline(
        ending=run.ending_balance_cents,
        spare=None if spare is None else cents_of(spare),
        lowest=cents_of(lowest["balance"]),
        ledger=frozenset(ledger_key(e) for e in run.ledger),
    )


def answer(inputs: Inputs, until: date) -> Answer:
    """Everything the view shows for one window."""
    return forecast(inputs, until), baseline(inputs, until)


def read_until(text: str, today: date) -> date:
    """A custom end date from words ('dec 12', '+18m'); raises CashError with what would work."""
    try:
        day = parse_day(text, today=today, base=today)
    except CashError as exc:
        if not text.strip():
            raise CashError(f"A day after today, like {DATE_EXAMPLES}", "invalid_date") from exc
        raise CashError(
            f"Couldn't read {text.strip()!r} as a date; try {DATE_EXAMPLES}", "invalid_date"
        ) from exc
    if day <= today:
        raise CashError(f"Pick a day after today, like {DATE_EXAMPLES}", "invalid_date")
    if day > add_months(today, MAX_MONTHS):
        raise CashError("Pick a day within ten years", "invalid_date")
    return day


# ── The view ──────────────────────────────────────────────────────────────────


class ForecastView(View):
    """The balance over the months ahead: headline numbers, a chart, every item or month."""

    title = "Forecast"

    DEFAULT_CSS = """
    ForecastView {
        layout: vertical;
        & #headline { height: auto; }
        & #numbers { height: auto; }
        & #left { width: 1fr; height: auto; margin-right: 4; }
        & #flows { width: auto; max-width: 50%; }
        & #paid { display: none; height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
        &.-paid #paid { display: block; }
        & #chart-panel { height: 1fr; min-height: 7; }
        & #list-panel { height: 1fr; min-height: 5; }
        & RowList { height: 1fr; }
        & #months, #months-heads { display: none; }
        &.-by-month #transactions { display: none; }
        &.-by-month #months, &.-by-month #months-heads { display: block; }
        & #warnings { height: auto; padding: 0 1; }
        & #welcome { display: none; }
        &.-empty > * { display: none; }
        &.-empty > #welcome { display: block; }
        &.-narrow #left { margin-right: 2; }
        &.-short #headline { padding: 0 2; }
    }
    """

    BINDINGS: ClassVar = [
        Binding("left_square_bracket", "shorter", "shorter horizon", group=HORIZON),
        Binding("right_square_bracket", "longer", "longer horizon", group=HORIZON),
        Binding("minus", "shorter", "shorter horizon", show=False),
        Binding("plus,equals_sign", "longer", "longer horizon", show=False),
        Binding("g", "until", "forecast until a date", show=False),
        Binding("v", "show_months", "month by month"),
        Binding("v", "show_transactions", "transactions"),
    ]

    def __init__(self, *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self.horizon: int | None = DEFAULT_HORIZON  # an index into HORIZONS; None: custom
        self.custom: date | None = None  # the end date chosen with g
        self.by_month = False
        self._shown: Forecast | None = None
        self._base: Baseline | None = None
        self._memo: dict[tuple, Answer] = {}  # (session version, until, lens) -> answer

    def compose(self) -> ComposeResult:
        with Panel(id="headline", variant="accent"), Horizontal(id="numbers"):
            with Vertical(id="left"):
                yield KeyValues(id="balances")
                yield Static(id="paid")
            yield KeyValues(id="flows")
        yield Panel(Chart(id="chart"), id="chart-panel")
        with Panel(id="list-panel"):
            months = HeadedList(
                Column(flex=True, min=8),
                *(Column(align="right") for _ in MONTH_HEADS),
                heads=["", *MONTH_HEADS],
                id="months",
            )
            months.heads.id = "months-heads"
            yield months.heads
            yield RowList(
                Column(style=FAINT),
                Column(flex=True, min=8),
                Column(align="right"),
                Column(align="right"),
                empty="Nothing scheduled in this stretch.",
                id="transactions",
            )
            yield months
        yield Static(id="warnings")
        yield EmptyState(
            "Nothing to forecast yet.",
            "Add your paycheck and your bills, and this shows your balance "
            "over the months ahead, day by day.",
            keys=[("a", "add your paycheck"), ("b", "record your balance")],
            id="welcome",
        )

    # the horizon ----------------------------------------------------------------------

    @property
    def until(self) -> date:
        """The last day of the forecast."""
        if self.custom is not None:
            return self.custom
        return add_months(self.session.today, HORIZONS[self.horizon or 0][1])

    def _preset_ends(self) -> list[date]:
        today = self.session.today
        return [add_months(today, months) for _, months in HORIZONS]

    def set_until(self, day: date) -> None:
        """Forecast to `day` (a preset when it's one of theirs, else a custom end)."""
        ends = self._preset_ends()
        if day in ends:
            self.horizon, self.custom = ends.index(day), None
        else:
            self.horizon, self.custom = None, day
        self.refresh_view()

    def action_shorter(self) -> None:
        """[ or -: the next shorter horizon."""
        until, ends = self.until, self._preset_ends()
        shorter = [i for i, end in enumerate(ends) if end < until]
        if not shorter:
            self.app.bell()
            return
        self.set_until(ends[shorter[-1]])

    def action_longer(self) -> None:
        """] or +: the next longer horizon."""
        until, ends = self.until, self._preset_ends()
        longer = [i for i, end in enumerate(ends) if end > until]
        if not longer:
            self.app.bell()
            return
        self.set_until(ends[longer[0]])

    def action_until(self) -> None:
        """g: forecast until any day, typed in words."""
        today = self.session.today

        def preview(text: str) -> tuple[bool, str]:
            try:
                day = read_until(text, today)
            except CashError as exc:
                return False, exc.message
            return True, f"{fmt_date(day, today, weekday=True)} {DOT} {relative(day, today)}"

        def chosen(text: str) -> None:
            try:
                self.set_until(read_until(text, today))
            except CashError as exc:  # the preview refuses these first
                bdbd(self).notify(exc.message, severity="error", markup=False)

        bdbd(self).prompt("Forecast until", "Until", chosen, preview=preview)

    def action_months(self) -> None:
        """v: Transactions, or Month by month."""
        self.by_month = not self.by_month
        self._show_mode()
        self._list().focus()
        self._mark()
        self.refresh_bindings()

    def action_show_months(self) -> None:
        self.action_months()

    def action_show_transactions(self) -> None:
        self.action_months()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """v's footer label names where it goes."""
        if action == "show_months":
            return not self.by_month
        if action == "show_transactions":
            return self.by_month
        return True

    # drawing ---------------------------------------------------------------------------

    def refresh_view(self) -> None:
        s = self.session
        empty, was_empty = not s.flows(), self.has_class("-empty")
        self.set_class(empty, "-empty")
        if empty:
            self._shown = None
            return
        if was_empty and self.screen.focused is None:  # the first flow just arrived
            self.call_after_refresh(self.focus_default)
        if self.custom is not None and self.custom <= s.today:  # a new day caught it up
            self.horizon, self.custom = DEFAULT_HORIZON, None
        until = self.until
        key = self._key(until)
        known = self._memo.get(key)
        if known is None and (until - s.today).days > WORKER_DAYS:
            self._title(until, busy=True)  # the numbers follow when the thread is done
            self._work(key, Inputs.of(s), until)
            return
        self._draw(known or self._remember(key, answer(Inputs.of(s), until)))

    def _key(self, until: date) -> tuple:
        return (self.session.version, until, self.session.lens_on)

    def _remember(self, key: tuple, found: Answer) -> Answer:
        """Keep answers for the session's current version only."""
        self._memo = {k: v for k, v in self._memo.items() if k[0] == key[0]}
        self._memo[key] = found
        return found

    @work(thread=True, exclusive=True, group="forecast", exit_on_error=False)
    def _work(self, key: tuple, inputs: Inputs, until: date) -> tuple[tuple, Answer]:
        """A long window's numbers, worked out from a snapshot (no sqlite in here)."""
        return key, answer(inputs, until)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group != "forecast":
            return
        if event.state == WorkerState.ERROR:
            box = self.query_one("#warnings", Static)
            box.update(Text(f"! Couldn't work this stretch out: {event.worker.error}", AMBER))
            box.display = True
            if self._shown is not None:
                self._title(self._shown.until)
            return
        result = event.worker.result
        if event.state != WorkerState.SUCCESS or not result:
            return
        key, found = result
        if key[0] != self.session.version:
            return  # the budget moved on while it worked
        self._remember(key, found)
        if key == self._key(self.until) and not self.has_class("-empty"):
            self._draw(found)

    def _draw(self, found: Answer) -> None:
        f, base = found
        self._shown, self._base = f, base
        self._headline(f, base)
        self._chart(f)
        self._transactions(f, base)
        self._months(f)
        self._warnings(f)
        self._show_mode()
        self._mark()

    def _headline(self, f: Forecast, base: Baseline | None) -> None:
        """The key numbers. Narrow screens get shorter notes; the totals' notes go first."""
        today = self.session.today
        narrow = self.has_class("-narrow")
        low = f.weekly
        d = f.data
        b = base

        def dated(day: date) -> str:
            return fmt_date(day, today, weekday=True)

        def noted(wide: str | Text, short: str, now: int, before: int | None) -> Text:
            if b is None:
                return Text(short, style=FAINT) if narrow else _faint(wide)
            if narrow:  # no room for both: the difference says more
                return _noted("", now, before)
            return _noted(short, now, before)

        end = cents_of(d["ending_balance"])
        rows: list[list[str | Text]] = [
            [
                "Starts at",
                self._start_value(f.start, low),
                Text(),  # the note once the room is known
            ],
            [
                "Ends at",
                bold_balance(end, low),
                noted(
                    f"end of {dated(f.until)}",
                    fmt_date(f.until, today),
                    end,
                    b.ending if b else None,
                ),
            ],
        ]
        spare = d["spare"] or {}
        nxt = spare.get("next_income")
        spare_row: list[str | Text] | None = None
        if d["spare_balance"] is not None and nxt:  # its note once the room is known
            spare_row = ["Spare then", bold_balance(cents_of(d["spare_balance"]), low), Text()]
            rows.append(spare_row)
        else:
            rows.append(
                [
                    "Spare then",
                    Text("—", style=FAINT),
                    Text("no income ahead" if narrow else "no income ahead to count to", FAINT),
                ]
            )
        lo = d["min_balance"]
        lo_cents, lo_day = cents_of(lo["balance"]), date.fromisoformat(lo["date"])
        rows.append(
            [
                "Lowest",
                bold_balance(lo_cents, low),
                noted(
                    f"{dated(lo_day)} {DOT} {relative(lo_day, today)}",
                    dated(lo_day),
                    lo_cents,
                    b.lowest if b else None,
                ),
            ]
        )

        totals = d["totals"]
        income, expense = cents_of(totals["income"]), cents_of(totals["expense"])
        everyday = cents_of(d["lifestyle_total"])
        flows: list[list[str | Text]] = [
            [
                "Money in",
                Text(money(income, sign=True), style=f"bold {GREEN}" if income else "bold"),
                f"net {money(cents_of(totals['net']), sign=True)}",
            ],
            ["Money out", Text(money(-expense), style="bold"), ""],
        ]
        if everyday or f.weekly:
            weekly = f"{money_short(f.weekly)}/week" if f.weekly else ""
            flows.append(["  everyday", Text(money(-everyday), style="bold"), weekly])
        if d["debts"]:
            interest = cents_of(totals["interest_paid"])
            principal = cents_of(totals["principal_paid"])
            left = cents_of(d["total_debt_at_until"])
            flows.append(
                [
                    "Interest paid",
                    Text(money(interest), style=f"bold {PURPLE}"),
                    f"{money(principal)} principal" if principal else "",
                ]
            )
            flows.append(
                [
                    "Debt left",
                    Text(money(left), style=f"bold {PURPLE}" if left else f"bold {GREEN}"),
                    "at the end" if left else "debt-free by the end",
                ]
            )
        # The balances' notes matter more: the totals keep theirs only when both fit, and
        # the balances' notes get whatever room the totals leave.
        inside = (self.size.width or self.screen.size.width - 2) - 6  # inside the panel
        gap = 2 if narrow else 4
        figures = KeyValues.width_of([r[:2] for r in rows]) + KeyValues.GAP
        if narrow or figures + NOTE_ROOM + gap + KeyValues.width_of(flows) > inside:
            flows = [row[:2] for row in flows]
        room = inside - gap - KeyValues.width_of(flows) - figures
        rows[0][2] = self._start_note(f.start, room)
        if spare_row is not None and nxt is not None:
            cents = cents_of(d["spare_balance"])
            payday = date.fromisoformat(nxt["date"])
            spare_row[2] = noted(
                _spare_note(spare, today, room),
                f"until {fmt_date(payday, today)}",
                cents,
                b.spare if b else None,
            )
        self.query_one("#balances", KeyValues).set_rows(rows)
        self.query_one("#flows", KeyValues).set_rows(flows)

        paid = self._paid_off(f)
        line = Text("✓ ", style=GREEN)
        if len(paid) == 1:  # '✓ Credit card paid off Dec 2028'
            line.append(paid[0][0], style=FOREGROUND)
            line.append(f" paid off {fmt_month(paid[0][1])}", style=FAINT)
        else:  # '✓ Paid off Credit card Dec 2028 · Car loan Feb 2030'
            line.append("Paid off ", style=FAINT)
            for i, (name, day) in enumerate(paid):
                line.append(f" {DOT} " if i else "", style=FAINT)
                line.append(name, style=FOREGROUND)
                line.append(f" {fmt_month(day)}", style=FAINT)
        self.query_one("#paid", Static).update(line)
        self.set_class(bool(paid), "-paid")

        self._title(f.until)

    def _title(self, until: date, *, busy: bool = False) -> None:
        """The headline's border: the stretch of days, and the horizon picker.

        While a long window is worked out, the title keeps the stretch whose numbers still
        show (and says what's coming), so the two never disagree.
        """
        today = self.session.today
        panel = self.query_one("#headline", Panel)
        shown = self._shown.until if busy and self._shown is not None else until
        stretch = f"{fmt_date(today, today, weekday=True)} → {fmt_date(shown, today, weekday=True)}"
        if busy:
            extra = f"working out {self._length(until)}…"
            length = self._length(shown) if self._shown is not None else ""
        else:
            extra = "with the what-if" if self.session.lens_on else ""
            length = self._length(until)
        panel.fit_title(stretch, length, extra)
        panel.set_subtitle(*self._picker())

    def _start_value(self, start: Start, low: int) -> Text:
        if not start.known:
            return Text(money(0), style=f"bold {AMBER}")
        return bold_balance(start.cents, low)

    def _start_note(self, start: Start, room: int) -> Text:
        """Where Starts at comes from: the longest way of saying it that fits `room`."""
        today = self.session.today
        rec = start.recorded
        key = f"bold {ACCENT}"
        if start.source == "carried" and rec and (today - rec.as_of).days > STALE_DAYS:
            ago = f"recorded {relative(rec.as_of, today)}"
            tries = [
                Text.assemble((f"{ago} {DOT} ", AMBER), ("b", key), (" to update it", AMBER)),
                Text.assemble((f"{ago} {DOT} ", AMBER), ("b", key)),
                Text(ago, style=AMBER),
            ]
        elif start.source == "carried" and rec:
            short = Text(f"est. from {fmt_date(rec.as_of, today)}", style=FAINT)
            tries = [start_note(start, today), short]
        elif not start.known:
            tries = [
                no_balance_note(),
                Text.assemble(("no balance recorded ", AMBER), (f"{DOT} ", AMBER), ("b", key),
                              (" to record it", AMBER)),
                Text.assemble((f"no balance recorded {DOT} ", AMBER), ("b", key)),
                Text.assemble((f"no balance {DOT} ", AMBER), ("b", key)),
            ]  # fmt: skip
        else:
            items = self._shown.run.ledger if self._shown else ()
            tries = [start_note(start, today, items), start_note(start, today)]
        return next((t for t in tries if t.cell_len <= room), tries[-1])

    def _length(self, until: date) -> str:
        """'3 months', '2 years', or '5 months' for a custom day."""
        if self.custom is None and self.horizon is not None:
            return span(HORIZONS[self.horizon][1])
        return relative(until, self.session.today).removeprefix("in ")

    def _picker(self) -> list[str | Content | Picker]:
        """The horizon picker in the headline's bottom border (clickable), then g."""
        choices: list[tuple[object, str]] = [(i, label) for i, (label, _) in enumerate(HORIZONS)]
        chosen: object = self.horizon if self.custom is None else "custom"
        if self.custom is not None:
            choices.append(("custom", fmt_date(self.custom, self.session.today)))
        picker = Picker(choices, chosen=chosen, on_pick=self._pick_horizon)
        tail = (
            Content(" ")
            if self.custom is not None
            else Content.assemble(
                (f" {DOT} ", FAINT), ("g", f"bold {ACCENT}"), (" any day ", FAINT)
            )
        )
        return [" ", picker, tail]

    def _pick_horizon(self, choice: object) -> None:
        if isinstance(choice, int) and (choice != self.horizon or self.custom is not None):
            self.horizon, self.custom = choice, None
            self.refresh_view()

    def _paid_off(self, f: Forecast) -> list[tuple[str, date]]:
        """Debts paid off inside the window, soonest first."""
        today = self.session.today
        done = [
            (d["name"], date.fromisoformat(d["paid_off_on"]))
            for d in f.data["debts"]
            if d["paid_off_on"]
        ]
        return sorted(
            ((name, day) for name, day in done if today <= day <= f.until), key=lambda t: t[1]
        )

    def _chart(self, f: Forecast) -> None:
        self.query_one("#chart", Chart).set_series(f.run.daily, marker=self._marker_day())
        self.query_one("#chart-panel", Panel).fit_title("Balance at the end of each day")

    def _transactions(self, f: Forecast, base: Baseline | None) -> None:
        """Every item with the balance after it. Cells are built as Content: five years is
        a thousand rows, and this skips converting each one from rich Text."""
        today = self.session.today
        low = f.weekly
        paid = {(d["flow"], d["paid_off_on"]) for d in f.data["debts"] if d["paid_off_on"]}
        last_payment: dict[tuple, int] = {}  # the item that clears each debt
        for i, e in enumerate(f.items):
            if e.kind in DEBT_KINDS and (e.key, e.date.isoformat()) in paid:
                last_payment[(e.key, e.date)] = i
        cleared = set(last_payment.values())
        rows: list[Item] = []
        week = None
        seen: dict[tuple, int] = {}
        for i, (e, after) in enumerate(zip(f.items, f.after, strict=True)):
            this_week = e.date.isocalendar()[:2]
            if week is not None and this_week != week:
                rows.append(None)
            week = this_week
            name: list[str | tuple[str, str]] = [e.name]
            if base is not None and what_if_only(e, base.ledger):
                name.insert(0, ("↳ ", ACCENT))
            if e.kind in DEBT_KINDS:
                name.append((" ◆", PURPLE))
            if i in cleared:
                name.append(("  ✓ paid off", GREEN))
            delta = e.delta_cents
            ident = (e.key, e.date, e.kind)
            n = seen.get(ident, 0)
            seen[ident] = n + 1
            rows.append(
                Row(
                    Content.styled(fmt_date(e.date, today, weekday=True), FAINT),
                    Content.assemble(*name),
                    Content.styled(money(delta, sign=True), GREEN) if delta > 0 else money(delta),
                    Content.styled(money(after), balance_color(after, low)),
                    key=(*ident, n),
                )
            )
        self.query_one("#transactions", RowList).set_rows(rows)

    def _months(self, f: Forecast) -> None:
        low = f.weekly
        rows: list[Item] = []
        previous: date | None = None
        for r in f.data["series"]:
            day = date.fromisoformat(r["date"])
            inc, exp, net = cents_of(r["income"]), cents_of(r["expense"]), cents_of(r["net"])
            bal = cents_of(r["balance"])
            spare = cents_of(r["spare"]) if r.get("spare") is not None else None
            rows.append(
                Row(
                    _period(previous, day),
                    Text(money(inc, sign=True), style=GREEN) if inc else _dash(),
                    Text(money(-exp)) if exp else _dash(),
                    signed(net) if net else _dash(),
                    bold_balance(bal, low),
                    _dash()
                    if spare is None
                    else Text(money(spare), style=balance_color(spare, low)),
                    key=("month", day),
                )
            )
            previous = day
        self.query_one("#months", HeadedList).set_rows(rows)

    def _warnings(self, f: Forecast) -> None:
        lines = [  # the Spare row and Starts at say these already, in words
            warning_line(w) for w in f.warnings if not w.startswith(NO_INCOME) and w != NO_BALANCE
        ]
        box = self.query_one("#warnings", Static)
        box.update(Text("\n").join(lines))
        box.display = bool(lines)

    def _show_mode(self) -> None:
        self.set_class(self.by_month, "-by-month")
        panel = self.query_one("#list-panel", Panel)
        halves = Picker(
            [(False, "Transactions"), (True, "Month by month")],
            chosen=self.by_month,
            on_pick=lambda months: self.action_months() if months != self.by_month else None,
        )
        panel.set_title(" ", halves, " ")
        f = self._shown
        weekly = f.weekly if f else 0
        panel.set_subtitle(
            f" balances include {money_short(weekly)}/week everyday " if weekly else ""
        )

    def _list(self) -> RowList:
        return self.query_one("#months" if self.by_month else "#transactions", RowList)

    def _marker_day(self) -> date | None:
        key = self._list().key
        return key[1] if isinstance(key, tuple) and isinstance(key[1], date) else None

    def _mark(self) -> None:
        """Put the chart's marker on the selected row's day, and say that day's balance."""
        day = self._marker_day()
        self.query_one("#chart", Chart).marker = day
        panel = self.query_one("#chart-panel", Panel)
        f = self._shown
        cents = f.closing.get(day) if f and day else None
        if day is None or cents is None or f is None:
            panel.border_subtitle = ""
            return
        today = self.session.today
        panel.border_subtitle = Content.assemble(
            (f" ▲ {fmt_date(day, today, weekday=True)} {DOT} ends the day at ", FAINT),
            (money(cents), f"bold {balance_color(cents, f.weekly)}"),
            " ",
        )

    # events ----------------------------------------------------------------------------

    def on_resize(self) -> None:
        """The headline's notes follow the width (the app sets -narrow before this runs)."""
        f = self._shown
        if f is not None and self._key(f.until) in self._memo:
            self._headline(f, self._base)

    def on_row_list_highlighted(self, event: RowList.Highlighted) -> None:
        if event.row_list is self._list():
            self._mark()

    def on_row_list_selected(self, event: RowList.Selected) -> None:
        key = event.key
        if not isinstance(key, tuple):
            return
        if key[0] == "month":
            self._open_month(key[1])
            return
        flow = key[0]
        if isinstance(flow, int):
            bdbd(self).open_flow_card(flow)
        else:
            bdbd(self).notify("That's part of the what-if, not your budget.")

    def _open_month(self, day: date) -> None:
        """Enter on a month: its transactions, from the first one."""
        f = self._shown
        points = [date.fromisoformat(r["date"]) for r in f.data["series"]] if f else []
        if f is None or day not in points:
            return
        i = points.index(day)
        first = points[i - 1] + timedelta(days=1) if i else f.run.as_of
        rows = self.query_one("#transactions", RowList)
        for item in rows.items:
            key = item.key if isinstance(item, Row) else None
            if isinstance(key, tuple) and first <= key[1] <= day:
                rows.select_key(key)
                break
        self.action_months()


# ── Pieces ────────────────────────────────────────────────────────────────────


def _dash() -> Text:
    return Text("—", style=FAINT)


def _noted(note: str | Text, now: int | None, before: int | None) -> Text:
    """A headline note, led by how far the what-if moved the number ('+$650.00 vs now')."""
    text = _faint(note)
    if now is None or before is None:
        return text
    diff = now - before
    delta = Text(f"{money(diff, sign=True)} vs now" if diff else "same as now", FAINT)
    return Text.assemble(delta, f" {DOT} ", text) if text else delta


def _faint(note: str | Text) -> Text:
    return note if isinstance(note, Text) else Text(note, style=FAINT)


def _spare_note(spare: dict, today: date, room: int = 1000) -> str:
    """'until Paycheck on Fri Oct 2 · after Rent, Gym and everyday spending', or as much of it
    as fits in `room`: the bills it counts go first, then the weekday, never half a date."""
    nxt = spare["next_income"]
    day = date.fromisoformat(nxt["date"])
    names = [c["name"] for c in spare["committed_before_next_income"]]
    shown = list(dict.fromkeys(names))
    if len(shown) > 3:
        shown = [*shown[:3], f"{len(shown) - 3} more"]
    if cents_of(spare["committed_lifestyle"]):
        shown.append("everyday spending")
    long = f"until {nxt['name']} on {fmt_date(day, today, weekday=True)}"
    tries = [f"{long} {DOT} after {join(shown)}"] if shown else []
    tries += [
        long,
        f"until {nxt['name']} on {fmt_date(day, today)}",
        f"until {fmt_date(day, today)}",
    ]
    return next((t for t in tries if len(t) <= room), tries[-1])


def _period(previous: date | None, day: date) -> str:
    """A Month by month row's name: 'Today', 'Sep 25-30', 'Oct 2026', 'Dec 1-24' (en dashes)."""
    if previous is None:
        return "Today"
    first = previous + timedelta(days=1)
    if first.day == 1 and day == month_end(day):
        return fmt_month(day)
    days = f"{first.day}" if first == day else f"{first.day}{EN_DASH}{day.day}"
    return f"{MONTH_SHORT[day.month]} {days}"  # the full months around it say the year
