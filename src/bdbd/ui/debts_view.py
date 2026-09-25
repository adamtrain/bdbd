"""Debts: the overview, setting one up, events and the amortization schedule."""

from __future__ import annotations

from datetime import date, timedelta

from rich.console import Console, Group
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from bdbd.ask import DEBT_HORIZON_DAYS, DebtRow
from bdbd.budget import Budget
from bdbd.core.models import DebtEvent, Flow
from bdbd.core.money import cents_to_str, rate_to_str
from bdbd.core.queries.debt_schedule import debt_schedule
from bdbd.ui import charts
from bdbd.ui.common import footer, humanize, meta
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
from bdbd.words import fmt_date, fmt_month, relative, span


def agent_data(rows: list[DebtRow]) -> dict:
    done = [r.paid_off_on for r in rows]
    return {
        "debts": [
            {
                "flow": r.key,
                "name": r.name,
                "balance": cents_to_str(r.balance),
                "annual_rate": rate_to_str(r.rate),
                "payment": cents_to_str(r.payment),
                "paid_off_on": r.paid_off_on.isoformat() if r.paid_off_on else None,
                "interest_remaining": cents_to_str(r.interest),
                "tags": list(r.tags),
            }
            for r in rows
        ],
        "total_owed": cents_to_str(sum(r.balance for r in rows)),
        "monthly_payments": cents_to_str(sum(r.payment for r in rows)),
        "interest_remaining": cents_to_str(sum(r.interest for r in rows)),
        "debt_free_on": (
            max(d for d in done if d is not None).isoformat() if rows and all(done) else None
        ),
    }


def render_overview(console: Console, budget: Budget, rows: list[DebtRow]) -> None:
    today = budget.today
    w = width(console)
    console.print()
    if not rows:
        console.print(Text("No debts. Lovely.", style="bold"))
        console.print(
            Text("A loan is an expense (its payment) plus the loan's terms:", style=FAINT)
        )
        from bdbd.ui.theme import hint

        console.print(
            hint('bdbd debt set "Car loan" --balance 14860 --rate 6.49% --compounding simple')
        )
        console.print()
        return
    owed = sum(r.balance for r in rows)
    monthly = sum(r.payment for r in rows)
    interest = sum(r.interest for r in rows)
    ends = [r.paid_off_on for r in rows if r.paid_off_on is not None]
    free = max(ends) if len(ends) == len(rows) else None
    title = Text.assemble(
        badge("DEBTS", PURPLE), "  ", meta(plural(len(rows), "debt"), fmt_date(today, weekday=True))
    )
    grid = Table.grid(padding=(0, 3))
    grid.add_column(no_wrap=True)
    grid.add_column(justify="right", no_wrap=True)
    grid.add_column(no_wrap=True, style=FAINT)
    grid.add_row(
        "Owed today", Text(money(owed), style="bold"), f"{money(monthly)} a month in payments"
    )
    grid.add_row(
        "Interest to go",
        Text(money(interest), style=f"bold {PURPLE}"),
        "if you keep paying as scheduled",
    )
    if free:
        grid.add_row(
            "Debt-free", Text(fmt_month(free), style=f"bold {GREEN}"), relative(free, today)
        )
    console.print(
        Panel(
            Group(title, Text(), grid), box=PANEL_BOX, border_style=PURPLE, padding=(1, 2), width=w
        )
    )
    console.print()
    console.print(section("Payoff, soonest first"), width=w)
    table = Table(box=None, pad_edge=False, padding=(0, 1), header_style=FAINT)
    table.add_column("", no_wrap=True, overflow="ellipsis", max_width=22)
    table.add_column("Owed", justify="right", no_wrap=True)
    table.add_column("Rate", justify="right", no_wrap=True)
    table.add_column("Payment", justify="right", no_wrap=True)
    table.add_column("", no_wrap=True)
    table.add_column("Paid off", no_wrap=True)
    table.add_column("Interest", justify="right", no_wrap=True)
    horizon = max(((r.paid_off_on - today).days for r in rows if r.paid_off_on), default=1)
    for r in rows:
        if r.paid_off_on:
            length = bar((r.paid_off_on - today).days, horizon, 14, PURPLE)
            when = Text(fmt_month(r.paid_off_on))
        else:
            length = Text("━" * 14, style=AMBER)
            when = Text("not within 60 years", style=AMBER)
        table.add_row(
            r.name,
            money(r.balance),
            Text(pct(r.rate), style=FAINT),
            Text.assemble(money(r.payment), ("/mo", FAINT)),
            length,
            when,
            Text(money(r.interest), style=PURPLE),
        )
    console.print(table)
    console.print()
    footer(console, budget, hints=("bdbd plan --extra 200", "bdbd debt schedule NAME"))
    console.print()


def ask_terms(
    console: Console, f: Flow, balance: str | None, rate: str | None, compounding: str | None
) -> tuple[str, str, str]:
    console.print(Text.assemble(("◇ ", ACCENT), (f"Loan details for {f.name}", f"bold {ACCENT}")))
    while not balance:
        balance = Prompt.ask("  Balance owed", console=console)
    while not rate:
        rate = Prompt.ask("  Annual rate [dim](e.g. 6.49%)[/]", console=console)
    if not compounding:
        console.print(
            Text(
                "  How interest works: simple (most car and student loans), daily "
                "(credit cards), monthly (mortgages)",
                style=FAINT,
            )
        )
        compounding = Prompt.ask(
            "  Compounding", choices=["simple", "daily", "monthly", "continuous"], console=console
        )
    return balance, rate, compounding


def _payoff(budget: Budget, f: Flow) -> dict:
    data, _ = debt_schedule(
        budget.model(include_inactive=True),
        f.id,
        as_of=budget.today,
        until=budget.today + timedelta(days=DEBT_HORIZON_DAYS),
        max_rows=0,
    )
    return data


def _payoff_line(budget: Budget, f: Flow) -> Text:
    data = _payoff(budget, f)
    today = budget.today
    if not data["payoff_date"]:
        return Text("  Not paid off within 60 years at this payment.", style=AMBER)
    pd = date.fromisoformat(data["payoff_date"])
    return Text.assemble(
        ("  Paid off ", FAINT),
        (fmt_date(pd, today), "bold"),
        (
            f" {DOT} {relative(pd, today)} {DOT} "
            f"{money(cents_of(data['total_interest_remaining']))} interest to go",
            FAINT,
        ),
    )


def render_set(console: Console, budget: Budget, f: Flow) -> None:
    d = f.debt
    assert d is not None
    success(
        console,
        Text.assemble(
            (f.name, "bold"),
            " is a debt: ",
            (money(d.balance_cents), "bold"),
            f" at {pct(d.annual_rate)} ({d.compounding}) as of "
            f"{fmt_date(d.balance_as_of, budget.today)}.",
        ),
    )
    console.print(_payoff_line(budget, f))


_EVENT_WORDS = {
    "extra_payment": "an extra payment of",
    "rate_change": "a new rate of",
    "payment_change": "a new payment of",
    "balance_adjustment": "a balance adjustment of",
    "payoff": "paying it off",
}


def render_event(console: Console, budget: Budget, f: Flow, ev: DebtEvent) -> None:
    what = _EVENT_WORDS.get(str(ev.type), str(ev.type))
    amount = ""
    if ev.rate is not None:
        amount = f" {pct(ev.rate)}"
    elif ev.amount_cents is not None:
        amount = f" {money(ev.amount_cents, sign=str(ev.type) == 'balance_adjustment')}"
    success(
        console,
        Text.assemble(
            f"Recorded {what}{amount} on ",
            (f.name, "bold"),
            f" for {fmt_date(ev.date, budget.today)} (event #{ev.id}).",
        ),
    )
    fresh = budget.find(str(f.id))
    console.print(_payoff_line(budget, fresh))


def render_schedule(
    console: Console, budget: Budget, data: dict, full: dict, warnings: list[str]
) -> None:
    today = budget.today
    w = width(console)
    terms = data["terms"]
    title = Text.assemble(
        badge("SCHEDULE", PURPLE),
        "  ",
        (data["name"], "bold"),
        "  ",
        meta(
            f"{pct(terms['annual_rate'])} {terms['compounding']}",
            f"{money(cents_of(terms['scheduled_payment']))} a payment",
        ),
    )
    grid = Table.grid(padding=(0, 3))
    grid.add_column(no_wrap=True)
    grid.add_column(justify="right", no_wrap=True)
    grid.add_column(no_wrap=True, style=FAINT)
    as_of = date.fromisoformat(data["as_of"])
    grid.add_row(
        "Owed",
        Text(money(cents_of(data["balance_at_as_of"])), style="bold"),
        fmt_date(as_of, today, weekday=True),
    )
    if data["payoff_date"]:
        pd = date.fromisoformat(data["payoff_date"])
        grid.add_row(
            "Paid off",
            Text(fmt_date(pd, today), style=f"bold {GREEN}"),
            f"{relative(pd, today)} {DOT} {plural(data['payments_remaining'], 'payment')}",
        )
    else:
        grid.add_row("Paid off", Text("not in this horizon", style=f"bold {AMBER}"), "")
    grid.add_row(
        "Total to pay",
        Text(money(cents_of(data["total_paid_remaining"]))),
        f"{money(cents_of(data['total_principal_remaining']))} principal",
    )
    grid.add_row(
        "Interest",
        Text(money(cents_of(data["total_interest_remaining"])), style=f"bold {PURPLE}"),
        "still to pay",
    )
    if data.get("solved_payment"):
        sp = data["solved_payment"]
        grid.add_row(
            f"To clear it in {span(sp['months'])}",
            Text(money(cents_of(sp["monthly_payment"])), style=f"bold {ACCENT}"),
            "a month",
        )
    console.print()
    console.print(
        Panel(
            Group(title, Text(), grid), box=PANEL_BOX, border_style=PURPLE, padding=(1, 2), width=w
        )
    )
    rows = full["rows"]
    if len(rows) > 1:
        series = [(as_of, cents_of(data["balance_at_as_of"]))] + [
            (date.fromisoformat(r["date"]), cents_of(r["balance"])) for r in rows
        ]
        console.print()
        console.print(section("Balance owed"), width=w)
        for line in charts.balance_chart(series, w, height=6, color=PURPLE):
            console.print(line)
    shown = data["rows"]
    if shown:
        console.print()
        console.print(section("Payments"), width=w)
        table = Table(box=None, pad_edge=False, padding=(0, 1), header_style=FAINT)
        table.add_column("#", justify="right", no_wrap=True, style=FAINT)
        table.add_column("Date", no_wrap=True)
        table.add_column("Payment", justify="right", no_wrap=True)
        table.add_column("Interest", justify="right", no_wrap=True, style=PURPLE)
        table.add_column("Principal", justify="right", no_wrap=True)
        table.add_column("Balance", justify="right", no_wrap=True)
        table.add_column("", style=FAINT)
        start_bal = cents_of(data["balance_at_as_of"]) or 1
        for r in shown:
            bal = cents_of(r["balance"])
            kind = "" if r["kind"] == "scheduled" else r["kind"]
            table.add_row(
                str(r["n"]),
                fmt_date(date.fromisoformat(r["date"]), today),
                money(cents_of(r["payment"])),
                money(cents_of(r["interest"])),
                money(cents_of(r["principal"])),
                money(bal),
                Text.assemble(bar(start_bal - bal, start_bal, 12, GREEN), f"  {kind}"),
            )
        console.print(table)
        more = len(rows) - len(shown)
        if more > 0:
            console.print(
                Text(f"  … {plural(more, 'more payment')} (see them all with --all)", style=FAINT)
            )
    for a in data.get("scenario_applied") or []:
        console.print(Text.assemble(("  ↳ ", ACCENT), (humanize(a), FAINT)))
    console.print()
    footer(console, budget, warnings=warnings)
    console.print()
