"""Debt payoff plans: a debt payoff plan — an extra amount per month, one debt at a time.

The extra (plus, with rollover, every scheduled payment freed by a payoff) goes to one target debt
at a time as a raised scheduled payment. When that debt is paid off, whatever was left of that
month's money is paid on the next target the same day and the pool moves on. The plan is built by
re-running the engine once per target with `payment_change` / `extra_payment` events, so all the
interest math is the engine's own; the generated events are returned as a scenario fragment that
`project` and friends accept via --scenario-json.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from bdbd.core import engine
from bdbd.core.dates import add_months, months_between
from bdbd.core.errors import CashError
from bdbd.core.models import (
    Debt,
    DebtEvent,
    EffectiveFlow,
    EffectiveModel,
    EventType,
    FlowKey,
    PaymentMode,
)
from bdbd.core.money import cents_to_str, dec_to_str, rate_to_str
from bdbd.core.queries.project import project
from bdbd.core.recurrence import occurrences_per_year

STRATEGIES = ("avalanche", "snowball", "order")


def _with_events(model: EffectiveModel, extra: dict[FlowKey, list[DebtEvent]]) -> EffectiveModel:
    flows = []
    for f in model.flows:
        if f.debt is not None and extra.get(f.key):
            debt = replace(f.debt, events=tuple(list(f.debt.events) + extra[f.key]))
            f = replace(f, debt=debt)
        flows.append(f)
    return EffectiveModel(
        flows=flows,
        applied=list(model.applied),
        warnings=list(model.warnings),
        scenario=model.scenario,
        weekly_spend_cents=model.weekly_spend_cents,
    )


def _debt(f: EffectiveFlow) -> Debt:
    assert f.debt is not None
    return f.debt


def _event(key: FlowKey, type_: EventType, day: date, cents: int) -> DebtEvent:
    return DebtEvent(id=None, flow_id=key, date=day, type=type_, amount_cents=cents, notes="plan")


def plan(
    model: EffectiveModel,
    *,
    as_of: date,
    until: date,
    extra_cents: int,
    start: date | None = None,
    strategy: str = "avalanche",
    order: list[str] | None = None,
    tag: str | None = None,
    exclude: list[str] | None = None,
    rollover: bool = True,
    starting_balance_cents: int | None = None,
    weekly_spend_cents: int = 0,
    verbose: bool = False,
) -> tuple[dict, list[str]]:
    if extra_cents < 0:
        raise CashError("--extra must not be negative", "usage")
    if strategy not in STRATEGIES:
        raise CashError(f"--strategy must be one of {STRATEGIES}", "usage")
    start = start or as_of
    if not (as_of <= start <= until):
        raise CashError(f"--from {start} must lie within the horizon {as_of}..{until}", "usage")
    warnings: list[str] = list(model.warnings)

    debts: list[EffectiveFlow] = [f for f in model.flows if f.debt is not None]
    if tag:
        t = tag.strip().lower()
        debts = [f for f in debts if t in f.tags]
    excl = {x.strip().lower() for x in exclude or []}
    debts = [f for f in debts if f.name.lower() not in excl and str(f.key) not in excl]
    if not debts:
        raise CashError("no debts to plan for (check --tag / --exclude / the scenario)", "usage")

    # baseline: everything as scheduled, no extra
    base = engine.run(model, as_of=as_of, until=until, stop_when_debts_paid=True)
    at_start = engine.run(model, as_of=as_of, until=start) if start > as_of else base
    warnings.extend(w for w in base.warnings if w not in warnings)

    targets: list[EffectiveFlow] = []
    for f in debts:
        b = base.debts[f.key]
        if b.paid_off_on is not None and b.paid_off_on <= start:
            warnings.append(
                f"debt {f.name!r} is already paid off on {b.paid_off_on} (before the plan starts); "
                "skipped"
            )
            continue
        if f.until is not None and f.until < start:
            warnings.append(f"debt {f.name!r}: its payment flow ends before {start}; skipped")
            continue
        if _debt(f).payment_mode != PaymentMode.FIXED:
            warnings.append(
                f"debt {f.name!r} has payment mode {_debt(f).payment_mode}; the plan's raised "
                "payment is applied as the base amount, which interest_only ignores"
            )
        targets.append(f)
    if not targets:
        raise CashError("every candidate debt is already paid off before the plan starts", "usage")

    def balance_at_start(f: EffectiveFlow) -> Decimal:
        d = at_start.debts[f.key]
        return d.balance_at_until if start > as_of else d.balance_at_as_of

    if order:
        by_name = {f.name.lower(): f for f in targets}
        by_key = {str(f.key): f for f in targets}
        seq: list[EffectiveFlow] = []
        for ref in order:
            f = by_name.get(ref.strip().lower()) or by_key.get(ref.strip())
            if f is None:
                raise CashError(f"--order: no plannable debt {ref!r}", "unknown_flow")
            if f not in seq:
                seq.append(f)
        rest = [f for f in targets if f not in seq]
        rest.sort(key=lambda f: (-_debt(f).annual_rate, -balance_at_start(f)))
        if rest:
            warnings.append(
                "debts not named in --order follow in avalanche order: "
                + ", ".join(f.name for f in rest)
            )
        targets = seq + rest
        strategy = "order"
    elif strategy == "avalanche":
        targets.sort(key=lambda f: (-_debt(f).annual_rate, -balance_at_start(f)))
    else:
        targets.sort(key=lambda f: (balance_at_start(f), -_debt(f).annual_rate))

    # build the plan one target at a time
    events: dict[FlowKey, list[DebtEvent]] = {}
    steps: list[dict] = []
    pool = extra_cents
    change_date = start
    carry_cents = 0
    carry_date: date | None = None
    result = base
    unfinished: EffectiveFlow | None = None
    for f in targets:
        already = result.debts[f.key].paid_off_on
        if already is not None and already < change_date:
            # paid off on its own schedule before its turn came: nothing to add, pool unchanged
            steps.append(
                {
                    "order": len(steps) + 1,
                    "name": f.name,
                    "flow": f.key,
                    "annual_rate": rate_to_str(_debt(f).annual_rate),
                    "balance_at_start": dec_to_str(balance_at_start(f)),
                    "scheduled_payment": cents_to_str(f.amount_on(start)),
                    "paid_off_on": already.isoformat(),
                    "months": months_between(start, already),
                    "interest_paid": cents_to_str(result.debts[f.key].interest_paid_in_window),
                    "baseline_paid_off_on": already.isoformat(),
                    "paid_off_before_turn": True,
                    "freed_to_pool": cents_to_str(f.amount_on(already) if rollover else 0),
                }
            )
            if rollover:
                pool += f.amount_on(already)
            continue
        scheduled = f.amount_on(change_date)
        override = scheduled + pool
        evs = [_event(f.key, EventType.PAYMENT_CHANGE, change_date, override)]
        if carry_cents > 0 and carry_date is not None:
            evs.append(_event(f.key, EventType.EXTRA_PAYMENT, carry_date, carry_cents))
        events.setdefault(f.key, []).extend(evs)
        result = engine.run(
            _with_events(model, events), as_of=as_of, until=until, stop_when_debts_paid=True
        )
        summary = result.debts[f.key]
        paid_off = summary.paid_off_on
        step = {
            "order": len(steps) + 1,
            "paid_off_before_turn": False,
            "name": f.name,
            "flow": f.key,
            "annual_rate": rate_to_str(_debt(f).annual_rate),
            "balance_at_start": dec_to_str(balance_at_start(f)),
            "scheduled_payment": cents_to_str(scheduled),
            "payment_during": cents_to_str(override),
            "from": change_date.isoformat(),
            "carry_in": cents_to_str(carry_cents),
            "paid_off_on": paid_off.isoformat() if paid_off else None,
            "months": months_between(start, paid_off) if paid_off else None,
            "interest_paid": cents_to_str(summary.interest_paid_in_window),
            "baseline_paid_off_on": (
                base_off.isoformat() if (base_off := base.debts[f.key].paid_off_on) else None
            ),
        }
        steps.append(step)
        if paid_off is None:
            unfinished = f
            break
        # what was planned for the payoff day but not needed rolls to the next target that day
        planned = paid = 0
        for r in summary.rows:
            if r.date != paid_off:
                continue
            paid += r.payment_cents
            if r.kind == "scheduled":
                planned += override if paid_off >= change_date else f.amount_on(paid_off)
            elif r.kind == "extra" and paid_off == carry_date:
                planned += carry_cents
            else:
                planned += r.payment_cents
        carry_cents = max(planned - paid, 0)
        carry_date = paid_off
        freed = f.amount_on(paid_off) if rollover else 0
        step["freed_to_pool"] = cents_to_str(freed)
        step["leftover_to_next"] = cents_to_str(carry_cents)
        pool += freed
        change_date = paid_off + timedelta(days=1)

    finished = [s for s in steps if s["paid_off_on"]]
    debt_free = max((date.fromisoformat(s["paid_off_on"]) for s in finished), default=None)
    if unfinished is not None:
        warnings.append(
            f"debt {unfinished.name!r} is not paid off by {until} even with the extra; extend "
            "--until/--months or raise --extra"
        )
    targets_keys = {f.key for f in targets}
    others_open = [
        d.name
        for k, d in result.debts.items()
        if k not in targets_keys and (d.paid_off_on is None or d.paid_off_on > (debt_free or until))
    ]
    if others_open:
        warnings.append(
            "debts outside the plan still open on the debt-free date: " + ", ".join(others_open)
        )

    def interest(res: engine.SimResult, keys) -> int:
        return sum(res.debts[k].interest_paid_in_window for k in keys)

    base_free = max(
        (off for f in targets if (off := base.debts[f.key].paid_off_on) is not None),
        default=None,
    )
    if any(base.debts[f.key].paid_off_on is None for f in targets):
        base_free = None
    plan_interest = interest(result, targets_keys)
    base_interest = interest(base, targets_keys)
    # the payments as a steady monthly amount (a fortnightly payment is ~2.17 of them)
    scheduled_total = sum(
        int(
            (
                Decimal(f.amount_on(start))
                * occurrences_per_year(f.rrule, f.dtstart, f.until, start)
                / 12
            ).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        )
        for f in targets
    )
    data: dict = {
        "as_of": as_of.isoformat(),
        "start": start.isoformat(),
        "until": until.isoformat(),
        "extra_monthly": cents_to_str(extra_cents),
        "strategy": strategy,
        "rollover": rollover,
        "status": "debt_free" if unfinished is None else "not_within_horizon",
        "debt_free_on": debt_free.isoformat() if debt_free and unfinished is None else None,
        "months": months_between(start, debt_free) if debt_free and unfinished is None else None,
        "monthly_outlay": {
            "scheduled_payments": cents_to_str(scheduled_total),
            "with_extra": cents_to_str(scheduled_total + extra_cents),
        },
        "total_balance_at_start": dec_to_str(
            sum((balance_at_start(f) for f in targets), start=Decimal(0))
        ),
        "interest_paid": cents_to_str(plan_interest),
        "baseline": {
            "debt_free_on": base_free.isoformat() if base_free else None,
            "months": months_between(start, base_free) if base_free else None,
            "interest_paid": cents_to_str(base_interest),
            "note": "same debts, scheduled payments only, within the same horizon",
        },
        "interest_saved": cents_to_str(base_interest - plan_interest),
        "steps": steps,
        "plan_scenario": {
            "debt_events": [
                {
                    "flow": next(f.name for f in model.flows if f.key == k),
                    "type": str(e.type),
                    "date": e.date.isoformat(),
                    "amount": cents_to_str(e.amount_cents or 0),
                }
                for k, evs in events.items()
                for e in evs
            ]
        },
        "cash_check": None,
    }
    if starting_balance_cents is not None:
        end = debt_free or until
        pdata, pwarn = project(
            _with_events(model, events),
            as_of=as_of,
            until=end,
            starting_balance_cents=starting_balance_cents,
            granularity="daily",
            weekly_spend_cents=weekly_spend_cents,
        )
        after = [r for r in pdata["series"] if r["date"] >= start.isoformat()]
        lo = min(after, key=lambda r: (Decimal(r["balance"]), r["date"]))
        data["cash_check"] = {
            "until": end.isoformat(),
            "starting_balance": cents_to_str(starting_balance_cents),
            "weekly_spend": pdata["weekly_spend"],
            "min_balance": pdata["min_balance"],
            "min_balance_after_start": {
                "date": lo["date"],
                "balance": lo["balance"],
                "spare": lo["spare"],
            },
            "ending_balance": pdata["ending_balance"],
            "spare_balance": pdata["spare_balance"],
            "affordable": Decimal(lo["balance"]) >= 0,
        }
        warnings.extend(w for w in pwarn if w not in warnings)
    if verbose:
        data["debts"] = [result.debts[f.key].to_json() for f in targets]
    data["scenario"] = (model.scenario or {}).get("name") if model.scenario else None
    data["scenario_applied"] = model.applied
    return data, warnings


__all__ = ["add_months", "plan"]
