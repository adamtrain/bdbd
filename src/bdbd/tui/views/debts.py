"""The Debts view (5): what's owed, and when it's gone.

The headline and the list are `bdbd debts`: owed today, a month in payments, the interest to
go and the debt-free month, then every debt soonest-paid-off first. The selected debt shows
what `bdbd show NAME` and `bdbd debt schedule NAME --all` report: its terms in words, owed
today, the payoff with the payments and interest to go, the balance owed as a chart, the
events recorded on it, and every payment ahead. With the what-if on, all of it includes the
what-if (like those commands given the same flags).

Interest to go is measured from today everywhere (owed today + interest to go = everything
still to pay), and a debt that's never paid off at its payment says so instead of summing
decades of interest.

Keys: a add a debt · e (or d) loan terms · r record an event · x delete the selected event ·
u stop tracking it as a debt · p payoff plan · enter every payment (esc back).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import ClassVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.content import Content
from textual.widgets import Static

from bdbd import ask
from bdbd.core.errors import CashError
from bdbd.core.models import DebtEvent, Flow
from bdbd.tui.debt_forms import EventForm, PlanMemory, PlanScreen, outlook, schedule
from bdbd.tui.text import (
    PAYMENT_KINDS,
    event_amount,
    event_words,
    terms_words,
    versus,
)
from bdbd.tui.widgets import (
    Chart,
    Column,
    EmptyState,
    HeadedList,
    Item,
    KeyValues,
    Panel,
    Row,
    RowList,
    View,
    bdbd,
)
from bdbd.ui.common import schedule_text
from bdbd.ui.theme import (
    AMBER,
    DOT,
    FAINT,
    GREEN,
    PURPLE,
    bar,
    cents_of,
    money,
    pct,
    plural,
)
from bdbd.words import fmt_date, fmt_month, join, months_apart, relative, span

LIST_HEADS = ("", "Owed", "Rate", "Payment", "Time to go", "Paid off")
SCHEDULE_HEADS = ("#", "Date", "Payment", "Interest", "Principal", "Balance", "")
PROGRESS_CELLS = 12  # a schedule row's progress bar
SCHEDULE_MONTHS = 600  # `bdbd debt schedule`'s horizon


# ── The view ──────────────────────────────────────────────────────────────────


class DebtsView(View):
    """What's owed and when it's gone: every debt, the selected one in detail, and a plan."""

    title = "Debts"

    DEFAULT_CSS = """
    DebtsView {
        layout: vertical;
        & #debts-headline { height: auto; }
        & #debts-list-panel { height: auto; }
        & #debts-list { height: auto; max-height: 8; }
        & #debt-panel { height: auto; }
        & #debt-top { height: auto; }
        & #debt-facts { width: auto; max-width: 76; }
        & #debt-chart { width: 1fr; height: 5; margin-left: 4; }
        & #debt-events-title { height: 1; }
        & #debt-events { height: auto; max-height: 4; }
        & #schedule-panel { height: 1fr; min-height: 5; }
        & #debt-schedule { height: 1fr; }
        & #debts-empty { display: none; }
        &.-empty > * { display: none; }
        &.-empty > #debts-empty { display: block; }
        &.-short #debts-headline { padding: 0 2; }
        &.-short #debt-chart { display: none; }
        &.-short #debt-events-title { margin-top: 1; }
        &.-cramped #schedule-panel { display: none; }
        &.-cramped #debt-panel { height: 1fr; }
        &.-cramped.-schedule #schedule-panel { display: block; }
        &.-cramped.-schedule #debt-panel { display: none; }
    }
    """

    BINDINGS: ClassVar = [
        Binding("a", "add", "add a debt"),
        Binding("r", "record", "record an event"),
        Binding("p", "plan", "payoff plan"),
        Binding("e", "edit", "edit the loan terms", show=False),
        Binding("d", "edit", "edit the loan terms", show=False),
        Binding("x", "delete_event", "delete the selected event", show=False),
        Binding("u", "untrack", "stop tracking as a debt", show=False),
        Binding("enter", "schedule", "every payment", show=False),
        Binding("escape", "back", "back to the list", show=False),
    ]

    def __init__(self, *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self.plan_memory = PlanMemory()
        self._drawn_detail: tuple | None = None  # (debt, version, narrow) the detail shows
        self._drawn_narrow: bool | None = None
        self._debts = HeadedList(
            Column(flex=True, min=8),  # name
            Column(align="right"),  # owed
            Column(align="right", style=FAINT),  # rate
            Column(align="right"),  # payment
            Column(),  # payoff bar
            Column(),  # paid off
            heads=LIST_HEADS,
            id="debts-list",
        )
        self._schedule = HeadedList(
            Column(align="right", style=FAINT),  # n
            Column(flex=True),  # date
            Column(align="right"),  # payment
            Column(align="right", style=PURPLE),  # interest
            Column(align="right"),  # principal
            Column(align="right"),  # balance after
            Column(),  # progress
            heads=SCHEDULE_HEADS,
            empty="Nothing left to pay.",
            id="debt-schedule",
        )

    def compose(self) -> ComposeResult:
        yield Panel(KeyValues(id="debts-numbers"), id="debts-headline", variant="purple")
        yield Panel(self._debts.heads, self._debts, id="debts-list-panel")
        with Panel(id="debt-panel"):
            with Horizontal(id="debt-top"):
                yield KeyValues(id="debt-facts")
                yield Chart(color=PURPLE, id="debt-chart")
            yield Static(id="debt-events-title")
            yield RowList(
                Column(style=FAINT),  # date
                Column(),  # what
                Column(align="right"),  # amount
                Column(flex=True, style=FAINT),  # notes
                empty="Nothing recorded yet. Press r to record one.",
                id="debt-events",
            )
        yield Panel(self._schedule.heads, self._schedule, id="schedule-panel")
        yield EmptyState(id="debts-empty")

    # drawing ---------------------------------------------------------------------------

    def refresh_view(self) -> None:
        s = self.session
        rows = s.debts()
        empty = not rows
        was_empty = self.has_class("-empty")
        self.set_class(empty, "-empty")
        self.refresh_bindings()
        if empty:
            self._empty()
            return
        if was_empty and self.screen.focused is None:  # the first debt just arrived
            self.call_after_refresh(self.focus_default)
        base = s.debts(baseline=True) if s.lens_on else None
        self._drawn_narrow = self.has_class("-narrow")
        self._headline(rows, base)
        self._list(rows)
        self._drawn_detail = None
        self._detail()

    def _empty(self) -> None:
        paused = [f.name for f in self.session.flows() if f.debt is not None and not f.active]
        body = (
            "A loan or a card is an expense with what's owed, the rate and how interest works. "
            "Add one, or give an expense its loan terms in Budget (4) with d."
        )
        if paused:
            names = ", ".join(paused)
            body += f"\n\n{names} {'is' if len(paused) == 1 else 'are'} paused, so not counted."
        self.query_one("#debts-empty", EmptyState).show(
            "No debts. Lovely.", body, keys=[("a", "add a debt")]
        )

    def _headline(self, rows: list[ask.DebtRow], base: list[ask.DebtRow] | None) -> None:
        today = self.session.today
        narrow = self.has_class("-narrow")
        owed = sum(r.balance for r in rows)
        monthly = sum(r.monthly for r in rows)
        interest = sum(r.interest or 0 for r in rows)
        never = [r.name for r in rows if r.paid_off_on is None]
        free = debt_free(rows)
        numbers: list[list[str | Text]] = [
            [
                "Owed today",
                Text(money(owed), style="bold"),
                Text(f"{money(monthly)} a month in payments", FAINT),
            ],
            [
                "Interest to go",
                Text(money(interest), style=f"bold {PURPLE}"),
                Text(
                    f"not counting {join(never)}, never paid off at its payment"
                    if never
                    else "if you keep paying as scheduled",
                    AMBER if never else FAINT,
                ),
            ],
        ]
        if free is not None:
            when = relative(free, today) if free > today else "every debt is paid off"
            numbers.append(
                ["Debt-free", Text(fmt_month(free), style=f"bold {GREEN}"), Text(when, FAINT)]
            )
        else:
            numbers.append(
                [
                    "Debt-free",
                    Text("not yet", style=f"bold {AMBER}"),
                    Text(
                        f"{join(never)} {'is' if len(never) == 1 else 'are'} never paid off", AMBER
                    ),
                ]
            )
        if base is not None:  # how far the what-if moved each number
            deltas = [
                versus(owed, sum(r.balance for r in base)),
                versus(interest, sum(r.interest or 0 for r in base)),
                _months_delta(free, debt_free(base)),
            ]
            for row, delta in zip(numbers, deltas, strict=True):
                if narrow:
                    row[2] = delta
                else:
                    row.insert(2, delta)
        self.query_one("#debts-numbers", KeyValues).set_rows(numbers)
        panel = self.query_one("#debts-headline", Panel)
        panel.fit_title(
            "What you owe",
            plural(len(rows), "debt"),
            "with the what-if" if base is not None else "",
        )

    def _list(self, rows: list[ask.DebtRow]) -> None:
        today = self.session.today
        narrow = self.has_class("-narrow")
        cells = 12 if narrow else 24
        horizon = max(((r.paid_off_on - today).days for r in rows if r.paid_off_on), default=1)
        items: list[Item] = []
        for r in rows:
            payment = Text(money(r.payment))
            if r.paid_off_on is None:
                length = Text("━" * cells, style=AMBER)
                when = Text("never", style=AMBER)
            elif r.paid_off_on <= today:
                length = Text("─" * cells, style=FAINT)
                when = Text("✓ paid off", style=GREEN)
                payment = Text("—", style=FAINT)
            else:
                length = bar((r.paid_off_on - today).days, max(horizon, 1), cells, PURPLE)
                when = Text(fmt_month(r.paid_off_on))
            items.append(
                Row(
                    r.name,
                    money(r.balance),
                    pct(r.rate),
                    payment,
                    length,
                    when,
                    key=r.key,
                )
            )
        self._debts.set_rows(items)
        panel = self.query_one("#debts-list-panel", Panel)
        panel.fit_title("Soonest paid off first")

    def _detail(self) -> None:
        """The selected debt: terms, where it stands, its events and every payment ahead."""
        key = self._debts.key
        if not isinstance(key, int):
            return
        s = self.session
        drawn = (key, s.version, self.has_class("-narrow"))
        if drawn == self._drawn_detail:
            return  # the list's highlight echo, or a resize that changed nothing here
        self._drawn_detail = drawn
        today = s.today
        try:
            flow = s.flow(key)
        except CashError:
            return
        row = next((r for r in s.debts() if r.key == key), None)
        out = outlook(s, key)
        if flow.debt is None or row is None or out is None:
            return
        narrow = self.has_class("-narrow")
        panel = self.query_one("#debt-panel", Panel)
        panel.fit_title(flow.name, pct(flow.debt.annual_rate), terms_words(flow.debt))
        numbers = facts(flow, row, out, today, narrow, s.growth(key))
        self.query_one("#debt-facts", KeyValues).set_rows(numbers)
        data = schedule(s, key)
        payments = data["rows"] if data else []
        self._chart(out, payments)
        self._events(flow)
        self._payments(data)
        self._fit()

    def _fit(self) -> None:
        """Small screens swap the schedule in (enter) rather than squeeze it in below."""
        short = self.has_class("-short")
        debts = sum(isinstance(it, Row) for it in self._debts.items)
        events = sum(isinstance(it, Row) for it in self.query_one("#debt-events", RowList).items)
        headline = 3 + 2 + (0 if short else 2)
        listed = min(debts, 8) + 1 + 2
        detail = (4 + 1 if short else 5) + 1 + max(1, min(events, 4)) + 2
        cramped = self.size.height < headline + listed + detail + 6
        self.set_class(cramped, "-cramped")
        self.query_one("#debt-panel", Panel).border_subtitle = (
            Content.assemble(("enter", f"bold {PURPLE}"), (" every payment ", FAINT))
            if cramped
            else ""
        )

    def _chart(self, out: dict, payments: list[dict]) -> None:
        chart = self.query_one("#debt-chart", Chart)
        if len(payments) < 2:
            chart.set_series([])
            return
        series = [(self.session.today, cents_of(out["balance_at_as_of"]))] + [
            (date.fromisoformat(p["date"]), cents_of(p["balance"])) for p in payments
        ]
        chart.set_series(series, color=PURPLE)

    def _events(self, flow: Flow) -> None:
        today = self.session.today
        assert flow.debt is not None
        evs = sorted(flow.debt.events, key=lambda e: (e.date, e.id or 0))
        rows: list[Item] = [
            Row(
                fmt_date(e.date, today, weekday=True),
                event_words(e),
                event_amount(e),
                e.notes or "",
                key=e.id,
            )
            for e in evs
        ]
        self.query_one("#debt-events", RowList).set_rows(rows)
        title = Text("Recorded events", style="bold")
        if len(evs) > 1:
            title.append(f"  {DOT}  tab picks one, x deletes it", style=FAINT)
        elif evs:
            title.append(f"  {DOT}  x deletes it", style=FAINT)
        self.query_one("#debt-events-title", Static).update(title)

    def _payments(self, data: dict | None) -> None:
        """Every payment ahead (`bdbd debt schedule NAME --all`), with its title numbers."""
        today = self.session.today
        payments = data["rows"] if data else []
        start = cents_of(data["balance_at_as_of"]) if data else 0
        start = start or 1
        rows: list[Item] = []
        for p in payments:
            owed = cents_of(p["balance"])
            progress = bar(start - owed, start, PROGRESS_CELLS, GREEN)
            if kind := PAYMENT_KINDS.get(p["kind"], p["kind"].replace("_", " ")):
                progress.append(f"  {kind}", style=FAINT)
            rows.append(
                Row(
                    str(p["n"]),
                    fmt_date(date.fromisoformat(p["date"]), today),
                    money(cents_of(p["payment"])),
                    money(cents_of(p["interest"])),
                    money(cents_of(p["principal"])),
                    money(owed),
                    progress,
                    key=p["n"],
                )
            )
        self._schedule.set_rows(rows)
        lens = "with the what-if" if self.session.lens_on else ""
        extra = ""
        if data and rows:
            count = plural(int(data["payments_remaining"]), "payment")
            paid = money(cents_of(data["total_paid_remaining"]))
            extra = f"{count} to go {DOT} {paid} in all"
            if not data["payoff_date"]:
                extra = (
                    f"{count} in the next {SCHEDULE_MONTHS // 12} years {DOT} still not paid off"
                )
        panel = self.query_one("#schedule-panel", Panel)
        panel.fit_title("Every payment", extra, lens)

    def on_resize(self) -> None:
        """Narrow screens get shorter notes and bars; small ones swap the schedule in."""
        if self.drawn != self.session.version or self.has_class("-empty"):
            return
        if self._drawn_narrow != self.has_class("-narrow"):
            self.refresh_view()
        else:
            self._fit()

    # moving around ---------------------------------------------------------------------

    def on_row_list_highlighted(self, event: RowList.Highlighted) -> None:
        if event.row_list is self._debts:
            self._detail()

    def on_row_list_selected(self, event: RowList.Selected) -> None:
        if event.row_list is self._debts:
            event.stop()
            self.action_schedule()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        self.set_class(event.widget is self._schedule, "-schedule")

    def focus_default(self) -> None:
        if self.has_class("-empty"):
            self.screen.set_focus(None)
        else:
            self._debts.focus()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("record", "plan", "edit", "delete_event", "untrack", "schedule", "back"):
            return not self.has_class("-empty")
        return True

    # actions ---------------------------------------------------------------------------

    def _selected(self) -> Flow | None:
        """The debt under the list's cursor, as stored (None when there isn't one)."""
        key = self._debts.key
        if not isinstance(key, int):
            return None
        try:
            flow = self.session.flow(key)
        except CashError:
            return None
        return flow if flow.debt is not None else None

    def action_add(self) -> None:
        """a: a new debt (the flow form, as an expense with its loan terms)."""
        bdbd(self).add_flow(loan=True)

    def action_edit(self) -> None:
        """e: the selected debt's loan terms (and the rest of its flow)."""
        if flow := self._selected():
            bdbd(self).edit_flow(int(flow.id), loan=True)

    def action_record(self) -> None:
        """r: record an extra payment, a new rate or payment, a correction, a payoff."""
        if flow := self._selected():
            self.app.push_screen(EventForm(flow))

    def action_delete_event(self) -> None:
        """x: delete the event under the events list's cursor (asks first)."""
        flow = self._selected()
        if flow is None or flow.debt is None:
            return
        key = self.query_one("#debt-events", RowList).key
        ev = next((e for e in flow.debt.events if e.id == key), None)
        if ev is None:
            bdbd(self).notify(f"There's no event on {flow.name} to delete; r records one.")
            return
        s = self.session
        app = bdbd(self)
        event_id = int(ev.id) if ev.id is not None else 0
        app.confirm(
            "Delete this event?",
            f"{_event_sentence(ev, s.today)} leaves {flow.name}'s schedule.",
            lambda: app.apply(lambda: s.remove_event(event_id)),
        )

    def action_untrack(self) -> None:
        """u: stop tracking the selected debt; its payment stays as a plain expense."""
        flow = self._selected()
        if flow is None:
            return
        s = self.session
        app = bdbd(self)
        what = "balance, rate and recorded events" if flow.debt and flow.debt.events else ""
        what = what or "balance and rate"
        app.confirm(
            f"Stop tracking {flow.name} as a debt?",
            f"bdbd forgets its {what}. "
            f"The {money(flow.amount_cents)} payment stays as a plain expense.",
            lambda: app.apply(lambda: s.unset_debt(flow)),
            yes="Stop tracking",
        )

    def action_plan(self) -> None:
        """p: the payoff plan."""
        if self.session.debts():
            self.app.push_screen(PlanScreen(self.plan_memory))

    def action_schedule(self) -> None:
        """enter: every payment ahead (esc comes back)."""
        self._schedule.focus()

    def action_back(self) -> None:
        """esc: back to the list of debts."""
        self._debts.focus()


# ── Pieces ────────────────────────────────────────────────────────────────────


def debt_free(rows: Sequence[ask.DebtRow]) -> date | None:
    """When the last debt is paid off (`bdbd debts`' debt_free_on); None if one never is."""
    done = [r.paid_off_on for r in rows]
    if not rows or not all(done):
        return None
    return max(d for d in done if d is not None)


def facts(
    flow: Flow,
    row: ask.DebtRow,
    out: dict,
    today: date,
    narrow: bool,
    growth: int | None = None,
) -> list[list[str | Text]]:
    """The selected debt's numbers: owed today, the payment, the payoff, what's left to pay.

    Interest to go is the app's one measure (from today), so owed + interest = in all.
    """
    assert flow.debt is not None
    d = flow.debt
    owed = cents_of(out["balance_at_as_of"])
    stated = ""
    if d.balance_as_of != today:
        stated = f"from {money(d.balance_cents)} on {fmt_date(d.balance_as_of, today)}"
    rows: list[list[str | Text]] = [
        ["Owed today", Text(money(owed), style="bold"), Text(stated, FAINT)],
        ["Payment", money(row.payment), schedule_text(flow)],
    ]
    payoff = out["payoff_date"]
    left = int(out["payments_remaining"])
    if payoff is None or row.interest is None:
        rows.append(
            [
                "Paid off",
                Text("never", style=f"bold {AMBER}"),
                Text("the payment doesn't keep up with the interest", AMBER),
            ]
        )
        if growth:
            rows.append(["Grows by", Text(money(growth), style=f"bold {AMBER}"), "a year"])
        return rows
    day = date.fromisoformat(payoff)
    if day <= today:
        rows[1] = ["Payment", money(row.payment), Text("no more payments: it's paid off", FAINT)]
        rows.append(["Paid off", Text(f"✓ {fmt_date(day, today)}", style=f"bold {GREEN}")])
        return rows
    note = f"{relative(day, today)} {DOT} {plural(left, 'payment')} to go"
    if narrow:
        note = plural(left, "payment") + " to go"
    rows.append(["Paid off", Text(fmt_date(day, today), style=f"bold {GREEN}"), Text(note, FAINT)])
    interest = Text(money(row.interest), style=f"bold {PURPLE}")
    rows.append(
        ["Interest", interest, Text(f"to go, of {money(owed + row.interest)} in all", FAINT)]
    )
    return rows


def _event_sentence(ev: DebtEvent, today: date) -> str:
    """'The extra payment of $500.00 on Fri Oct 2' (for the delete confirmation)."""
    what = event_words(ev).lower()
    amount = event_amount(ev)
    on = fmt_date(ev.date, today, weekday=True)
    if not amount:
        return f"The payoff on {on}"
    return f"The {what} of {amount} on {on}"


def _months_delta(now: date | None, before: date | None) -> Text:
    """'5 months sooner than now' (faint)."""
    if now is None or before is None:
        return Text("")
    if now == before:
        return Text("same as now", style=FAINT)
    months = months_apart(now, before) if now < before else months_apart(before, now)
    side = "sooner" if now < before else "later"
    if months == 0:
        return Text(f"a few days {side} than now", style=FAINT)
    return Text(f"{span(months)} {side} than now", style=FAINT)
