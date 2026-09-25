"""The Overview (1): where you stand.

The same numbers as `bdbd overview`: the balance now, what's spare until payday, the lowest
point ahead, the monthly totals, the next 90 days as a chart, what's coming up, and the debts.
With a what-if on, each headline number says how far it moved from the budget as it is.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.content import Content
from textual.widgets import Static

from bdbd import ask
from bdbd.tui.text import (
    DEBT_KINDS,
    STALE_DAYS,
    bold_balance,
    ledger_key,
    versus,
    what_if_only,
)
from bdbd.tui.widgets import (
    Chart,
    Column,
    EmptyState,
    Item,
    KeyValues,
    Panel,
    Row,
    RowList,
    View,
    balance,
    bdbd,
    day_cell,
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
    RED,
    bar,
    cents_of,
    money,
    money_short,
    pct,
)
from bdbd.words import fmt_date, fmt_month, relative

DEBTS_MIN_WIDTH = 96  # below this the Debts panel gives its room to Coming up
DEBT_BAR_WIDTH = 110  # below this the Debts panel drops its payoff bars


class OverviewView(View):
    """Where you stand: headline numbers, the next 90 days, coming up, and debts."""

    title = "Overview"

    DEFAULT_CSS = """
    OverviewView {
        layout: vertical;
        & #headline { height: auto; }
        & #chart-panel { height: 1fr; min-height: 6; }
        & #lower { height: auto; }
        & #lower > Panel { height: 1fr; width: 1fr; }  /* halves: the debts' bars need it */
        & RowList { height: 1fr; }
        & #warnings { height: auto; padding: 0 1; color: $warning; }
        & #welcome { display: none; }
        &.-empty > * { display: none; }
        &.-empty > #welcome { display: block; }
        & #chart-empty { display: none; }
        &.-no-balance #chart { display: none; }
        &.-no-balance #chart-empty { display: block; }
        &.-no-debts #debts-panel { display: none; }
        &.-no-debt-room #debts-panel { display: none; }
        &.-short #headline { padding: 0 2; }
    }
    """

    def compose(self) -> ComposeResult:
        yield Panel(KeyValues(id="numbers"), id="headline", variant="accent")
        yield Panel(
            Chart(id="chart"),
            EmptyState(
                "Your next 90 days show up here once bdbd knows your balance.",
                keys=[("b", "record your balance")],
                id="chart-empty",
            ),
            id="chart-panel",
        )
        with Horizontal(id="lower"):
            yield Panel(
                RowList(
                    Column(style=FAINT),
                    Column(flex=True),
                    Column(align="right"),
                    Column(align="right"),
                    empty="Nothing scheduled in the next ten days.",
                    id="upcoming",
                ),
                id="upcoming-panel",
            )
            yield Panel(
                RowList(
                    Column(flex=True, min=8),
                    Column(align="right"),
                    Column(align="right", style=FAINT),
                    Column(),
                    Column(),
                    id="debts",
                ),
                id="debts-panel",
            )
        yield Static(id="warnings")
        yield EmptyState(
            "Welcome to bdbd.",
            "It projects your balance day by day from your incomes, bills and debts, "
            "so you always know what's spare.",
            keys=[("a", "add your paycheck"), ("b", "record your balance"), ("?", "help")],
            id="welcome",
        )

    # drawing ---------------------------------------------------------------------------

    def refresh_view(self) -> None:
        s = self.session
        empty = not s.flows()
        self.set_class(empty, "-empty")
        if empty:
            return
        p = s.overview()
        base = s.overview(baseline=True) if s.lens_on else None
        low = s.weekly()
        self.set_class(not p.start.known, "-no-balance")
        self.set_class(not p.debts, "-no-debts")
        self._headline(p, base, low)
        self._chart(p)
        self._upcoming(p, base, low)
        self._debts(p)
        stale = ask.stale_warning(p.start, s.today)  # the headline says it already
        warnings = [warning_line(w) for w in p.warnings if w != stale]
        box = self.query_one("#warnings", Static)
        box.update(Text("\n").join(warnings))
        box.display = bool(warnings)

    def _headline(self, p: ask.Picture, base: ask.Picture | None, low: int) -> None:
        today = self.session.today
        rows: list[list[str | Text]] = []
        if p.start.known:
            rows.append(["Balance now", bold_balance(p.start.cents, low), self._start_note(p)])
            spare = _spare(p)
            if spare is not None:
                nxt = (p.spare or {})["next_payday"]
                day = date.fromisoformat(nxt["date"])
                when = fmt_date(day, today, weekday=not self.has_class("-narrow"))
                note = Text.assemble(
                    nxt["name"],
                    " ",
                    (money(cents_of(nxt["amount"]), sign=True), GREEN),
                    (f"  {when} {DOT} {relative(day, today)}", FAINT),
                )
                rows.append(["Spare until payday", bold_balance(spare, low), note])
            else:
                rows.append(
                    ["Spare", Text("—", style=FAINT), Text("no payday ahead to count to", FAINT)]
                )
            if lo := p.low_point:
                note = Text(
                    f"{fmt_date(lo[0], today, weekday=True)} {DOT} next {p.horizon_days} days",
                    style=FAINT,
                )
                rows.append(["Lowest ahead", bold_balance(lo[1], low), note])
            if base is not None:  # how far the what-if moved each number
                deltas = [None, versus(_spare(p), _spare(base))]
                if lo and (blo := base.low_point):
                    deltas.append(versus(lo[1], blo[1]))
                narrow = self.has_class("-narrow")
                for row, delta in zip(rows, deltas, strict=False):
                    if narrow and delta is not None:
                        row[2] = delta  # no room for both: the difference says more
                    elif not narrow:
                        row.insert(2, delta or Text(""))
        else:
            rows.append(
                [
                    "Balance now",
                    Text("unknown", style=f"bold {AMBER}"),
                    Text.assemble(
                        ("press ", AMBER),
                        ("b", f"bold {ACCENT}"),
                        (" to tell bdbd what's in your account", AMBER),
                    ),
                ]
            )
        self.query_one("#numbers", KeyValues).set_rows(rows)
        panel = self.query_one("#headline", Panel)
        panel.fit_title("Where you stand", "with the what-if" if base else "")
        panel.border_subtitle = _monthly(p, short=self.size.width < 100)

    def _start_note(self, p: ask.Picture) -> Text:
        today = self.session.today
        rec = p.start.recorded
        if p.start.source == "carried" and rec and (today - rec.as_of).days > STALE_DAYS:
            return Text.assemble(
                (f"recorded {relative(rec.as_of, today)} {DOT} ", AMBER),
                ("b", f"bold {ACCENT}"),
                (" to update it", AMBER),
            )
        return start_note(p.start, today, p.run.ledger)

    def _chart(self, p: ask.Picture) -> None:
        panel = self.query_one("#chart-panel", Panel)
        title = f"Next {p.horizon_days} days"
        if not p.start.known or not p.run.daily:
            panel.fit_title(title)
            return
        self.query_one("#chart", Chart).set_series(p.run.daily)
        end, cents = p.run.daily[-1]
        extra = f"ends at {money(cents)} on {fmt_date(end, self.session.today)}"
        panel.fit_title(title, extra)

    def _upcoming(self, p: ask.Picture, base: ask.Picture | None, low: int) -> None:
        s = self.session
        today = s.today
        items = ask.coming_up(p, today)
        after = closing_balances(items, p.run.daily)
        known = p.start.known
        baseline = {ledger_key(e) for e in base.run.ledger} if base else None
        rows: list[Item] = []
        week = None
        seen: dict[tuple, int] = {}
        for e, bal in zip(items, after, strict=True):
            if week is not None and e.date.isocalendar()[:2] != week:
                rows.append(None)
            week = e.date.isocalendar()[:2]
            name = Text(e.name)
            if e.kind in DEBT_KINDS:
                name.append(" ◆", style=PURPLE)
            if base is not None and what_if_only(e, baseline):
                name = Text.assemble(("↳ ", ACCENT), name)
            ident = (e.key, e.date, e.kind)
            seen[ident] = seen.get(ident, -1) + 1  # the same flow twice that day, say
            rows.append(
                Row(
                    day_cell(e.date, today),
                    name,
                    signed(e.delta_cents),
                    balance(bal, low) if known else "",
                    key=(*ident, seen[ident]),
                )
            )
        upcoming = self.query_one("#upcoming", RowList)
        upcoming.set_rows(rows)
        weekly = s.weekly()
        extra = f"balances include {money_short(weekly)}/week everyday" if weekly and known else ""
        panel = self.query_one("#upcoming-panel", Panel)
        panel.fit_title("Coming up", extra)
        shown = p.debts and not self.has_class("-no-debt-room")
        self._fit_lower(len(rows), len(p.debts) if shown else 0)

    def _debts(self, p: ask.Picture) -> None:
        if not p.debts:
            return
        today = self.session.today
        bars = self.size.width >= DEBT_BAR_WIDTH
        horizon = max(((r.paid_off_on - today).days for r in p.debts if r.paid_off_on), default=1)
        rows: list[Item] = []
        for r in p.debts:
            if r.paid_off_on is None:
                length = Text("━" * 8, style=AMBER)
                when = Text("never", style=AMBER)
            elif r.paid_off_on <= today:
                length = Text("─" * 8, style=FAINT)
                when = Text("✓ paid off", style=GREEN)
            else:
                length = bar((r.paid_off_on - today).days, max(horizon, 1), 8, PURPLE)
                when = Text(fmt_month(r.paid_off_on))
            cells = [r.name, money(r.balance), pct(r.rate), *([length] if bars else []), when]
            rows.append(Row(*cells, key=r.key))
        debts = self.query_one("#debts", RowList)
        wanted = 5 if bars else 4
        if len(debts.columns) != wanted:
            debts.columns = (
                Column(flex=True, min=8),
                Column(align="right"),
                Column(align="right", style=FAINT),
                *([Column()] if bars else []),
                Column(),
            )
            debts._natural = [0] * wanted
        debts.set_rows(rows)
        owed = sum(r.balance for r in p.debts)
        done = [r.paid_off_on for r in p.debts]
        free = f"debt-free {fmt_month(max(d for d in done if d))}" if all(done) else ""
        panel = self.query_one("#debts-panel", Panel)
        panel.fit_title("Debts", f"{money(owed)} owed", free)

    def _fit_lower(self, upcoming: int, debts: int) -> None:
        """The lower panels are as tall as their longer list (within reason)."""
        lower = self.query_one("#lower", Horizontal)
        rows = max(upcoming, debts, 1)
        cap = 14 if self.screen.size.height >= 32 else 7
        lower.styles.height = min(rows, cap) + 2

    def on_resize(self) -> None:
        """Narrow screens shorten the headline's notes and the monthly line, and the Debts
        panel drops its bars, then gives its room to Coming up."""
        self.set_class(self.size.width < DEBTS_MIN_WIDTH, "-no-debt-room")
        s = self.session
        if self.drawn == s.version and not self.has_class("-empty"):
            p = s.overview()
            base = s.overview(baseline=True) if s.lens_on else None
            self._headline(p, base, s.weekly())
            self._debts(p)

    # actions ---------------------------------------------------------------------------

    def on_row_list_selected(self, event: RowList.Selected) -> None:
        key = event.key
        flow = key[0] if isinstance(key, tuple) else key
        if isinstance(flow, int):
            bdbd(self).open_flow_card(flow)
        else:
            bdbd(self).notify("That's part of the what-if, not your budget.")


# ── Pieces ────────────────────────────────────────────────────────────────────


def _spare(p: ask.Picture) -> int | None:
    if p.spare and p.spare["spare_balance"] is not None:
        return cents_of(p.spare["spare_balance"])
    return None


def _monthly(p: ask.Picture, *, short: bool) -> Content:
    """'+$5,760.07/mo in · -$3,378.09 bills · -$760.94 everyday · net +$1,621.04/mo'."""

    def m(cents: int, sign: bool = False) -> str:
        if short:  # whole dollars, rounded half up
            dollars = int(Decimal(cents).scaleb(-2).quantize(Decimal(1), rounding=ROUND_HALF_UP))
            return money_short(dollars * 100, sign=sign)
        return money(cents, sign=sign)

    net = p.monthly_net
    return Content.assemble(
        " ",
        (m(p.monthly_in, True), GREEN),
        ("/mo in · ", FAINT),
        (m(-p.monthly_bills), FOREGROUND),
        (" bills · ", FAINT),
        (m(-p.monthly_everyday), FOREGROUND),
        (" everyday · net ", FAINT),
        (m(net, True), f"bold {GREEN if net >= 0 else RED}"),
        ("/mo ", FAINT),
    )
