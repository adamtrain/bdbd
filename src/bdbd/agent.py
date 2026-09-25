"""Agent mode: one JSON envelope per command, made for LLM agents and scripts.

    {"ok": true,  "command": "project", "data": {...}, "warnings": [...], "tidied": [...]}
    {"ok": false, "command": "add", "error": {"code": "...", "message": "...", "hint": "..."}}

Money is a string with two decimals, dates are ISO, rates are decimal fractions. `--select`
trims `data` to a few dotted paths; when a path misses, the error lists what is there.
"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from typing import Any

from bdbd.budget import Recorded, Start
from bdbd.core import balance as balances
from bdbd.core.engine import LedgerEntry
from bdbd.core.errors import CashError
from bdbd.core.models import DebtEvent, Flow
from bdbd.core.money import cents_to_str, rate_to_str
from bdbd.words import describe


def _default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return f"{o:.2f}"
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, frozenset | set):
        return sorted(o)
    if hasattr(o, "to_json"):
        return o.to_json()
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=_default, ensure_ascii=False, separators=(",", ":"))


def envelope(
    command: str,
    data: Any,
    warnings: list[str] | None = None,
    tidied: list[str] | None = None,
) -> dict:
    env: dict[str, Any] = {
        "ok": True,
        "command": command,
        "data": data,
        "warnings": list(dict.fromkeys(warnings or [])),
    }
    if tidied:
        env["tidied"] = tidied
    return env


def failure(command: str, code: str, message: str, hint: str | None = None) -> dict:
    error = {"code": code, "message": message}
    if hint:
        error["hint"] = re.sub(r"\[/?[a-z #0-9]*\]", "", hint)  # drop rich markup
    return {"ok": False, "command": command, "error": error}


# ── --select ──────────────────────────────────────────────────────────────────

_TOKEN = re.compile(r"([^.\[\]]+)|\[(-?\d+)\]")

# Keys that exist only when a flag is given; the error says so instead of "not found".
GATED_KEYS = {"flows_used": "--verbose", "ledger": "--ledger"}


def _describe(value: Any) -> str:
    if isinstance(value, dict):
        return f"an object with keys: {', '.join(sorted(value))}" if value else "an empty object"
    if isinstance(value, list):
        return f"a list of {len(value)} items (index it: [0], [-1], ...)"
    return f"a {type(value).__name__} with no fields"


def _tokens(path: str) -> list[tuple[str | None, str | None]]:
    out: list[tuple[str | None, str | None]] = []
    pos = 0
    for m in _TOKEN.finditer(path):
        if m.start() != pos and path[pos : m.start()] not in (".", ""):
            raise CashError(f"bad --select path {path!r}", "usage")
        pos = m.end()
        out.append((m.group(1), m.group(2)))
    return out


def get_path(data: Any, path: str) -> Any:
    tokens = _tokens(path)
    relative = isinstance(data, dict) and "data" not in data
    if len(tokens) > 1 and tokens[0] == ("data", None) and relative:
        tokens = tokens[1:]  # forgive `data.x`: paths are already relative to data
    cur = data
    resolved = ""
    for key, idx in tokens:
        try:
            cur = cur[int(idx)] if idx is not None else cur[key]
        except (KeyError, IndexError, TypeError):
            where = f"{resolved!r}" if resolved else "data"
            want = f"index [{idx}]" if idx is not None else f"key {key!r}"
            msg = f"--select: {path!r} not found: {where} has no {want}; it is {_describe(cur)}"
            if key in GATED_KEYS and isinstance(cur, dict):
                msg += f". {key!r} is only in the output when {GATED_KEYS[key]} is given"
            raise CashError(msg, "select_not_found") from None
        resolved += f"[{idx}]" if idx is not None else (f".{key}" if resolved else str(key))
    return cur


def select(data: Any, paths: str) -> dict:
    return {p.strip(): get_path(data, p.strip()) for p in paths.split(",") if p.strip()}


# ── Stable shapes ─────────────────────────────────────────────────────────────


def event_json(e: DebtEvent) -> dict:
    return {
        "id": e.id,
        "flow_id": e.flow_id,
        "date": e.date.isoformat(),
        "type": str(e.type),
        "rate": rate_to_str(e.rate) if e.rate is not None else None,
        "amount": cents_to_str(e.amount_cents) if e.amount_cents is not None else None,
        "notes": e.notes,
    }


def debt_json(f: Flow) -> dict | None:
    d = f.debt
    if d is None:
        return None
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
        "events": [event_json(e) for e in d.events],
    }


def flow_json(f: Flow, *, next_date: date | None = None) -> dict:
    return {
        "id": f.id,
        "name": f.name,
        "kind": str(f.kind),
        "amount": cents_to_str(f.amount_cents),
        "schedule": describe(f.rrule, f.dtstart, f.until),
        "rrule": f.rrule,
        "dtstart": f.dtstart.isoformat(),
        "until": f.until.isoformat() if f.until else None,
        "next": next_date.isoformat() if next_date else None,
        "active": f.active,
        "tags": list(f.tags),
        "weekend": str(f.weekend),
        "notes": f.notes,
        "debt": debt_json(f),
    }


def ledger_json(e: LedgerEntry) -> dict:
    row = {
        "date": e.date.isoformat(),
        "name": e.name,
        "kind": e.kind,
        "amount": cents_to_str(e.delta_cents),
        "balance_after": cents_to_str(e.balance_after_cents),
    }
    if e.debt is not None:
        row["debt"] = {
            "interest": cents_to_str(e.debt.interest_cents),
            "principal": cents_to_str(e.debt.principal_cents),
            "balance_after": f"{e.debt.balance_after:.2f}",
        }
    return row


def balance_json(b: balances.Balance | None) -> dict | None:
    if b is None:
        return None
    return {
        "as_of": b.as_of.isoformat(),
        "balance": cents_to_str(b.amount_cents),
        "recorded_at": b.recorded_at,
    }


def start_json(s: Start) -> dict:
    """Where a projection's starting balance came from."""
    return {
        "balance": cents_to_str(s.cents),
        "as_of": s.as_of.isoformat(),
        "source": s.source,
        "recorded": balance_json(s.recorded),
    }


def recorded_json(r: Recorded) -> dict:
    return {
        "recorded": balance_json(r.balance),
        "typed": cents_to_str(r.typed_cents),
        "already_posted": [ledger_json(e) for e in r.posted],
        "previous": balance_json(r.previous),
        "expected": cents_to_str(r.expected_cents) if r.expected_cents is not None else None,
        "difference_from_expected": (
            cents_to_str(r.balance.amount_cents - r.expected_cents)
            if r.expected_cents is not None
            else None
        ),
    }
