"""Compare and breakeven: two projections side by side and the crossover date."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from bdbd.core import engine
from bdbd.core.models import EffectiveModel
from bdbd.core.money import cents_to_str
from bdbd.core.queries.project import extremes, series_rows


def breakeven_analysis(a: engine.SimResult, b: engine.SimResult) -> dict:
    """diff(d) = B(d) - A(d) on end-of-day balances. Breakeven = first day, after diff has been
    negative, on which diff >= 0."""
    diffs = [(d, bb - ab) for (d, ab), (_, bb) in zip(a.daily, b.daily, strict=True)]
    first_div = next((d for d, x in diffs if x != 0), None)
    out: dict = {
        "date": None,
        "status": "identical",
        "first_divergence": first_div.isoformat() if first_div else None,
        "diff_at_end": cents_to_str(diffs[-1][1]) if diffs else "0.00",
        "max_shortfall": None,
        "caveat": None,
    }
    if first_div is None:
        return out
    neg = [(d, x) for d, x in diffs if x < 0]
    if not neg:
        out["status"] = "immediate"
        out["date"] = first_div.isoformat()
        out["caveat"] = (
            "the scenario is never behind the baseline; it is better from the first divergence"
        )
        return out
    worst = min(neg, key=lambda t: (t[1], t[0]))
    out["max_shortfall"] = {"date": worst[0].isoformat(), "amount": cents_to_str(worst[1])}
    first_neg = neg[0][0]
    cross = next((d for d, x in diffs if d > first_neg and x >= 0), None)
    if cross is not None:
        out["status"] = "reached"
        out["date"] = cross.isoformat()
        after = [x for d, x in diffs if d >= cross]
        if any(x < 0 for x in after):
            out["caveat"] = (
                "the scenario falls behind again after the breakeven date; "
                "inspect difference.series"
            )
        return out
    out["status"] = "never_in_horizon"
    # recovery trend: from the worst shortfall to the end of the horizon
    end_d, end_x = diffs[-1]
    start_d, start_x = worst
    span_days = (end_d - start_d).days
    per_month = (  # cents a month
        Decimal(end_x - start_x) / Decimal(span_days) * Decimal("30.4375")
        if span_days
        else Decimal(0)
    )
    out["trend_per_month_since_worst"] = cents_to_str(
        int(per_month.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    )
    if per_month > 0:
        months_needed = Decimal(-end_x) / per_month
        est = end_d + timedelta(days=int(months_needed * Decimal("30.4375")))
        out["caveat"] = (
            f"the scenario never catches up within the horizon (until {end_d}); "
            "it is closing the gap "
            f"at ~{out['trend_per_month_since_worst']}/month since the worst point; linear "
            f"extrapolation crosses ~{est}"
        )
        out["extrapolated_date"] = est.isoformat()
    else:
        out["caveat"] = (
            f"the scenario never catches up within the horizon (until {end_d}) and the gap is not "
            "closing (trend <= 0)"
        )
    return out


def compare(
    baseline: EffectiveModel,
    scenario: EffectiveModel,
    *,
    as_of: date,
    until: date,
    starting_balance_cents: int = 0,
    granularity: str = "monthly",
    include_series: bool = True,
    labels: tuple[str, str] = ("baseline", "scenario"),
    weekly_spend_cents: int = 0,
    verbose: bool = False,
) -> tuple[dict, list[str]]:
    wa = (
        baseline.weekly_spend_cents
        if baseline.weekly_spend_cents is not None
        else weekly_spend_cents
    )
    wb = (
        scenario.weekly_spend_cents
        if scenario.weekly_spend_cents is not None
        else weekly_spend_cents
    )
    a = engine.run(
        baseline,
        as_of=as_of,
        until=until,
        starting_balance_cents=starting_balance_cents,
        weekly_spend_cents=wa,
    )
    b = engine.run(
        scenario,
        as_of=as_of,
        until=until,
        starting_balance_cents=starting_balance_cents,
        weekly_spend_cents=wb,
    )

    def side(label, r: engine.SimResult, weekly: int) -> dict:
        lo, hi = extremes(r)
        d = {
            "label": label,
            "weekly_spend": cents_to_str(weekly),
            "ending_balance": cents_to_str(r.ending_balance_cents),
            "min_balance": lo,
            "max_balance": hi,
            "total_income": cents_to_str(r.income_cents),
            "total_expense": cents_to_str(r.expense_cents),
            "interest_paid": cents_to_str(r.interest_paid_cents),
        }
        if verbose:
            d["debts"] = [x.to_json() for x in r.debts.values()]
        if include_series:
            d["series"] = series_rows(r, granularity)
        return d

    difference: dict[str, Any] = {
        "ending": cents_to_str(b.ending_balance_cents - a.ending_balance_cents)
    }
    data: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "until": until.isoformat(),
        "starting_balance": cents_to_str(starting_balance_cents),
        "a": side(labels[0], a, wa),
        "b": side(labels[1], b, wb),
        "difference": difference,
        "breakeven": breakeven_analysis(a, b),
    }
    if include_series:
        sa, sb = data["a"]["series"], data["b"]["series"]
        data["difference"]["series"] = [
            {
                "date": ra["date"],
                "a": ra["balance"],
                "b": rb["balance"],
                "diff": f"{Decimal(rb['balance']) - Decimal(ra['balance']):.2f}",
            }
            for ra, rb in zip(sa, sb, strict=True)
        ]
    warnings = [f"[{labels[0]}] {w}" for w in a.warnings] + [
        f"[{labels[1]}] {w}" for w in b.warnings
    ]
    return data, warnings
