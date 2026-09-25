"""Paydays (2): what's left of each paycheck once its pay cycle's bills are paid.

A pay cycle runs from a payday to the day before the next one; the incomes flagged as paydays
(your paycheck) start them. The same numbers as `bdbd paydays`: the paycheck, the bills that
land in the cycle, the everyday spending allowance for its days, and what's left, free to
spend or save. The headline is the selected cycle (this one, when the view opens), with and
without everyday spending; under it, a chart of what's left of every paycheck a year ahead
(click a column to pick its cycle), then the list of those cycles, and beside it the cycle's
items with what's left after each. Other money coming in during a cycle (a refund) counts in
it, on its day. With a what-if on, all of it includes the what-if.

Keys: v with or without everyday spending · p choose your paydays · enter the cycle's items
(esc back) · a add a flow.
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from datetime import date
from typing import ClassVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.content import Content
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget

from bdbd import ask
from bdbd.core.models import Kind
from bdbd.tui import modals
from bdbd.tui.text import DEBT_KINDS, versus
from bdbd.tui.widgets import (
    Column,
    HeadedList,
    Hint,
    Item,
    KeyValues,
    Panel,
    Picker,
    Row,
    RowList,
    View,
    bdbd,
    signed,
)
from bdbd.ui import charts
from bdbd.ui.common import DEBT_MARK
from bdbd.ui.theme import ACCENT, AMBER, DOT, FAINT, GREEN, PURPLE, RED, money, money_short, plural
from bdbd.words import fmt_date, join, relative

LIST_HEADS = ["Pay cycle", "Paycheck", "Bills", "Left"]


class PayChart(Widget):
    """What's left of each paycheck, a column per pay cycle (`charts.pay_bars`), the selected
    cycle's lit. A click on a column picks its cycle; a double click opens its items."""

    DEFAULT_CSS = """
    PayChart {
        height: 1fr;
        text-wrap: nowrap;
        text-overflow: clip;
    }
    """

    class Pressed(Message):
        """A click on a pay cycle's column (`twice` for a double click)."""

        def __init__(self, day: date, twice: bool) -> None:
            super().__init__()
            self.day = day
            self.twice = twice

    selected: reactive[date | None] = reactive(None)

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._bars: list[charts.PayBar] = []

    def show(self, bars: Sequence[charts.PayBar]) -> None:
        self._bars = list(bars)
        self.refresh()

    def render(self) -> Text:
        width, height = self.content_size
        if not self._bars or height < 3 or width < 16:
            return Text()
        lines = charts.pay_bars(self._bars, width, height - 1, selected=self.selected)
        return Text("\n").join(lines)

    def on_click(self, event: events.Click) -> None:
        width, height = self.content_size
        day = charts.pay_bar_at(self._bars, width, height - 1, event.x)
        if day is not None:
            event.stop()
            self.post_message(self.Pressed(day, event.chain >= 2))


class PaydaysView(View):
    """Each paycheck and what's left of it after the bills of its pay cycle."""

    title = "Paydays"

    DEFAULT_CSS = """
    PaydaysView {
        layout: vertical;
        & #paydays-headline { height: auto; }
        & #paydays-chart-panel { height: 9; }
        & #paydays-lower { height: 1fr; }
        & #cycles-panel { width: 1fr; height: 1fr; }
        & #cycle-panel { width: 1fr; height: 1fr; }
        & RowList { height: 1fr; }
        & #paydays-empty { display: none; }
        &.-empty > * { display: none; }
        &.-empty > #paydays-empty { display: block; }
        &.-short #paydays-headline { padding: 0 2; }
        &.-short #paydays-chart-panel { display: none; }
        &.-narrow #paydays-lower { layout: vertical; }
        &.-narrow #cycles-panel, &.-narrow #cycle-panel { width: 1fr; }
    }
    """

    BINDINGS: ClassVar = [
        Binding("v", "without_everyday", "without everyday"),
        Binding("v", "with_everyday", "with everyday"),
        Binding("p", "paydays", "choose paydays"),
        Binding("enter", "items", "the cycle's items", show=False),
        Binding("escape", "back", "back to the pay cycles", show=False),
    ]

    def __init__(self, *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self.with_everyday = True  # the numbers count the everyday spending allowance
        self._cycles = HeadedList(
            Column(flex=True, min=10),  # the cycle's days
            Column(align="right"),  # paycheck (only when they differ)
            Column(align="right"),  # bills
            Column(align="right"),  # left
            heads=LIST_HEADS,
            id="cycles",
        )
        self._items = RowList(
            Column(style=FAINT),  # date
            Column(flex=True, min=8),  # what
            Column(align="right"),  # amount
            Column(align="right"),  # what's left of the paycheck after it
            empty="Nothing in this pay cycle.",
            id="cycle-items",
        )
        self._shown: ask.PayCycles | None = None

    def compose(self) -> ComposeResult:
        yield Panel(KeyValues(id="paydays-numbers"), id="paydays-headline", variant="accent")
        yield Panel(PayChart(id="paydays-chart"), id="paydays-chart-panel")
        with Horizontal(id="paydays-lower"):
            yield Panel(self._cycles.heads, self._cycles, id="cycles-panel")
            yield Panel(self._items, id="cycle-panel")
        yield Hint(id="paydays-empty")

    # drawing ---------------------------------------------------------------------------

    def refresh_view(self) -> None:
        s = self.session
        pc = s.pay_cycles()
        self._shown = pc
        empty = not pc.cycles
        was_empty = self.has_class("-empty")
        self.set_class(empty, "-empty")
        self.refresh_bindings()
        if empty:
            self._empty(pc)
            if not was_empty and self.has_focus_within:  # the list it had focus in went
                self.call_after_refresh(self.focus_default)
            return
        if was_empty and self.screen.focused is None:  # the first payday just arrived
            self.call_after_refresh(self.focus_default)
        self._list(pc)
        self._show(self._selected())

    def _empty(self, pc: ask.PayCycles) -> None:
        box = self.query_one("#paydays-empty", Hint)
        incomes = [f for f in self.session.flows() if f.kind == Kind.INCOME]
        how = (
            "Each payday starts a pay cycle, which runs to the day before the next one. Here "
            "you'll see what's left of each paycheck once the bills in its cycle are paid: "
            "yours to spend or save."
        )
        if not incomes:
            box.show("Your paychecks show up here.", how, keys=[("a", "add your paycheck")])
        elif not pc.paydays:
            box.show(
                "Which income is your paycheck?",
                how + " Mark the incomes whose dates start a pay cycle.",
                keys=[("p", "choose your paydays")],
            )
        else:
            box.show(
                f"No payday in the next {ask.PAY_CYCLE_MONTHS} months.",
                f"{join(pc.paydays)} {'starts' if len(pc.paydays) == 1 else 'start'} your pay "
                "cycles, but none comes in this stretch.",
                keys=[("p", "choose your paydays")],
            )

    def _left(self, c: ask.PayCycle) -> int:
        return c.left if self.with_everyday else c.left_before_everyday

    def _list(self, pc: ask.PayCycles) -> None:
        today = self.session.today
        varies = len({c.paycheck for c in pc.cycles}) > 1  # else the headline says it
        items: list[Item] = []
        for c in pc.cycles:
            now = c.holds(today)  # this one: its payday in the accent
            span = Text(fmt_date(c.start, today, weekday=True), style=ACCENT if now else "")
            span.append(f" → {fmt_date(c.end, today)}" if not c.open else " → …", style=FAINT)
            items.append(
                Row(
                    span,
                    signed(c.paycheck) if varies else "",
                    money(-c.bills_total),
                    _left_text(self._left(c)),
                    key=c.start,
                )
            )
        heads = LIST_HEADS if varies else [h if h != "Paycheck" else "" for h in LIST_HEADS]
        self._cycles.set_rows(items, heads=heads)
        lens = "with the what-if" if self.session.lens_on else ""
        self.query_one("#cycles-panel", Panel).fit_title(
            "Every pay cycle", f"the next {ask.PAY_CYCLE_MONTHS} months", lens
        )
        self._chart(pc, lens)

    def _chart(self, pc: ask.PayCycles, lens: str) -> None:
        """A column per cycle: what's left, and (counting it) what everyday spending takes."""
        bars = [
            charts.PayBar(c.start, self._left(c), c.everyday if self.with_everyday else 0)
            for c in pc.cycles
        ]
        self.query_one("#paydays-chart", PayChart).show(bars)
        panel = self.query_one("#paydays-chart-panel", Panel)
        panel.fit_title("What's left of each paycheck", lens)
        key: list[Content | str] = [" ", Content.styled("█", GREEN), " free "]
        if any(b.cap for b in bars):
            key += [" ", Content.styled("█", charts.EVERYDAY), " everyday "]
        if any(b.cents < 0 for b in bars):
            key += [" ", Content.styled("█", RED), " short "]
        panel.set_subtitle(*key)

    def _selected(self) -> ask.PayCycle | None:
        pc = self._shown
        if pc is None or not pc.cycles:
            return None
        key = self._cycles.key
        return next((c for c in pc.cycles if c.start == key), pc.cycles[0])

    def _show(self, c: ask.PayCycle | None) -> None:
        if c is None:
            return
        self._headline(c)
        self._cycle_items(c)
        self.query_one("#paydays-chart", PayChart).selected = c.start

    def _headline(self, c: ask.PayCycle) -> None:
        s = self.session
        today = s.today
        pc = self._shown
        weekly = pc.weekly if pc else 0
        left = self._left(c)
        counted = "after bills and everyday spending" if self.with_everyday else "after bills"
        if left >= 0:
            first = [
                "Free to spend or save",
                Text(money(left), style=f"bold {GREEN}"),
                Text(counted, FAINT),
            ]
        else:
            short = "the bills come to more than the paycheck"
            if self.with_everyday and c.left_before_everyday >= 0:
                short = "the bills and everyday spending come to more than the paycheck"
            first = ["Short by", Text(money(-left), style=f"bold {RED}"), Text(short, AMBER)]
        names = join(list(dict.fromkeys(i.name for i in c.paychecks)))
        rows: list[list[str | Text]] = [
            first,
            [
                "Paycheck",
                signed(c.paycheck),
                Text(f"{names} {DOT} {fmt_date(c.start, today, weekday=True)}", FAINT),
            ],
            ["Bills", Text(money(-c.bills_total)), Text(_bills_note(c), FAINT)],
        ]
        days = f"{plural(c.days, 'day')} at {money_short(weekly)}/week"
        if self.with_everyday:
            rows.append(["Everyday", Text(money(-c.everyday)), Text(days, FAINT)])
            rows.append(
                [
                    "Before everyday",
                    _left_text(c.left_before_everyday),
                    Text("not counting everyday spending", FAINT),
                ]
            )
        else:
            rows.append(
                [
                    "Everyday",
                    Text(money(-c.everyday), style=FAINT),
                    Text(f"{days}, not counted", FAINT),
                ]
            )
            rows.append(
                ["After everyday", _left_text(c.left), Text("what's left counting it", FAINT)]
            )
        if c.money_in:  # it counts: it's in the cycle, so it's there to spend too
            what = join(list(dict.fromkeys(i.name for i in c.money_in)))
            rows.insert(2, ["Also coming in", signed(c.money_in_total), Text(what, FAINT)])
        if s.lens_on:  # how far the what-if moved what's left of this paycheck
            base = next((b for b in s.pay_cycles(baseline=True).cycles if b.start == c.start), None)
            if base is not None:
                rows[0].append(versus(left, self._left(base)))
        self.query_one("#paydays-numbers", KeyValues).set_rows(rows)
        panel = self.query_one("#paydays-headline", Panel)
        for v in ("accent", "amber"):
            panel.set_class(v == ("accent" if left >= 0 else "amber"), f"-{v}")
        span = f"{fmt_date(c.start, today, weekday=True)} → "
        span += fmt_date(c.end, today, weekday=True) if not c.open else "no payday after it"
        title = "This pay cycle" if c.holds(today) else "Pay cycle"
        when = "" if c.holds(today) or c.start < today else relative(c.start, today)
        lens = "with the what-if" if s.lens_on else ""
        panel.fit_title(title, span, when, lens)
        picker = Picker(
            [(True, "with everyday"), (False, "without")],
            chosen=self.with_everyday,
            on_pick=self._pick_everyday,
        )
        panel.set_subtitle(" ", picker, " ")

    def _cycle_items(self, c: ask.PayCycle) -> None:
        today = self.session.today
        rows: list[Item] = []
        running = c.paycheck
        for n, i in enumerate(c.items):
            when = fmt_date(i.date, today, weekday=True)
            name = Text(i.name)
            if i.kind in DEBT_KINDS:
                name.append(f" {DEBT_MARK}", style=PURPLE)
            if i.payday:
                rows.append(Row(when, name, signed(i.cents), Text(money(running)), key=(n, i.key)))
            else:  # a bill, or other money coming in on its day
                running += i.cents
                rows.append(Row(when, name, signed(i.cents), _running(running), key=(n, i.key)))
        rows.append(None)
        everyday = Text(f"Everyday, {plural(c.days, 'day')}")
        if self.with_everyday:
            running -= c.everyday
            rows.append(Row("", everyday, signed(-c.everyday), _running(running), key="everyday"))
        else:
            everyday.append("  not counted", style=FAINT)
            amount = Text(money(-c.everyday), style=FAINT)
            rows.append(Row("", everyday, amount, "", key="everyday"))
        self._items.set_rows(rows)
        span = f"{fmt_date(c.start, today)} → " + (fmt_date(c.end, today) if not c.open else "…")
        count = plural(len(c.bills), "bill")
        self.query_one("#cycle-panel", Panel).fit_title(span, count, "what's left after each")

    # moving around ---------------------------------------------------------------------

    def on_row_list_highlighted(self, event: RowList.Highlighted) -> None:
        if event.row_list is self._cycles:
            self._show(self._selected())

    def on_row_list_selected(self, event: RowList.Selected) -> None:
        event.stop()
        if event.row_list is self._cycles:
            self.action_items()
            return
        key: Hashable = event.key
        if isinstance(key, tuple) and isinstance(key[1], int):
            bdbd(self).open_flow_card(key[1])  # a stored flow (what-if items have no card)
        else:
            self.app.bell()

    def on_pay_chart_pressed(self, event: PayChart.Pressed) -> None:
        event.stop()
        if not self._cycles.select_key(event.day):
            return
        self._cycles.focus()
        if event.twice:
            self.action_items()

    def action_items(self) -> None:
        if self._items.current is None:
            self.app.bell()
            return
        self._items.focus()

    def action_back(self) -> None:
        self._cycles.focus()

    def action_with_everyday(self) -> None:
        self._pick_everyday(True)

    def action_without_everyday(self) -> None:
        self._pick_everyday(False)

    def _pick_everyday(self, on: object) -> None:
        if on == self.with_everyday:
            return
        self.with_everyday = bool(on)
        self.refresh_bindings()
        if self._shown is not None and self._shown.cycles:
            self._list(self._shown)
            self._show(self._selected())

    def action_paydays(self) -> None:
        modals.open_paydays(bdbd(self))

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """v's footer label says what it switches to; esc only from the cycle's items."""
        if action == "with_everyday":
            return not self.with_everyday
        if action == "without_everyday":
            return self.with_everyday
        if action == "back":
            return self._items.has_focus
        if action == "items":
            return not self.has_class("-empty")
        return True

    def focus_default(self) -> None:
        """The pay cycles, or the empty state (it takes focus, so p works there)."""
        if self.has_class("-empty"):
            self.query_one("#paydays-empty", Hint).focus()
        else:
            self._cycles.focus()


def _left_text(cents: int) -> Text:
    """What's left: green when there's some, red when the paycheck falls short."""
    if cents > 0:
        return Text(money(cents), style=f"bold {GREEN}")
    if cents < 0:
        return Text(money(cents), style=f"bold {RED}")
    return Text(money(0), style="bold")


def _running(cents: int) -> Text:
    """What's left of the paycheck after an item: amber once it's gone."""
    return Text(money(cents), style=AMBER if cents < 0 else "")


def _bills_note(c: ask.PayCycle) -> str:
    """'3 bills · Rent the biggest' or 'no bills in it'."""
    if not c.bills:
        return "no bills in it"
    biggest = min(c.bills, key=lambda i: (i.cents, i.date))
    note = plural(len(c.bills), "bill")
    if len(c.bills) > 1:
        note += f" {DOT} {biggest.name} the biggest"
    return note
