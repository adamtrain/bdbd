"""Spending: what actually goes out (or comes in) between two dates, by tag or flow."""

from __future__ import annotations

from datetime import date

from bdbd.core import engine
from bdbd.core.errors import CashError
from bdbd.core.models import EffectiveModel, Kind
from bdbd.core.money import cents_to_str


def _resolve_terms(
    model: EffectiveModel, terms: list[str], known_tags: frozenset[str]
) -> tuple[set, list[dict]]:
    """Each term is a tag name, else a flow name (case-insensitive). Returns matched flow keys."""
    tags = {t for f in model.flows for t in f.tags} | known_tags | {"lifestyle"}
    by_name = {f.name.lower(): f for f in model.flows}
    keys: set = set()
    matched: list[dict] = []
    for term in terms:
        t = term.strip().lower()
        if t in tags:
            ks = (
                [engine.LIFESTYLE_KEY]
                if t == "lifestyle"
                else [f.key for f in model.flows if t in f.tags]
            )
            matched.append({"term": term, "as": "tag"})
        elif t in by_name:
            ks = [by_name[t].key]
            matched.append({"term": term, "as": "flow"})
        else:
            raise CashError(
                f"{term!r} is neither a tag nor a flow name; tags: {', '.join(sorted(tags))} "
                "(`bdbd tags` shows which flows carry each)",
                "unknown_term",
            )
        keys.update(ks)
    return keys, matched


def spend(
    model: EffectiveModel,
    *,
    as_of: date,
    until: date,
    terms: list[str],
    exclude: list[str] | None = None,
    income: bool = False,
    known_tags: frozenset[str] = frozenset(),
    weekly_spend_cents: int = 0,
) -> tuple[dict, list[str]]:
    if model.weekly_spend_cents is not None:
        weekly_spend_cents = model.weekly_spend_cents
    keys, matched = _resolve_terms(model, terms, known_tags) if terms else (None, [])
    excluded, excluded_matched = (
        _resolve_terms(model, exclude, known_tags) if exclude else (set(), [])
    )
    result = engine.run(model, as_of=as_of, until=until, weekly_spend_cents=weekly_spend_cents)
    sign = 1 if income else -1
    rows = [
        e
        for e in result.ledger
        if (keys is None or e.key in keys)
        and e.key not in excluded
        and e.delta_cents * sign > 0
        # an income flow never counts as spending, and a settle surplus never as income
        and ((result.flows_used[e.key].kind == Kind.INCOME) == income)
    ]
    by_flow: dict = {}
    for e in rows:
        u = result.flows_used[e.key]
        b = by_flow.setdefault(
            e.key,
            {"flow": e.key, "name": u.name, "tags": sorted(u.tags), "count": 0, "cents": 0},
        )
        b["count"] += 1
        b["cents"] += e.delta_cents * sign
    total = sum(b["cents"] for b in by_flow.values())
    lifestyle = by_flow.get(engine.LIFESTYLE_KEY, {}).get("cents", 0)
    data = {
        "from": as_of.isoformat(),
        "until": until.isoformat(),
        "direction": "in" if income else "out",
        "matched": matched or None,
        "excluded": excluded_matched or None,
        "total": cents_to_str(total),
        "by_flow": [
            {**{k: v for k, v in b.items() if k != "cents"}, "total": cents_to_str(b["cents"])}
            for b in sorted(by_flow.values(), key=lambda b: -b["cents"])
        ],
        "items": [
            {
                "date": e.date.isoformat(),
                "name": e.name,
                "amount": cents_to_str(e.delta_cents * sign),
            }
            for e in rows
            if e.kind != "lifestyle"
        ],
    }
    data["lifestyle_total"] = cents_to_str(lifestyle)
    if lifestyle:
        data["note"] = "lifestyle spend (weekly_spend, prorated daily) is in total, not in items"
    return data, list(result.warnings)
