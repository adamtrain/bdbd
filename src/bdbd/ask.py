"""Questions bdbd asks of a budget, shared by the app and the JSON commands.

Each function returns plain data; the app draws it and the commands serialize it, so both
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
    next_year: int = 0  # what it brings in (+) or costs (-) in the next 12 months, on its dates
    debt: dict | None = None  # debt_schedule() output (as of today) for debts


def months_ahead(start: date, months: int) -> date:
    """The last day of the `months` months from `start`: the day before the same date then
    (so a yearly bill due today counts once in the next 12 months, not twice)."""
    return add_months(start, months) - timedelta(days=1)


def next_year(budget: Budget, flow: Flow, model: EffectiveModel | None = None) -> int:
    """What a flow brings in (+) or costs (-) in the next 12 months on its real dates, so an end
    date, a later start or a loan's payoff cuts it short (`bdbd spend NAME --months 12`).

    A paused flow counts as if it were running, like its other numbers.
    """
    model = model or budget.model(include_inactive=True)
    ef = next((f for f in model.flows if f.key == flow.id), None)
    if ef is None:
        return 0
    today = budget.today
    res = engine.run(EffectiveModel(flows=[ef]), as_of=today, until=months_ahead(today, 12))
    return sum(e.delta_cents for e in res.ledger if e.key == ef.key)


def details(budget: Budget, flow: Flow, n: int = 6, *, lst: Listing | None = None) -> Details:
    # `lst`: an up-to-date listing(budget, include_inactive=True) to reuse (the Budget view's)
    lst = lst or listing(budget, include_inactive=True)
    model = budget.model(include_inactive=True)
    debt = None
    if flow.debt is not None:
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
        next_year=next_year(budget, flow, model),
        debt=debt,
    )


# ── Debts ─────────────────────────────────────────────────────────────────────


@dataclass
class DebtRow:
    key: FlowKey
    name: str
    balance: int  # owed today
    rate: Decimal
    payment: int  # each payment
    paid_off_on: date | None  # None: never, at this payment (within DEBT_HORIZON_DAYS)
    interest: int | None  # interest still to pay from today until payoff; None if never paid off
    monthly: int = 0  # the payments as a steady monthly amount; 0 once it's paid off
    tags: tuple[str, ...] = ()

    def done(self, today: date) -> bool:
        """Paid off already (e.g. a payoff was recorded)."""
        return self.paid_off_on is not None and self.paid_off_on <= today


DEBT_HORIZON_YEARS = DEBT_HORIZON_DAYS // 365


def never_paid_warning(rows: list[DebtRow]) -> str | None:
    """A warning naming the debts that are never paid off at their current payment."""
    never = [r.name for r in rows if r.paid_off_on is None]
    if not never:
        return None
    from bdbd.words import join

    verb = "is" if len(never) == 1 else "are"
    return (
        f"{join(never)} {verb} never paid off at the current payment (not within "
        f"{DEBT_HORIZON_YEARS} years), so their interest isn't counted in interest_remaining"
    )


def debts(budget: Budget, model: EffectiveModel | None = None) -> list[DebtRow]:
    """Balance today, rate, payment and payoff date for every active debt."""
    from bdbd.core.recurrence import occurrences_per_year

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
        payment = f.amount_on(today)
        done = s.paid_off_on is not None and s.paid_off_on <= today
        per_year = occurrences_per_year(f.rrule, f.dtstart, f.until, today)
        rows.append(
            DebtRow(
                key=f.key,
                name=f.name,
                balance=_cents(s.balance_at_as_of),
                rate=f.debt.annual_rate,
                payment=payment,
                paid_off_on=s.paid_off_on,
                interest=_cents(s.interest_in_window) if s.paid_off_on else None,
                monthly=0 if done else _cents(Decimal(payment).scaleb(-2) * per_year / 12),
                tags=tuple(sorted(f.tags)),
            )
        )
    rows.sort(key=debt_order)
    return rows


def debt_order(row: DebtRow) -> tuple:
    """Debts are listed soonest paid off first (never last), then the most owed, then by name."""
    return (row.paid_off_on or date.max, -row.balance, row.name.casefold())


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


def calendar(
    budget: Budget,
    month: date,
    *,
    given: int | None = None,
    model: EffectiveModel | None = None,
) -> tuple[list[Day], Start]:
    """Every day of `month` with its scheduled items, and projected balances from today on.

    `model` (e.g. with a what-if applied) drives the days from today on; past days always show
    what the stored budget scheduled.
    """
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
        w = window(budget, last, given=given, model=model)
        start = w.start
        for e in w.items:
            if e.date in days:
                days[e.date].items.append((e.name, e.delta_cents, e.kind))
        if start.known:
            for d, bal in w.run.daily:
                if d in days:
                    days[d].balance = bal
    for day in days.values():  # past days come flow by flow; list them like the rest
        day.items.sort(key=lambda it: engine.same_day_order(it[1], it[0]))
    return [days[d] for d in sorted(days)], start


# ── The overview ──────────────────────────────────────────────────────────────

HORIZON_DAYS = 90
MIN_UPCOMING_DAYS = 10
MAX_UPCOMING_ROWS = 12


@dataclass
class Picture:
    """Where you stand, computed once: what the app's overview and `bdbd overview` show."""

    start: Start
    run: engine.SimResult
    spare: dict | None
    monthly_in: int
    monthly_bills: int
    monthly_everyday: int
    debts: list[DebtRow]
    warnings: list[str]
    horizon_days: int = HORIZON_DAYS

    @property
    def monthly_net(self) -> int:
        return self.monthly_in - self.monthly_bills - self.monthly_everyday

    @property
    def low_point(self) -> tuple[date, int] | None:
        """The lowest end-of-day balance in the horizon (the earliest, on a tie)."""
        if not self.start.known or not self.run.daily:
            return None
        return min(self.run.daily, key=lambda t: (t[1], t[0]))


def stale_warning(start: Start, today: date) -> str | None:
    from bdbd.words import relative

    rec = start.recorded
    if start.source == "carried" and rec is not None and (today - rec.as_of).days > 14:
        return (
            f"your balance was last recorded {relative(rec.as_of, today)}; "
            "update it with `bdbd balance AMOUNT` for sharper numbers"
        )
    return None


def overview(
    budget: Budget, model: EffectiveModel | None = None, horizon_days: int = HORIZON_DAYS
) -> Picture:
    from bdbd.core.queries.project import SpareCalculator

    today = budget.today
    start = budget.start(today)
    model = model or budget.model()
    weekly = budget.weekly_for(model)
    until = today + timedelta(days=horizon_days)
    res = engine.run(
        model,
        as_of=today,
        until=until,
        starting_balance_cents=start.cents,
        weekly_spend_cents=weekly,
    )
    spare = None
    if start.known and res.daily:
        spare = SpareCalculator(model, today, until, weekly).compute(today, res.daily[0][1])
    net, _ = summary(model, as_of=today, by="flow")
    warnings = list(dict.fromkeys([*model.warnings, *res.warnings]))
    if w := stale_warning(start, today):
        warnings.append(w)
    return Picture(
        start=start,
        run=res,
        spare=spare,
        monthly_in=_cents(Decimal(net["net"]["income_monthly"])),
        monthly_bills=_cents(Decimal(net["net"]["expense_monthly"])),
        monthly_everyday=everyday_monthly(weekly),
        debts=debts(budget, model),
        warnings=warnings,
        horizon_days=horizon_days,
    )


def coming_up(p: Picture, today: date) -> list[engine.LedgerEntry]:
    """Scheduled items until the next income (at least ten days ahead, at most a dozen)."""
    entries = [e for e in p.run.ledger if e.kind != "lifestyle"]
    income = [e for e in entries if e.delta_cents > 0 and e.date > today]  # not today's own
    stop = income[0].date if income else today + timedelta(days=MIN_UPCOMING_DAYS)
    stop = max(stop, today + timedelta(days=MIN_UPCOMING_DAYS))
    return [e for e in entries if e.date <= stop][:MAX_UPCOMING_ROWS]


# ── Pay cycles ────────────────────────────────────────────────────────────────

PAY_CYCLE_MONTHS = 12  # how far ahead the Paydays view looks
_NEXT_PAYDAY_DAYS = 93  # past the window: how far to look for the payday that ends a cycle
_LAST_PAYDAY_DAYS = 400  # before today: how far back to look for the current cycle's payday


@dataclass(frozen=True)
class CycleItem:
    """Money in (+) or out (-) on a day of a pay cycle."""

    date: date
    name: str
    cents: int
    kind: str  # income | expense | debt_payment | extra_payment | payoff | settle
    key: FlowKey | None = None
    payday: bool = False  # a paycheck: the money the cycle starts with


@dataclass(frozen=True)
class PayCycle:
    """From a payday to the day before the next one: what that paycheck has to cover.

    `left` is what's left of the paycheck, and any other money coming in during the cycle (a
    refund, in `money_in`), once the cycle's bills and its everyday spending allowance are
    paid: what can be spent or saved freely.
    """

    start: date
    end: date
    items: tuple[CycleItem, ...]  # by date, a day's items in listing order
    everyday: int  # the everyday spending allowance for its days (positive)
    open: bool = False  # no payday after it in the window: it's cut off at `end`

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def paychecks(self) -> list[CycleItem]:
        return [i for i in self.items if i.payday]

    @property
    def paycheck(self) -> int:
        return sum(i.cents for i in self.paychecks)

    @property
    def money_in(self) -> list[CycleItem]:
        return [i for i in self.items if i.cents > 0 and not i.payday]

    @property
    def bills(self) -> list[CycleItem]:
        return [i for i in self.items if i.cents < 0]

    @property
    def bills_total(self) -> int:
        return -sum(i.cents for i in self.bills)

    @property
    def money_in_total(self) -> int:
        return sum(i.cents for i in self.money_in)

    @property
    def left_before_everyday(self) -> int:
        return self.paycheck + self.money_in_total - self.bills_total

    @property
    def left(self) -> int:
        return self.left_before_everyday - self.everyday

    def holds(self, day: date) -> bool:
        return self.start <= day <= self.end


@dataclass(frozen=True)
class PayCycles:
    cycles: list[PayCycle]
    paydays: list[str]  # the incomes that start pay cycles
    weekly: int  # the everyday spending allowance a week that the cycles use


def everyday_for(weekly_cents: int, days: int) -> int:
    """The everyday spending allowance for a number of days (it's charged daily)."""
    return int((Decimal(weekly_cents) * days / 7).quantize(1, rounding=ROUND_HALF_UP))


def pay_cycles(
    budget: Budget,
    *,
    months: int = PAY_CYCLE_MONTHS,
    model: EffectiveModel | None = None,
    weekly: int | None = None,
) -> PayCycles:
    """Every pay cycle from the one under way through `months` ahead.

    A cycle runs from a payday (any date of an income flagged payday) to the day before the
    next. From today on it's the projection (`model`, so a what-if counts); the current
    cycle's days before today are what the stored budget scheduled, like the calendar's past.
    """
    today = budget.today
    model = model or budget.model()
    weekly = budget.weekly_for(model, weekly)
    horizon = add_months(today, months)
    res = engine.run(model, as_of=today, until=horizon + timedelta(days=_NEXT_PAYDAY_DAYS))
    paying = {f.key for f in model.flows if f.payday and f.kind == Kind.INCOME}
    ahead = [
        CycleItem(
            e.date, e.name, e.delta_cents, e.kind, e.key, e.key in paying and e.delta_cents > 0
        )
        for e in res.ledger
        if e.kind != "lifestyle"
    ]
    # the cycle under way started on the last payday before today: its days so far, as scheduled
    stored = budget.flows()
    yesterday = today - timedelta(days=1)
    last = max(
        (
            d
            for f in stored
            if f.payday and f.kind == Kind.INCOME
            for d in occurrences(
                f.rrule, f.dtstart, f.until, today - timedelta(days=_LAST_PAYDAY_DAYS), yesterday,
                f.weekend,
            )
        ),
        default=None,
    )  # fmt: skip
    past: list[CycleItem] = []
    if last is not None:
        for f in stored:
            sign = 1 if f.kind == Kind.INCOME else -1
            kind = "debt_payment" if f.debt is not None else str(f.kind)
            for d in occurrences(f.rrule, f.dtstart, f.until, last, yesterday, f.weekend):
                paid = f.payday and f.kind == Kind.INCOME
                past.append(CycleItem(d, f.name, sign * f.amount_cents, kind, f.id, paid))
        past.sort(key=lambda i: (i.date, *engine.same_day_order(i.cents, i.name, i.key)))
    everything = past + ahead
    starts = sorted({i.date for i in everything if i.payday})
    cycles: list[PayCycle] = []
    for n, start in enumerate(starts):
        if start > horizon:
            break
        nxt = starts[n + 1] if n + 1 < len(starts) else None
        end = nxt - timedelta(days=1) if nxt else horizon
        inside = tuple(i for i in everything if start <= i.date <= end)
        days = (end - start).days + 1
        cycles.append(PayCycle(start, end, inside, everyday_for(weekly, days), open=nxt is None))
    names = sorted((f.name for f in model.flows if f.key in paying), key=str.casefold)
    return PayCycles(cycles, names, weekly)


# ── Tags ──────────────────────────────────────────────────────────────────────


@dataclass
class TagRow:
    name: str
    flows: int
    flow_names: list[str]
    expense_monthly: int
    income_monthly: int


def tag_rows(budget: Budget, model: EffectiveModel | None = None) -> list[TagRow]:
    """Every tag with the flows carrying it and its steady-state monthly money."""
    from bdbd.core import repo

    names: dict[str, list[str]] = {}
    for f in budget.flows():
        for t in f.tags:
            names.setdefault(t, []).append(f.name)
    data, _ = summary(model or budget.model(), as_of=budget.today, by="tag")
    monthly = {r["tag"]: r for r in data["by_tag"]}
    rows = []
    for t in repo.list_tags(budget.conn):
        m = monthly.get(t.name)
        rows.append(
            TagRow(
                name=t.name,
                flows=t.flow_count,
                flow_names=names.get(t.name, []),
                expense_monthly=_cents(Decimal(m["expense_monthly"])) if m else 0,
                income_monthly=_cents(Decimal(m["income_monthly"])) if m else 0,
            )
        )
    return rows
