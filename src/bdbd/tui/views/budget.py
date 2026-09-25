"""The Budget view (5): what's in the budget, and where the money goes.

Two halves, switched with v. **Flows**: every income and expense, grouped Income / Expenses /
Paused and sorted by next date, with the selected flow's details on the right. **Tags**: each
tag's monthly bills as a share of all your bills, with what it will cost over the months ahead.

The numbers are the ones `bdbd ls --all`, `bdbd summary`, `bdbd tags` and
`bdbd spend TAG --months N` give. Like those commands, this view shows the budget as it is,
never the what-if (the headline says what the what-if would leave instead).
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import ClassVar, Literal

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.content import Content
from textual.widgets import Input, Static

from bdbd import ask
from bdbd.core import repo
from bdbd.core.models import EffectiveModel, Flow, Kind
from bdbd.core.queries.spend import spend as spend_query
from bdbd.core.queries.summary import summary
from bdbd.tui.cards import Card, CardLine, Pair, flow_lines, flow_money
from bdbd.tui.forms import parse_money
from bdbd.tui.session import Session
from bdbd.tui.widgets import (
    Column,
    Header,
    Hint,
    Item,
    Panel,
    Picker,
    Row,
    RowList,
    View,
    bdbd,
    signed,
)
from bdbd.ui.common import DEBT_MARK, schedule_text
from bdbd.ui.theme import (
    ACCENT,
    DOT,
    FAINT,
    FOREGROUND,
    GREEN,
    PURPLE,
    RED,
    bar,
    cents_of,
    money,
    money_short,
    plural,
)
from bdbd.words import fmt_date

Mode = Literal["flows", "tags"]
SHARE_CELLS = 12  # a tag's share bar
SPEND_MONTHS = ((1, "Next month"), (3, "Next 3 months"), (12, "Next 12 months"))


# ── Questions this view asks (the budget as it is, memoized per session version) ──────


@dataclass(frozen=True)
class Totals:
    """A month in the budget: what `bdbd summary` and `bdbd overview` call monthly."""

    income: int
    bills: int
    everyday: int
    weekly: int

    @property
    def left(self) -> int:
        return self.income - self.bills - self.everyday


def budget_summary(s: Session, *, paused: bool = False) -> dict:
    """`bdbd summary` (with --all when `paused`) as of today, without the what-if."""
    return s.summary(include_inactive=paused)


def totals(s: Session) -> Totals:
    """In, bills and everyday spending a month, and what's left over."""
    net = budget_summary(s)["net"]
    weekly = s.stored_weekly()
    return Totals(
        income=cents_of(net["income_monthly"]),
        bills=cents_of(net["expense_monthly"]),
        everyday=ask.everyday_monthly(weekly),
        weekly=weekly,
    )


def group_monthly(s: Session, keys: frozenset[Hashable]) -> int:
    """What these active flows add up to a month (summed before rounding, like summary)."""

    def compute() -> int:
        model = s.model(baseline=True)
        some = EffectiveModel(flows=[f for f in model.flows if f.key in keys])
        net = summary(some, as_of=s.today, by="flow")[0]["net"]
        return cents_of(net["income_monthly"]) + cents_of(net["expense_monthly"])

    return s.cached(("budget.group", keys), compute)


def tag_spend(s: Session, name: str, months: int, *, income: bool) -> int:
    """What a tag costs (or brings in) from today: `bdbd spend TAG [--income] --months N`."""

    def compute() -> int:
        model = s.model(baseline=True)
        data, _ = spend_query(
            model,
            as_of=s.today,
            until=ask.months_ahead(s.today, months),
            terms=[name],
            income=income,
            known_tags=frozenset(t.name for t in s.tags()),
            weekly_spend_cents=s.budget.weekly_for(model),
        )
        return cents_of(data["total"])

    return s.cached(("budget.spend", name, months, income), compute)


def tag_is_income(s: Session, name: str) -> bool:
    """A tag carried only by incomes (a paycheck, a refund) is about money in."""
    kinds = {f.kind for f in s.flows() if name in f.tags}
    return kinds == {Kind.INCOME}


def tag_quiet(s: Session, name: str) -> str:
    """Why a tag has no steady monthly money: 'paused', 'one-off' or '—' (nothing ahead)."""
    flows = [f for f in s.flows() if name in f.tags]
    if flows and not any(f.active for f in flows):
        return "paused"
    if any(f.active and f.rrule is None for f in flows):
        return "one-off"
    return "—"


def matches(flow: Flow, needle: str) -> bool:
    """The filter: part of the name or of a tag; '#car' means exactly the tag car."""
    if needle.startswith("#"):
        tag = needle[1:].strip()
        return not tag or tag in flow.tags
    return not needle or needle in flow.name.lower() or any(needle in t for t in flow.tags)


def tag_lines(s: Session, row: ask.TagRow) -> list[CardLine]:
    """What the card on the right says about a tag: a month, then the months ahead on their
    real dates (end dates and all)."""
    income = tag_is_income(s, row.name)
    sign = 1 if income else -1
    monthly = row.income_monthly if income else row.expense_monthly
    lines: list[CardLine] = []
    if monthly:
        lines.append(Pair("Per month", signed(sign * monthly)))
    else:
        why = {"paused": "nothing while it's paused", "one-off": "nothing steady, one-offs only"}
        quiet = why.get(tag_quiet(s, row.name), "nothing ahead")
        lines.append(Pair("Per month", Text(quiet, style=FAINT), align="left"))
    heading = "What it brings in" if income else "What it will cost"
    lines += [None, Text.assemble((heading, "bold"), (f" {DOT} from today", FAINT))]
    for months, label in SPEND_MONTHS:
        cents = tag_spend(s, row.name, months, income=income)
        lines.append(Pair(label, signed(sign * cents)))
    lst = s.listing()
    flows = sorted(
        (f for f in lst.flows if row.name in f.tags),
        key=lambda f: (not f.active, -lst.monthly[f.id], f.name),
    )
    lines += [None, Text.assemble(("Flows", "bold"), (f" {DOT} a month", FAINT))]
    for f in flows:
        name = Text(f.name, style=FOREGROUND if f.active else FAINT)
        if f.debt is not None:
            name.append(f" {DEBT_MARK}", style=PURPLE)
        cents = lst.monthly[f.id]
        fsign = 1 if f.kind == Kind.INCOME else -1
        if not f.active:
            name.append(" paused", style=FAINT)
            value = flow_money(fsign * cents, f)
        elif cents:
            value = signed(fsign * cents)
        else:
            value = Text("one-off", style=FAINT)
        lines.append(Pair(name, value))
    return lines


# ── The view ──────────────────────────────────────────────────────────────────


class FilterInput(Input):
    """The / filter above the flows: esc clears it, down goes to the list."""

    BINDINGS: ClassVar = [
        Binding("escape", "clear", "clear"),
        Binding("down", "to_list", "to the list", show=False),
    ]

    def action_clear(self) -> None:
        self._view().clear_filter()

    def action_to_list(self) -> None:
        self._view().focus_list()

    def _view(self) -> BudgetView:
        return next(a for a in self.ancestors if isinstance(a, BudgetView))

    def on_blur(self) -> None:
        if not self.value.strip():
            self._view().clear_filter(focus=False)


FLOWS_ONLY = {"edit", "pause", "delete", "loan", "filter", "clear_filter"}
TAGS_ONLY = {"rename_tag", "remove_tag", "tag_flows"}
ON_A_FLOW = {"edit", "pause", "delete", "loan"}  # need a flow under the cursor
ON_A_TAG = TAGS_ONLY  # need a tag under the cursor


class BudgetView(View):
    """What's in the budget (Flows) and where the money goes (Tags); v switches."""

    title = "Budget"

    DEFAULT_CSS = """
    BudgetView {
        layout: vertical;
        & #budget-head { height: auto; }
        &.-short #budget-head { padding: 0 2; }
        & #budget-totals { height: auto; }
        & .body { height: 1fr; }
        & .body > Panel { height: 1fr; }
        & .main { width: 1fr; padding: 0 0 0 1; }
        & .side { width: 38; padding: 1 1 0 2; }
        & RowList { height: 1fr; }
        & #tags-body { display: none; }
        &.-tags #flows-body { display: none; }
        &.-tags #tags-body { display: block; }
        & #flow-filter {
            display: none;
            height: 1;
            margin: 0 0 1 1;
            padding: 0 1;
            border: none;
            background: $panel;
            &:focus { background: $primary 18%; border: none; }
            & > .input--placeholder { color: $text-muted; }
        }
        &.-filtering #flow-filter { display: block; }
        &.-narrow .body { layout: vertical; }
        &.-narrow .side { width: 1fr; height: auto; max-height: 14; padding: 0 1; }
        &.-narrow.-short .side { display: none; }
        & #budget-welcome { display: none; }
        & #no-tags { display: none; }
        &.-no-tags #tag-list { display: none; }
        &.-no-tags #no-tags { display: block; }
        &.-no-tags #tag-side { display: none; }
        &.-empty > * { display: none; }
        &.-empty > #budget-welcome { display: block; }
        &.-empty #tags-body, &.-empty #flows-body, &.-empty #budget-head { display: none; }
    }
    """

    # Flows and Tags share keys; check_action keeps each binding to its half. Tags' keys come
    # first so its footer reads "r rename tag · x remove tag". v is on the headline's border.
    BINDINGS: ClassVar = [
        Binding("r", "rename_tag", "rename tag"),
        Binding("x", "remove_tag", "remove tag"),
        Binding("enter", "tag_flows", "show the tag's flows", show=False),
        Binding("e", "edit", "edit"),
        Binding("enter", "edit", "edit", show=False),
        Binding("space", "pause", "pause or resume", show=False),
        Binding("x", "delete", "delete", show=False),
        Binding("delete", "delete", "delete", show=False),
        Binding("d", "loan", "loan terms", show=False),
        Binding("slash", "filter", "filter"),
        Binding("escape", "clear_filter", "clear the filter", show=False),
        Binding("s", "everyday", "everyday spending", show=False),
        Binding("a", "add", "add a flow"),
        Binding("v", "switch('tags')", "show tags", show=False),
        Binding("v", "switch('flows')", "show flows", show=False),
    ]

    def __init__(self, *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self.mode: Mode = "flows"
        self.needle = ""  # the filter, lowercased
        self._stale: set[Mode] = {"flows", "tags"}
        self._pending_tag: str | None = None
        self._weekday = False
        self._had_row = True  # whether the list on show had a row under the cursor

    def compose(self) -> ComposeResult:
        yield Panel(Static(id="budget-totals"), id="budget-head", variant="accent")
        with Horizontal(id="flows-body", classes="body"):
            yield Panel(
                FilterInput(
                    placeholder="part of a name or a tag, or #tag", compact=True, id="flow-filter"
                ),
                RowList(
                    Column(flex=True, min=8),  # name
                    Column(align="right"),  # amount
                    Column(),  # schedule
                    Column(style=FAINT),  # next date
                    Column(align="right"),  # per month
                    id="flow-list",
                ),
                id="flow-panel",
                classes="main",
            )
            yield Panel(Card(id="flow-card"), id="flow-side", classes="side")
        with Horizontal(id="tags-body", classes="body"):
            yield Panel(
                RowList(
                    Column(),  # name
                    Column(align="right"),  # a month
                    Column(),  # share bar
                    Column(align="right", style=FAINT),  # share
                    Column(flex=True, min=8, style=FAINT),  # flows
                    id="tag-list",
                ),
                Hint(
                    "No tags yet.",
                    "Tags group flows so you can see what something costs, like car or "
                    "housing. Add them when you edit a flow.",
                    keys=[("v", "back to your flows, then e edits one")],
                    id="no-tags",
                ),
                id="tag-panel",
                classes="main",
            )
            yield Panel(Card(id="tag-card"), id="tag-side", classes="side")
        yield Hint(
            "No flows yet.",
            "Your budget is the money that comes in and goes out on a schedule: paychecks, "
            "rent, subscriptions, loans. Start with what you're paid.",
            keys=[("a", "add your paycheck"), ("b", "record your balance"), ("?", "help")],
            id="budget-welcome",
        )

    # drawing ---------------------------------------------------------------------------

    def refresh_view(self) -> None:
        s = self.session
        empty = not s.flows()
        self.set_class(empty, "-empty")
        self._stale = {"flows", "tags"}
        self._headline()
        if empty:
            return
        self._draw(self.mode)

    def _draw(self, mode: Mode) -> None:
        self._stale.discard(mode)
        if mode == "flows":
            self._draw_flows()
        else:
            self._draw_tags()

    def _headline(self) -> None:
        s = self.session
        t = totals(s)
        wide = self.size.width >= 100 or not self.size.width
        left_color = GREEN if t.left >= 0 else RED
        parts: list[str | tuple[str, str]] = []
        if wide:
            parts.append(("Each month  ", FAINT))
        parts += [
            (money(t.income, sign=True), f"bold {GREEN}"),
            (" in  ·  " if wide else " in · ", FAINT),
            (money(-t.bills), "bold"),
            (" bills  ·  " if wide else " bills · ", FAINT),
            (money(-t.everyday), "bold"),
            (" everyday  =  " if wide else " everyday = ", FAINT),
            (money(t.left, sign=True), f"bold {left_color}"),
            (" left over" if wide else " left", FAINT),
        ]
        line = Text.assemble(*parts)
        second = Text("Everyday spending ", style=FAINT)
        if t.weekly:
            second.append(f"{money_short(t.weekly)}/week", style="bold")
        else:
            second.append("off", style="bold")
        second.append(f" {DOT} ", style=FAINT)
        second.append("s", style=f"bold {ACCENT}")
        second.append(" to change", style=FAINT)
        if s.lens_on:  # this view is the budget as it is; say what the what-if would leave
            since, what_if = self._what_if_left()
            when = f" from {fmt_date(since, s.today)}" if since else ""
            if what_if == t.left:
                second.append(f"     ↳ the what-if leaves these as they are{when}", style=ACCENT)
            else:
                second.append(f"     ↳ with the what-if{when}: ", style=ACCENT)
                second.append(money(what_if, sign=True), style=f"bold {ACCENT}")
                second.append(" left over", style=ACCENT)
        self.query_one("#budget-totals", Static).update(Text("\n").join([line, second]))
        panel = self.query_one("#budget-head", Panel)
        halves = Picker(
            [("flows", "Flows"), ("tags", "Tags")],
            chosen=self.mode,
            on_pick=self._pick_half,
        )
        panel.set_title(" ", halves, " ")
        panel.set_subtitle(
            Content.assemble(
                ("v", f"bold {ACCENT}"), (f" {'tags' if self.mode == 'flows' else 'flows'} ", FAINT)
            )
        )

    def _pick_half(self, mode: object) -> None:
        if mode in ("flows", "tags") and mode != self.mode:
            self.action_switch("tags" if mode == "tags" else "flows")

    def _what_if_left(self) -> tuple[date | None, int]:
        """What the what-if leaves a month, and from when: a dated change (a sale on Nov 1)
        only changes the steady numbers after it, so they're counted from the day after the
        last one (like `bdbd summary --as-of`)."""
        s = self.session
        days = [c.day for c in s.scenario.active if c.day is not None and c.day > s.today]
        since = max(days) + timedelta(days=1) if days else None

        def compute() -> int:
            day = since or s.today
            model = s.model(as_of=day)
            net = summary(model, as_of=day, by="flow")[0]["net"]
            return cents_of(net["net_monthly"]) - ask.everyday_monthly(s.weekly(model))

        return since, s.cached(("budget.what_if_left", since), compute)

    def _draw_flows(self) -> None:
        s = self.session
        lst = s.listing()
        today = s.today
        every = lst.flows
        shown = [f for f in every if matches(f, self.needle)]
        self._weekday = self._room_for_weekdays(shown)
        groups: list[tuple[str, list[Flow], list[Flow]]] = [
            (
                "Income",
                [f for f in shown if f.active and f.kind == Kind.INCOME],
                [f for f in every if f.active and f.kind == Kind.INCOME],
            ),
            (
                "Expenses",
                [f for f in shown if f.active and f.kind == Kind.EXPENSE],
                [f for f in every if f.active and f.kind == Kind.EXPENSE],
            ),
            ("Paused", [f for f in shown if not f.active], [f for f in every if not f.active]),
        ]
        items: list[Item] = []
        for title, flows, whole in groups:
            if not flows:
                continue
            if items:
                items.append(None)
            # by next date; the same day, like every list of a day's items: largest, then name
            flows.sort(
                key=lambda f: (
                    lst.next[f.id] is None,
                    lst.next[f.id] or today,
                    -f.amount_cents,
                    f.name.casefold(),
                )
            )
            items.append(self._group_header(title, flows, whole))
            items += [self._flow_row(f, lst) for f in flows]
        rows = self.query_one("#flow-list", RowList)
        rows.empty = (
            f"Nothing matches “{self.needle}”. Press esc to clear the filter."
            if self.needle
            else "No flows yet. Press a to add your paycheck."
        )
        rows.set_rows(items)
        self._show_flow(rows.key)
        panel = self.query_one("#flow-panel", Panel)
        panel.border_title = self._flows_title(len(shown), len(every))
        self._keys_follow(rows.key)

    def _keys_follow(self, key: Hashable | None) -> None:
        """Show or hide the keys that need a row, when there starts or stops being one."""
        if (key is not None) != self._had_row:
            self._had_row = key is not None
            self.refresh_bindings()

    def _flows_title(self, shown: int, every: int) -> Content:
        extra = f"{shown} of {every} match" if self.needle else "by next date"
        return Content.assemble((" Flows", "bold"), (f" {DOT} {extra} ", FAINT))

    def _group_header(self, title: str, flows: list[Flow], whole: list[Flow]) -> Header:
        count = plural(len(whole), "flow")
        if len(flows) != len(whole):
            count = f"{len(flows)} of {len(whole)}"
        head = Text.assemble((title, "bold"), (f"  {count}", f"not bold {FAINT}"))
        if title == "Paused":
            return Header(head, Text("not in any projection", style=FAINT))
        income = title == "Income"
        if len(flows) == len(whole):  # the whole group: summary's monthly in or bills
            t = totals(self.session)
            cents = t.income if income else t.bills
        else:
            cents = group_monthly(self.session, frozenset(f.id for f in flows))
        amount = money(cents if income else -cents, sign=income)
        right = Text.assemble(("a month  ", FAINT), (amount, f"bold {GREEN}" if income else "bold"))
        return Header(head, right)

    def _flow_row(self, f: Flow, lst: ask.Listing) -> Row:
        today = self.session.today
        income = f.kind == Kind.INCOME
        sign = 1 if income else -1
        name = Text(f.name, style="" if f.active else FAINT)
        if f.debt is not None:
            name.append(f" {DEBT_MARK}", style=PURPLE)
        schedule = schedule_text(f)
        nxt = lst.next[f.id]
        if not f.active:
            schedule.stylize(FAINT)
            when: str | Text = "paused"
        elif nxt is None:
            when = "—"
        else:
            when = fmt_date(nxt, today, weekday=self._weekday)
        monthly = lst.monthly[f.id]
        if monthly:
            per_month = flow_money(sign * monthly, f)
        else:
            per_month = Text("one-off" if f.rrule is None else "—", style=FAINT)
        return Row(
            name, flow_money(sign * f.amount_cents, f), schedule, when, per_month, key=int(f.id)
        )

    def _room_for_weekdays(self, flows: Sequence[Flow]) -> bool:
        """'Fri Oct 2' when every name still fits beside it, else 'Oct 2'."""
        rows = self.query_one("#flow-list", RowList)
        width = rows.size.width - 2  # the cursor bar and the right padding
        if width <= 0 or not flows:
            return False
        name = max(Text(f.name).cell_len + (2 if f.debt else 0) for f in flows)
        schedule = max(schedule_text(f).cell_len for f in flows)
        # amount, schedule, "Tue Oct 20", per month, and a gap of 2 between the five columns
        return name + 10 + schedule + 10 + 10 + 8 <= width

    def _show_flow(self, key: Hashable | None) -> None:
        s = self.session
        panel = self.query_one("#flow-side", Panel)
        card = self.query_one("#flow-card", Card)
        flow = next((f for f in s.flows() if f.id == key), None)
        if flow is None:
            panel.border_title = Content.styled(" Details ", "bold")
            card.show([Text("Nothing selected.", style=FAINT)])
            return
        title: list[str | tuple[str, str]] = [(f" {flow.name}", "bold")]
        if flow.debt is not None:
            title.append((f" {DEBT_MARK}", PURPLE))
        title.append(" ")
        panel.border_title = Content.assemble(*title)
        card.show(flow_lines(s, flow))

    def _draw_tags(self) -> None:
        s = self.session
        rows_data = s.tag_rows(baseline=True)
        self.set_class(not rows_data, "-no-tags")
        bills = totals(s).bills
        ordered = sorted(rows_data, key=lambda r: (-r.expense_monthly, -r.income_monthly, r.name))
        items: list[Item] = [self._tag_row(r, bills) for r in ordered]
        rows = self.query_one("#tag-list", RowList)
        rows.set_rows(items)
        if self._pending_tag is not None:
            rows.select_key(self._pending_tag)
            self._pending_tag = None
        self._show_tag(rows.key)
        panel = self.query_one("#tag-panel", Panel)
        overlap = any(len(f.tags) > 1 for f in s.flows())
        panel.border_title = Content.assemble(
            (" Tags", "bold"),
            (f" {DOT} a month, as a share of your bills ", FAINT),
        )
        panel.border_subtitle = (
            Content.styled(" a flow with several tags counts in each ", FAINT) if overlap else ""
        )
        self._keys_follow(rows.key)

    def _tag_row(self, r: ask.TagRow, bills: int) -> Row:
        if r.expense_monthly:
            amount = signed(-r.expense_monthly)
            share = bar(r.expense_monthly, bills, SHARE_CELLS, ACCENT)
            part = f"{round(100 * r.expense_monthly / bills)}%" if bills else ""
        elif r.income_monthly:
            amount = signed(r.income_monthly)
            share, part = Text(" " * SHARE_CELLS), ""
        else:
            amount = Text(tag_quiet(self.session, r.name), style=FAINT)
            share, part = Text(" " * SHARE_CELLS), ""
        names = ", ".join(r.flow_names)
        return Row(Text(r.name, style="bold"), amount, share, part, names, key=r.name)

    def _show_tag(self, key: Hashable | None) -> None:
        s = self.session
        panel = self.query_one("#tag-side", Panel)
        card = self.query_one("#tag-card", Card)
        row = next((r for r in s.tag_rows(baseline=True) if r.name == key), None)
        if row is None:
            panel.border_title = Content.styled(" Tag ", "bold")
            card.show([Text("Nothing selected.", style=FAINT)])
            return
        panel.border_title = Content.assemble((f" {row.name}", "bold"), " ")
        card.show(tag_lines(s, row))

    def on_resize(self, event: events.Resize) -> None:
        """The headline shortens on narrow screens; dates lose their weekday when names need
        the room."""
        s = self.session
        if self.drawn != s.version or self.has_class("-empty"):
            return
        self._headline()
        if self.mode == "flows":
            self.call_after_refresh(self._recheck_weekdays)

    def _recheck_weekdays(self) -> None:
        flows = [f for f in self.session.listing().flows if matches(f, self.needle)]
        if self._room_for_weekdays(flows) != self._weekday:
            self._draw_flows()

    def on_row_list_highlighted(self, event: RowList.Highlighted) -> None:
        if event.row_list.id == "flow-list":
            self._show_flow(event.key)
        elif event.row_list.id == "tag-list":
            self._show_tag(event.key)

    def on_row_list_selected(self, event: RowList.Selected) -> None:
        if event.row_list.id == "flow-list":
            self.action_edit()
        elif event.row_list.id == "tag-list":
            self.action_tag_flows()

    # focus -----------------------------------------------------------------------------

    def focus_default(self) -> None:
        """The list on show, or the empty state (focusable, so this view's keys still work)."""
        if self.has_class("-empty"):
            self.query_one("#budget-welcome", Hint).focus()
        elif self.mode == "tags" and self.has_class("-no-tags"):
            self.query_one("#no-tags", Hint).focus()
        else:
            self.query_one("#flow-list" if self.mode == "flows" else "#tag-list", RowList).focus()

    def focus_list(self) -> None:
        self.query_one("#flow-list", RowList).focus()

    # the filter ------------------------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "flow-filter":
            return
        event.stop()
        needle = event.value.strip().lower()
        if needle != self.needle:
            self.needle = needle
            self._draw_flows()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "flow-filter":
            event.stop()
            if not event.value.strip():
                self.clear_filter()
            else:
                self.focus_list()

    def action_filter(self) -> None:
        self.add_class("-filtering")
        self.query_one("#flow-filter", FilterInput).focus()
        self.refresh_bindings()

    def action_clear_filter(self) -> None:
        self.clear_filter()

    def clear_filter(self, *, focus: bool = True) -> None:
        """Forget the filter and hide its box."""
        box = self.query_one("#flow-filter", FilterInput)
        had = bool(self.needle)
        self.needle = ""
        with box.prevent(Input.Changed):
            box.value = ""
        self.remove_class("-filtering")
        if had:
            self._draw_flows()
        if focus:
            self.focus_list()
        self.refresh_bindings()

    def set_filter(self, text: str) -> None:
        """Show only the flows matching `text` (a name or a tag)."""
        box = self.query_one("#flow-filter", FilterInput)
        with box.prevent(Input.Changed):
            box.value = text
        self.needle = text.strip().lower()
        self.set_class(bool(self.needle), "-filtering")
        self._draw_flows()
        self.refresh_bindings()

    # actions ---------------------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Each half's keys only in that half, and only when there's something to act on."""
        if action in FLOWS_ONLY and self.mode != "flows":
            return False
        if action in TAGS_ONLY and self.mode != "tags":
            return False
        if not self.is_mounted:
            return True
        if action in ON_A_FLOW and self._flow_id() is None:
            return False
        if action in ON_A_TAG and self._tag() is None:
            return False
        if action == "switch":
            return bool(parameters) and parameters[0] != self.mode
        if action == "clear_filter":
            return self.has_class("-filtering")
        if action == "filter":  # while typing in it, / is just a character
            return not isinstance(self.app.focused, FilterInput)
        return True

    def _flow_id(self) -> int | None:
        key = self.query_one("#flow-list", RowList).key
        return key if isinstance(key, int) else None

    def _tag(self) -> str | None:
        key = self.query_one("#tag-list", RowList).key
        return key if isinstance(key, str) else None

    def action_add(self) -> None:
        """a: a new flow (an income when there's none yet: the paycheck comes first)."""
        s = self.session
        bdbd(self).add_flow(income=not any(f.kind == Kind.INCOME for f in s.flows()))

    def action_edit(self) -> None:
        if (flow_id := self._flow_id()) is not None:
            bdbd(self).edit_flow(flow_id)

    def action_pause(self) -> None:
        if (flow_id := self._flow_id()) is not None:
            bdbd(self).toggle_paused(flow_id)

    def action_delete(self) -> None:
        if (flow_id := self._flow_id()) is not None:
            bdbd(self).delete_flow(flow_id)

    def action_loan(self) -> None:
        """d: the loan terms of an expense (making it a debt when it isn't one)."""
        flow_id = self._flow_id()
        if flow_id is None:
            return
        app = bdbd(self)
        flow = self.session.flow(flow_id)
        if flow.kind == Kind.INCOME:
            app.notify(f"{flow.name} is money coming in, so it can't be a loan.", markup=False)
            return
        app.edit_flow(flow_id, loan=True)

    def action_switch(self, mode: Mode) -> None:
        """v: Flows or Tags."""
        if self.has_class("-empty"):
            self.app.bell()
            return
        self.mode = mode
        self.set_class(mode == "tags", "-tags")
        self._headline()
        if mode in self._stale and not self.has_class("-empty"):
            self._draw(mode)
        self.focus_default()
        self._had_row = (self._flow_id() if mode == "flows" else self._tag()) is not None
        self.refresh_bindings()

    def action_tag_flows(self) -> None:
        """Enter on a tag: its flows, in Flows."""
        name = self._tag()
        if name is None:
            return
        self.action_switch("flows")
        self.set_filter(f"#{name}")
        self.focus_list()

    def action_everyday(self) -> None:
        """s: everyday spending a week, with what it does to the month as you type."""
        s = self.session
        app = bdbd(self)
        t = totals(s)
        before = t.left + t.everyday  # left over with no everyday spending at all

        read = parse_money(optional=True, example="175")

        def preview(text: str) -> tuple[bool, str]:
            typed = text.strip()
            if not typed:
                return True, f"Off {DOT} {money(before, sign=True)} left over a month"
            parsed = read(typed)
            if not parsed.ok:
                return False, str(parsed.preview)
            cents = parsed.value
            monthly = ask.everyday_monthly(cents)
            left = money(before - monthly, sign=True)
            return True, f"{money(monthly)} a month {DOT} {left} left over"

        def submit(text: str) -> None:
            cents = read(text.strip()).value if text.strip() else None
            app.apply(lambda: s.set_weekly_spend(cents))

        value = money(t.weekly, symbol=False) if t.weekly else ""
        app.prompt("Everyday spending", "Per week", submit, value=value, preview=preview)

    def action_rename_tag(self) -> None:
        """r: rename the tag on every flow that carries it."""
        name = self._tag()
        if name is None:
            return
        s = self.session
        app = bdbd(self)
        existing = {t.name: t.flow_count for t in s.tags()}
        count = plural(existing.get(name, 0), "flow")

        def preview(text: str) -> tuple[bool, str]:
            new = text.strip().lower()
            if not new:
                return False, "A new name for the tag, e.g. vehicle"
            if new == name:
                return False, f"It's called {name} already"
            if "," in new:
                return False, "A tag can't contain a comma (commas separate tags)"
            if new in existing:
                return False, f"There's a tag called {new} already; pick another name"
            return True, f"{name} becomes {new} on {count}"

        def submit(text: str) -> None:
            new = repo.normalize_tag(text)
            self._pending_tag = new
            app.apply(lambda: s.rename_tag(name, new))

        app.prompt(f"Rename the tag {name}", "New name", submit, value=name, preview=preview)

    def action_remove_tag(self) -> None:
        """x in Tags: take the tag off every flow (after asking)."""
        name = self._tag()
        if name is None:
            return
        s = self.session
        app = bdbd(self)
        flows = [f.name for f in s.flows() if name in f.tags]
        which = ", ".join(flows)
        app.confirm(
            f"Remove the tag {name}?",
            f"It comes off {plural(len(flows), 'flow')} ({which}). The flows themselves stay.",
            lambda: app.apply(lambda: s.remove_tag(name)),
            yes="Remove",
        )
