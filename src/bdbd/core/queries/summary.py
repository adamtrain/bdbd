"""Summary: monthly / annual equivalents by flow and by tag."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from bdbd.core.dates import add_months
from bdbd.core.models import EffectiveModel, Kind, PaymentMode
from bdbd.core.money import dec_to_str
from bdbd.core.recurrence import occurrences, occurrences_per_year

_ZERO = Decimal(0)


def _fmt_occ(x: Decimal) -> str:
    s = f"{x:.4f}".rstrip("0").rstrip(".")
    return s or "0"


def summary(
    model: EffectiveModel,
    *,
    as_of: date,
    mode: str = "steady",
    months: int = 12,
    by: str = "both",
    tag_filter: str | None = None,
) -> tuple[dict, list[str]]:
    warnings = list(model.warnings)
    by_flow: list[dict] = []
    one_offs: list[dict] = []
    tag_agg: dict[str, dict] = {}
    untagged = {"income_monthly": _ZERO, "expense_monthly": _ZERO}
    net = {"income_monthly": _ZERO, "expense_monthly": _ZERO}
    window_end = add_months(as_of, months) - timedelta(days=1)

    for f in model.flows:
        if tag_filter is not None and tag_filter.lower() not in f.tags:
            continue
        amount = Decimal(f.amount_on(as_of)).scaleb(-2)
        if f.debt is not None and f.debt.payment_mode != PaymentMode.FIXED:
            warnings.append(
                f"flow {f.name!r}: payment mode {f.debt.payment_mode} varies with the balance; "
                "summary uses the flow's fixed amount as an approximation"
            )
        if mode == "steady":
            if f.rrule is None:
                if f.dtstart >= as_of:
                    one_offs.append(
                        {
                            "flow": f.key,
                            "name": f.name,
                            "kind": str(f.kind),
                            "date": f.dtstart.isoformat(),
                            "amount": dec_to_str(amount),
                            "tags": sorted(f.tags),
                        }
                    )
                continue
            occ = occurrences_per_year(f.rrule, f.dtstart, f.until, as_of)
            if occ == 0:
                continue
            annual = amount * occ
            monthly = annual / 12
            occ_str = _fmt_occ(occ)
        else:
            dates = occurrences(f.rrule, f.dtstart, f.until, as_of, window_end, f.weekend)
            total = sum((Decimal(f.amount_on(d)).scaleb(-2) for d in dates), start=_ZERO)
            if not dates:
                continue
            monthly = total / months
            annual = monthly * 12
            occ_str = str(len(dates))
        row = {
            "flow": f.key,
            "name": f.name,
            "kind": str(f.kind),
            "tags": sorted(f.tags),
            "amount": dec_to_str(amount),
            "rrule": f.rrule,
            ("occurrences_per_year" if mode == "steady" else "occurrences_in_window"): occ_str,
            "monthly": dec_to_str(monthly),
            "annual": dec_to_str(annual),
            "is_debt": f.debt is not None,
        }
        by_flow.append(row)
        bucket = "income_monthly" if f.kind == Kind.INCOME else "expense_monthly"
        net[bucket] += monthly
        if not f.tags:
            untagged[bucket] += monthly
        for t in f.tags:
            agg = tag_agg.setdefault(
                t, {"income_monthly": _ZERO, "expense_monthly": _ZERO, "flows": []}
            )
            agg[bucket] += monthly
            agg["flows"].append(f.key)

    def money_block(d: dict) -> dict:
        inc, exp = d["income_monthly"], d["expense_monthly"]
        return {
            "income_monthly": dec_to_str(inc),
            "expense_monthly": dec_to_str(exp),
            "net_monthly": dec_to_str(inc - exp),
            "income_annual": dec_to_str(inc * 12),
            "expense_annual": dec_to_str(exp * 12),
            "net_annual": dec_to_str((inc - exp) * 12),
        }

    by_tag = [{"tag": t, **money_block(a), "flows": a["flows"]} for t, a in sorted(tag_agg.items())]
    data: dict = {
        "as_of": as_of.isoformat(),
        "mode": mode,
        "window_months": months if mode == "actual" else None,
        "tag_filter": tag_filter,
        "net": money_block(net),
    }
    if by in ("flow", "both"):
        data["by_flow"] = by_flow
    if by in ("tag", "both"):
        data["by_tag"] = by_tag
        data["untagged"] = money_block(untagged)
    data["one_offs"] = one_offs
    data["note"] = (
        "steady mode: amount x occurrences-per-year / 12, one-offs excluded (listed separately); "
        "a flow with several tags is counted under each tag, so by_tag totals do not sum to net"
        if mode == "steady"
        else f"actual mode: real occurrences in [{as_of}, {window_end}] divided by {months} months"
    )
    return data, warnings
