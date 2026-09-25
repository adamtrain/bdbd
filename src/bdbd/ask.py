"""Questions bdbd asks of a budget, shared by the human views and agent mode.

Each function returns plain data; the views draw it and agent mode serializes it, so both
always report the same numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from bdbd.budget import Budget, Start
from bdbd.core import engine
from bdbd.core.dates import add_months, month_end
from bdbd.core.models import EffectiveModel, Flow, FlowKey, Kind
from bdbd.core.queries.debt_schedule import debt_schedule
from bdbd.core.queries.summary import summary
from bdbd.core.recurrence import occurrences
from bdbd.ui.common import next_date, upcoming_dates

DEBT_HORIZON_DAYS = 365 * 60


def _cents(value: Decimal) -> int:
    return int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


# ── Flows ─────────────────────────────────────────────────────────────────────


@dataclass
class Listing:
    flows: list[Flow]
    next: dict[FlowKey, date | None]
    monthly: dict[FlowKey, int]  # steady-state monthly cost (0 for one-offs)


def listing(
    budget: Budget,
    *,
    kind: Kind | None = None,
    tag: str | None = None,
    include_inactive: bool = False,
) -> Listing:
    flows = budget.flows(include_inactive=include_inactive)
    if kind is not None:
        flows = [f for f in flows if f.kind == kind]
    if tag is not None:
        t = tag.strip().lower()
        flows = [f for f in flows if t in f.tags]
    model = budget.model(include_inactive=True)
    data, _ = summary(model, as_of=budget.today, by="flow")
    monthly = {row["flow"]: _cents(Decimal(row["monthly"])) for row in data["by_flow"]}
    return Listing(
        flows=flows,
        next={f.id: next_date(f, budget.today) for f in flows},
        monthly={f.id: monthly.get(f.id, 0) for f in flows},
    )


DAYS_PER_MONTH = Decimal("30.4375")


def everyday_monthly(weekly_cents: int) -> int:
    """Weekly everyday spending as a monthly amount (it's charged daily)."""
    return int((Decimal(weekly_cents) * DAYS_PER_MONTH / 7).quantize(1, rounding=ROUND_HALF_UP))


def monthly_net(budget: Budget) -> int:
    """What's left each month in the steady state: income minus bills and everyday spending."""
    data, _ = summary(budget.model(), as_of=budget.today, by="flow")
    return _cents(Decimal(data["net"]["net_monthly"])) - everyday_monthly(budget.weekly_spend())


@dataclass
class Details:
    flow: Flow
    upcoming: list[date]
    monthly: int
    debt: dict | None = None  # debt_schedule() output (as of today) for debts


def details(budget: Budget, flow: Flow, n: int = 6) -> Details:
    lst = listing(budget, include_inactive=True)
    debt = None
    if flow.debt is not None:
        model = budget.model(include_inactive=True)
        debt, _ = debt_schedule(
            model,
            flow.id,
            as_of=budget.today,
            until=budget.today + timedelta(days=DEBT_HORIZON_DAYS),
            max_rows=0,
        )
        debt["rows"] = []
    return Details(
        flow=flow,
        upcoming=upcoming_dates(flow, budget.today, n) if flow.active else [],
        monthly=lst.monthly.get(flow.id, 0),
        debt=debt,
    )


# ── Debts ─────────────────────────────────────────────────────────────────────


@dataclass
class DebtRow:
    key: FlowKey
    name: str
    balance: int  # owed today
    rate: Decimal
    payment: int
    paid_off_on: date | None
    interest: int  # interest still to pay until payoff
    tags: tuple[str, ...] = ()


def debts(budget: Budget, model: EffectiveModel | None = None) -> list[DebtRow]:
    """Balance today, rate, payment and payoff date for every active debt."""
    model = model or budget.model()
    flows = [f for f in model.flows if f.debt is not None]
    if not flows:
        return []
    today = budget.today
    res = engine.run(
        EffectiveModel(flows=flows),
        as_of=today,
        until=today + timedelta(days=DEBT_HORIZON_DAYS),
        stop_when_debts_paid=True,
    )
    rows = []
    for f in flows:
        s = res.debts[f.key]
        assert f.debt is not None
        rows.append(
            DebtRow(
                key=f.key,
                name=f.name,
                balance=_cents(s.balance_at_as_of),
                rate=f.debt.annual_rate,
                payment=f.amount_on(today),
                paid_off_on=s.paid_off_on,
                interest=_cents(s.interest_in_window),
                tags=tuple(sorted(f.tags)),
            )
        )
    rows.sort(key=lambda r: (r.paid_off_on or date.max, r.name))
    return rows


# ── Upcoming and calendar ─────────────────────────────────────────────────────


@dataclass
class Window:
    """A simulated stretch of days starting from a known (or unknown) balance."""

    start: Start
    run: engine.SimResult
    weekly: int

    @property
    def items(self) -> list[engine.LedgerEntry]:
        return [e for e in self.run.ledger if e.kind != "lifestyle"]

    @property
    def lifestyle_total(self) -> int:
        return sum(-e.delta_cents for e in self.run.ledger if e.kind == "lifestyle")


def window(
    budget: Budget,
    until: date,
    *,
    as_of: date | None = None,
    given: int | None = None,
    weekly: int | None = None,
    model: EffectiveModel | None = None,
) -> Window:
    as_of = as_of or budget.today
    start = budget.start(as_of, given=given)
    model = model or budget.model(as_of=as_of)
    weekly = budget.weekly_for(model, weekly)
    res = engine.run(
        model,
        as_of=as_of,
        until=until,
        starting_balance_cents=start.cents,
        weekly_spend_cents=weekly,
    )
    return Window(start, res, weekly)


@dataclass
class Day:
    date: date
    items: list[tuple[str, int, str]] = field(default_factory=list)  # name, signed cents, kind
    balance: int | None = None  # end of day, when projected

    @property
    def net(self) -> int:
        return sum(c for _, c, _ in self.items)


def month_of(text: str | None, today: date) -> date:
    """'oct', '2026-11', 'next', '+2', '-1' or None (this month) -> the 1st of that month."""
    from bdbd.core.errors import CashError
    from bdbd.words import parse_day

    first = today.replace(day=1)
    if not text:
        return first
    s = text.strip().lower()
    if s in ("next", "next month"):
        return add_months(first, 1)
    if s in ("last", "last month", "prev", "previous"):
        return add_months(first, -1)
    if s.lstrip("+-").isdigit() and s[0] in "+-":
        return add_months(first, int(s))
    try:
        y, m = s.split("-")
        return date(int(y), int(m), 1)
    except ValueError:
        pass
    try:
        return parse_day(s, today=today, prefer="future").replace(day=1)
    except CashError as exc:
        raise CashError(
            f"couldn't read {text!r} as a month; try oct, 2026-11, next or +2", "invalid_date"
        ) from exc


def calendar(budget: Budget, month: date, *, given: int | None = None) -> tuple[list[Day], Start]:
    """Every day of `month` with its scheduled items, and projected balances from today on."""
    today = budget.today
    first, last = month.replace(day=1), month_end(month)
    days = {first + timedelta(days=i): Day(first + timedelta(days=i)) for i in range(last.day)}
    # past days of the month: what was scheduled (no balances, the past isn't simulated)
    for f in budget.flows(include_inactive=False):
        end = min(last, today - timedelta(days=1))
        if end < first:
            break
        for d in occurrences(f.rrule, f.dtstart, f.until, first, end, f.weekend):
            sign = 1 if f.kind == Kind.INCOME else -1
            kind = "debt_payment" if f.debt is not None else str(f.kind)
            days[d].items.append((f.name, sign * f.amount_cents, kind))
    start = budget.start(today, given=given)
    if last >= today:
        w = window(budget, last, given=given)
        start = w.start
        for e in w.items:
            if e.date in days:
                days[e.date].items.append((e.name, e.delta_cents, e.kind))
        if start.known:
            for d, bal in w.run.daily:
                if d in days:
                    days[d].balance = bal
    return [days[d] for d in sorted(days)], start
