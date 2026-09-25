"""Scenario overlays: ephemeral JSON what-ifs applied on top of the stored flows.

A scenario is never persisted. It is echoed back in query output so results are reproducible.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from bdbd.core.dates import parse_date
from bdbd.core.errors import CashError
from bdbd.core.models import (
    AmountSegment,
    Compounding,
    DayCount,
    Debt,
    DebtEvent,
    EffectiveFlow,
    EffectiveModel,
    EventType,
    Flow,
    Kind,
    PaymentMode,
    Weekend,
)
from bdbd.core.money import parse_amount, parse_rate
from bdbd.core.recurrence import validate_rrule
from bdbd.core.repo import normalize_tag, validate_event_fields

_KNOWN_KEYS = {
    "name",
    "disable",
    "enable",
    "end",
    "amount_changes",
    "add_flows",
    "debt_events",
    "weekly_spend",
}

# ---- the `?` date placeholder (resolved by `bdbd earliest`, or `--on DATE`) ------------------

PLACEHOLDER = "?"
_PLACEHOLDER_RE = re.compile(r"^\?(?:([+-])(\d+))?$")
_DATE_KEYS = {"on", "date", "from", "until", "after", "dtstart"}


def _placeholder_offset(value) -> int | None:
    """Days offset for '?', '?+N', '?-N'; None when the value is not a placeholder."""
    if not isinstance(value, str):
        return None
    m = _PLACEHOLDER_RE.match(value.strip())
    if not m:
        return None
    sign, n = m.group(1), m.group(2)
    return 0 if n is None else (int(n) if sign == "+" else -int(n))


def has_placeholder(scenario: dict | None) -> bool:
    """True when any date field in the scenario is '?' (optionally with a +N/-N day offset)."""
    return scenario is not None and any(
        _placeholder_offset(v) is not None for _, v in _walk_dates(scenario)
    )


def resolve_placeholders(scenario: dict | None, day: date) -> dict | None:
    """Copy of the scenario with every '?' date field replaced by `day` (+/- its offset)."""
    if scenario is None:
        return None
    out = copy.deepcopy(scenario)
    for holder, key in _walk_dates(out, with_holders=True):
        off = _placeholder_offset(holder[key])
        if off is not None:
            holder[key] = (day + timedelta(days=off)).isoformat()
    return out


def placeholder_dates(scenario: dict | None, day: date) -> set[date]:
    """The concrete dates every '?' field resolves to for `day`."""
    if scenario is None:
        return set()
    out = set()
    for _, v in _walk_dates(scenario):
        off = _placeholder_offset(v)
        if off is not None:
            out.add(day + timedelta(days=off))
    return out


def _walk_dates(node, with_holders: bool = False, key=None):
    """Yield (key, value) — or (holder, key) — for every date-like field in the scenario."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _DATE_KEYS and isinstance(v, str):
                yield (node, k) if with_holders else (k, v)
            elif isinstance(v, dict | list):
                yield from _walk_dates(v, with_holders, k)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_dates(item, with_holders, key)


def load_scenario(source: str | None, inline: str | None) -> dict | None:
    """--scenario FILE | '-' (stdin), or --scenario-json '{...}'."""
    if source and inline:
        raise CashError("pass either --scenario or --scenario-json, not both", "usage")
    text: str | None = None
    if inline:
        text = inline
    elif source == "-":
        text = sys.stdin.read()
    elif source:
        p = Path(source).expanduser()
        if not p.exists():
            raise CashError(f"scenario file {source} not found", "scenario_not_found")
        text = p.read_text()
    if text is None:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CashError(f"scenario is not valid JSON: {exc}", "invalid_scenario") from exc
    if not isinstance(data, dict):
        raise CashError("scenario must be a JSON object", "invalid_scenario")
    unknown = set(data) - _KNOWN_KEYS
    if unknown:
        raise CashError(
            f"unknown scenario keys: {sorted(unknown)}; allowed: {sorted(_KNOWN_KEYS)}",
            "invalid_scenario",
        )
    return data


def _as_list(value, what: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CashError(f"scenario.{what} must be a list", "invalid_scenario")
    return value


def _debt_from_json(key, spec: dict, name: str) -> Debt:
    try:
        compounding = Compounding(spec.get("compounding", "monthly"))
        payment_mode = PaymentMode(spec.get("payment_mode", "fixed"))
        day_count = DayCount(spec.get("day_count", "actual/365"))
    except ValueError as exc:
        raise CashError(f"scenario flow {name!r}: {exc}", "invalid_scenario") from exc
    cap = spec.get("capitalize_interest")
    if cap is None:
        cap = compounding != Compounding.SIMPLE
    return Debt(
        flow_id=key,
        balance_cents=parse_amount(spec["balance"]),
        balance_as_of=parse_date(spec["balance_as_of"]),
        annual_rate=parse_rate(spec["annual_rate"]),
        compounding=compounding,
        day_count=day_count,
        capitalize_interest=bool(cap) and compounding != Compounding.SIMPLE,
        payment_mode=payment_mode,
        payment_pct=parse_rate(spec["payment_pct"]) if spec.get("payment_pct") else None,
        original_principal_cents=(
            parse_amount(spec["original_principal"]) if spec.get("original_principal") else None
        ),
        posting_day=int(spec["posting_day"]) if spec.get("posting_day") else None,
    )


def _base_effective(flow: Flow) -> EffectiveFlow:
    return EffectiveFlow(
        key=flow.id,
        name=flow.name,
        kind=flow.kind,
        tags=frozenset(flow.tags),
        rrule=flow.rrule,
        dtstart=flow.dtstart,
        until=flow.until,
        base_cents=flow.amount_cents,
        segments=(),
        debt=flow.debt,
        origin="db",
        weekend=flow.weekend,
    )


def build_effective_model(
    all_flows: list[Flow],
    scenario: dict | None,
    as_of: date,
    *,
    include_inactive: bool = False,
) -> EffectiveModel:
    """Overlay `scenario` on the DB flows (active + inactive) and return what the engine runs."""
    by_id: dict[int, Flow] = {int(f.id): f for f in all_flows}
    by_name: dict[str, Flow] = {f.name.lower(): f for f in all_flows}
    model = EffectiveModel(flows=[], scenario=scenario)
    if scenario is not None and scenario.get("weekly_spend") is not None:
        model.weekly_spend_cents = parse_amount(scenario["weekly_spend"])
        model.applied.append(f"weekly lifestyle spend set to {scenario['weekly_spend']}")
    if scenario is None:
        model.flows = [_base_effective(f) for f in all_flows if f.active or include_inactive]
        return model

    def resolve(ref, what: str) -> Flow:
        if isinstance(ref, bool):
            raise CashError(
                f"scenario.{what}: flow reference must be an id or name", "invalid_scenario"
            )
        if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
            f = by_id.get(int(ref))
        else:
            f = by_name.get(str(ref).strip().lower())
        if f is None:
            raise CashError(f"scenario.{what}: no flow {ref!r}", "scenario_unknown_flow")
        return f

    active: dict[int, bool] = {int(f.id): (f.active or include_inactive) for f in all_flows}

    # enable first, then disable (disable wins on conflict)
    enable = scenario.get("enable") or {}
    for ref in _as_list(enable.get("flow_ids"), "enable.flow_ids") + _as_list(
        enable.get("flows"), "enable.flows"
    ):
        f = resolve(ref, "enable")
        if not active[int(f.id)]:
            model.applied.append(f"enabled flow {f.name!r}")
        active[int(f.id)] = True

    disable = scenario.get("disable") or {}
    for ref in _as_list(disable.get("flow_ids"), "disable.flow_ids") + _as_list(
        disable.get("flows"), "disable.flows"
    ):
        f = resolve(ref, "disable")
        if active[int(f.id)]:
            model.applied.append(f"disabled flow {f.name!r}")
        active[int(f.id)] = False
    for tag in _as_list(disable.get("tags"), "disable.tags"):
        t = normalize_tag(str(tag))
        hit = [f for f in all_flows if t in {x.lower() for x in f.tags} and active[int(f.id)]]
        if not hit:
            model.warnings.append(f"scenario: tag {t!r} matched no active flow")
            continue
        for f in hit:
            active[int(f.id)] = False
        model.applied.append(
            f"disabled {len(hit)} flow(s) via tag {t!r}: " + ", ".join(f.name for f in hit)
        )

    # end: a flow (or every flow with a tag) has no occurrences after a date
    ends: dict[int, date] = {}
    for item in _as_list(scenario.get("end"), "end"):
        if (
            not isinstance(item, dict)
            or "after" not in item
            or not (("flow" in item) ^ ("tag" in item))
        ):
            raise CashError(
                "scenario.end entries need 'after' and exactly one of 'flow' or 'tag'",
                "invalid_scenario",
            )
        after = parse_date(item["after"])
        if "flow" in item:
            hit = [resolve(item["flow"], "end")]
        else:
            t = normalize_tag(str(item["tag"]))
            hit = [f for f in all_flows if t in {x.lower() for x in f.tags} and active[int(f.id)]]
            if not hit:
                model.warnings.append(f"scenario: end tag {t!r} matched no active flow")
                continue
        for f in hit:
            fid = int(f.id)
            ends[fid] = min(ends[fid], after) if fid in ends else after
        model.applied.append(
            f"ended {len(hit)} flow(s) after {after.isoformat()}: " + ", ".join(f.name for f in hit)
        )

    # amount changes -> segments
    segments: dict[int, list[AmountSegment]] = {}
    for ch in _as_list(scenario.get("amount_changes"), "amount_changes"):
        if not isinstance(ch, dict) or "flow" not in ch or "amount" not in ch:
            raise CashError(
                "scenario.amount_changes entries need 'flow' and 'amount'", "invalid_scenario"
            )
        f = resolve(ch["flow"], "amount_changes")
        seg = AmountSegment(
            from_date=parse_date(ch["from"]) if ch.get("from") else None,
            until=parse_date(ch["until"]) if ch.get("until") else None,
            cents=parse_amount(ch["amount"]),
        )
        segments.setdefault(int(f.id), []).append(seg)
        model.applied.append(
            f"amount of {f.name!r} set to {ch['amount']} from "
            f"{ch.get('from') or as_of.isoformat()}"
            + (f" until {ch['until']}" if ch.get("until") else "")
        )

    # scenario debt events, keyed by flow id (db) or later by scenario key
    extra_events: dict[int | str, list[DebtEvent]] = {}
    pending_scenario_events: list[dict] = []
    for ev in _as_list(scenario.get("debt_events"), "debt_events"):
        if not isinstance(ev, dict) or "flow" not in ev or "type" not in ev or "date" not in ev:
            raise CashError(
                "scenario.debt_events entries need 'flow', 'type' and 'date'", "invalid_scenario"
            )
        ref = ev["flow"]
        if isinstance(ref, str) and ref.startswith("s:"):
            pending_scenario_events.append(ev)
            continue
        f = resolve(ref, "debt_events")
        if f.debt is None:
            raise CashError(
                f"scenario.debt_events: flow {f.name!r} has no debt record", "scenario_no_debt"
            )
        extra_events.setdefault(int(f.id), []).append(_event_from_json(int(f.id), ev))
        model.applied.append(f"debt event on {f.name!r}: {ev['type']} on {ev['date']}")

    # A disabled debt flow that carries a payoff/settle event stays in the model: it keeps paying
    # until that event, then stops. Without one, disabling simply removes debt + payments (warn).
    _closing = (EventType.PAYOFF, EventType.SETTLE)
    for f in all_flows:
        if f.debt is None:
            continue
        closes = [
            e
            for e in list(extra_events.get(int(f.id), [])) + list(f.debt.events)
            if e.type in _closing
        ]
        if not active[int(f.id)] and f.active:
            if closes:
                active[int(f.id)] = True
                model.applied.append(
                    f"kept debt flow {f.name!r} despite disable: it has a {closes[0].type} event, "
                    "so payments continue until then and stop afterwards"
                )
            else:
                model.warnings.append(
                    f"scenario disables debt flow {f.name!r} without a payoff/settle event; the "
                    "debt and its payments simply vanish from the projection (no payoff cost "
                    "modelled)"
                )
        if int(f.id) in ends and not any(e.date <= ends[int(f.id)] for e in closes):
            model.warnings.append(
                f"scenario ends debt flow {f.name!r} after {ends[int(f.id)]} without a "
                "payoff/settle by then; its payments stop but the balance stays and keeps "
                "accruing interest"
            )

    for f in all_flows:
        if not active[int(f.id)]:
            continue
        debt = f.debt
        if debt is not None and int(f.id) in extra_events:
            debt = replace(debt, events=tuple(list(debt.events) + extra_events[int(f.id)]))
        until = f.until
        if int(f.id) in ends:
            until = min(until, ends[int(f.id)]) if until else ends[int(f.id)]
        model.flows.append(
            EffectiveFlow(
                key=f.id,
                name=f.name,
                kind=f.kind,
                tags=frozenset(f.tags),
                rrule=f.rrule,
                dtstart=f.dtstart,
                until=until,
                base_cents=f.amount_cents,
                segments=tuple(segments.get(int(f.id), [])),
                debt=debt,
                origin="db",
                weekend=f.weekend,
            )
        )

    # ad-hoc flows
    for n, spec in enumerate(_as_list(scenario.get("add_flows"), "add_flows"), start=1):
        key = f"s:{n}"
        if not isinstance(spec, dict):
            raise CashError("scenario.add_flows entries must be objects", "invalid_scenario")
        for req in ("name", "kind", "amount"):
            if req not in spec:
                raise CashError(f"scenario.add_flows entry missing {req!r}", "invalid_scenario")
        try:
            kind = Kind(spec["kind"])
        except ValueError as exc:
            raise CashError(
                f"scenario.add_flows: bad kind {spec['kind']!r}", "invalid_scenario"
            ) from exc
        one_off = spec.get("on")
        rrule = spec.get("rrule")
        if one_off and rrule:
            raise CashError(
                "scenario.add_flows: use 'on' (one-off) or 'rrule'+'dtstart'", "invalid_scenario"
            )
        if one_off:
            dtstart, rrule = parse_date(one_off), None
        else:
            if not rrule or not spec.get("dtstart"):
                raise CashError(
                    f"scenario.add_flows {spec['name']!r}: need 'on' or both 'rrule' and 'dtstart'",
                    "invalid_scenario",
                )
            dtstart = parse_date(spec["dtstart"])
            rrule = validate_rrule(rrule)
        debt = None
        if spec.get("debt"):
            if kind != Kind.EXPENSE:
                raise CashError("scenario.add_flows: a debt must be an expense", "invalid_scenario")
            debt = _debt_from_json(key, spec["debt"], spec["name"])
            evs = [_event_from_json(key, ev) for ev in pending_scenario_events if ev["flow"] == key]
            if evs:
                debt = replace(debt, events=tuple(evs))
        try:
            weekend = Weekend(spec.get("weekend", "none"))
        except ValueError as exc:
            raise CashError(
                f"scenario.add_flows: bad weekend {spec.get('weekend')!r}", "invalid_scenario"
            ) from exc
        model.flows.append(
            EffectiveFlow(
                key=key,
                name=str(spec["name"]),
                kind=kind,
                tags=frozenset(normalize_tag(t) for t in (spec.get("tags") or [])),
                rrule=rrule,
                dtstart=dtstart,
                until=parse_date(spec["until"]) if spec.get("until") else None,
                base_cents=parse_amount(spec["amount"]),
                segments=(),
                debt=debt,
                origin="scenario",
                weekend=weekend,
            )
        )
        model.applied.append(f"added {kind} {spec['name']!r} as {key}")
    handled = {f.key for f in model.flows if f.origin == "scenario"}
    for ev in pending_scenario_events:
        if ev["flow"] not in handled:
            raise CashError(
                f"scenario.debt_events: no scenario flow {ev['flow']!r}", "scenario_unknown_flow"
            )
    return model


def _event_from_json(key, ev: dict) -> DebtEvent:
    try:
        et = EventType(ev["type"])
    except ValueError as exc:
        raise CashError(
            f"scenario.debt_events: bad type {ev['type']!r}", "invalid_scenario"
        ) from exc
    rate = parse_rate(ev["rate"]) if ev.get("rate") is not None else None
    amount = (
        parse_amount(ev["amount"], allow_negative=True) if ev.get("amount") is not None else None
    )
    validate_event_fields(et, rate, amount)
    return DebtEvent(
        id=None,
        flow_id=key,
        date=parse_date(ev["date"]),
        type=et,
        rate=rate,
        amount_cents=amount,
        notes=ev.get("notes"),
    )


def describe(scenario: dict | None) -> dict | None:
    if scenario is None:
        return None
    return {"name": scenario.get("name"), "spec": scenario}


__all__ = [
    "PLACEHOLDER",
    "Decimal",
    "build_effective_model",
    "describe",
    "has_placeholder",
    "load_scenario",
    "placeholder_dates",
    "resolve_placeholders",
]
