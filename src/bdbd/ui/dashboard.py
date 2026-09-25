"""`bdbd` with no command: where you stand, what's coming, and how the months add up."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bdbd import agent, ask
from bdbd.budget import Budget, Start
from bdbd.core import engine
from bdbd.core.models import Kind
from bdbd.core.queries.project import SpareCalculator
from bdbd.core.queries.summary import summary
from bdbd.ui import charts
from bdbd.ui.common import (
    closing_balances,
    footer,
    meta,
    signed,
    stale_warning,
    start_note,
)
from bdbd.ui.theme import (
    ACCENT,
    AMBER,
    FAINT,
    GREEN,
    PANEL_BOX,
    PURPLE,
    RED,
    badge,
    balance_color,
    bar,
    cents_of,
    money,
    pct,
    plural,
    section,
    width,
)
from bdbd.words import fmt_date, fmt_month, months_apart, relative, span

HORIZON_DAYS = 90
MIN_UPCOMING_DAYS = 10
MAX_UPCOMING_ROWS = 12


@dataclass
class Picture:
    """Everything the dashboard shows, computed once."""

    start: Start
    run: engine.SimResult
    spare: dict | None
    monthly_in: int
    monthly_bills: int
    monthly_everyday: int
    debts: list[ask.DebtRow]
    warnings: list[str]


def picture(budget: Budget, horizon_days: int = HORIZON_DAYS) -> Picture:
    today = budget.today
    start = budget.start(today)
    model = budget.model()
    weekly = budget.weekly_spend()
    until = today + timedelta(days=horizon_days)
    run = engine.run(
        model,
        as_of=today,
        until=until,
        starting_balance_cents=start.cents,
        weekly_spend_cents=weekly,
    )
    spare = None
    if start.known and run.daily:
        calc = SpareCalculator(model, today, until, weekly)
        spare = calc.compute(today, run.daily[0][1])
    net, _ = summary(model, as_of=today, by="flow")
    debts = ask.debts(budget, model)
    warnings = list(dict.fromkeys(run.warnings))
    if w := stale_warning(start, today):
        warnings.append(w)
    return Picture(
        start=start,
        run=run,
        spare=spare,
        monthly_in=cents_of(net["net"]["income_monthly"]),
        monthly_bills=cents_of(net["net"]["expense_monthly"]),
        monthly_everyday=ask.everyday_monthly(weekly),
        debts=debts,
        warnings=warnings,
    )


# ── Rendering ─────────────────────────────────────────────────────────────────


def _headline(budget: Budget, p: Picture, w: int) -> Panel:
    today = budget.today
    n_flows = len(budget.flows(include_inactive=False))
    n_debts = len(p.debts)
    title = Text.assemble(
        badge("bdbd"),
        "  ",
        meta(
            budget.path.name,
            plural(n_flows, "flow"),
            plural(n_debts, "debt") if n_debts else "",
            fmt_date(today, weekday=True),
        ),
    )
    grid = Table.grid(padding=(0, 3))
    grid.add_column(no_wrap=True)
    grid.add_column(justify="right", no_wrap=True)
    grid.add_column(overflow="ellipsis", no_wrap=True)
    low = weekly_low(budget)
    if p.start.known:
        grid.add_row(
            "Balance now",
            Text(money(p.start.cents), style=f"bold {balance_color(p.start.cents)}"),
            start_note(p.start, today),
        )
        if p.spare and p.spare["spare_balance"] is not None:
            spare = cents_of(p.spare["spare_balance"])
            nxt = p.spare["next_income"]
            nd = date.fromisoformat(nxt["date"])
            grid.add_row(
                "Spare until payday",
                Text(money(spare), style=f"bold {balance_color(spare, low)}"),
                Text.assemble(
                    (nxt["name"], ""),
                    " ",
                    (money(cents_of(nxt["amount"]), sign=True), GREEN),
                    (f"  {fmt_date(nd, today, weekday=True)} · {relative(nd, today)}", FAINT),
                ),
            )
        lo_day, lo = min(p.run.daily, key=lambda t: (t[1], t[0]))
        grid.add_row(
            "Lowest ahead",
            Text(money(lo), style=f"bold {balance_color(lo, low)}"),
            Text(
                f"{fmt_date(lo_day, today, weekday=True)} · next {HORIZON_DAYS} days", style=FAINT
            ),
        )
    else:
        grid.add_row(
            "Balance now",
            Text("unknown", style=f"bold {AMBER}"),
            Text.from_markup(f"[{FAINT}]tell me with[/] [bold {ACCENT}]bdbd balance AMOUNT[/]"),
        )
    net = p.monthly_in - p.monthly_bills - p.monthly_everyday
    subtitle = Text.assemble(
        " ",
        (money(p.monthly_in, sign=True), GREEN),
        ("/mo in · ", FAINT),
        (money(p.monthly_bills), "default"),
        (" bills · ", FAINT),
        (money(p.monthly_everyday), "default"),
        (" everyday · net ", FAINT),
        (money(net, sign=True), f"bold {GREEN if net >= 0 else RED}"),
        ("/mo ", FAINT),
    )
    return Panel(
        Group(title, Text(), grid),
        box=PANEL_BOX,
        border_style=ACCENT,
        padding=(1, 2),
        subtitle=subtitle if n_flows else None,
        subtitle_align="right",
        width=w,
    )


def weekly_low(budget: Budget) -> int:
    """Balances under one week of everyday spending read as 'low'."""
    return budget.weekly_spend()


def _upcoming(budget: Budget, p: Picture) -> list[engine.LedgerEntry]:
    today = budget.today
    entries = [e for e in p.run.ledger if e.kind != "lifestyle"]
    income = [e for e in entries if e.delta_cents > 0]
    stop = income[0].date if income else today + timedelta(days=MIN_UPCOMING_DAYS)
    stop = max(stop, today + timedelta(days=MIN_UPCOMING_DAYS))
    return [e for e in entries if e.date <= stop][:MAX_UPCOMING_ROWS]


def _upcoming_table(budget: Budget, p: Picture, w: int) -> Table:
    today = budget.today
    table = Table(box=None, pad_edge=False, padding=(0, 1), show_header=False)
    table.add_column(no_wrap=True, style=FAINT)
    table.add_column(no_wrap=True, max_width=28, overflow="ellipsis")
    table.add_column(justify="right", no_wrap=True)
    table.add_column(justify="right", no_wrap=True)
    low = weekly_low(budget)
    last_week = None
    items = _upcoming(budget, p)
    closing = closing_balances(items, p.run.daily)
    for e, after in zip(items, closing, strict=True):
        week = e.date.isocalendar()[:2]
        if last_week is not None and week != last_week:
            table.add_row("", "", "", "")
        last_week = week
        name = Text(e.name)
        if e.kind in ("debt_payment", "extra_payment", "payoff"):
            name.append(" ◆", style=PURPLE)
        kind = Kind.INCOME if e.delta_cents > 0 else Kind.EXPENSE
        bal = Text(money(after), style=balance_color(after, low)) if p.start.known else Text("")
        table.add_row(
            fmt_date(e.date, today, weekday=True), name, signed(abs(e.delta_cents), kind), bal
        )
    return table


def _debts_table(budget: Budget, p: Picture, w: int) -> Table:
    today = budget.today
    table = Table(box=None, pad_edge=False, padding=(0, 1), show_header=False)
    table.add_column(no_wrap=True, overflow="ellipsis", max_width=22)
    table.add_column(justify="right", no_wrap=True)
    table.add_column(justify="right", no_wrap=True, style=FAINT)
    table.add_column(justify="right", no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True)
    horizon = max(((r.paid_off_on - today).days for r in p.debts if r.paid_off_on), default=1)
    for r in p.debts:
        off = r.paid_off_on
        cells = 14
        if off:
            months = months_apart(today, off)
            length = bar((off - today).days, horizon, cells, PURPLE)
            when = Text.assemble(fmt_month(off), (f"  {span(max(months, 0))}", FAINT))
        else:
            length = Text("━" * cells, style=AMBER)
            when = Text("not paid off", style=AMBER)
        table.add_row(
            r.name,
            money(r.balance),
            pct(r.rate),
            Text.assemble(money(r.payment), ("/mo", FAINT)),
            length,
            when,
        )
    return table


def _hints(budget: Budget, p: Picture) -> tuple[str, ...]:
    """What to try next, given what's in the budget so far."""
    if not budget.flows():
        return ("bdbd add Rent 1950 monthly on the 1st", "bdbd add --help", "bdbd guide")
    hints = ["bdbd upcoming", "bdbd cal", "bdbd ls"]
    if not p.start.known:
        hints.insert(0, "bdbd balance AMOUNT")
    if p.debts:
        hints.append("bdbd plan --extra 200")
    return (*hints, "bdbd --help")


def render(console: Console, budget: Budget) -> None:
    p = picture(budget)
    w = width(console)
    console.print()
    console.print(_headline(budget, p, w))
    if p.start.known and p.run.daily:
        console.print()
        console.print(section(f"Next {HORIZON_DAYS} days"), width=w)
        for line in charts.balance_chart(p.run.daily, w, height=6):
            console.print(line)
    upcoming = _upcoming(budget, p)
    if upcoming:
        console.print()
        weekly = budget.weekly_spend()
        title = "Coming up"
        if weekly and p.start.known:
            title += f" · balances include {money(weekly)}/week everyday spending"
        console.print(section(title), width=w)
        console.print(_upcoming_table(budget, p, w))
    if p.debts:
        console.print()
        owed = sum(r.balance for r in p.debts)
        free = [r.paid_off_on for r in p.debts if r.paid_off_on is not None]
        tail = f" · debt-free {fmt_month(max(free))}" if len(free) == len(p.debts) else ""
        console.print(section(f"Debts · {money(owed)} owed{tail}"), width=w)
        console.print(_debts_table(budget, p, w))
    console.print()
    footer(
        console,
        budget,
        warnings=p.warnings,
        hints=_hints(budget, p),
    )
    console.print()


def agent_data(budget: Budget) -> tuple[dict, list[str]]:
    """The overview for agent mode: the same numbers the dashboard shows."""
    from bdbd.core.money import cents_to_str

    p = picture(budget)
    lo = min(p.run.daily, key=lambda t: (t[1], t[0])) if p.run.daily else None
    net = p.monthly_in - p.monthly_bills - p.monthly_everyday
    return {
        "today": budget.today.isoformat(),
        "balance": agent.start_json(p.start),
        "spare": p.spare,
        "spare_balance": p.spare["spare_balance"] if p.spare else None,
        "low_point": (
            {
                "date": lo[0].isoformat(),
                "balance": cents_to_str(lo[1]),
                "horizon_days": HORIZON_DAYS,
            }
            if lo and p.start.known
            else None
        ),
        "monthly": {
            "income": cents_to_str(p.monthly_in),
            "bills": cents_to_str(p.monthly_bills),
            "everyday": cents_to_str(p.monthly_everyday),
            "net": cents_to_str(net),
        },
        "upcoming": [agent.ledger_json(e) for e in _upcoming(budget, p)],
        "debts": debts_view_agent(p.debts),
    }, p.warnings


def debts_view_agent(rows: list[ask.DebtRow]) -> dict:
    from bdbd.ui.debts_view import agent_data as debts_agent

    return debts_agent(rows)
