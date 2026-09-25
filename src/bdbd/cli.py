"""The `bdbd` command."""

import functools
import inspect
import json
import os
import re
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, cast

import typer
from rich.markdown import Markdown

from bdbd import __version__, agent, ask
from bdbd.budget import Budget, Problem, WhatIf
from bdbd.core import balance as balances
from bdbd.core import db, engine, repo
from bdbd.core.dates import add_months
from bdbd.core.dates import today as today_fn
from bdbd.core.errors import CashError
from bdbd.core.migrations import LATEST_VERSION
from bdbd.core.models import Compounding, DayCount, EventType, Kind, PaymentMode, Weekend
from bdbd.core.money import cents_to_str, parse_amount, parse_rate
from bdbd.core.queries.compare import compare as compare_query
from bdbd.core.queries.debt_schedule import debt_schedule
from bdbd.core.queries.earliest import earliest as earliest_query
from bdbd.core.queries.plan import plan as plan_query
from bdbd.core.queries.project import project as project_query
from bdbd.core.queries.spend import spend as spend_query
from bdbd.core.queries.summary import summary as summary_query
from bdbd.core.scenario import describe as describe_scenario
from bdbd.core.scenario import load_scenario
from bdbd.ui import balance as balance_view
from bdbd.ui import dashboard, debts_view, flows_view, forecast, insights
from bdbd.ui.theme import err, error, out, success
from bdbd.words import SCHEDULE_EXAMPLES, parse_day, parse_schedule

LOOK, CHANGE, DEBTS, ASK, DATA = "Look", "Change", "Debts", "Ask what if", "Data"
WHAT_IF = "What if… (never saved; combine freely)"


# ── Global state ──────────────────────────────────────────────────────────────


@dataclass
class State:
    agent: bool = False
    db: str | None = None
    select: str | None = None
    tidy: bool = True


STATE = State()


def interactive() -> bool:
    """Prompts only when a person is at a terminal (never in agent mode)."""
    return not STATE.agent and sys.stdin.isatty() and sys.stdout.isatty()


DATE_OPTIONS = {"--on", "--until", "--from", "--as-of", "--balance-as-of", "--before"}
TEXT_OPTIONS = {"--when"}
MAX_MERGE = 8


def _merge_words(argv: list[str]) -> list[str]:
    """Let dates and schedules after an option go unquoted: `--until dec 31`, `--on next fri`,
    `--when monthly on the 15th`. The longest run of plain words that still reads as a date
    (or schedule) becomes the option's value; everything after it is left alone."""
    today = today_fn()

    def valid(option: str, text: str) -> bool:
        try:
            if option in TEXT_OPTIONS:
                parse_schedule(text, today=today)
            else:
                parse_day(text, today=today)
        except CashError:
            return False
        return True

    out: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        out.append(arg)
        i += 1
        if arg == "--":
            out.extend(argv[i:])
            break
        if (arg in DATE_OPTIONS or arg in TEXT_OPTIONS) and i < len(argv):
            best = 1
            for n in range(2, MAX_MERGE + 1):
                words = argv[i : i + n]
                if len(words) < n or words[-1].startswith("-"):
                    break
                if valid(arg, " ".join(words)):
                    best = n
            out.append(" ".join(argv[i : i + best]))
            i += best
    return out


def _take_globals(argv: list[str]) -> list[str]:
    """Pull the global flags out of argv wherever they are, so `bdbd ls --agent` works too."""
    rest: list[str] = []
    it = iter(argv)
    for arg in it:
        if arg == "--":
            rest.append(arg)
            rest.extend(it)
            break
        if arg == "--agent":
            STATE.agent = True
        elif arg == "--no-tidy":
            STATE.tidy = False
        elif arg in ("--db", "--select"):
            value = next(it, None)
            if value is None:
                rest.append(arg)
                continue
            setattr(STATE, arg[2:], value)
        elif arg.startswith(("--db=", "--select=")):
            key, _, value = arg[2:].partition("=")
            setattr(STATE, key, value)
        else:
            rest.append(arg)
    if STATE.select:
        STATE.agent = True
    return rest


def db_path() -> Path:
    return db.resolve_db_path(STATE.db)


# ── Running a command ─────────────────────────────────────────────────────────


@dataclass
class Ctx:
    command: str
    budget: Budget
    data: Any = None
    warnings: list[str] = field(default_factory=list)

    @property
    def agent(self) -> bool:
        return STATE.agent

    @property
    def today(self) -> date:
        return self.budget.today

    def emit(self, data: Any, warnings: list[str] | tuple[str, ...] = ()) -> None:
        self.data = data
        self.warnings.extend(warnings)

    def day(self, text: str | None, *, base: date | None = None, prefer: str = "future") -> date:
        if not text:
            return base or self.today
        return parse_day(text, today=self.today, base=base, prefer=prefer)


def _print_json(env: dict) -> None:
    sys.stdout.write(agent.dumps(env) + "\n")
    sys.stdout.flush()


def _fail(command: str, code: str, message: str, hint: str | None = None) -> None:
    usage = code == "usage"
    if STATE.agent:
        _print_json(agent.failure(command, code, message, hint))
    else:
        error(
            err, message[0].upper() + message[1:] if message else "Something went wrong.", hint=hint
        )
    raise typer.Exit(2 if usage else 1)


@contextmanager
def run(command: str, *, tidy: bool | None = None) -> Iterator[Ctx]:
    budget: Budget | None = None
    try:
        budget = Budget.open(db_path(), tidy=STATE.tidy if tidy is None else tidy)
        ctx = Ctx(command, budget)
        yield ctx
        if STATE.agent:
            data = ctx.data
            if STATE.select:
                data = agent.select(data, STATE.select)
            _print_json(agent.envelope(command, data, ctx.warnings, budget.tidied))
        else:
            _print_tidied(budget)
    except Problem as exc:
        _fail(command, exc.code, exc.message, exc.hint)
    except CashError as exc:
        hint = _hint_for(exc.code)
        if exc.code == "unknown_term" and re.match(r"^'\d+'", exc.message):
            hint = 'Put a date with a space in quotes, e.g. [bold]--until "dec 31"[/].'
        _fail(command, exc.code, exc.message, hint)
    except sqlite3.IntegrityError as exc:
        _fail(command, "integrity", str(exc))
    except KeyboardInterrupt:
        err.print("[dim]Cancelled.[/]")
        raise typer.Exit(130) from None
    finally:
        if budget is not None:
            budget.close()


def _print_tidied(budget: Budget) -> None:
    from rich.text import Text

    from bdbd.ui.common import humanize
    from bdbd.ui.theme import FAINT, note

    for action in budget.tidied:
        note(out, Text(f"Tidied up: {humanize(action)}", style=FAINT), glyph="↺")
    budget.tidied = []


def _hint_for(code: str) -> str | None:
    return {
        "invalid_schedule": "Try e.g. " + ", ".join(f"[bold]{s}[/]" for s in SCHEDULE_EXAMPLES[:4]),
        "duplicate_flow": "Change the existing one with [bold]bdbd edit NAME[/].",
        "no_debt": "Add the loan details with [bold]bdbd debt set NAME --balance … --rate …[/].",
        "db_exists": "Pass [bold]--force[/] to start over (this deletes everything in it).",
    }.get(code)


def _need(value: Any, what: str, command: str) -> None:
    if value is None or value == "" or value == []:
        raise Problem(f"{what} is required", "usage", hint=f"See [bold]bdbd {command} --help[/].")


def _money(text: str | None, what: str = "amount", *, negative: bool = False) -> int | None:
    if text is None:
        return None
    try:
        return parse_amount(text, allow_negative=negative)
    except CashError as exc:
        raise Problem(f"{what} {text!r} isn't an amount of money", exc.code) from exc


# ── Shared options ────────────────────────────────────────────────────────────

UntilOpt = Annotated[
    str | None,
    typer.Option(
        "--until",
        metavar="DATE",
        help="Last day (oct 31, eom, +3m, 2027-03-31).",
        show_default=False,
    ),
]
AsOfOpt = Annotated[
    str | None,
    typer.Option(
        "--as-of", metavar="DATE", help="Start on this date instead of today.", show_default=False
    ),
]
BalanceOpt = Annotated[
    str | None,
    typer.Option(
        "--balance",
        "-b",
        metavar="AMOUNT",
        help="Start from this balance instead of the recorded one.",
        show_default=False,
    ),
]
WeeklyOpt = Annotated[
    str | None,
    typer.Option(
        "--weekly-spend",
        metavar="AMOUNT",
        help="Everyday spending per week for this run (default: your setting).",
        show_default=False,
    ),
]
VerboseOpt = Annotated[
    bool, typer.Option("--verbose", help="Agent mode: include bulky sections too.")
]


def _months(default: int) -> Any:
    return typer.Option(
        "--months",
        "-m",
        metavar="N",
        help=f"How many months ahead (default {default}).",
        show_default=False,
    )


Months1 = Annotated[int | None, _months(1)]
Months12 = Annotated[int | None, _months(12)]
Months24 = Annotated[int | None, _months(24)]
Months120 = Annotated[int | None, _months(120)]
Months600 = Annotated[int | None, _months(600)]


def _window(ctx: Ctx, as_of: str | None, until: str | None, months: int | None, default: int):
    start = ctx.day(as_of, prefer="nearest") if as_of else ctx.today
    end = ctx.day(until, base=start) if until else add_months(start, months or default)
    if end < start:
        raise Problem("--until is before the start date", "usage")
    return start, end


_WHAT_IF_OPTIONS: list[tuple[str, str, str, str]] = [
    # name, flag, metavar, help
    ("disable", "--disable", "FLOW", "Leave a flow out."),
    ("disable_tag", "--disable-tag", "TAG", "Leave out every flow with a tag."),
    ("enable", "--enable", "FLOW", "Bring back a paused flow."),
    ("stop", "--stop", "FLOW@DATE", "No more of FLOW after DATE."),
    ("stop_tag", "--stop-tag", "TAG@DATE", "No more of anything tagged TAG after DATE."),
    ("payoff", "--payoff", "FLOW@DATE", "Pay a debt off in full on DATE."),
    (
        "settle",
        "--settle",
        "FLOW:PROCEEDS@DATE",
        "Sell and clear a debt: PROCEEDS go to it, you pay any shortfall, keep any surplus.",
    ),
    ("extra_payment", "--extra-payment", "FLOW:AMOUNT@DATE", "A one-off extra debt payment."),
    ("set_payment", "--set-payment", "FLOW:AMOUNT@DATE", "A new regular debt payment from DATE."),
    ("rate_change", "--rate-change", "FLOW:RATE@DATE", "A new interest rate from DATE."),
    ("add_income", "--add-income", "NAME:AMOUNT@DATE", "One-off money in."),
    ("add_expense", "--add-expense", "NAME:AMOUNT@DATE", "One-off money out."),
    ("set_amount", "--set-amount", "FLOW:AMOUNT[@DATE]", "Change a flow's amount (from DATE)."),
]


def _what_if_params() -> list[inspect.Parameter]:
    params = []
    for name, flag, metavar, help_ in _WHAT_IF_OPTIONS:
        ann = Annotated[
            list[str] | None,
            typer.Option(
                flag, metavar=metavar, help=help_, rich_help_panel=WHAT_IF, show_default=False
            ),
        ]
        params.append(
            inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=ann)
        )
    for name, flag, metavar, help_ in (
        ("scenario", "--scenario", "FILE", "A scenario JSON file (- for stdin)."),
        ("scenario_json", "--scenario-json", "JSON", "A scenario as inline JSON."),
        ("on", "--on", "DATE", "Pin every what-if dated '?' to DATE."),
    ):
        ann = Annotated[
            str | None,
            typer.Option(
                flag, metavar=metavar, help=help_, rich_help_panel=WHAT_IF, show_default=False
            ),
        ]
        params.append(
            inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=ann)
        )
    return params


def what_ifs(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Give a command the what-if flags; it receives them as one `what_if: WhatIf` argument."""
    sig = inspect.signature(fn)
    own = [p for p in sig.parameters.values() if p.name != "what_if"]
    extra = _what_if_params()
    names = [p.name for p in extra]

    lists = {n for n, *_ in _WHAT_IF_OPTIONS}

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        values = {n: kwargs.pop(n) for n in names}
        for n in lists:
            values[n] = values[n] or []
        return fn(*args, what_if=WhatIf(**values), **kwargs)

    params = own + extra
    cast(Any, wrapper).__signature__ = sig.replace(parameters=params)
    wrapper.__annotations__ = {p.name: p.annotation for p in params}
    return wrapper


def _scenario_block(data: dict, model, verbose: bool) -> None:
    scen = model.scenario
    data["scenario"] = describe_scenario(scen) if verbose else (scen or {}).get("name")
    data["scenario_applied"] = model.applied


# ── The app ───────────────────────────────────────────────────────────────────

app = typer.Typer(
    add_completion=False,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=False,
    pretty_exceptions_enable=False,
)


def _version(value: bool) -> None:
    if value:
        if STATE.agent:
            _print_json(agent.envelope("version", {"version": __version__}))
        else:
            out.print(f"bdbd {__version__}")
        raise typer.Exit()


EPILOG = (
    "[bold]Examples[/]\n\n"
    "  [cyan]bdbd[/]                                  where you stand\n"
    "  [cyan]bdbd balance 3200[/]                     tell it what's in your account\n"
    "  [cyan]bdbd add Netflix 15.49 monthly on the 12th[/]\n"
    "  [cyan]bdbd cal[/]                              this month, day by day\n"
    "  [cyan]bdbd project --until eoy[/]              the balance through December\n"
    "  [cyan]bdbd plan --extra 300[/]                 how fast the debts could go\n\n"
    "Reads the budget at [bold]--db[/], else [bold]$BDBD_DB[/], else "
    "[bold]~/.config/bdbd/budget.sqlite[/]. For LLM agents: [bold]--agent[/] (JSON), "
    "then [bold]bdbd --agent guide[/]."
)


@app.callback(invoke_without_command=True, epilog=EPILOG)
def main_callback(
    ctx: typer.Context,
    db_: Annotated[
        str | None,
        typer.Option("--db", metavar="PATH", help="The budget file to use.", show_default=False),
    ] = None,
    agent_: Annotated[
        bool, typer.Option("--agent", help="Print one JSON envelope for LLM agents and scripts.")
    ] = False,
    select_: Annotated[
        str | None,
        typer.Option(
            "--select",
            metavar="PATHS",
            help="Agent mode: only these comma-separated paths of data.",
            show_default=False,
        ),
    ] = None,
    no_tidy: Annotated[
        bool, typer.Option("--no-tidy", help="Skip the automatic tidy-up of past months.")
    ] = False,
    version: Annotated[
        bool | None,
        typer.Option("--version", "-V", callback=_version, is_eager=True, help="Show version."),
    ] = None,
) -> None:
    """A beautiful budget in your terminal: incomes, bills and debts, projected day by day.

    With no command, [bold]bdbd[/] shows where you stand: your balance, the money that's
    spare until payday, what's coming up and how your debts are going.
    """
    if db_:
        STATE.db = db_
    if agent_:
        STATE.agent = True
    if select_:
        STATE.select, STATE.agent = select_, True
    if no_tidy:
        STATE.tidy = False
    if ctx.invoked_subcommand is None:
        with run("overview") as c:
            if c.agent:
                c.emit(*dashboard.agent_data(c.budget))
            else:
                dashboard.render(out, c.budget)


# ── Look ──────────────────────────────────────────────────────────────────────


@app.command("balance", rich_help_panel=LOOK)
def balance_cmd(
    amount: Annotated[
        str | None,
        typer.Argument(
            help="What's in your account, e.g. 3200. Leave it out to see the balance bdbd has.",
            show_default=False,
        ),
    ] = None,
    on: Annotated[
        str | None,
        typer.Option(
            "--on",
            metavar="DATE",
            help="The day the balance is from (default today).",
            show_default=False,
        ),
    ] = None,
    posted: Annotated[
        bool | None,
        typer.Option(
            "--posted/--pending",
            help="Whether that day's scheduled items are already in AMOUNT "
            "(asked when it matters; --pending otherwise).",
            show_default=False,
        ),
    ] = None,
    forget: Annotated[
        bool, typer.Option("--forget", help="Forget every recorded balance.")
    ] = False,
) -> None:
    """Record your balance, or see the one bdbd has.

    bdbd remembers the balance you give it and carries it forward day by day, so every
    projection starts from it. Update it whenever you check your bank.
    """
    with run("balance") as c:
        b = c.budget
        if forget:
            n = balances.forget(b.conn)
            if c.agent:
                c.emit({"forgot": n})
            else:
                success(out, f"Forgot {n} recorded balance{'s' if n != 1 else ''}.")
            return
        if amount is None:
            start = b.start(c.today)
            rec = b.recorded()
            since = []
            if rec is not None and rec.as_of < c.today:
                w = ask.window(b, c.today - timedelta(days=1), as_of=rec.as_of)
                since = w.items
            if c.agent:
                c.emit(
                    {
                        "today": agent.start_json(start),
                        "recorded": agent.balance_json(rec),
                        "since_recorded": [agent.ledger_json(e) for e in since],
                        "history": [agent.balance_json(h) for h in balances.history(b.conn)],
                    }
                )
            else:
                balance_view.render_show(out, b, start, since)
            return
        cents = _money(amount, "the balance", negative=True)
        assert cents is not None
        day = c.day(on, prefer="past")
        items = b.items_on(day)
        warnings = []
        if posted is None and items:
            if interactive():
                posted = balance_view.ask_posted(out, b, day, items)
            else:
                posted = False
                listed = ", ".join(f"{e.name} {cents_to_str(e.delta_cents)}" for e in items)
                warnings.append(
                    f"{day.isoformat()} has scheduled items ({listed}); assumed they are not in "
                    "the balance yet. Pass --posted if they are."
                )
        rec = b.record_balance(cents, day, posted=bool(posted))
        if c.agent:
            c.emit(agent.recorded_json(rec), warnings)
        else:
            balance_view.render_recorded(out, b, rec)


@app.command("upcoming", rich_help_panel=LOOK)
def upcoming_cmd(
    until: UntilOpt = None,
    days: Annotated[
        int | None,
        typer.Option(
            "--days",
            "-d",
            metavar="N",
            help="How many days ahead (default 30).",
            show_default=False,
        ),
    ] = None,
    balance: BalanceOpt = None,
    weekly_spend: WeeklyOpt = None,
) -> None:
    """Everything coming in and going out, with your balance after each."""
    with run("upcoming") as c:
        b = c.budget
        end = c.day(until) if until else c.today + timedelta(days=days or 30)
        if end < c.today:
            raise Problem("--until is in the past", "usage")
        w = ask.window(
            b,
            end,
            given=_money(balance, "--balance", negative=True),
            weekly=_money(weekly_spend, "--weekly-spend"),
        )
        if c.agent:
            c.emit(
                {
                    "from": c.today.isoformat(),
                    "until": end.isoformat(),
                    "opening": agent.start_json(w.start),
                    "weekly_spend": cents_to_str(w.weekly),
                    "items": [agent.ledger_json(e) for e in w.items],
                    "lifestyle_total": cents_to_str(w.lifestyle_total),
                    "ending_balance": cents_to_str(w.run.ending_balance_cents),
                },
                w.run.warnings,
            )
        else:
            forecast.render_upcoming(out, b, w, end)


@app.command("cal", rich_help_panel=LOOK)
def cal_cmd(
    month: Annotated[
        str | None,
        typer.Argument(
            help="Which month: oct, 2026-11, next, +2 (default this month).", show_default=False
        ),
    ] = None,
    balance: BalanceOpt = None,
) -> None:
    """A month at a glance: what lands on each day and the balance at the end of it."""
    with run("cal") as c:
        first = ask.month_of(month, c.today)
        days, start = ask.calendar(
            c.budget, first, given=_money(balance, "--balance", negative=True)
        )
        if c.agent:
            c.emit(
                {
                    "month": first.strftime("%Y-%m"),
                    "opening": agent.start_json(start),
                    "days": [
                        {
                            "date": d.date.isoformat(),
                            "items": [
                                {"name": n, "amount": cents_to_str(a), "kind": k}
                                for n, a, k in d.items
                            ],
                            "net": cents_to_str(d.net),
                            "balance": cents_to_str(d.balance) if d.balance is not None else None,
                        }
                        for d in days
                    ],
                }
            )
        else:
            forecast.render_calendar(out, c.budget, first, days, start)


@app.command("ls", rich_help_panel=LOOK)
def ls_cmd(
    income: Annotated[bool, typer.Option("--income", help="Only money coming in.")] = False,
    expenses: Annotated[bool, typer.Option("--expenses", help="Only money going out.")] = False,
    tag: Annotated[
        str | None,
        typer.Option(
            "--tag", "-t", metavar="TAG", help="Only flows with this tag.", show_default=False
        ),
    ] = None,
    all_: Annotated[bool, typer.Option("--all", "-a", help="Include paused flows.")] = False,
) -> None:
    """Every income and expense, with its schedule, next date and monthly cost."""
    with run("ls") as c:
        kind = Kind.INCOME if income and not expenses else Kind.EXPENSE if expenses else None
        lst = ask.listing(c.budget, kind=kind, tag=tag, include_inactive=all_)
        if c.agent:
            rows = []
            for f in lst.flows:
                row = agent.flow_json(f, next_date=lst.next[f.id])
                row["monthly"] = cents_to_str(lst.monthly[f.id])
                rows.append(row)
            c.emit({"flows": rows, "count": len(rows)})
        else:
            flows_view.render_list(out, c.budget, lst, tag=tag)


@app.command("show", rich_help_panel=LOOK)
def show_cmd(
    flow: Annotated[str, typer.Argument(help="The flow's name (or id).", show_default=False)],
) -> None:
    """Everything about one flow: schedule, next dates and, for a debt, where it's heading."""
    with run("show") as c:
        f = c.budget.find(flow)
        info = ask.details(c.budget, f)
        if c.agent:
            data = agent.flow_json(f, next_date=info.upcoming[0] if info.upcoming else None)
            data["upcoming"] = [d.isoformat() for d in info.upcoming]
            data["monthly"] = cents_to_str(info.monthly)
            if info.debt:
                d = info.debt
                data["debt_outlook"] = {
                    k: d[k]
                    for k in (
                        "balance_at_as_of",
                        "payoff_date",
                        "payments_remaining",
                        "total_paid_remaining",
                        "total_interest_remaining",
                    )
                }
            c.emit(data)
        else:
            flows_view.render_card(out, c.budget, info)


tags_app = typer.Typer(help="Your tags and the flows carrying each.", no_args_is_help=False)
app.add_typer(tags_app, name="tags", rich_help_panel=LOOK)


@tags_app.callback(invoke_without_command=True)
def tags_cmd(ctx: typer.Context) -> None:
    """Your tags and the flows carrying each (rename or remove them with the subcommands)."""
    if ctx.invoked_subcommand is not None:
        return
    with run("tags") as c:
        rows = flows_view.tag_rows(c.budget)
        if c.agent:
            c.emit({"tags": rows})
        else:
            flows_view.render_tags(out, c.budget, rows)


@tags_app.command("rename")
def tags_rename(old: str, new: str) -> None:
    """Rename a tag everywhere."""
    with run("tags rename") as c:
        repo.rename_tag(c.budget.conn, old, new)
        data = {"from": repo.normalize_tag(old), "to": repo.normalize_tag(new)}
        if c.agent:
            c.emit({"renamed": data})
        else:
            success(out, f"Renamed tag [bold]{data['from']}[/] to [bold]{data['to']}[/].")


@tags_app.command("rm")
def tags_rm(tag: str) -> None:
    """Take a tag off every flow."""
    with run("tags rm") as c:
        repo.remove_tag(c.budget.conn, tag)
        if c.agent:
            c.emit({"removed": repo.normalize_tag(tag)})
        else:
            success(out, f"Removed tag [bold]{repo.normalize_tag(tag)}[/] from every flow.")


# ── Change ────────────────────────────────────────────────────────────────────

TagOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--tag", "-t", metavar="TAG", help="A tag (repeat it, or use commas).", show_default=False
    ),
]


def _tags(values: list[str] | None) -> list[str]:
    return [t.strip() for v in values or [] for t in v.split(",") if t.strip()]


def _weekend(ach: bool, weekend: str | None) -> Weekend | None:
    if ach:
        return Weekend.NEXT
    if weekend is None:
        return None
    try:
        return Weekend(weekend)
    except ValueError as exc:
        raise Problem("--weekend must be none, next or previous", "usage") from exc


@app.command("add", rich_help_panel=CHANGE)
def add_cmd(
    name: Annotated[
        str | None, typer.Argument(help="What it is, e.g. Netflix.", show_default=False)
    ] = None,
    amount: Annotated[
        str | None, typer.Argument(help="How much each time, e.g. 15.49.", show_default=False)
    ] = None,
    when: Annotated[
        list[str] | None,
        typer.Argument(
            help="When, in words: monthly on the 12th, every 2 weeks on fri, once on oct 15.",
            show_default=False,
        ),
    ] = None,
    income: Annotated[bool, typer.Option("--income", "-i", help="Money coming in.")] = False,
    kind: Annotated[
        str | None,
        typer.Option(
            "--kind",
            metavar="KIND",
            help="income or expense (default expense).",
            show_default=False,
        ),
    ] = None,
    when_opt: Annotated[
        str | None,
        typer.Option(
            "--when",
            metavar="TEXT",
            help="The schedule, instead of trailing words.",
            show_default=False,
        ),
    ] = None,
    start: Annotated[
        str | None,
        typer.Option("--from", metavar="DATE", help="When it starts.", show_default=False),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option("--until", metavar="DATE", help="The last possible date.", show_default=False),
    ] = None,
    tag: TagOpt = None,
    ach: Annotated[
        bool, typer.Option("--ach", help="Pulled on business days: weekend dates move to Monday.")
    ] = False,
    weekend: Annotated[
        str | None,
        typer.Option(
            "--weekend",
            metavar="RULE",
            help="What happens on weekends: none, next (Monday) or previous (Friday).",
            show_default=False,
        ),
    ] = None,
    notes: Annotated[
        str | None, typer.Option("--notes", help="Anything to remember.", show_default=False)
    ] = None,
    paused: Annotated[bool, typer.Option("--paused", help="Add it paused.")] = False,
) -> None:
    """Add an income or expense.

    [bold]bdbd add Rent 1950 monthly on the 1st --ach[/]
    [bold]bdbd add Paycheck 2650 every 2 weeks on fri from sep 18 --income[/]
    [bold]bdbd add Dentist 240 once on oct 28[/]

    Leave things out at a terminal and bdbd asks for them.
    """
    with run("add") as c:
        b = c.budget
        text = " ".join(when or []) or when_opt
        if kind is not None and kind not in ("income", "expense"):
            raise Problem("--kind must be income or expense", "usage")
        is_income = income or kind == "income"
        tags = _tags(tag)
        wk = _weekend(ach, weekend)
        if interactive() and (name is None or amount is None or not text):
            answers = flows_view.ask_new_flow(out, b, name, amount, text, is_income, tags, wk)
            name, amount, text, is_income, tags, wk = answers
        _need(name, "a name", "add")
        _need(amount, "an amount", "add")
        _need(text, "a schedule (like 'monthly on the 1st')", "add")
        assert name is not None and text is not None
        phrase = text + (f" from {start}" if start else "") + (f" until {until}" if until else "")
        sched = parse_schedule(phrase, today=c.today)
        cents = _money(amount)
        assert cents is not None
        before = ask.monthly_net(b)
        f = repo.add_flow(
            b.conn,
            name=name,
            kind=Kind.INCOME if is_income else Kind.EXPENSE,
            amount_cents=cents,
            rrule=sched.rrule,
            dtstart=sched.dtstart,
            until=sched.until,
            tags=tags,
            notes=notes,
            active=not paused,
            weekend=wk or Weekend.NONE,
        )
        if c.agent:
            from bdbd.ui.common import next_date

            c.emit(agent.flow_json(f, next_date=next_date(f, c.today)))
        else:
            flows_view.render_added(out, b, f, before, ask.monthly_net(b))


@app.command("edit", rich_help_panel=CHANGE)
def edit_cmd(
    flow: Annotated[str, typer.Argument(help="The flow's name (or id).", show_default=False)],
    name: Annotated[
        str | None, typer.Option("--name", help="A new name.", show_default=False)
    ] = None,
    amount: Annotated[
        str | None, typer.Option("--amount", help="A new amount.", show_default=False)
    ] = None,
    when: Annotated[
        str | None,
        typer.Option(
            "--when", metavar="TEXT", help="A new schedule, in words.", show_default=False
        ),
    ] = None,
    start: Annotated[
        str | None,
        typer.Option("--from", metavar="DATE", help="A new start date.", show_default=False),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option("--until", metavar="DATE", help="A new last date.", show_default=False),
    ] = None,
    no_until: Annotated[bool, typer.Option("--no-until", help="Let it run forever.")] = False,
    income: Annotated[bool, typer.Option("--income", help="Make it money coming in.")] = False,
    expense: Annotated[bool, typer.Option("--expense", help="Make it money going out.")] = False,
    tag: TagOpt = None,
    untag: Annotated[
        list[str] | None,
        typer.Option("--untag", metavar="TAG", help="Remove a tag.", show_default=False),
    ] = None,
    tags: Annotated[
        str | None,
        typer.Option(
            "--tags", metavar="A,B", help="Replace all tags ('' clears them).", show_default=False
        ),
    ] = None,
    ach: Annotated[bool, typer.Option("--ach", help="Weekend dates move to Monday.")] = False,
    weekend: Annotated[
        str | None,
        typer.Option(
            "--weekend", metavar="RULE", help="none, next or previous.", show_default=False
        ),
    ] = None,
    notes: Annotated[
        str | None, typer.Option("--notes", help="New notes ('' clears them).", show_default=False)
    ] = None,
) -> None:
    """Change a flow: its name, amount, schedule, tags or weekend rule."""
    with run("edit") as c:
        b = c.budget
        before = b.find(flow)
        fid = int(before.id)
        kw: dict[str, Any] = {}
        if name is not None:
            kw["name"] = name
        if amount is not None:
            kw["amount_cents"] = _money(amount)
        if income and expense:
            raise Problem("pick one of --income and --expense", "usage")
        if income or expense:
            kw["kind"] = Kind.INCOME if income else Kind.EXPENSE
        if when is not None:
            phrase = when + (f" from {start}" if start else "")
            sched = parse_schedule(phrase, today=c.today)
            kw.update(rrule=sched.rrule, dtstart=sched.dtstart)
            if sched.until is not None:
                kw["until"] = sched.until
        elif start is not None:
            kw["dtstart"] = c.day(start, prefer="nearest")
        if until is not None:
            kw["until"] = c.day(until)
        if no_until:
            kw["until"] = None
        wk = _weekend(ach, weekend)
        if wk is not None:
            kw["weekend"] = wk
        if notes is not None:
            kw["notes"] = notes or None
        b.conn.execute("BEGIN")
        try:
            after = repo.update_flow(b.conn, fid, **kw)
            if tags is not None:
                repo.set_flow_tags(b.conn, fid, _tags([tags]))
            if tag:
                repo.add_flow_tags(b.conn, fid, _tags(tag))
            if untag:
                repo.remove_flow_tags(b.conn, fid, _tags(untag))
            repo.prune_unused_tags(b.conn)
            b.conn.execute("COMMIT")
        except Exception:
            b.conn.execute("ROLLBACK")
            raise
        after = repo.get_flow(b.conn, fid)
        if c.agent:
            from bdbd.ui.common import next_date

            c.emit(agent.flow_json(after, next_date=next_date(after, c.today)))
        else:
            flows_view.render_edited(out, b, before, after)


@app.command("rm", rich_help_panel=CHANGE)
def rm_cmd(
    flow: Annotated[str, typer.Argument(help="The flow's name (or id).", show_default=False)],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask first.")] = False,
) -> None:
    """Delete a flow (and its debt record, if it has one)."""
    with run("rm") as c:
        f = c.budget.find(flow)
        if interactive() and not yes and not flows_view.confirm_remove(out, f):
            out.print("[dim]Kept it.[/]")
            return
        repo.remove_flow(c.budget.conn, int(f.id))
        if c.agent:
            c.emit({"removed": agent.flow_json(f)})
        else:
            success(out, f"Removed [bold]{f.name}[/].")


def _set_active(command: str, flow: str, active: bool) -> None:
    with run(command) as c:
        f = c.budget.find(flow)
        after = repo.update_flow(c.budget.conn, int(f.id), active=active)
        if c.agent:
            c.emit(agent.flow_json(after))
        else:
            verb = "Resumed" if active else "Paused"
            success(out, f"{verb} [bold]{after.name}[/].")


@app.command("pause", rich_help_panel=CHANGE)
def pause_cmd(flow: Annotated[str, typer.Argument(help="The flow's name (or id).")]) -> None:
    """Pause a flow: it stays saved but leaves every projection."""
    _set_active("pause", flow, False)


@app.command("resume", rich_help_panel=CHANGE)
def resume_cmd(flow: Annotated[str, typer.Argument(help="The flow's name (or id).")]) -> None:
    """Bring a paused flow back."""
    _set_active("resume", flow, True)


# ── Debts ─────────────────────────────────────────────────────────────────────


def _debts_overview() -> None:
    with run("debts") as c:
        rows = ask.debts(c.budget)
        if c.agent:
            c.emit(debts_view.agent_data(rows))
        else:
            debts_view.render_overview(out, c.budget, rows)


@app.command("debts", rich_help_panel=DEBTS)
def debts_cmd() -> None:
    """Every debt: what's owed, the rate, and when it's paid off."""
    _debts_overview()


debt_app = typer.Typer(help="Loans and cards: set them up, record events, see schedules.")
app.add_typer(debt_app, name="debt", rich_help_panel=DEBTS)


@debt_app.callback(invoke_without_command=True)
def debt_callback(ctx: typer.Context) -> None:
    """Loans and cards: set them up, record events, see schedules. Alone, the same as `debts`."""
    if ctx.invoked_subcommand is None:
        _debts_overview()


@debt_app.command("show")
def debt_show(flow: Annotated[str, typer.Argument(help="The debt's name.")]) -> None:
    """A debt's terms, events and payoff outlook."""
    show_cmd(flow)


@debt_app.command("set")
def debt_set(
    flow: Annotated[str, typer.Argument(help="The expense that pays the debt, e.g. Car loan.")],
    balance: Annotated[
        str | None,
        typer.Option(
            "--balance",
            metavar="AMOUNT",
            help="What's owed, right after the payment due on --as-of.",
            show_default=False,
        ),
    ] = None,
    as_of: Annotated[
        str | None,
        typer.Option(
            "--as-of",
            "--balance-as-of",
            metavar="DATE",
            help="The date of that balance (default today).",
            show_default=False,
        ),
    ] = None,
    rate: Annotated[
        str | None,
        typer.Option(
            "--rate", metavar="RATE", help="Annual rate: 6.49% or 0.0649.", show_default=False
        ),
    ] = None,
    compounding: Annotated[
        str | None,
        typer.Option(
            "--compounding",
            metavar="HOW",
            help="simple (most car and student loans), daily (cards), monthly "
            "(mortgages) or continuous.",
            show_default=False,
        ),
    ] = None,
    day_count: Annotated[
        str, typer.Option("--day-count", metavar="BASIS", help="actual/365, actual/360 or 30/360.")
    ] = "actual/365",
    capitalize: Annotated[
        bool | None,
        typer.Option(
            "--capitalize/--no-capitalize",
            help="Whether unpaid interest joins the balance (default: yes, except simple).",
            show_default=False,
        ),
    ] = None,
    payment_mode: Annotated[
        str,
        typer.Option(
            "--payment-mode", metavar="MODE", help="fixed, interest_only or percent_of_balance."
        ),
    ] = "fixed",
    payment_pct: Annotated[
        str | None,
        typer.Option(
            "--payment-pct",
            metavar="PCT",
            help="With percent_of_balance: the minimum as a share of the balance.",
            show_default=False,
        ),
    ] = None,
    original_principal: Annotated[
        str | None,
        typer.Option(
            "--original-principal", metavar="AMOUNT", help="What was borrowed.", show_default=False
        ),
    ] = None,
    posting_day: Annotated[
        int | None,
        typer.Option(
            "--posting-day",
            metavar="1-31",
            help="Monthly compounding: the day interest posts.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Attach loan details to an expense: balance, rate and how interest works."""
    with run("debt set") as c:
        b = c.budget
        f = b.find(flow)
        if interactive() and (balance is None or rate is None or compounding is None):
            balance, rate, compounding = debts_view.ask_terms(out, f, balance, rate, compounding)
        _need(balance, "--balance", "debt set")
        _need(rate, "--rate", "debt set")
        _need(compounding, "--compounding", "debt set")
        try:
            comp = Compounding(compounding)
            dc = DayCount(day_count)
            mode = PaymentMode(payment_mode)
        except ValueError as exc:
            raise Problem(str(exc), "usage") from exc
        bal = _money(balance, "--balance")
        assert bal is not None
        repo.set_debt(
            b.conn,
            int(f.id),
            balance_cents=bal,
            balance_as_of=c.day(as_of, prefer="nearest") if as_of else c.today,
            annual_rate=parse_rate(str(rate)),
            compounding=comp,
            day_count=dc,
            capitalize_interest=capitalize,
            payment_mode=mode,
            payment_pct=parse_rate(payment_pct) if payment_pct else None,
            original_principal_cents=_money(original_principal, "--original-principal"),
            posting_day=posting_day,
        )
        after = repo.get_flow(b.conn, int(f.id))
        if c.agent:
            c.emit(agent.flow_json(after))
        else:
            debts_view.render_set(out, b, after)


@debt_app.command("unset")
def debt_unset(flow: Annotated[str, typer.Argument(help="The debt's name.")]) -> None:
    """Remove a debt record (the payment stays as a plain expense)."""
    with run("debt unset") as c:
        f = c.budget.find(flow)
        repo.unset_debt(c.budget.conn, int(f.id))
        if c.agent:
            c.emit(agent.flow_json(repo.get_flow(c.budget.conn, int(f.id))))
        else:
            success(out, f"[bold]{f.name}[/] is a plain expense again.")


def _event(
    command: str, flow: str, type_: EventType, on: str | None, *, rate=None, cents=None, notes=None
) -> None:
    with run(command) as c:
        f = c.budget.find(flow)
        ev = repo.add_event(
            c.budget.conn,
            int(f.id),
            type=type_,
            date=c.day(on, prefer="nearest") if on else c.today,
            rate=rate,
            amount_cents=cents,
            notes=notes,
        )
        if c.agent:
            c.emit(agent.event_json(ev))
        else:
            debts_view.render_event(out, c.budget, f, ev)


OnOpt = Annotated[
    str | None,
    typer.Option("--on", metavar="DATE", help="When (default today).", show_default=False),
]
NotesOpt = Annotated[str | None, typer.Option("--notes", show_default=False)]


@debt_app.command("extra")
def debt_extra(
    flow: Annotated[str, typer.Argument(help="The debt's name.")],
    amount: Annotated[str, typer.Argument(help="How much extra.")],
    on: OnOpt = None,
    notes: NotesOpt = None,
) -> None:
    """Record an extra payment (a what-if? use --extra-payment on project instead)."""
    _event("debt extra", flow, EventType.EXTRA_PAYMENT, on, cents=_money(amount), notes=notes)


@debt_app.command("rate")
def debt_rate(
    flow: Annotated[str, typer.Argument(help="The debt's name.")],
    rate: Annotated[str, typer.Argument(help="The new annual rate, e.g. 5.5%.")],
    on: OnOpt = None,
    notes: NotesOpt = None,
) -> None:
    """Record a new interest rate from a date."""
    _event("debt rate", flow, EventType.RATE_CHANGE, on, rate=parse_rate(rate), notes=notes)


@debt_app.command("payment")
def debt_payment(
    flow: Annotated[str, typer.Argument(help="The debt's name.")],
    amount: Annotated[str, typer.Argument(help="The new regular payment.")],
    on: OnOpt = None,
    notes: NotesOpt = None,
) -> None:
    """Record a new regular payment from a date."""
    _event("debt payment", flow, EventType.PAYMENT_CHANGE, on, cents=_money(amount), notes=notes)


@debt_app.command("adjust", context_settings={"ignore_unknown_options": True})
def debt_adjust(
    flow: Annotated[str, typer.Argument(help="The debt's name.")],
    amount: Annotated[str, typer.Argument(help="Add to the balance (use -250 to take off).")],
    on: OnOpt = None,
    notes: NotesOpt = None,
) -> None:
    """Record a balance correction (a fee, a refund, a rounding fix)."""
    cents = _money(amount, negative=True)
    _event("debt adjust", flow, EventType.BALANCE_ADJUSTMENT, on, cents=cents, notes=notes)


@debt_app.command("payoff")
def debt_payoff(
    flow: Annotated[str, typer.Argument(help="The debt's name.")],
    on: OnOpt = None,
    notes: NotesOpt = None,
) -> None:
    """Record paying the whole balance on a date."""
    _event("debt payoff", flow, EventType.PAYOFF, on, notes=notes)


@debt_app.command("drop")
def debt_drop(event_id: Annotated[int, typer.Argument(help="The event's id (see debt show).")]):
    """Delete a recorded debt event."""
    with run("debt drop") as c:
        repo.remove_event(c.budget.conn, event_id)
        if c.agent:
            c.emit({"removed": event_id})
        else:
            success(out, f"Deleted debt event {event_id}.")


@debt_app.command("schedule")
@what_ifs
def debt_schedule_cmd(
    flow: Annotated[str, typer.Argument(help="The debt's name.")],
    rows: Annotated[
        int | None,
        typer.Option(
            "--rows",
            "-n",
            metavar="N",
            help="Show at most N payments (default 12).",
            show_default=False,
        ),
    ] = None,
    all_rows: Annotated[bool, typer.Option("--all", "-a", help="Show every payment.")] = False,
    as_of: AsOfOpt = None,
    until: UntilOpt = None,
    months: Months600 = None,
    solve_payment: Annotated[
        int | None,
        typer.Option(
            "--solve-payment",
            metavar="MONTHS",
            help="Also work out the payment that clears it in MONTHS.",
            show_default=False,
        ),
    ] = None,
    verbose: VerboseOpt = False,
    what_if: WhatIf | None = None,
) -> None:
    """The amortization schedule: every payment split into interest and principal."""
    with run("debt schedule") as c:
        f = c.budget.find(flow)
        start, end = _window(c, as_of, until, months, 600)
        assert what_if is not None
        model = c.budget.model(
            what_if.spec(today=c.today, base=start), as_of=start, include_inactive=True
        )
        limit = None if all_rows else (rows or (None if c.agent else 12))
        data, warnings = debt_schedule(
            model,
            f.id,
            as_of=start,
            until=end,
            solve_payment_months=solve_payment,
            max_rows=limit,
        )
        _scenario_block(data, model, verbose)
        if c.agent:
            c.emit(data, warnings)
        else:
            full, _ = debt_schedule(model, f.id, as_of=start, until=end)
            debts_view.render_schedule(out, c.budget, data, full, warnings)


# ── Ask ───────────────────────────────────────────────────────────────────────


@app.command("project", rich_help_panel=ASK)
@what_ifs
def project_cmd(
    until: UntilOpt = None,
    months: Months12 = None,
    as_of: AsOfOpt = None,
    balance: BalanceOpt = None,
    weekly_spend: WeeklyOpt = None,
    daily: Annotated[
        bool, typer.Option("--daily", help="Agent mode: a row for every day.")
    ] = False,
    ledger: Annotated[
        bool, typer.Option("--ledger", help="Agent mode: include every transaction.")
    ] = False,
    verbose: VerboseOpt = False,
    what_if: WhatIf | None = None,
) -> None:
    """Your balance over the months ahead, with the spare money at the end and the low point."""
    with run("project") as c:
        b = c.budget
        start_day, end = _window(c, as_of, until, months, 12)
        assert what_if is not None
        model = b.model(what_if.spec(today=c.today, base=start_day), as_of=start_day)
        start = b.start(start_day, given=_money(balance, "--balance", negative=True))
        weekly = b.weekly_for(model, _money(weekly_spend, "--weekly-spend"))
        data, warnings = project_query(
            model,
            as_of=start_day,
            until=end,
            starting_balance_cents=start.cents,
            granularity="daily" if daily else "monthly",
            include_ledger=ledger,
            weekly_spend_cents=weekly,
            verbose=verbose,
        )
        data["opening"] = agent.start_json(start)
        _scenario_block(data, model, verbose)
        if not start.known:
            warnings.append("no balance recorded: this projection starts from 0")
        if c.agent:
            c.emit(data, warnings)
        else:
            run_ = engine.run(
                model,
                as_of=start_day,
                until=end,
                starting_balance_cents=start.cents,
                weekly_spend_cents=weekly,
            )
            forecast.render_project(out, b, data, run_, start, warnings)


@app.command("spend", rich_help_panel=ASK)
@what_ifs
def spend_cmd(
    terms: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="[TAG_OR_FLOW]...",
            help="Tags or flow names to add up (none = everything).",
            show_default=False,
        ),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude", "-x", metavar="TAG_OR_FLOW", help="Leave these out.", show_default=False
        ),
    ] = None,
    income: Annotated[bool, typer.Option("--income", help="Add up money coming in.")] = False,
    until: UntilOpt = None,
    months: Months1 = None,
    as_of: Annotated[
        str | None,
        typer.Option(
            "--from",
            "--as-of",
            metavar="DATE",
            help="First day (default today).",
            show_default=False,
        ),
    ] = None,
    weekly_spend: WeeklyOpt = None,
    verbose: VerboseOpt = False,
    what_if: WhatIf | None = None,
) -> None:
    """What you'll spend on something between two dates.

    [bold]bdbd spend car --until dec 12[/]
    """
    with run("spend") as c:
        b = c.budget
        start_day, end = _window(c, as_of, until, months, 1)
        assert what_if is not None
        model = b.model(what_if.spec(today=c.today, base=start_day), as_of=start_day)
        data, warnings = spend_query(
            model,
            as_of=start_day,
            until=end,
            terms=terms or [],
            exclude=exclude or [],
            income=income,
            known_tags=frozenset(t.name for t in repo.list_tags(b.conn)),
            weekly_spend_cents=b.weekly_for(model, _money(weekly_spend, "--weekly-spend")),
        )
        _scenario_block(data, model, verbose)
        if c.agent:
            c.emit(data, warnings)
        else:
            insights.render_spend(out, b, data, warnings)


@app.command("summary", rich_help_panel=ASK)
@what_ifs
def summary_cmd(
    tag: Annotated[
        str | None,
        typer.Option(
            "--tag", "-t", metavar="TAG", help="Only flows with this tag.", show_default=False
        ),
    ] = None,
    actual: Annotated[
        bool,
        typer.Option(
            "--actual",
            help="Average the real dates in the next --months instead of the steady-state rate.",
        ),
    ] = False,
    months: Annotated[
        int, typer.Option("--months", "-m", metavar="N", help="The window for --actual.")
    ] = 12,
    by: Annotated[str, typer.Option("--by", metavar="GROUP", help="tag, flow or both.")] = "both",
    as_of: AsOfOpt = None,
    all_: Annotated[bool, typer.Option("--all", "-a", help="Include paused flows.")] = False,
    verbose: VerboseOpt = False,
    what_if: WhatIf | None = None,
) -> None:
    """Where the money goes each month and each year, by tag and by flow."""
    with run("summary") as c:
        b = c.budget
        start_day = c.day(as_of, prefer="nearest") if as_of else c.today
        if months <= 0:
            raise Problem("--months must be positive", "usage")
        if by not in ("tag", "flow", "both"):
            raise Problem("--by must be tag, flow or both", "usage")
        assert what_if is not None
        model = b.model(
            what_if.spec(today=c.today, base=start_day), as_of=start_day, include_inactive=all_
        )
        data, warnings = summary_query(
            model,
            as_of=start_day,
            mode="actual" if actual else "steady",
            months=months,
            by=by,
            tag_filter=tag,
        )
        _scenario_block(data, model, verbose)
        data["weekly_spend"] = cents_to_str(b.weekly_for(model))
        if c.agent:
            c.emit(data, warnings)
        else:
            insights.render_summary(out, b, data, warnings)


def _compare(
    command: str,
    *,
    breakeven: bool,
    baseline,
    balance,
    until,
    months,
    as_of,
    weekly_spend,
    verbose,
    what_if: WhatIf,
) -> None:
    with run(command) as c:
        b = c.budget
        start_day, end = _window(c, as_of, until, months, 120 if breakeven else 24)
        spec = what_if.spec(today=c.today, base=start_day)
        if spec is None:
            raise Problem(
                "compare needs a what-if",
                "usage",
                hint='e.g. [bold]--payoff "Car loan@dec 1"[/] or [bold]--disable-tag car[/] '
                "(see [bold]bdbd compare --help[/]).",
            )
        base_spec = load_scenario(baseline, None) if baseline else None
        base_model = b.model(base_spec, as_of=start_day)
        scen_model = b.model(spec, as_of=start_day)
        start = b.start(start_day, given=_money(balance, "--balance", negative=True))
        labels = ((base_spec or {}).get("name") or "baseline", spec.get("name") or "what if")
        data, warnings = compare_query(
            base_model,
            scen_model,
            as_of=start_day,
            until=end,
            starting_balance_cents=0 if breakeven else start.cents,
            granularity="monthly",
            include_series=not breakeven,
            labels=labels,
            weekly_spend_cents=b.weekly_for(b.model(), _money(weekly_spend, "--weekly-spend")),
            verbose=verbose,
        )
        data["scenario"] = describe_scenario(spec) if verbose else spec.get("name")
        data["scenario_applied"] = scen_model.applied
        if base_model.applied:
            data["baseline_applied"] = base_model.applied
        if not breakeven:
            data["opening"] = agent.start_json(start)
        warnings = (
            [f"[baseline] {w}" for w in base_model.warnings]
            + [f"[what if] {w}" for w in scen_model.warnings]
            + warnings
        )
        if c.agent:
            c.emit(data, warnings)
        else:
            weekly = b.weekly_for(b.model(), _money(weekly_spend, "--weekly-spend"))
            runs = tuple(
                engine.run(m, as_of=start_day, until=end, weekly_spend_cents=weekly)
                for m in (base_model, scen_model)
            )
            insights.render_compare(
                out, b, data, warnings, breakeven=breakeven, runs=(runs[0], runs[1])
            )


BaselineOpt = Annotated[
    str | None,
    typer.Option(
        "--baseline",
        metavar="FILE",
        help="A scenario file for the other side (default: your budget as it is).",
        show_default=False,
    ),
]


@app.command("compare", rich_help_panel=ASK)
@what_ifs
def compare_cmd(
    baseline: BaselineOpt = None,
    balance: BalanceOpt = None,
    until: UntilOpt = None,
    months: Months24 = None,
    as_of: AsOfOpt = None,
    weekly_spend: WeeklyOpt = None,
    verbose: VerboseOpt = False,
    what_if: WhatIf | None = None,
) -> None:
    """Your budget as it is against a what-if, side by side, and when the what-if catches up."""
    assert what_if is not None
    _compare(
        "compare",
        breakeven=False,
        baseline=baseline,
        balance=balance,
        until=until,
        months=months,
        as_of=as_of,
        weekly_spend=weekly_spend,
        verbose=verbose,
        what_if=what_if,
    )


@app.command("breakeven", rich_help_panel=ASK)
@what_ifs
def breakeven_cmd(
    baseline: BaselineOpt = None,
    until: UntilOpt = None,
    months: Months120 = None,
    as_of: AsOfOpt = None,
    weekly_spend: WeeklyOpt = None,
    verbose: VerboseOpt = False,
    what_if: WhatIf | None = None,
) -> None:
    """When a what-if (like selling the car) catches up with your budget as it is."""
    assert what_if is not None
    _compare(
        "breakeven",
        breakeven=True,
        baseline=baseline,
        balance=None,
        until=until,
        months=months,
        as_of=as_of,
        weekly_spend=weekly_spend,
        verbose=verbose,
        what_if=what_if,
    )


@app.command("earliest", rich_help_panel=ASK)
@what_ifs
def earliest_cmd(
    floor: Annotated[
        str,
        typer.Option(
            "--floor",
            metavar="AMOUNT",
            help="The balance must never drop below this from the date on.",
        ),
    ],
    measure: Annotated[
        str,
        typer.Option(
            "--measure",
            metavar="WHAT",
            help="balance, or spare (balance minus bills due before payday).",
        ),
    ] = "balance",
    balance: BalanceOpt = None,
    until: UntilOpt = None,
    months: Months12 = None,
    as_of: AsOfOpt = None,
    first: Annotated[
        str | None,
        typer.Option(
            "--from",
            metavar="DATE",
            help="The first date to try (default today).",
            show_default=False,
        ),
    ] = None,
    before: Annotated[
        str | None,
        typer.Option(
            "--before",
            metavar="DATE",
            help="The last date to try (default --until).",
            show_default=False,
        ),
    ] = None,
    step: Annotated[int, typer.Option("--step", metavar="DAYS", help="Days between tries.")] = 1,
    weekdays: Annotated[
        bool, typer.Option("--weekdays", help="Only try Monday to Friday.")
    ] = False,
    weekly_spend: WeeklyOpt = None,
    verbose: VerboseOpt = False,
    what_if: WhatIf | None = None,
) -> None:
    """The first date a what-if dated '?' keeps your balance above a floor.

    [bold]bdbd earliest --floor 1000 --add-expense "Flight:550@?"[/]
    """
    with run("earliest") as c:
        b = c.budget
        start_day, end = _window(c, as_of, until, months, 12)
        assert what_if is not None
        spec = what_if.spec(today=c.today, base=start_day, allow_placeholder=True)
        start = b.start(start_day, given=_money(balance, "--balance", negative=True))
        floor_cents = _money(floor, "--floor", negative=True)
        assert floor_cents is not None
        data, warnings = earliest_query(
            b.flows(),
            spec,
            as_of=start_day,
            until=end,
            starting_balance_cents=start.cents,
            floor_cents=floor_cents,
            measure=measure,
            first=c.day(first, base=start_day) if first else None,
            last=c.day(before, base=start_day) if before else None,
            step=step,
            weekdays_only=weekdays,
            weekly_spend_cents=b.weekly_for(b.model(), _money(weekly_spend, "--weekly-spend")),
            verbose=verbose,
        )
        data["opening"] = agent.start_json(start)
        if not start.known:
            warnings.append("no balance recorded: the search starts from a balance of 0")
        if c.agent:
            c.emit(data, warnings)
        else:
            insights.render_earliest(out, b, data, warnings)


@app.command("plan", rich_help_panel=ASK)
@what_ifs
def plan_cmd(
    extra: Annotated[
        str, typer.Option("--extra", metavar="AMOUNT", help="Extra to put toward debt each month.")
    ],
    strategy: Annotated[
        str,
        typer.Option(
            "--strategy",
            metavar="HOW",
            help="avalanche (highest rate first) or snowball (smallest first).",
        ),
    ] = "avalanche",
    order: Annotated[
        str | None,
        typer.Option("--order", metavar="A,B,…", help="Your own payoff order.", show_default=False),
    ] = None,
    tag: Annotated[
        str | None,
        typer.Option(
            "--tag", "-t", metavar="TAG", help="Only debts with this tag.", show_default=False
        ),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude", "-x", metavar="DEBT", help="Leave a debt out.", show_default=False
        ),
    ] = None,
    start: Annotated[
        str | None,
        typer.Option(
            "--from",
            metavar="DATE",
            help="When the extra starts (default today).",
            show_default=False,
        ),
    ] = None,
    no_rollover: Annotated[
        bool,
        typer.Option("--no-rollover", help="Don't add a paid-off debt's payment to the extra."),
    ] = False,
    balance: BalanceOpt = None,
    until: UntilOpt = None,
    months: Months600 = None,
    as_of: AsOfOpt = None,
    weekly_spend: WeeklyOpt = None,
    verbose: VerboseOpt = False,
    what_if: WhatIf | None = None,
) -> None:
    """How fast your debts go with an extra amount each month, one debt at a time."""
    with run("plan") as c:
        b = c.budget
        start_day, end = _window(c, as_of, until, months, 600)
        assert what_if is not None
        model = b.model(what_if.spec(today=c.today, base=start_day), as_of=start_day)
        begin = b.start(start_day, given=_money(balance, "--balance", negative=True))
        extra_cents = _money(extra, "--extra")
        assert extra_cents is not None
        data, warnings = plan_query(
            model,
            as_of=start_day,
            until=end,
            extra_cents=extra_cents,
            start=c.day(start, base=start_day) if start else None,
            strategy="order" if order else strategy,
            order=[x for x in order.split(",") if x.strip()] if order else None,
            tag=tag,
            exclude=exclude,
            rollover=not no_rollover,
            starting_balance_cents=begin.cents if begin.known else None,
            weekly_spend_cents=b.weekly_for(model, _money(weekly_spend, "--weekly-spend")),
            verbose=verbose,
        )
        data["opening"] = agent.start_json(begin)
        if c.agent:
            c.emit(data, warnings)
        else:
            insights.render_plan(out, b, data, warnings)


# ── Data ──────────────────────────────────────────────────────────────────────


@app.command("init", rich_help_panel=DATA)
def init_cmd(
    force: Annotated[
        bool, typer.Option("--force", help="Delete an existing budget and start over.")
    ] = False,
) -> None:
    """Create a new, empty budget file."""
    path = db_path()
    command = "init"
    try:
        if path.exists():
            if not force:
                raise Problem(
                    f"there's already a budget at {path}",
                    "db_exists",
                    hint="Pass [bold]--force[/] to start over (this deletes everything in it).",
                )
            path.unlink()
        db.connect(path, create=True).close()
    except Problem as exc:
        _fail(command, exc.code, exc.message, exc.hint)
    if STATE.agent:
        _print_json(agent.envelope(command, {"db": str(path), "schema_version": LATEST_VERSION}))
    else:
        success(out, f"Created a new budget at [bold]{path}[/].")
        from bdbd.ui.theme import hint

        out.print(hint("bdbd add Paycheck 2500 every 2 weeks on fri --income", "bdbd balance 1200"))


@app.command("config", rich_help_panel=DATA)
def config_cmd(
    key: Annotated[
        str | None, typer.Argument(help="A setting, e.g. weekly_spend.", show_default=False)
    ] = None,
    value: Annotated[str | None, typer.Argument(help="Its new value.", show_default=False)] = None,
    unset: Annotated[bool, typer.Option("--unset", help="Remove the setting.")] = False,
) -> None:
    """See or change settings. [bold]bdbd config weekly_spend 175[/] sets everyday spending."""
    with run("config") as c:
        conn = c.budget.conn
        key_ = key.replace("-", "_") if key else None
        if key_ and key_ not in repo.CONFIG_KEYS:
            raise Problem(
                f"there's no setting called {key!r}",
                "unknown_config",
                hint="Settings: " + ", ".join(f"[bold]{k}[/]" for k in repo.CONFIG_KEYS),
            )
        if key_ and unset:
            repo.config_unset(conn, key_)
        elif key_ and value is not None:
            if key_ == "weekly_spend":
                _money(value, "weekly_spend")
            repo.config_set(conn, key_, value)
        data = {"config": repo.config_all(conn), "keys": repo.CONFIG_KEYS}
        if c.agent:
            c.emit(
                data
                if not (key_ and value is None and not unset)
                else {"key": key_, "value": repo.config_get(conn, key_ or "")}
            )
        else:
            insights.render_config(out, data["config"], repo.CONFIG_KEYS)


@app.command("export", rich_help_panel=DATA)
def export_cmd(
    output: Annotated[
        str | None,
        typer.Option(
            "--output",
            "-o",
            metavar="FILE",
            help="Write to a file instead of stdout.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Back up the whole budget as JSON."""
    with run("export") as c:
        payload = repo.export_all(c.budget.conn, db.current_version(c.budget.conn))
        payload["balances"] = [
            {"as_of": h.as_of.isoformat(), "balance": cents_to_str(h.amount_cents)}
            for h in balances.history(c.budget.conn)
        ]
        payload["exported_at"] = c.today.isoformat()
        if output:
            Path(output).expanduser().write_text(json.dumps(payload, indent=2) + "\n")
            if c.agent:
                c.emit({"written": output, "flows": len(payload["flows"])})
            else:
                success(out, f"Wrote {len(payload['flows'])} flows to [bold]{output}[/].")
        elif c.agent:
            c.emit(payload)
        else:
            sys.stdout.write(json.dumps(payload, indent=2) + "\n")


@app.command("import", rich_help_panel=DATA)
def import_cmd(
    file: Annotated[str, typer.Argument(help="A backup made by bdbd export.")],
    replace: Annotated[
        bool, typer.Option("--replace", help="Replace everything in the budget with it.")
    ] = False,
) -> None:
    """Restore a backup made with [bold]bdbd export[/]."""
    with run("import") as c:
        p = Path(file).expanduser()
        if not p.exists():
            raise Problem(f"there's no file at {file}", "file_not_found")
        try:
            payload = json.loads(p.read_text())
        except json.JSONDecodeError as exc:
            raise Problem(f"{file} isn't valid JSON: {exc}", "invalid_import") from exc
        n = repo.import_all(c.budget.conn, payload, replace=replace)
        if payload.get("balances"):
            balances.replace_all(
                c.budget.conn,
                [
                    (
                        date.fromisoformat(x["as_of"]),
                        parse_amount(x["balance"], allow_negative=True),
                    )
                    for x in payload["balances"]
                ],
            )
        if c.agent:
            c.emit({"imported_flows": n})
        else:
            success(out, f"Imported {n} flows.")


@app.command("tidy", rich_help_panel=DATA)
def tidy_cmd() -> None:
    """Clear out past months now (bdbd also does this before every command)."""
    with run("tidy", tidy=False) as c:
        actions = c.budget.tidy()
        if c.agent:
            c.emit({"month": c.today.replace(day=1).isoformat(), "actions": actions})
        elif actions:
            for a in actions:
                success(out, a[0].upper() + a[1:])
        else:
            success(out, "Nothing from past months to tidy up.")


@app.command("sql", rich_help_panel=DATA)
def sql_cmd(
    query: Annotated[str, typer.Argument(help="A read-only query: SELECT, WITH, PRAGMA …")],
    limit: Annotated[int, typer.Option("--limit", metavar="N", help="At most N rows.")] = 1000,
) -> None:
    """Look at the raw tables with a read-only SQL query."""
    with run("sql", tidy=False) as c:
        q = query.strip()
        first_word = q.split(None, 1)[0].upper().rstrip("(") if q else ""
        if first_word not in {"SELECT", "WITH", "EXPLAIN", "PRAGMA"}:
            raise Problem(
                "only read-only queries are allowed (SELECT, WITH, EXPLAIN, PRAGMA)", "readonly_sql"
            )
        conn = db.readonly_connect(c.budget.path)
        try:
            cur = conn.execute(q)
            cols = [d[0] for d in cur.description] if cur.description else []
            fetched = cur.fetchmany(limit + 1)
        except sqlite3.Error as exc:
            raise Problem(f"sql error: {exc}", "sql_error") from exc
        finally:
            conn.close()
        rows = [dict(zip(cols, r, strict=True)) for r in fetched[:limit]]
        data = {
            "columns": cols,
            "rows": rows,
            "row_count": len(rows),
            "truncated": len(fetched) > limit,
        }
        if c.agent:
            c.emit(data)
        else:
            insights.render_sql(out, data)


@app.command("guide", rich_help_panel=DATA)
def guide_cmd() -> None:
    """The full reference: every command, the agent-mode output, what-ifs and examples."""
    text = (resources.files("bdbd") / "guide.md").read_text()
    if STATE.agent:
        _print_json(agent.envelope("guide", {"guide": text, "format": "markdown"}))
    else:
        out.print(Markdown(text, code_theme="ansi_dark"))


# ── Entry point ───────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    STATE.__init__()  # fresh global flags each run (tests call main repeatedly)
    argv = _merge_words(_take_globals(list(sys.argv[1:] if argv is None else argv)))
    if os.environ.get("BDBD_AGENT", "").strip() not in ("", "0", "false", "no"):
        STATE.agent = True
    if not STATE.agent:
        app(args=argv, prog_name="bdbd")
        return
    command = next((a for a in argv if not a.startswith("-")), "overview")
    try:
        code = app(args=argv, prog_name="bdbd", standalone_mode=False)
    except typer.Exit as exc:
        code = exc.exit_code
    except typer.Abort:
        code = 130
    except typer.TyperException as exc:  # usage errors: unknown command, missing argument, …
        message = exc.format_message() if hasattr(exc, "format_message") else str(exc)
        _print_json(agent.failure(command, "usage", message))
        code = 2
    if isinstance(code, int) and code:
        sys.exit(code)
