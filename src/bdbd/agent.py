"""The JSON every command prints: one envelope per run, made for LLM agents and scripts.

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

from bdbd.ask import CycleItem, DebtRow, PayCycles, Picture, TagRow, coming_up
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


def failure(
    command: str,
    code: str,
    message: str,
    hint: str | None = None,
    tidied: list[str] | None = None,
) -> dict:
    error = {"code": code, "message": message}
    if hint:
        error["hint"] = re.sub(r"\[/?[a-z #0-9]*\]", "", hint)  # drop rich markup
    env: dict[str, Any] = {"ok": False, "command": command, "error": error}
    if tidied:  # the tidy-up ran (and was saved) before the command failed
        env["tidied"] = tidied
    return env


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
        "payday": f.payday,
        "notes": f.notes,
        "debt": debt_json(f),
    }


def pay_cycles_json(pc: PayCycles, today: date) -> dict:
    """`bdbd paydays`: each pay cycle, what its paycheck covers and what's left of it."""

    def item(i: CycleItem) -> dict:
        return {
            "date": i.date.isoformat(),
            "name": i.name,
            "kind": i.kind,
            "amount": cents_to_str(i.cents),
        }

    return {
        "today": today.isoformat(),
        "weekly_spend": cents_to_str(pc.weekly),
        "paydays": pc.paydays,
        "cycles": [
            {
                "start": c.start.isoformat(),
                "end": c.end.isoformat(),
                "days": c.days,
                "current": c.holds(today),
                "open": c.open,
                "paycheck": cents_to_str(c.paycheck),
                "paychecks": [item(i) for i in c.paychecks],
                "bills": [item(i) for i in c.bills],
                "bills_total": cents_to_str(c.bills_total),
                "everyday": cents_to_str(c.everyday),
                "left_before_everyday": cents_to_str(c.left_before_everyday),
                "left": cents_to_str(c.left),
                "money_in": [item(i) for i in c.money_in],
            }
            for c in pc.cycles
        ],
    }


def ledger_json(e: LedgerEntry, end_of_day: dict[date, int] | None = None) -> dict:
    """One scheduled item. `balance_after` is right after it; `end_of_day_balance` (when the
    day's closing balances are known) is at the end of its day, after everyday spending: the
    figure the app shows beside a day's last item, and the one low points use."""
    row = {
        "date": e.date.isoformat(),
        "name": e.name,
        "kind": e.kind,
        "amount": cents_to_str(e.delta_cents),
        "balance_after": cents_to_str(e.balance_after_cents),
    }
    if end_of_day is not None and e.date in end_of_day:
        row["end_of_day_balance"] = cents_to_str(end_of_day[e.date])
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


def debts_json(rows: list[DebtRow]) -> dict:
    done = [r.paid_off_on for r in rows]
    return {
        "debts": [
            {
                "flow": r.key,
                "name": r.name,
                "balance": cents_to_str(r.balance),
                "annual_rate": rate_to_str(r.rate),
                "payment": cents_to_str(r.payment),
                "monthly": cents_to_str(r.monthly),
                "paid_off_on": r.paid_off_on.isoformat() if r.paid_off_on else None,
                "interest_remaining": (
                    cents_to_str(r.interest) if r.interest is not None else None
                ),
                "tags": list(r.tags),
            }
            for r in rows
        ],
        "total_owed": cents_to_str(sum(r.balance for r in rows)),
        "monthly_payments": cents_to_str(sum(r.monthly for r in rows)),
        "interest_remaining": cents_to_str(sum(r.interest or 0 for r in rows)),
        "debt_free_on": (
            max(d for d in done if d is not None).isoformat() if rows and all(done) else None
        ),
    }


def overview_json(p: Picture, today: date) -> dict:
    """`bdbd overview`: the same numbers the app's overview shows."""
    lo = p.low_point
    return {
        "today": today.isoformat(),
        "balance": start_json(p.start),
        "spare": p.spare,
        "spare_balance": p.spare["spare_balance"] if p.spare else None,
        "low_point": (
            {
                "date": lo[0].isoformat(),
                "balance": cents_to_str(lo[1]),
                "horizon_days": p.horizon_days,
            }
            if lo
            else None
        ),
        "monthly": {
            "income": cents_to_str(p.monthly_in),
            "bills": cents_to_str(p.monthly_bills),
            "everyday": cents_to_str(p.monthly_everyday),
            "net": cents_to_str(p.monthly_net),
        },
        "upcoming": [ledger_json(e, dict(p.run.daily)) for e in coming_up(p, today)],
        "debts": debts_json(p.debts),
    }


def tags_json(rows: list[TagRow]) -> list[dict]:
    return [
        {
            "name": r.name,
            "flows": r.flows,
            "flow_names": r.flow_names,
            "expense_monthly": cents_to_str(r.expense_monthly),
            "income_monthly": cents_to_str(r.income_monthly),
        }
        for r in rows
    ]
