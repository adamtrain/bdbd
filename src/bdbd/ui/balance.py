"""`bdbd balance`: the balance bdbd carries forward, and recording a new one."""

from __future__ import annotations

from datetime import date

from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

from bdbd.budget import Budget, Recorded, Start
from bdbd.core.engine import LedgerEntry
from bdbd.core.models import Kind
from bdbd.ui.common import signed
from bdbd.ui.theme import ACCENT, AMBER, DOT, FAINT, GREEN, balance_color, hint, money, success
from bdbd.words import fmt_date, relative


def render_show(console: Console, budget: Budget, start: Start, since: list[LedgerEntry]) -> None:
    today = budget.today
    rec = start.recorded
    console.print()
    if rec is None:
        console.print(Text("bdbd doesn't know your balance yet.", style="bold"))
        console.print(
            Text("Tell it what's in your account and every projection starts there:", style=FAINT)
        )
        console.print(hint("bdbd balance 3200"))
        console.print()
        return
    grid = Table.grid(padding=(0, 3))
    grid.add_column(no_wrap=True, style=FAINT)
    grid.add_column(justify="right", no_wrap=True)
    grid.add_column(no_wrap=True, style=FAINT)
    grid.add_row(
        "Recorded",
        Text(money(rec.amount_cents), style="bold"),
        f"{fmt_date(rec.as_of, today, weekday=True)} {DOT} {relative(rec.as_of, today)}",
    )
    if rec.as_of != today:
        grid.add_row(
            "Today",
            Text(money(start.cents), style=f"bold {balance_color(start.cents)}"),
            "estimated, before anything due today",
        )
    console.print(grid)
    if since:
        console.print()
        console.print(Text("Since then, going by the budget:", style=FAINT))
        table = Table(box=None, pad_edge=False, padding=(0, 1), show_header=False)
        table.add_column(style=FAINT, no_wrap=True)
        table.add_column(no_wrap=True)
        table.add_column(justify="right", no_wrap=True)
        for e in since:
            kind = Kind.INCOME if e.delta_cents > 0 else Kind.EXPENSE
            table.add_row(
                fmt_date(e.date, today, weekday=True), e.name, signed(abs(e.delta_cents), kind)
            )
        console.print(table)
    console.print()
    if (today - rec.as_of).days > 7:
        console.print(
            Text(
                f"It's been {relative(rec.as_of, today).removesuffix(' ago')}. "
                "Update it for sharper numbers:",
                style=AMBER,
            )
        )
    console.print(hint("bdbd balance AMOUNT"))
    console.print()


def ask_posted(console: Console, budget: Budget, day: date, items: list[LedgerEntry]) -> bool:
    today = budget.today
    listed = ", ".join(f"{e.name} {money(e.delta_cents, sign=True)}" for e in items)
    when = "Today's" if day == today else f"{fmt_date(day, today)}'s"
    console.print(Text.assemble((f"{when} scheduled items: ", FAINT), (listed, "")))
    return Confirm.ask("Are they already in that balance?", default=True, console=console)


def render_recorded(console: Console, budget: Budget, r: Recorded) -> None:
    today = budget.today
    day = r.balance.as_of
    when = "today" if day == today else fmt_date(day, today, weekday=True)
    success(console, Text.assemble("Recorded ", (money(r.typed_cents), "bold"), f" for {when}."))
    if r.posted:
        listed = ", ".join(f"{e.name} {money(e.delta_cents, sign=True)}" for e in r.posted)
        console.print(Text(f"  Counted {listed} as already in it.", style=FAINT))
    if r.expected_cents is not None and r.previous is not None:
        diff = r.balance.amount_cents - r.expected_cents
        prev = f"{money(r.previous.amount_cents)} on {fmt_date(r.previous.as_of, today)}"
        line = Text.assemble(
            ("  From ", FAINT),
            (prev, FAINT),
            (", the budget expected ", FAINT),
            money(r.expected_cents),
        )
        if diff == 0:
            line.append(": right on it.", style=GREEN)
        else:
            ahead = diff > 0
            line.append(": you're ", style=FAINT)
            line.append(money(abs(diff)), style=f"bold {GREEN if ahead else AMBER}")
            line.append(" ahead of it." if ahead else " behind it.", style=FAINT)
        console.print(line)
    console.print(
        Text.assemble(
            ("  Everything now starts from here. ", FAINT),
            ("bdbd", f"bold {ACCENT}"),
            (" shows where that leaves you.", FAINT),
        )
    )
