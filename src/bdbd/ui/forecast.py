"""Projections, the upcoming list and the month calendar."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bdbd.ask import Day, Window
from bdbd.budget import Budget, Start
from bdbd.core import engine
from bdbd.core.models import Kind
from bdbd.ui import charts
from bdbd.ui.common import closing_balances, footer, humanize, meta, signed, start_note
from bdbd.ui.theme import (
    ACCENT,
    AMBER,
    DARK,
    DOT,
    FAINT,
    GREEN,
    MINUS,
    PANEL_BOX,
    PURPLE,
    badge,
    balance_color,
    cents_of,
    currency,
    money,
    section,
    width,
)
from bdbd.words import DAY_SHORT, MONTH_LONG, fmt_date, fmt_month, relative, span

# ── project ───────────────────────────────────────────────────────────────────


def _kv(grid: Table, label: str, value: Text, note: Text | str = "") -> None:
    grid.add_row(label, value, note if isinstance(note, Text) else Text(note, style=FAINT))


def render_project(
    console: Console,
    budget: Budget,
    data: dict,
    run: engine.SimResult,
    start: Start,
    warnings: list[str],
) -> None:
    today = budget.today
    w = width(console)
    as_of, until = date.fromisoformat(data["as_of"]), date.fromisoformat(data["until"])
    months = (until.year - as_of.year) * 12 + until.month - as_of.month
    low = budget.weekly_spend()
    title = Text.assemble(
        badge("PROJECTION"),
        "  ",
        meta(
            f"{fmt_date(as_of, today, weekday=True)} → {fmt_date(until, today, weekday=True)}",
            span(max(months, 0)) if months else f"{(until - as_of).days} days",
            "with what-ifs" if data.get("scenario_applied") else "",
        ),
    )
    grid = Table.grid(padding=(0, 3))
    grid.add_column(no_wrap=True)
    grid.add_column(justify="right", no_wrap=True)
    grid.add_column(no_wrap=True, overflow="ellipsis")
    _kv(grid, "Starts at", Text(money(start.cents), style="bold"), start_note(start, today))
    end = cents_of(data["ending_balance"])
    _kv(
        grid,
        "Ends at",
        Text(money(end), style=f"bold {balance_color(end, low)}"),
        f"end of {fmt_date(until, today, weekday=True)}",
    )
    spare = data.get("spare") or {}
    if data.get("spare_balance") is not None and spare.get("next_income"):
        s = cents_of(data["spare_balance"])
        nxt = spare["next_income"]
        committed = spare.get("committed_before_next_income") or []
        names = ", ".join(c["name"] for c in committed[:3]) + (" …" if len(committed) > 3 else "")
        note = f"after {names} before " if names else "before "
        note += f"{nxt['name']} on {fmt_date(date.fromisoformat(nxt['date']), today)}"
        _kv(grid, "Spare then", Text(money(s), style=f"bold {balance_color(s, low)}"), note)
    lo = data["min_balance"]
    lo_c = cents_of(lo["balance"])
    _kv(
        grid,
        "Lowest",
        Text(money(lo_c), style=f"bold {balance_color(lo_c, low)}"),
        fmt_date(date.fromisoformat(lo["date"]), today, weekday=True),
    )
    totals = data["totals"]
    _kv(
        grid,
        "Money in",
        Text(money(cents_of(totals["income"]), sign=True), style=GREEN),
        f"out {money(-cents_of(totals['expense']))}"
        + (
            f", {money(cents_of(data['lifestyle_total']))} of it everyday spending"
            if cents_of(data["lifestyle_total"])
            else ""
        ),
    )
    interest = cents_of(totals["interest_paid"])
    if interest:
        debt_left = cents_of(data["total_debt_at_until"])
        _kv(
            grid,
            "Interest paid",
            Text(money(interest), style=PURPLE),
            f"{money(debt_left)} of debt left at the end" if debt_left else "debt-free by the end",
        )
    subtitle = None
    if data.get("scenario_applied"):
        subtitle = Text(
            f" what if: {len(data['scenario_applied'])} change"
            f"{'s' if len(data['scenario_applied']) != 1 else ''} ",
            style=FAINT,
        )
    console.print()
    console.print(
        Panel(
            Group(title, Text(), grid),
            box=PANEL_BOX,
            border_style=ACCENT,
            padding=(1, 2),
            width=w,
            subtitle=subtitle,
            subtitle_align="right",
        )
    )
    if run.daily:
        console.print()
        console.print(section("Balance, day by day"), width=w)
        for line in charts.balance_chart(run.daily, w, height=8):
            console.print(line)
    rows = data["series"]
    if len(rows) > 1:
        console.print()
        console.print(section("Month by month"), width=w)
        table = Table(box=None, pad_edge=False, padding=(0, 1), header_style=FAINT)
        table.add_column("", no_wrap=True, style=FAINT)
        table.add_column("In", justify="right", no_wrap=True)
        table.add_column("Out", justify="right", no_wrap=True)
        table.add_column("Net", justify="right", no_wrap=True)
        table.add_column("Balance", justify="right", no_wrap=True)
        table.add_column("Spare", justify="right", no_wrap=True)
        for r in rows[1:]:
            d = date.fromisoformat(r["date"])
            inc, exp, net = cents_of(r["income"]), cents_of(r["expense"]), cents_of(r["net"])
            bal = cents_of(r["balance"])
            sp = r.get("spare")
            table.add_row(
                fmt_date(d, today),
                Text(money(inc, sign=True), style=GREEN) if inc else Text("—", style=FAINT),
                Text(money(-exp)) if exp else Text("—", style=FAINT),
                Text(money(net, sign=True), style=GREEN if net >= 0 else AMBER),
                Text(money(bal), style=f"bold {balance_color(bal, low)}"),
                Text(money(cents_of(sp)), style=balance_color(cents_of(sp), low))
                if sp is not None
                else Text(""),
            )
        console.print(table)
    open_debts = [d for d in data.get("debts", []) if Decimal(d["balance_at_until"]) > 0]
    paid = [d for d in data.get("debts", []) if d.get("paid_off_on")]
    if paid or open_debts:
        console.print()
        line = Text()
        for d in paid:
            line.append("✓ ", style=GREEN)
            line.append(f"{d['name']} paid off {fmt_date(date.fromisoformat(d['paid_off_on']))}   ")
        for d in open_debts:
            line.append(f"{d['name']} ", style="")
            line.append(f"{money(cents_of(d['balance_at_until']))} left   ", style=FAINT)
        console.print(line)
    for a in data.get("scenario_applied") or []:
        console.print(Text.assemble(("  ↳ ", ACCENT), (humanize(a), FAINT)))
    console.print()
    footer(console, budget, warnings=warnings)
    console.print()


# ── upcoming ──────────────────────────────────────────────────────────────────


def render_upcoming(console: Console, budget: Budget, w_: Window, end: date) -> None:
    today = budget.today
    low = budget.weekly_spend()
    known = w_.start.known
    console.print()
    head = Text.assemble(
        ("Coming up", "bold"),
        (
            f" {DOT} {fmt_date(today, today, weekday=True)} "
            f"to {fmt_date(end, today, weekday=True)}",
            FAINT,
        ),
    )
    console.print(head)
    if known:
        note = Text.assemble(
            ("From ", FAINT),
            (money(w_.start.cents), "bold"),
            ("  ", ""),
            start_note(w_.start, today),
        )
        if w_.weekly:
            note.append(
                f" {DOT} balances include {money(w_.weekly)}/week everyday spending", style=FAINT
            )
        console.print(note)
    console.print()
    items = w_.items
    if not items:
        console.print(Text("Nothing scheduled.", style=FAINT))
    table = Table(box=None, pad_edge=False, padding=(0, 1), show_header=False)
    table.add_column(no_wrap=True, style=FAINT)
    table.add_column(no_wrap=True, max_width=30, overflow="ellipsis")
    table.add_column(justify="right", no_wrap=True)
    table.add_column(justify="right", no_wrap=True)
    table.add_column(no_wrap=True, style=FAINT)
    last_week = None
    closing = closing_balances(items, w_.run.daily)
    for e, after in zip(items, closing, strict=True):
        week = e.date.isocalendar()[:2]
        if last_week is not None and week != last_week:
            table.add_row("", "", "", "", "")
        last_week = week
        name = Text(e.name)
        if e.debt is not None or e.kind in ("extra_payment", "payoff", "settle"):
            name.append(" ◆", style=PURPLE)
        kind = Kind.INCOME if e.delta_cents > 0 else Kind.EXPENSE
        bal = Text(money(after), style=balance_color(after, low))
        table.add_row(
            fmt_date(e.date, today, weekday=True),
            name,
            signed(abs(e.delta_cents), kind),
            bal if known else Text(""),
            relative(e.date, today),
        )
    console.print(table)
    if known and w_.run.daily:
        console.print()
        lo_d, lo = min(w_.run.daily, key=lambda t: (t[1], t[0]))
        end_bal = w_.run.ending_balance_cents
        console.print(
            Text.assemble(
                ("Ends at ", FAINT),
                (money(end_bal), f"bold {balance_color(end_bal, low)}"),
                (f" on {fmt_date(end, today)} {DOT} lowest ", FAINT),
                (money(lo), f"bold {balance_color(lo, low)}"),
                (f" on {fmt_date(lo_d, today, weekday=True)}", FAINT),
            )
        )
    console.print()
    footer(console, budget, warnings=list(dict.fromkeys(w_.run.warnings)))


# ── calendar ──────────────────────────────────────────────────────────────────


def _short(cents: int) -> str:
    """Calendar amounts in whole units: '+2,650', '-2,150', '-18' (with a real minus)."""
    v = (Decimal(abs(cents)).scaleb(-2)).quantize(Decimal(1))
    return ("+" if cents > 0 else MINUS) + f"{v:,}"


def _whole(cents: int) -> str:
    v = (Decimal(abs(cents)).scaleb(-2)).quantize(Decimal(1))
    return (MINUS if cents < 0 else "") + f"{currency()}{v:,}"


def _cell(day: Day, cw: int, today: date, low: int, rows: int) -> Text:
    """Day number and net on top, what lands that day below, the day's closing balance last."""
    past = day.date < today
    text = Text(no_wrap=True, overflow="ellipsis")
    num = f"{day.date.day:>2}"
    if day.date == today:
        head = Text(f"{num} ", style=f"bold {DARK} on {ACCENT}")
    else:
        head = Text(f"{num} ", style=FAINT if past or day.date.weekday() >= 5 else "bold")
    if day.items:
        net = day.net
        amount = _short(net)
        color = FAINT if past else GREEN if net > 0 else ""
        head.append(amount.rjust(cw - len(head.plain)), style=color)
    text.append_text(head)
    shown = day.items if len(day.items) <= rows else day.items[: rows - 1]
    for name, cents, kind in shown:
        label = name if len(name) <= cw else name[: cw - 1] + "…"
        color = PURPLE if kind == "debt_payment" else GREEN if cents > 0 else ""
        text.append("\n")
        text.append(label, style="dim" if past else color)
    hidden = len(day.items) - len(shown)
    if hidden:
        text.append(f"\n+{hidden} more", style=FAINT)
    text.append("\n" * max(rows - len(shown) - (1 if hidden else 0), 0))
    text.append("\n")
    if day.balance is not None:
        text.append(_whole(day.balance).rjust(cw), style=balance_color(day.balance, low))
    return text


def render_calendar(
    console: Console, budget: Budget, month: date, days: list[Day], start: Start
) -> None:
    today = budget.today
    w = width(console)
    cw = max(9, (w - 8) // 7)
    low = budget.weekly_spend()
    rows = max(1, min(3, max((len(d.items) for d in days), default=0)))
    table = Table(
        box=box.ROUNDED,
        show_lines=True,
        border_style=FAINT,
        pad_edge=False,
        padding=(0, 0),
        header_style=FAINT,
    )
    for i, name in enumerate(DAY_SHORT):
        table.add_column(
            name, width=cw, no_wrap=True, justify="left", header_style=FAINT if i < 5 else "grey30"
        )
    week: list[Text] = [Text("")] * days[0].date.weekday()
    for d in days:
        week.append(_cell(d, cw, today, low, rows))
        if len(week) == 7:
            table.add_row(*week)
            week = []
    if week:
        table.add_row(*(week + [Text("")] * (7 - len(week))))
    console.print()
    title = Text.assemble((f"{MONTH_LONG[month.month]} {month.year}", "bold"))
    if start.known:
        title.append(f"  {DOT} balances at the end of each day, from ", style=FAINT)
        title.append(money(start.cents), style=FAINT)
        title.append(" today", style=FAINT)
    console.print(title)
    console.print(table)
    ahead = [d for d in days if d.date >= today]
    inc = sum(c for d in ahead for _, c, _ in d.items if c > 0)
    out_ = sum(-c for d in ahead for _, c, _ in d.items if c < 0)
    line = Text()
    scope = "Rest of the month" if month <= today else fmt_month(month)
    line.append(f"{scope}: ", style=FAINT)
    line.append(money(inc, sign=True), style=GREEN)
    line.append(" in, ", style=FAINT)
    line.append(money(-out_))
    line.append(" out", style=FAINT)
    known = [d for d in days if d.balance is not None]
    if known:
        lo = min(known, key=lambda d: (d.balance or 0, d.date))
        end = known[-1]
        assert lo.balance is not None and end.balance is not None
        line.append(f" {DOT} lowest ", style=FAINT)
        line.append(money(lo.balance), style=f"bold {balance_color(lo.balance, low)}")
        line.append(f" on {fmt_date(lo.date, today)} {DOT} ends at ", style=FAINT)
        line.append(money(end.balance), style=f"bold {balance_color(end.balance, low)}")
    console.print(line)
    console.print()
    footer(
        console,
        budget,
        hints=(
            f"bdbd cal {fmt_month(month + timedelta(days=32)).split()[0].lower()}",
            "bdbd upcoming",
        ),
    )
    console.print()
