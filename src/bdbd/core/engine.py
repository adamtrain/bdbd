"""Daily event-driven cash-flow simulation.

The engine knows nothing about sqlite: it takes an EffectiveModel (flows + debts + events)
and produces a SimResult with daily balances, a ledger, and per-debt amortization rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from bdbd.core.debt import DebtState
from bdbd.core.models import EffectiveFlow, EffectiveModel, EventType, FlowKey, Kind
from bdbd.core.money import cents_to_str, dec_to_str, q
from bdbd.core.recurrence import occurrences

# the order a day's events are applied in (the debt math needs it: a scheduled payment before an
# extra one or a payoff); they're listed in `same_day_order`
P_PARAM = 0  # rate_change / balance_adjustment / payment_change
P_INCOME = 2
P_DEBT_PAYMENT = 3
P_EXTRA = 4  # extra_payment / payoff / settle
P_EXPENSE = 5
LIFESTYLE_KEY = "lifestyle"


@dataclass(frozen=True)
class DebtSplit:
    interest_cents: int
    principal_cents: int
    balance_after: Decimal


@dataclass(frozen=True)
class LedgerEntry:
    date: date
    key: FlowKey
    name: str
    kind: str  # income | expense | debt_payment | extra_payment | payoff | settle | lifestyle
    amount_cents: int
    delta_cents: int
    balance_after_cents: int
    debt: DebtSplit | None = None

    def to_json(self) -> dict:
        d = {
            "date": self.date.isoformat(),
            "flow": self.key,
            "name": self.name,
            "kind": self.kind,
            "amount": cents_to_str(self.amount_cents),
            "delta": cents_to_str(self.delta_cents),
            "balance_after": cents_to_str(self.balance_after_cents),
        }
        if self.debt is not None:
            d["debt"] = {
                "interest": cents_to_str(self.debt.interest_cents),
                "principal": cents_to_str(self.debt.principal_cents),
                "balance_after": dec_to_str(self.debt.balance_after),
            }
        return d


@dataclass(frozen=True)
class DebtRow:
    n: int
    date: date
    payment_cents: int
    interest_cents: int
    principal_cents: int
    balance_after: Decimal
    kind: str  # scheduled | extra | payoff | settle
    before_as_of: bool
    interest_exact: Decimal = Decimal(0)

    def to_json(self) -> dict:
        return {
            "n": self.n,
            "date": self.date.isoformat(),
            "payment": cents_to_str(self.payment_cents),
            "interest": cents_to_str(self.interest_cents),
            "principal": cents_to_str(self.principal_cents),
            "balance": dec_to_str(self.balance_after),
            "kind": self.kind,
        }


@dataclass
class DebtSummary:
    key: FlowKey
    name: str
    balance_at_as_of: Decimal
    balance_at_until: Decimal
    interest_in_window: Decimal
    principal_paid_in_window: int
    interest_paid_in_window: int
    paid_off_on: date | None
    rows: list[DebtRow]

    def to_json(self) -> dict:
        return {
            "flow": self.key,
            "name": self.name,
            "balance_at_as_of": dec_to_str(self.balance_at_as_of),
            "balance_at_until": dec_to_str(self.balance_at_until),
            "interest_accrued_in_window": dec_to_str(self.interest_in_window),
            "interest_paid_in_window": cents_to_str(self.interest_paid_in_window),
            "principal_paid_in_window": cents_to_str(self.principal_paid_in_window),
            "paid_off_on": self.paid_off_on.isoformat() if self.paid_off_on else None,
        }


@dataclass
class FlowUsage:
    key: FlowKey
    name: str
    kind: Kind
    tags: frozenset[str]
    occurrences: int = 0
    total_cents: int = 0
    origin: str = "db"

    def to_json(self) -> dict:
        return {
            "flow": self.key,
            "name": self.name,
            "kind": str(self.kind),
            "tags": sorted(self.tags),
            "occurrences": self.occurrences,
            "total": cents_to_str(self.total_cents),
            "origin": self.origin,
        }


@dataclass
class SimResult:
    as_of: date
    until: date
    starting_balance_cents: int
    daily: list[tuple[date, int]] = field(default_factory=list)  # end-of-day cash
    ledger: list[LedgerEntry] = field(default_factory=list)
    debts: dict[FlowKey, DebtSummary] = field(default_factory=dict)
    flows_used: dict[FlowKey, FlowUsage] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    income_cents: int = 0
    expense_cents: int = 0  # all outflows incl. debt payments
    interest_paid_cents: int = 0
    principal_paid_cents: int = 0

    @property
    def ending_balance_cents(self) -> int:
        return self.daily[-1][1] if self.daily else self.starting_balance_cents


def same_day_order(delta_cents: int, name: str, key: object = "") -> tuple:
    """How one day's items are listed everywhere: money in before money out, each largest
    first, then by name."""
    return (delta_cents <= 0, -abs(delta_cents), name.casefold(), str(key))


def _list_in_day_order(ledger: list[LedgerEntry], start: int, opening: int) -> None:
    """Put the day's items from `start` on in listing order, each with the running balance that
    order gives (the day still ends on the same balance)."""
    todays = ledger[start:]
    if len(todays) < 2:
        return
    todays.sort(key=lambda e: same_day_order(e.delta_cents, e.name, e.key))
    running = opening
    for i, e in enumerate(todays):
        running += e.delta_cents
        ledger[start + i] = replace(e, balance_after_cents=running)


@dataclass(order=True)
class _Event:
    date: date
    priority: int
    seq: int
    kind: str = field(compare=False)
    flow: EffectiveFlow = field(compare=False)
    payload: Any = field(compare=False, default=None)  # the DebtEvent, for debt events


def run(
    model: EffectiveModel,
    *,
    as_of: date,
    until: date,
    starting_balance_cents: int = 0,
    stop_when_debts_paid: bool = False,
    weekly_spend_cents: int = 0,
) -> SimResult:
    """Simulate from as_of to until.

    weekly_spend_cents: variable lifestyle spending (groceries, incidentals) as a weekly amount,
    prorated over every day after as_of. Daily charges are rounded cumulatively so that the total
    over N days is exactly round(N x weekly / 7) to the cent.
    """
    if until < as_of:
        raise ValueError("until must not be before as_of")
    result = SimResult(as_of=as_of, until=until, starting_balance_cents=starting_balance_cents)
    result.warnings.extend(model.warnings)

    # debts and the simulation start (warm-up from the earliest balance date)
    states: dict[FlowKey, DebtState] = {}
    debt_flows: dict[FlowKey, EffectiveFlow] = {}
    sim_start = as_of
    for f in model.flows:
        if f.debt is not None:
            states[f.key] = DebtState(f.debt, f.name)
            debt_flows[f.key] = f
            sim_start = min(sim_start, f.debt.balance_as_of)

    # precompute every event once; never touch rrule inside the day loop
    events: list[_Event] = []
    seq = 0
    for f in model.flows:
        result.flows_used[f.key] = FlowUsage(f.key, f.name, f.kind, f.tags, origin=f.origin)
        if f.kind == Kind.INCOME:
            prio, kind = P_INCOME, "income"
        elif f.debt is not None:
            prio, kind = P_DEBT_PAYMENT, "debt_payment"
        else:
            prio, kind = P_EXPENSE, "expense"
        for d in occurrences(f.rrule, f.dtstart, f.until, sim_start, until, f.weekend):
            seq += 1
            events.append(_Event(d, prio, seq, kind, f))
        if f.debt is not None:
            for ev in f.debt.events:
                if ev.date < sim_start or ev.date > until:
                    continue
                seq += 1
                if ev.type in (EventType.EXTRA_PAYMENT, EventType.PAYOFF, EventType.SETTLE):
                    events.append(_Event(ev.date, P_EXTRA, seq, str(ev.type), f, ev))
                else:
                    events.append(_Event(ev.date, P_PARAM, seq, str(ev.type), f, ev))
    events.sort()

    balance = starting_balance_cents
    lifestyle_charged = 0
    lifestyle_daily = Decimal(weekly_spend_cents) / 7 if weekly_spend_cents else Decimal(0)
    lifestyle_name = f"Lifestyle spend ({cents_to_str(weekly_spend_cents)}/week)"
    if weekly_spend_cents:
        result.flows_used[LIFESTYLE_KEY] = FlowUsage(
            LIFESTYLE_KEY, lifestyle_name, Kind.EXPENSE, frozenset({"lifestyle"}), origin="param"
        )
    rows: dict[FlowKey, list[DebtRow]] = {k: [] for k in states}
    at_as_of: dict[FlowKey, tuple[Decimal, Decimal]] = {}  # owed, total_interest at start of as_of
    paid_in_window: dict[FlowKey, list[int]] = {k: [0, 0] for k in states}  # interest, principal
    dormant_payment: set[FlowKey] = set()

    def ledger(day: date, key, name, kind, amount, delta, split=None):
        nonlocal balance
        balance += delta
        result.ledger.append(LedgerEntry(day, key, name, kind, amount, delta, balance, split))
        if delta > 0:
            result.income_cents += delta
        else:
            result.expense_cents -= delta

    i = 0
    n_events = len(events)
    day = sim_start
    one = timedelta(days=1)
    while day <= until:
        in_window = day >= as_of
        if in_window and day == as_of:
            for k, st in states.items():
                at_as_of[k] = (st.owed(), st.total_interest)
        todays: list[_Event] = []
        while i < n_events and events[i].date == day:
            todays.append(events[i])
            i += 1

        # phase 0: parameter events
        for e in todays:
            if e.priority != P_PARAM:
                continue
            st = states[e.flow.key]
            ev = e.payload
            if ev.type == EventType.RATE_CHANGE:
                st.set_rate(ev.rate)
            elif ev.type == EventType.BALANCE_ADJUSTMENT:
                clamped = st.adjust_balance(ev.amount_cents)
                if clamped:
                    result.warnings.append(
                        f"debt {e.flow.name!r}: balance adjustment on {day} would go below zero; "
                        f"clamped to 0 (ignored {dec_to_str(clamped)})"
                    )
            elif ev.type == EventType.PAYMENT_CHANGE:
                st.set_scheduled_override(ev.amount_cents)

        # phase 1: interest
        for st in states.values():
            st.accrue(day)

        # phase 2..5: cash events (already sorted by priority)
        listed, opening = len(result.ledger), balance
        for e in todays:
            if e.priority == P_PARAM:
                continue
            f = e.flow
            usage = result.flows_used[f.key]
            if e.kind == "income":
                if in_window:
                    amt = f.amount_on(day)
                    usage.occurrences += 1
                    usage.total_cents += amt
                    ledger(day, f.key, f.name, "income", amt, amt)
            elif e.kind == "expense":
                if in_window:
                    amt = f.amount_on(day)
                    usage.occurrences += 1
                    usage.total_cents += amt
                    ledger(day, f.key, f.name, "expense", amt, -amt)
            elif e.kind == "debt_payment":
                st = states[f.key]
                if st.is_paid_off:
                    continue
                if day <= st.anchor:
                    # dormant: the stated balance already reflects this payment
                    if in_window:
                        dormant_payment.add(f.key)
                        amt = f.amount_on(day)
                        usage.occurrences += 1
                        usage.total_cents += amt
                        ledger(day, f.key, f.name, "debt_payment", amt, -amt)
                    continue
                amt = st.scheduled_payment_cents(f.amount_on(day))
                period_interest = int(q(st.interest_since_payment) * 100)
                if (
                    amt < period_interest
                    and not st.negative_amortization_warned
                    and st.owed_cents() > amt
                ):
                    st.negative_amortization_warned = True
                    result.warnings.append(
                        f"debt {f.name!r}: scheduled payment {cents_to_str(amt)} < period interest "
                        f"{cents_to_str(period_interest)} on {day}; balance grows "
                        "(negative amortization)"
                    )
                res = st.apply_payment(amt, day)
                rows[f.key].append(
                    DebtRow(
                        len(rows[f.key]) + 1,
                        day,
                        res.cash_cents,
                        res.interest_cents,
                        res.principal_cents,
                        res.balance_after,
                        "scheduled",
                        not in_window,
                        res.interest_exact,
                    )
                )
                if in_window:
                    usage.occurrences += 1
                    usage.total_cents += res.cash_cents
                    paid_in_window[f.key][0] += res.interest_cents
                    paid_in_window[f.key][1] += res.principal_cents
                    result.interest_paid_cents += res.interest_cents
                    result.principal_paid_cents += res.principal_cents
                    ledger(
                        day,
                        f.key,
                        f.name,
                        "debt_payment",
                        res.cash_cents,
                        -res.cash_cents,
                        DebtSplit(res.interest_cents, res.principal_cents, res.balance_after),
                    )
            else:  # extra_payment / payoff / settle
                st = states[f.key]
                ev = e.payload
                if st.is_paid_off:
                    continue
                if day < st.anchor:
                    result.warnings.append(
                        f"debt {f.name!r}: {ev.type} on {day} predates the balance date "
                        f"{st.anchor}; ignored (the stated balance already reflects it)"
                    )
                    continue
                if ev.type == EventType.EXTRA_PAYMENT:
                    amt, kind = ev.amount_cents, "extra"
                else:
                    amt, kind = st.owed_cents(), str(ev.type)
                res = st.apply_payment(amt, day)
                rows[f.key].append(
                    DebtRow(
                        len(rows[f.key]) + 1,
                        day,
                        res.cash_cents,
                        res.interest_cents,
                        res.principal_cents,
                        res.balance_after,
                        kind,
                        not in_window,
                        res.interest_exact,
                    )
                )
                if not in_window:
                    continue
                # settle: proceeds (a sale price) clear part of the debt; only the shortfall is
                # cash out, and any surplus is cash in.
                cash = res.cash_cents - (ev.amount_cents if kind == "settle" else 0)
                interest_c = min(max(cash, 0), res.interest_cents)
                principal_c = max(cash, 0) - interest_c
                paid_in_window[f.key][0] += interest_c
                paid_in_window[f.key][1] += principal_c
                result.interest_paid_cents += interest_c
                result.principal_paid_cents += principal_c
                ledger(
                    day,
                    f.key,
                    f.name,
                    {"extra": "extra_payment"}.get(kind, kind),
                    abs(cash),
                    -cash,
                    DebtSplit(res.interest_cents, res.principal_cents, res.balance_after),
                )

        _list_in_day_order(result.ledger, listed, opening)

        if in_window and weekly_spend_cents and day > as_of:
            due = int(
                (lifestyle_daily * (day - as_of).days).quantize(Decimal(1), rounding=ROUND_HALF_UP)
            )
            charge = due - lifestyle_charged
            lifestyle_charged = due
            if charge:
                u = result.flows_used[LIFESTYLE_KEY]
                u.occurrences += 1
                u.total_cents += charge
                ledger(day, LIFESTYLE_KEY, lifestyle_name, "lifestyle", charge, -charge)
        if in_window:
            result.daily.append((day, balance))
        if stop_when_debts_paid and states and all(s.is_paid_off for s in states.values()):
            if in_window:
                result.until = day
            break
        day += one

    for k, st in states.items():
        f = debt_flows[k]
        owed0, int0 = at_as_of.get(k, (st.owed(), st.total_interest))
        if k not in at_as_of and f.debt is not None and f.debt.balance_as_of > as_of:
            owed0 = Decimal(0)  # dormant at as_of: no balance known yet
        result.debts[k] = DebtSummary(
            key=k,
            name=f.name,
            balance_at_as_of=owed0,
            balance_at_until=st.owed(),
            interest_in_window=st.total_interest - int0,
            interest_paid_in_window=paid_in_window[k][0],
            principal_paid_in_window=paid_in_window[k][1],
            paid_off_on=st.paid_off_on,
            rows=rows[k],
        )
        if st.paid_off_on is not None and st.paid_off_on < as_of:
            result.warnings.append(
                f"debt {f.name!r} was paid off on {st.paid_off_on} (before as-of {as_of}); "
                "its payment flow generates no expenses"
            )
        if k in dormant_payment and f.debt is not None:
            result.warnings.append(
                f"debt {f.name!r}: balance date {f.debt.balance_as_of} is after as-of {as_of}; "
                "payments before it are treated as plain expenses and no interest accrues "
                "until then"
            )
    return result
