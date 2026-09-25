"""Listing, showing, adding and changing flows, and tags."""

from __future__ import annotations

from decimal import Decimal

from rich.console import Console, Group
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from bdbd.ask import Details, Listing
from bdbd.budget import Budget
from bdbd.core.errors import CashError
from bdbd.core.models import Flow, Kind, Weekend
from bdbd.core.queries.summary import summary
from bdbd.ui.common import DEBT_MARK, flow_name, next_date, schedule_text, signed
from bdbd.ui.theme import (
    ACCENT,
    AMBER,
    DOT,
    FAINT,
    GREEN,
    PANEL_BOX,
    PURPLE,
    badge,
    bar,
    cents_of,
    money,
    pct,
    plural,
    section,
    success,
    width,
)
from bdbd.words import (
    SCHEDULE_EXAMPLES,
    describe,
    fmt_date,
    next_dates,
    parse_schedule,
    relative,
)

# ── ls ────────────────────────────────────────────────────────────────────────


def _group_table(w: int, widths: dict[str, int]) -> Table:
    """One group's table; `widths` are shared so every group's columns line up."""
    table = Table(box=None, pad_edge=False, padding=(0, 1), show_header=False, width=w)
    table.add_column(no_wrap=True, width=widths["name"], overflow="ellipsis")
    table.add_column(justify="right", no_wrap=True, width=widths["amount"])
    table.add_column(no_wrap=True, overflow="ellipsis", ratio=5, min_width=18)  # schedule
    table.add_column(no_wrap=True, width=widths["next"])
    table.add_column(justify="right", no_wrap=True, width=widths["monthly"])
    table.add_column(no_wrap=True, overflow="ellipsis", ratio=2, style=FAINT)  # tags
    return table


def _widths(lst: Listing, today) -> dict[str, int]:
    flows = lst.flows
    return {
        "name": min(24, max(len(f.name) + (2 if f.debt else 0) for f in flows)),
        "amount": max(len(money(f.amount_cents)) + 1 for f in flows),
        "next": max([len(fmt_date(d, today, weekday=True)) for d in lst.next.values() if d] + [6]),
        "monthly": max(len(money(m)) + 1 for m in lst.monthly.values()),
    }


def render_list(console: Console, budget: Budget, lst: Listing, *, tag: str | None = None) -> None:
    w = width(console)
    today = budget.today
    groups = [
        ("Income", [f for f in lst.flows if f.active and f.kind == Kind.INCOME], GREEN),
        ("Expenses", [f for f in lst.flows if f.active and f.kind == Kind.EXPENSE], ""),
        ("Paused", [f for f in lst.flows if not f.active], FAINT),
    ]
    console.print()
    if not lst.flows:
        what = f" tagged {tag}" if tag else ""
        console.print(Text(f"No flows{what} yet.", style="bold"))
        from bdbd.ui.theme import hint

        console.print(hint("bdbd add Rent 1950 monthly on the 1st", "bdbd add --help"))
        console.print()
        return
    widths = _widths(lst, today)
    for title, flows, _color in groups:
        if not flows:
            continue
        flows.sort(key=lambda f: (lst.next[f.id] is None, lst.next[f.id] or today, f.name))
        total = sum(lst.monthly[f.id] for f in flows)
        heading = f"{title} {DOT} {plural(len(flows), 'flow')}"
        if title != "Paused":
            sign = 1 if title == "Income" else -1
            heading += f" {DOT} {money(sign * total, sign=True)} a month"
        console.print(section(heading), width=w)
        table = _group_table(w, widths)
        for f in flows:
            nxt = lst.next[f.id]
            monthly = lst.monthly[f.id]
            per_month = (
                Text(
                    money(monthly if f.kind == Kind.INCOME else -monthly, sign=True),
                    style=GREEN if f.kind == Kind.INCOME else "",
                )
                if monthly
                else Text("")
            )
            dim = not f.active
            table.add_row(
                flow_name(f, dim=dim),
                signed(f.amount_cents, f.kind) if not dim else Text(money(f.amount_cents), "dim"),
                schedule_text(f),
                Text(fmt_date(nxt, today, weekday=True))
                if f.active and nxt
                else Text("paused" if not f.active else "—", style=FAINT),
                per_month,
                ", ".join(f.tags),
            )
        console.print(table)
        console.print()
    legend = Text.assemble(
        (DEBT_MARK, PURPLE),
        (" debt   ", FAINT),
        ("→Mon", FAINT),
        (" moves weekend dates to Monday", FAINT),
    )
    console.print(legend)
    console.print()


# ── show ──────────────────────────────────────────────────────────────────────


def _kv(grid: Table, label: str, value: Text | str) -> None:
    grid.add_row(Text(label, style=FAINT), value)


def _compounding_text(flow: Flow) -> str:
    d = flow.debt
    assert d is not None
    comp = str(d.compounding)
    if comp == "simple":
        text = "simple interest, accrued daily, never compounds"
    elif comp == "daily":
        text = "compounds daily"
    elif comp == "monthly":
        day = d.posting_day or d.balance_as_of.day
        from bdbd.words import ordinal

        text = f"compounds monthly, posted on the {ordinal(day)}"
    else:
        text = "compounds continuously"
    if str(d.payment_mode) == "interest_only":
        text += ", interest-only payments"
    elif str(d.payment_mode) == "percent_of_balance" and d.payment_pct is not None:
        text += f", pays the larger of the amount or {pct(d.payment_pct)} of the balance"
    if str(d.day_count) != "actual/365":
        text += f", {d.day_count}"
    return text


def render_card(console: Console, budget: Budget, info: Details) -> None:
    f = info.flow
    today = budget.today
    w = min(width(console), 96)
    kind_color = GREEN if f.kind == Kind.INCOME else AMBER
    heading = Text.assemble(
        badge("INCOME" if f.kind == Kind.INCOME else "EXPENSE", kind_color),
        *((" ", badge("DEBT", PURPLE)) if f.debt else ()),
        *((" ", badge("PAUSED", "grey50")) if not f.active else ()),
        "  ",
        (f.name, "bold"),
    )
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True)
    grid.add_column()
    _kv(grid, "Amount", signed(f.amount_cents, f.kind))
    sched = Text(describe(f.rrule, f.dtstart, f.until))
    if str(f.weekend) == "next":
        sched.append(f" {DOT} weekend dates move to Monday", style=FAINT)
    elif str(f.weekend) == "previous":
        sched.append(f" {DOT} weekend dates move to Friday", style=FAINT)
    _kv(grid, "Schedule", sched)
    if f.rrule is not None:
        starts = "Started" if f.dtstart <= today else "Starts"
        span = fmt_date(f.dtstart)
        if f.until:
            span += f", ends {fmt_date(f.until)}"
        _kv(grid, starts, span)
    if info.upcoming:
        dates = Text(f" {DOT} ".join(fmt_date(d, today, weekday=True) for d in info.upcoming))
        _kv(grid, "Next" if f.rrule else "On", dates)
    elif f.active:
        _kv(grid, "Next", Text("nothing ahead", style=FAINT))
    if info.monthly:
        yearly = info.monthly * 12
        _kv(
            grid,
            "Per month",
            Text.assemble(money(info.monthly), (f"  {money(yearly)} a year", FAINT)),
        )
    if f.tags:
        _kv(grid, "Tags", ", ".join(f.tags))
    if f.notes:
        _kv(grid, "Notes", f.notes)
    parts: list = [heading, Text(), grid]
    if f.debt is not None and info.debt is not None:
        d = f.debt
        debt = Table.grid(padding=(0, 2))
        debt.add_column(no_wrap=True)
        debt.add_column()
        now = cents_of(info.debt["balance_at_as_of"])
        owed = Text.assemble((money(now), "bold"))
        if d.balance_as_of != today:
            owed.append(
                f"  today {DOT} {money(d.balance_cents)} on {fmt_date(d.balance_as_of, today)}",
                style=FAINT,
            )
        _kv(debt, "Owed", owed)
        _kv(debt, "Rate", Text.assemble(pct(d.annual_rate), (f"  {_compounding_text(f)}", FAINT)))
        payoff = info.debt["payoff_date"]
        if payoff:
            from datetime import date as _date

            pd = _date.fromisoformat(payoff)
            _kv(
                debt,
                "Paid off",
                Text.assemble(
                    fmt_date(pd, today),
                    (
                        f"  {relative(pd, today)} {DOT} "
                        f"{plural(info.debt['payments_remaining'], 'payment')} {DOT} "
                        f"{money(cents_of(info.debt['total_interest_remaining']))} interest to go",
                        FAINT,
                    ),
                ),
            )
        else:
            _kv(debt, "Paid off", Text("not within 60 years at this payment", style=AMBER))
        for ev in d.events:
            what = str(ev.type).replace("_", " ")
            detail = (
                pct(ev.rate)
                if ev.rate is not None
                else (money(ev.amount_cents) if ev.amount_cents is not None else "")
            )
            _kv(
                debt,
                f"Event #{ev.id}",
                Text.assemble(f"{what} {detail}".strip(), (f"  {fmt_date(ev.date, today)}", FAINT)),
            )
        parts += [Text(), Text("Debt", style=f"bold {PURPLE}"), debt]
    console.print()
    console.print(Panel(Group(*parts), box=PANEL_BOX, border_style=ACCENT, padding=(1, 2), width=w))
    from bdbd.ui.theme import hint

    tips = [f'bdbd edit "{f.name}"' if " " in f.name else f"bdbd edit {f.name}"]
    if f.debt:
        tips.append(
            f'bdbd debt schedule "{f.name}"' if " " in f.name else f"bdbd debt schedule {f.name}"
        )
    console.print(hint(*tips))
    console.print()


# ── add / edit / rm ───────────────────────────────────────────────────────────


def render_added(console: Console, budget: Budget, f: Flow, before: int, after: int) -> None:
    today = budget.today
    nxt = next_date(f, today)
    when = describe(f.rrule, f.dtstart, f.until)
    line = Text.assemble(
        ("Added ", ""),
        (f.name, "bold"),
        ": ",
        signed(f.amount_cents, f.kind),
        f", {when[0].lower() + when[1:]}",
    )
    if nxt and f.rrule is not None:
        line.append(f", next on {fmt_date(nxt, today, weekday=True)}")
    if not f.active:
        line.append(" (paused)", style=FAINT)
    success(console, line)
    if before != after:
        console.print(
            Text.assemble(
                ("  Monthly net ", FAINT),
                money(before, sign=True),
                (" → ", FAINT),
                (money(after, sign=True), f"bold {GREEN if after >= 0 else AMBER}"),
            )
        )


def _flow_fields(f: Flow) -> dict[str, str]:
    return {
        "name": f.name,
        "kind": str(f.kind),
        "amount": money(f.amount_cents),
        "schedule": describe(f.rrule, f.dtstart, f.until),
        "starts": fmt_date(f.dtstart),
        "until": fmt_date(f.until) if f.until else "never",
        "weekends": {"next": "move to Monday", "previous": "move to Friday"}.get(
            str(f.weekend), "stay put"
        ),
        "tags": ", ".join(f.tags) or "none",
        "notes": f.notes or "none",
        "paused": "yes" if not f.active else "no",
    }


def render_edited(console: Console, budget: Budget, before: Flow, after: Flow) -> None:
    a, b = _flow_fields(before), _flow_fields(after)
    changed = [(k, a[k], b[k]) for k in a if a[k] != b[k]]
    if not changed:
        console.print(Text(f"Nothing to change in {after.name}.", style=FAINT))
        return
    success(console, Text.assemble("Updated ", (after.name, "bold")))
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=FAINT, no_wrap=True)
    grid.add_column()
    for key, old, new in changed:
        grid.add_row(f"  {key}", Text.assemble((old, FAINT), (" → ", FAINT), (new, "bold")))
    console.print(grid)


def confirm_remove(console: Console, f: Flow) -> bool:
    extra = ""
    if f.debt is not None:
        n = len(f.debt.events)
        extra = " and its debt record" + (f" ({plural(n, 'event')})" if n else "")
    return Confirm.ask(f"Delete [bold]{f.name}[/]{extra}?", default=False, console=console)


def ask_new_flow(
    console: Console,
    budget: Budget,
    name: str | None,
    amount: str | None,
    text: str | None,
    is_income: bool,
    tags: list[str],
    weekend: Weekend | None,
) -> tuple[str, str, str, bool, list[str], Weekend | None]:
    """Ask for whatever `bdbd add` wasn't given."""
    from bdbd.core.money import parse_amount

    console.print(
        Text.assemble(("◇ ", ACCENT), ("New flow", f"bold {ACCENT}"), ("  Ctrl-C to cancel", FAINT))
    )
    while not name:
        name = Prompt.ask("  Name", console=console).strip()
    while True:
        amount = amount or Prompt.ask("  Amount", console=console)
        try:
            parse_amount(amount)
            break
        except CashError as exc:
            console.print(f"  [{AMBER}]{exc.message}[/]")
            amount = None
    if not text:
        kind = Prompt.ask(
            "  Money [bold]in[/] or [bold]out[/]?",
            choices=["in", "out"],
            default="in" if is_income else "out",
            console=console,
        )
        is_income = kind == "in"
    examples = f" {DOT} ".join(SCHEDULE_EXAMPLES[:3])
    while True:
        text = text or Prompt.ask(f"  When? [{FAINT}]({examples})[/]", console=console)
        try:
            s = parse_schedule(text, today=budget.today)
        except CashError as exc:
            console.print(f"  [{AMBER}]{exc.message}[/]")
            text = None
            continue
        dates = next_dates(s.rrule, s.dtstart, s.until, budget.today, 3)
        shown = f" {DOT} ".join(fmt_date(d, budget.today, weekday=True) for d in dates)
        console.print(
            Text.assemble(
                ("    ", ""), (describe(s.rrule, s.dtstart, s.until), GREEN), (f"  {shown}", FAINT)
            )
        )
        break
    if not tags:
        raw = Prompt.ask(
            f"  Tags [{FAINT}](optional, comma-separated)[/]",
            default="",
            show_default=False,
            console=console,
        )
        tags = [t.strip() for t in raw.split(",") if t.strip()]
    if (
        weekend is None
        and s.rrule is not None
        and Confirm.ask(
            "  Move weekend dates to Monday (like an ACH pull)?", default=False, console=console
        )
    ):
        weekend = Weekend.NEXT
    return name, amount, text, is_income, tags, weekend


# ── tags ──────────────────────────────────────────────────────────────────────


def tag_rows(budget: Budget) -> list[dict]:
    from bdbd.core import repo

    names: dict[str, list[str]] = {}
    for f in budget.flows():
        for t in f.tags:
            names.setdefault(t, []).append(f.name)
    data, _ = summary(budget.model(), as_of=budget.today, by="tag")
    monthly = {r["tag"]: r for r in data["by_tag"]}
    rows = []
    for t in repo.list_tags(budget.conn):
        m = monthly.get(t.name)
        rows.append(
            {
                "name": t.name,
                "flows": t.flow_count,
                "flow_names": names.get(t.name, []),
                "expense_monthly": m["expense_monthly"] if m else "0.00",
                "income_monthly": m["income_monthly"] if m else "0.00",
            }
        )
    return rows


def render_tags(console: Console, budget: Budget, rows: list[dict]) -> None:
    w = width(console)
    console.print()
    if not rows:
        console.print(Text("No tags yet. Add some with [bold]bdbd edit NAME --tag TAG[/]."))
        console.print()
        return
    rows = sorted(rows, key=lambda r: (-Decimal(r["expense_monthly"]), r["name"]))
    top = max((cents_of(r["expense_monthly"]) for r in rows), default=0)
    console.print(section(f"Tags {DOT} a month"), width=w)
    table = Table(box=None, pad_edge=False, padding=(0, 1), show_header=False, width=w)
    table.add_column(no_wrap=True, style=f"bold {AMBER}")
    table.add_column(justify="right", no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(ratio=1, overflow="ellipsis", no_wrap=True, style=FAINT)
    for r in rows:
        out_ = cents_of(r["expense_monthly"])
        in_ = cents_of(r["income_monthly"])
        if in_ and not out_:
            amount = Text(money(in_, sign=True), style=GREEN)
        elif out_:
            amount = Text(money(-out_))
        else:
            amount = Text("one-off", style=FAINT)
        table.add_row(
            r["name"],
            amount,
            bar(out_, top, 14, AMBER) if out_ else Text(" " * 14),
            ", ".join(r["flow_names"]),
        )
    console.print(table)
    console.print()
