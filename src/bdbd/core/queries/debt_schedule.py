"""Debt schedule: amortization table for one debt."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from bdbd.core import engine
from bdbd.core.debt import pmt
from bdbd.core.errors import CashError
from bdbd.core.models import EffectiveFlow, EffectiveModel
from bdbd.core.money import cents_to_str, dec_to_str, q, rate_to_str


def debt_terms(f: EffectiveFlow) -> dict:
    d = f.debt
    assert d is not None
    return {
        "balance": cents_to_str(d.balance_cents),
        "balance_as_of": d.balance_as_of.isoformat(),
        "annual_rate": rate_to_str(d.annual_rate),
        "compounding": str(d.compounding),
        "day_count": str(d.day_count),
        "capitalize_interest": d.capitalize_interest,
        "payment_mode": str(d.payment_mode),
        "payment_pct": rate_to_str(d.payment_pct) if d.payment_pct is not None else None,
        "original_principal": (
            cents_to_str(d.original_principal_cents)
            if d.original_principal_cents is not None
            else None
        ),
        "posting_day": d.posting_day,
        "scheduled_payment": cents_to_str(f.base_cents),
        "payment_rrule": f.rrule,
        "payment_dtstart": f.dtstart.isoformat(),
        "payment_until": f.until.isoformat() if f.until else None,
        "events": [
            {
                "id": e.id,
                "date": e.date.isoformat(),
                "type": str(e.type),
                "rate": rate_to_str(e.rate) if e.rate is not None else None,
                "amount": cents_to_str(e.amount_cents) if e.amount_cents is not None else None,
                "notes": e.notes,
            }
            for e in d.events
        ],
    }


def debt_schedule(
    model: EffectiveModel,
    key,
    *,
    as_of: date,
    until: date,
    solve_payment_months: int | None = None,
    max_rows: int | None = None,
) -> tuple[dict, list[str]]:
    flow = next((f for f in model.flows if f.key == key), None)
    if flow is None:
        raise CashError(
            f"flow {key!r} is not in the effective model (inactive or disabled by the scenario)",
            "unknown_flow",
        )
    if flow.debt is None:
        raise CashError(f"flow {flow.name!r} has no debt record", "no_debt")
    sub = EffectiveModel(flows=[flow], applied=model.applied, warnings=[], scenario=model.scenario)
    result = engine.run(sub, as_of=as_of, until=until, stop_when_debts_paid=True)
    summary = result.debts[key]
    rows_after = [r for r in summary.rows if not r.before_as_of]
    rows = []
    for n, r in enumerate(rows_after, start=1):
        j = r.to_json()
        j["n"] = n
        rows.append(j)
    interest_exact = sum((r.interest_exact for r in rows_after), start=Decimal(0))
    total_paid = sum(r.payment_cents for r in rows_after)
    interest_cents = int(q(interest_exact) * 100)
    data = {
        "flow": key,
        "name": flow.name,
        "terms": debt_terms(flow),
        "as_of": as_of.isoformat(),
        "until": result.until.isoformat(),
        "balance_at_as_of": dec_to_str(summary.balance_at_as_of),
        "payoff_date": summary.paid_off_on.isoformat() if summary.paid_off_on else None,
        "payments_before_as_of": len(summary.rows) - len(rows_after),
        "payments_remaining": len(rows_after),
        "total_paid_remaining": cents_to_str(total_paid),
        "total_interest_remaining": cents_to_str(interest_cents),
        "total_principal_remaining": cents_to_str(total_paid - interest_cents),
        "balance_at_until": dec_to_str(summary.balance_at_until),
        "rows": rows if not max_rows or len(rows) <= max_rows else rows[:max_rows],
        "rows_truncated": bool(max_rows and len(rows) > max_rows),
        "solved_payment": None,
    }
    warnings = list(model.warnings) + result.warnings
    if summary.paid_off_on is None:
        warnings.append(
            f"debt {flow.name!r} is not paid off by {result.until}; "
            "extend --until/--months or raise the payment"
        )
    if solve_payment_months:
        bal = int(summary.balance_at_as_of.scaleb(2).to_integral_value())
        rate = flow.debt.annual_rate
        data["solved_payment"] = {
            "months": solve_payment_months,
            "monthly_payment": cents_to_str(pmt(bal, rate, solve_payment_months)),
            "note": "level-payment formula at the current rate on balance_at_as_of; exact for "
            "monthly compounding, approximate for daily/continuous/simple",
        }
    return data, warnings
