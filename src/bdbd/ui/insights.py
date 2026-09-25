"""Answers to questions: spend, summary, compare, earliest, plan, and the small data views."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bdbd.budget import Budget
from bdbd.core import engine
from bdbd.ui import charts
from bdbd.ui.common import footer, humanize, meta
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
from bdbd.words import describe, fmt_date, fmt_month, join, months_apart, relative, span

# ── spend ─────────────────────────────────────────────────────────────────────


def render_spend(console: Console, budget: Budget, data: dict, warnings: list[str]) -> None:
    today = budget.today
    w = width(console)
    start, end = date.fromisoformat(data["from"]), date.fromisoformat(data["until"])
    total = cents_of(data["total"])
    incoming = data["direction"] == "in"
    terms = [m["term"] for m in data.get("matched") or []]
    what = join(terms) if terms else ("everything" if not incoming else "all income")
    excluded = [m["term"] for m in data.get("excluded") or []]
    verb = "coming in from" if incoming else "going out on"
    head = Text.assemble(
        (money(total), f"bold {GREEN if incoming else AMBER}"),
        (f" {verb} ", FAINT),
        (what, "bold"),
        (
            f" from {fmt_date(start, today, weekday=True)} to {fmt_date(end, today, weekday=True)}",
            FAINT,
        ),
    )
    if excluded:
        head.append(f" (not counting {join(excluded)})", style=FAINT)
    console.print()
    console.print(head)
    rows = data["by_flow"]
    if rows:
        console.print()
        console.print(section("By flow"), width=w)
        top = max(cents_of(r["total"]) for r in rows)
        table = Table(box=None, pad_edge=False, padding=(0, 1), show_header=False)
        table.add_column(no_wrap=True, overflow="ellipsis", max_width=28)
        table.add_column(justify="right", no_wrap=True)
        table.add_column(no_wrap=True)
        table.add_column(justify="right", no_wrap=True, style=FAINT)
        table.add_column(no_wrap=True, style=FAINT)
        for r in rows:
            c = cents_of(r["total"])
            share = f"{Decimal(c) * 100 / Decimal(total):.0f}%" if total else ""
            table.add_row(
                r["name"],
                money(c),
                bar(c, top, 20, GREEN if incoming else AMBER),
                share,
                f"{plural(r['count'], 'time')}",
            )
        console.print(table)
    items = data["items"]
    if items:
        console.print()
        console.print(section("When"), width=w)
        table = Table(box=None, pad_edge=False, padding=(0, 1), show_header=False)
        table.add_column(no_wrap=True, style=FAINT)
        table.add_column(no_wrap=True, max_width=28, overflow="ellipsis")
        table.add_column(justify="right", no_wrap=True)
        for it in items[:40]:
            table.add_row(
                fmt_date(date.fromisoformat(it["date"]), today, weekday=True),
                it["name"],
                money(cents_of(it["amount"])),
            )
        console.print(table)
        if len(items) > 40:
            console.print(Text(f"  … and {len(items) - 40} more", style=FAINT))
    life = cents_of(data.get("lifestyle_total", "0"))
    if life:
        console.print(
            Text(
                f"  Includes {money(life)} of everyday spending, spread over each day.", style=FAINT
            )
        )
    console.print()
    footer(console, budget, warnings=warnings)


# ── summary ───────────────────────────────────────────────────────────────────


def render_summary(console: Console, budget: Budget, data: dict, warnings: list[str]) -> None:
    today = budget.today
    w = width(console)
    net = data["net"]
    weekly = cents_of(data.get("weekly_spend", "0"))
    everyday = int((Decimal(weekly) * Decimal("30.4375") / 7).quantize(Decimal(1)))
    inc, exp = cents_of(net["income_monthly"]), cents_of(net["expense_monthly"])
    left = inc - exp - (everyday if not data.get("tag_filter") else 0)
    title = Text.assemble(
        badge("EACH MONTH"),
        "  ",
        meta(
            "steady state, one-offs aside"
            if data["mode"] == "steady"
            else f"averaged over the next {data['window_months']} months",
            f"tag {data['tag_filter']}" if data.get("tag_filter") else "",
        ),
    )
    grid = Table.grid(padding=(0, 3))
    grid.add_column(no_wrap=True)
    grid.add_column(justify="right", no_wrap=True)
    grid.add_column(justify="right", no_wrap=True, style=FAINT)
    inc_y, exp_y = cents_of(net["income_annual"]), cents_of(net["expense_annual"])
    everyday_y = int((Decimal(weekly) * Decimal("365.25") / 7).quantize(Decimal(1)))
    show_everyday = bool(everyday) and not data.get("tag_filter")
    left_y = inc_y - exp_y - (everyday_y if show_everyday else 0)
    grid.add_row("", Text("a month", style=FAINT), Text("a year", style=FAINT))
    grid.add_row("Money in", Text(money(inc, sign=True), style=GREEN), money(inc_y, sign=True))
    grid.add_row("Bills", Text(money(-exp)), money(-exp_y))
    if show_everyday:
        grid.add_row("Everyday spending", Text(money(-everyday)), money(-everyday_y))
    grid.add_row(
        "Left over",
        Text(money(left, sign=True), style=f"bold {GREEN if left >= 0 else RED}"),
        money(left_y, sign=True),
    )
    console.print()
    console.print(
        Panel(
            Group(title, Text(), grid), box=PANEL_BOX, border_style=ACCENT, padding=(1, 2), width=w
        )
    )
    by_tag = [r for r in data.get("by_tag", []) if cents_of(r["expense_monthly"])]
    if by_tag:
        by_tag.sort(key=lambda r: -Decimal(r["expense_monthly"]))
        top = cents_of(by_tag[0]["expense_monthly"])
        console.print()
        console.print(section("Bills by tag, a month"), width=w)
        table = Table(box=None, pad_edge=False, padding=(0, 1), show_header=False)
        table.add_column(no_wrap=True, style=f"bold {AMBER}")
        table.add_column(justify="right", no_wrap=True)
        table.add_column(no_wrap=True)
        table.add_column(justify="right", no_wrap=True, style=FAINT)
        for r in by_tag:
            c = cents_of(r["expense_monthly"])
            share = f"{Decimal(c) * 100 / Decimal(exp):.0f}%" if exp else ""
            table.add_row(r["tag"], money(c), bar(c, top, 28, AMBER), share)
        untagged = cents_of(data.get("untagged", {}).get("expense_monthly", "0"))
        if untagged:
            table.add_row(
                Text("untagged", style=FAINT),
                money(untagged),
                bar(untagged, top, 28, FAINT),
                "",
            )
        console.print(table)
        console.print(Text("  A flow with several tags counts under each.", style=FAINT))
    by_flow = data.get("by_flow", [])
    if by_flow:
        console.print()
        console.print(section("By flow"), width=w)
        table = Table(box=None, pad_edge=False, padding=(0, 1), header_style=FAINT)
        table.add_column("", no_wrap=True, overflow="ellipsis", max_width=24)
        table.add_column("Amount", justify="right", no_wrap=True)
        table.add_column("Schedule", no_wrap=True, overflow="ellipsis", max_width=34)
        table.add_column("A month", justify="right", no_wrap=True)
        table.add_column("A year", justify="right", no_wrap=True, style=FAINT)
        rows = sorted(by_flow, key=lambda r: (r["kind"] != "income", -Decimal(r["monthly"])))
        for r in rows:
            income = r["kind"] == "income"
            sign = 1 if income else -1
            name = Text(r["name"])
            if r.get("is_debt"):
                name.append(" ◆", style=PURPLE)
            m = cents_of(r["monthly"])
            flow = next((f for f in budget.flows() if f.id == r["flow"]), None)
            sched = describe(flow.rrule, flow.dtstart, flow.until) if flow else r["rrule"] or ""
            table.add_row(
                name,
                money(sign * cents_of(r["amount"]), sign=income),
                Text(sched, style=FAINT),
                Text(money(sign * m, sign=income), style=GREEN if income else ""),
                money(sign * cents_of(r["annual"]), sign=income),
            )
        console.print(table)
    if data.get("one_offs"):
        console.print()
        console.print(section("One-offs (not in the monthly numbers)"), width=w)
        table = Table(box=None, pad_edge=False, padding=(0, 1), show_header=False)
        table.add_column(no_wrap=True, style=FAINT)
        table.add_column(no_wrap=True)
        table.add_column(justify="right", no_wrap=True)
        for o in data["one_offs"]:
            c = cents_of(o["amount"]) * (1 if o["kind"] == "income" else -1)
            table.add_row(
                fmt_date(date.fromisoformat(o["date"]), today, weekday=True),
                o["name"],
                Text(money(c, sign=c > 0), style=GREEN if c > 0 else ""),
            )
        console.print(table)
    console.print()
    footer(console, budget, warnings=warnings)
    console.print()


# ── compare / breakeven ───────────────────────────────────────────────────────


def _verdict(data: dict, today: date) -> tuple[str, Text, str]:
    be = data["breakeven"]
    status = be["status"]
    label = data["b"]["label"] if "b" in data else "the what-if"
    if status == "reached":
        d = date.fromisoformat(be["date"])
        return (
            GREEN,
            Text.assemble(
                ("Catches up on ", ""),
                (fmt_date(d, today, weekday=True), "bold"),
                (f"  {relative(d, today)}", FAINT),
            ),
            label,
        )
    if status == "immediate":
        d = date.fromisoformat(be["date"])
        return (
            GREEN,
            Text.assemble(
                ("Ahead from the start", "bold"), (f"  from {fmt_date(d, today)}", FAINT)
            ),
            label,
        )
    if status == "identical":
        return FAINT, Text("No difference at all", style="bold"), label
    text = Text("Doesn't catch up in this window", style="bold")
    if be.get("extrapolated_date"):
        d = date.fromisoformat(be["extrapolated_date"])
        text.append(f"  on its current trend, around {fmt_month(d)}", style=FAINT)
    return AMBER, text, label


def render_compare(
    console: Console,
    budget: Budget,
    data: dict,
    warnings: list[str],
    *,
    breakeven: bool,
    runs: tuple[engine.SimResult, engine.SimResult] | None = None,
) -> None:
    today = budget.today
    w = width(console)
    color, verdict, _ = _verdict(data, today)
    be = data["breakeven"]
    grid = Table.grid(padding=(0, 3))
    grid.add_column(no_wrap=True)
    grid.add_column(justify="right", no_wrap=True)
    grid.add_column(no_wrap=True, style=FAINT)
    end = date.fromisoformat(data["until"])
    diff_end = cents_of(data["difference"]["ending"])
    grid.add_row(
        "Difference at the end",
        Text(money(diff_end, sign=True), style=f"bold {GREEN if diff_end >= 0 else RED}"),
        fmt_date(end, today),
    )
    if be.get("max_shortfall"):
        ms = be["max_shortfall"]
        grid.add_row(
            "Furthest behind",
            Text(money(cents_of(ms["amount"]), sign=True), style=f"bold {AMBER}"),
            fmt_date(date.fromisoformat(ms["date"]), today),
        )
    if not breakeven and "a" in data:
        a, b = data["a"], data["b"]
        grid.add_row(
            "Ending balance",
            Text(money(cents_of(b["ending_balance"]))),
            f"vs {money(cents_of(a['ending_balance']))} as it is",
        )
        grid.add_row(
            "Lowest",
            Text(money(cents_of(b["min_balance"]["balance"]))),
            f"vs {money(cents_of(a['min_balance']['balance']))} as it is",
        )
    title = Text.assemble(badge("BREAKEVEN" if breakeven else "COMPARE", color), "  ", verdict)
    body: list = [title, Text(), grid]
    applied = data.get("scenario_applied") or []
    if applied:
        body.append(Text())
        for a in applied:
            body.append(Text.assemble(("↳ ", ACCENT), (humanize(a), FAINT)))
    console.print()
    console.print(Panel(Group(*body), box=PANEL_BOX, border_style=color, padding=(1, 2), width=w))
    if runs is not None:
        a_run, b_run = runs
        series = [(d, bb - ab) for (d, ab), (_, bb) in zip(a_run.daily, b_run.daily, strict=True)]
        if series:
            console.print()
            console.print(
                section("What-if minus as-it-is, day by day (above zero = ahead)"), width=w
            )
            for line in charts.balance_chart(series, w, height=7, color=GREEN):
                console.print(line)
    rows = data["difference"].get("series") or []
    if rows and not breakeven:
        console.print()
        console.print(section("Month by month"), width=w)
        table = Table(box=None, pad_edge=False, padding=(0, 1), header_style=FAINT)
        table.add_column("", no_wrap=True, style=FAINT)
        table.add_column("As it is", justify="right", no_wrap=True)
        table.add_column("What if", justify="right", no_wrap=True)
        table.add_column("Difference", justify="right", no_wrap=True)
        for r in rows[1:]:
            diff = cents_of(r["diff"])
            table.add_row(
                fmt_date(date.fromisoformat(r["date"]), today),
                money(cents_of(r["a"])),
                money(cents_of(r["b"])),
                Text(money(diff, sign=True), style=GREEN if diff >= 0 else AMBER),
            )
        console.print(table)
    console.print()
    footer(console, budget, warnings=warnings)
    console.print()


# ── earliest ──────────────────────────────────────────────────────────────────


def render_earliest(console: Console, budget: Budget, data: dict, warnings: list[str]) -> None:
    today = budget.today
    w = width(console)
    floor = cents_of(data["floor"])
    found = data["status"] == "found"
    color = GREEN if found else RED
    grid = Table.grid(padding=(0, 3))
    grid.add_column(no_wrap=True)
    grid.add_column(justify="right", no_wrap=True)
    grid.add_column(no_wrap=True, style=FAINT)
    measure = "spare money" if data["measure"] == "spare" else "balance"
    if found:
        d = date.fromisoformat(data["date"])
        verdict = Text.assemble(
            (fmt_date(d, today, weekday=True), "bold"),
            (f"  {relative(d, today)}", FAINT),
        )
        r = data["result"]
        m = r["min_after"]
        grid.add_row(
            "Keeps your " + measure + " above", Text(money(floor), style="bold"), "from that day on"
        )
        low = cents_of(m["spare"] if data["measure"] == "spare" else m["balance"])
        grid.add_row(
            "Lowest after",
            Text(money(low), style=f"bold {balance_color(low, floor)}"),
            fmt_date(date.fromisoformat(m["date"]), today, weekday=True),
        )
        grid.add_row(
            "Headroom",
            Text(money(cents_of(r["headroom"])), style=f"bold {GREEN}"),
            "above the floor at its tightest",
        )
        for e in r.get("placeholder_entries", []):
            c = cents_of(e["delta"])
            grid.add_row(
                Text(f"↳ {e['name']}", style=FAINT),
                Text(money(c, sign=c > 0), style=GREEN if c > 0 else ""),
                f"{e['kind'].replace('_', ' ')} on "
                f"{fmt_date(date.fromisoformat(e['date']), today)}",
            )
    else:
        verdict = Text("No date in the window works", style="bold")
        best = data.get("best_infeasible")
        if best:
            grid.add_row(
                "Closest miss",
                Text(fmt_date(date.fromisoformat(best["date"]), today)),
                f"short by {money(cents_of(best['shortfall']))}",
            )
    title = Text.assemble(badge("EARLIEST", color), "  ", verdict)
    console.print()
    console.print(
        Panel(
            Group(title, Text(), grid), box=PANEL_BOX, border_style=color, padding=(1, 2), width=w
        )
    )
    cand = data["candidates"]
    first, last = date.fromisoformat(cand["from"]), date.fromisoformat(cand["before"])
    step = cand["step_days"]
    days = []
    d = first
    while d <= last:
        if not cand["weekdays_only"] or d.weekday() < 5:
            days.append(d)
        d += timedelta(days=step)
    if found and days:
        found_d = date.fromisoformat(data["date"])
        through = date.fromisoformat(data["feasible_through"])
        cells = [(x, found_d <= x <= through) for x in days if x <= through + timedelta(days=step)]
        console.print()
        console.print(section("Dates tried: red too soon, green works"), width=w)
        for line in charts.strip(cells, w, marker=found_d):
            console.print(line)
    last_bad = data.get("last_infeasible")
    if last_bad:
        m = last_bad["min_after"]
        console.print(
            Text.assemble(
                ("  A day earlier (", FAINT),
                (fmt_date(date.fromisoformat(last_bad["date"]), today), FAINT),
                (f") the {measure} would dip to ", FAINT),
                (
                    money(cents_of(m["spare"] if data["measure"] == "spare" else m["balance"])),
                    AMBER,
                ),
                (f" on {fmt_date(date.fromisoformat(m['date']), today)}.", FAINT),
            )
        )
    console.print()
    tip = ()
    if found:
        tip = (f"bdbd project --on {data['date']} … (the same what-ifs)",)
    footer(console, budget, warnings=warnings, hints=tip)
    console.print()


# ── plan ──────────────────────────────────────────────────────────────────────


def render_plan(console: Console, budget: Budget, data: dict, warnings: list[str]) -> None:
    today = budget.today
    w = width(console)
    start = date.fromisoformat(data["start"])
    done = data["status"] == "debt_free"
    color = GREEN if done else AMBER
    base = data["baseline"]
    extra = cents_of(data["extra_monthly"])
    if done:
        free = date.fromisoformat(data["debt_free_on"])
        verdict = Text.assemble(
            ("Debt-free ", ""), (fmt_month(free), "bold"), (f"  {relative(free, today)}", FAINT)
        )
    else:
        verdict = Text("Not debt-free in this window", style="bold")
    grid = Table.grid(padding=(0, 3))
    grid.add_column(no_wrap=True)
    grid.add_column(justify="right", no_wrap=True)
    grid.add_column(no_wrap=True, style=FAINT)
    grid.add_row(
        "Extra each month",
        Text(money(extra), style=f"bold {ACCENT}"),
        f"{data['strategy']}, from {fmt_date(start, today)}"
        + ("" if data["rollover"] else ", no rollover"),
    )
    outlay = data["monthly_outlay"]
    grid.add_row(
        "Paying in total",
        Text(money(cents_of(outlay["with_extra"]))),
        f"a month, instead of {money(cents_of(outlay['scheduled_payments']))}",
    )
    if done and base.get("debt_free_on"):
        bf = date.fromisoformat(base["debt_free_on"])
        free = date.fromisoformat(data["debt_free_on"])
        sooner = months_apart(free, bf)
        grid.add_row(
            "Instead of",
            Text(fmt_month(bf)),
            f"{span(sooner)} sooner" if sooner > 0 else "about the same",
        )
    saved = cents_of(data["interest_saved"])
    grid.add_row(
        "Interest saved",
        Text(money(saved), style=f"bold {GREEN}"),
        f"{money(cents_of(data['interest_paid']))} paid instead of "
        f"{money(cents_of(base['interest_paid']))}",
    )
    cash = data.get("cash_check")
    if cash:
        m = cash["min_balance_after_start"]
        low = cents_of(m["balance"])
        ok = cash["affordable"]
        grid.add_row(
            "Lowest balance",
            Text(money(low), style=f"bold {GREEN if ok else RED}"),
            f"{fmt_date(date.fromisoformat(m['date']), today)}"
            + ("" if ok else ": you'd run short"),
        )
    title = Text.assemble(badge("PLAN", color), "  ", verdict)
    console.print()
    console.print(
        Panel(
            Group(title, Text(), grid), box=PANEL_BOX, border_style=color, padding=(1, 2), width=w
        )
    )
    steps = data["steps"]
    if steps:
        spans = []
        ends = [start]
        for s in steps:
            off = date.fromisoformat(s["paid_off_on"]) if s["paid_off_on"] else None
            b_off = s.get("baseline_paid_off_on")
            b_date = date.fromisoformat(b_off) if b_off else None
            spans.append(charts.Span(s["name"], start, off, b_date, PURPLE))
            ends += [x for x in (off, b_date) if x]
        console.print()
        console.print(section("Payoff order: solid with the plan, dotted as scheduled"), width=w)
        for line in charts.timeline(spans, start, max(ends), w):
            console.print(line)
        console.print()
        table = Table(box=None, pad_edge=False, padding=(0, 1), header_style=FAINT)
        table.add_column("#", justify="right", style=FAINT, no_wrap=True)
        table.add_column("Debt", no_wrap=True, overflow="ellipsis", max_width=22)
        table.add_column("Rate", justify="right", no_wrap=True, style=FAINT)
        table.add_column("Owed", justify="right", no_wrap=True)
        table.add_column("Paying", justify="right", no_wrap=True)
        table.add_column("Paid off", no_wrap=True)
        table.add_column("Interest", justify="right", no_wrap=True, style=PURPLE)
        for s in steps:
            off = date.fromisoformat(s["paid_off_on"]) if s["paid_off_on"] else None
            paying = s.get("payment_during")
            table.add_row(
                str(s["order"]),
                s["name"],
                pct(s["annual_rate"]),
                money(cents_of(s["balance_at_start"])),
                Text.assemble(money(cents_of(paying)), ("/mo", FAINT))
                if paying
                else Text("on its own", style=FAINT),
                fmt_month(off) if off else Text("not paid off", style=AMBER),
                money(cents_of(s["interest_paid"])),
            )
        console.print(table)
    console.print()
    footer(console, budget, warnings=warnings, hints=("bdbd plan --extra … --strategy snowball",))
    console.print()


# ── config and sql ────────────────────────────────────────────────────────────


def render_config(console: Console, config: dict, keys: dict) -> None:
    console.print()
    for key, help_ in keys.items():
        value = config.get(key)
        shown = Text(value, style="bold") if value is not None else Text("not set", style=FAINT)
        if key == "weekly_spend" and value is not None:
            shown = Text.assemble((money(cents_of(value)), "bold"), (" a week", FAINT))
        console.print(Text.assemble((f"{key}  ", f"bold {ACCENT}"), shown))
        console.print(Text(f"  {help_}", style=FAINT))
    console.print()


def render_sql(console: Console, data: dict) -> None:
    table = Table(header_style=f"bold {ACCENT}", border_style=FAINT, pad_edge=False)
    for c in data["columns"]:
        table.add_column(str(c), overflow="fold")
    for r in data["rows"]:
        table.add_row(*[("" if v is None else str(v)) for v in r.values()])
    console.print(table)
    note = f"{plural(data['row_count'], 'row')}" + (" (truncated)" if data["truncated"] else "")
    console.print(Text(note, style=FAINT))
