"""All SQL for CRUD. Returns model objects; raises CashError with stable codes."""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from bdbd.core.dates import iso, parse_date
from bdbd.core.errors import CashError
from bdbd.core.models import (
    Compounding,
    DayCount,
    Debt,
    DebtEvent,
    EventType,
    Flow,
    Kind,
    PaymentMode,
    Tag,
    Weekend,
)
from bdbd.core.money import parse_rate, rate_to_str
from bdbd.core.recurrence import validate_rrule

# ---- tags ---------------------------------------------------------------------------------


def normalize_tag(name: str) -> str:
    t = name.strip().lower()
    if not t:
        raise CashError("tag name must not be empty", "invalid_tag")
    if "," in t:
        raise CashError(f"a tag can't contain a comma: {name.strip()!r}", "invalid_tag")
    return t


def ensure_tag(conn: sqlite3.Connection, name: str) -> int:
    t = normalize_tag(name)
    row = conn.execute("SELECT id FROM tag WHERE name = ?", (t,)).fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute("INSERT INTO tag(name) VALUES (?)", (t,))
    assert cur.lastrowid is not None
    return cur.lastrowid


def list_tags(conn: sqlite3.Connection) -> list[Tag]:
    rows = conn.execute(
        "SELECT t.id, t.name, count(ft.flow_id) AS n FROM tag t "
        "LEFT JOIN flow_tag ft ON ft.tag_id = t.id GROUP BY t.id ORDER BY t.name"
    ).fetchall()
    return [Tag(int(r["id"]), r["name"], int(r["n"])) for r in rows]


def rename_tag(conn: sqlite3.Connection, old: str, new: str) -> None:
    o, n = normalize_tag(old), normalize_tag(new)
    if conn.execute("SELECT 1 FROM tag WHERE name = ?", (o,)).fetchone() is None:
        raise CashError(f"no tag named {old!r}", "unknown_tag")
    if conn.execute("SELECT 1 FROM tag WHERE name = ?", (n,)).fetchone() is not None:
        raise CashError(f"tag {new!r} already exists", "duplicate_tag")
    conn.execute("UPDATE tag SET name = ? WHERE name = ?", (n, o))


def remove_tag(conn: sqlite3.Connection, name: str) -> None:
    cur = conn.execute("DELETE FROM tag WHERE name = ?", (normalize_tag(name),))
    if cur.rowcount == 0:
        raise CashError(f"no tag named {name!r}", "unknown_tag")


def set_flow_tags(conn: sqlite3.Connection, flow_id: int, tags: list[str]) -> None:
    conn.execute("DELETE FROM flow_tag WHERE flow_id = ?", (flow_id,))
    for t in tags:
        conn.execute(
            "INSERT OR IGNORE INTO flow_tag(flow_id, tag_id) VALUES (?, ?)",
            (flow_id, ensure_tag(conn, t)),
        )


def add_flow_tags(conn: sqlite3.Connection, flow_id: int, tags: list[str]) -> None:
    for t in tags:
        conn.execute(
            "INSERT OR IGNORE INTO flow_tag(flow_id, tag_id) VALUES (?, ?)",
            (flow_id, ensure_tag(conn, t)),
        )


def remove_flow_tags(conn: sqlite3.Connection, flow_id: int, tags: list[str]) -> None:
    for t in tags:
        conn.execute(
            "DELETE FROM flow_tag WHERE flow_id = ? "
            "AND tag_id = (SELECT id FROM tag WHERE name = ?)",
            (flow_id, normalize_tag(t)),
        )


def prune_unused_tags(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM tag WHERE id NOT IN (SELECT tag_id FROM flow_tag)")


# ---- flows --------------------------------------------------------------------------------


def _flow_tags(conn: sqlite3.Connection, flow_id: int) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT t.name FROM tag t JOIN flow_tag ft ON ft.tag_id = t.id "
        "WHERE ft.flow_id = ? ORDER BY t.name",
        (flow_id,),
    ).fetchall()
    return tuple(r["name"] for r in rows)


def _row_to_event(r: sqlite3.Row) -> DebtEvent:
    return DebtEvent(
        id=int(r["id"]),
        flow_id=int(r["flow_id"]),
        date=parse_date(r["date"]),
        type=EventType(r["type"]),
        rate=Decimal(r["rate"]) if r["rate"] is not None else None,
        amount_cents=int(r["amount_cents"]) if r["amount_cents"] is not None else None,
        notes=r["notes"],
    )


def list_events(conn: sqlite3.Connection, flow_id: int) -> list[DebtEvent]:
    rows = conn.execute(
        "SELECT * FROM debt_event WHERE flow_id = ? ORDER BY date, id", (flow_id,)
    ).fetchall()
    return [_row_to_event(r) for r in rows]


def _load_debt(conn: sqlite3.Connection, flow_id: int) -> Debt | None:
    r = conn.execute("SELECT * FROM debt WHERE flow_id = ?", (flow_id,)).fetchone()
    if r is None:
        return None
    return Debt(
        flow_id=flow_id,
        balance_cents=int(r["balance_cents"]),
        balance_as_of=parse_date(r["balance_as_of"]),
        annual_rate=Decimal(r["annual_rate"]),
        compounding=Compounding(r["compounding"]),
        day_count=DayCount(r["day_count"]),
        capitalize_interest=bool(r["capitalize_interest"]),
        payment_mode=PaymentMode(r["payment_mode"]),
        payment_pct=Decimal(r["payment_pct"]) if r["payment_pct"] is not None else None,
        original_principal_cents=(
            int(r["original_principal_cents"])
            if r["original_principal_cents"] is not None
            else None
        ),
        posting_day=int(r["posting_day"]) if r["posting_day"] is not None else None,
        events=tuple(list_events(conn, flow_id)),
    )


def _row_to_flow(conn: sqlite3.Connection, r: sqlite3.Row) -> Flow:
    fid = int(r["id"])
    return Flow(
        id=fid,
        name=r["name"],
        kind=Kind(r["kind"]),
        amount_cents=int(r["amount_cents"]),
        rrule=r["rrule"],
        dtstart=parse_date(r["dtstart"]),
        until=parse_date(r["until"]) if r["until"] else None,
        active=bool(r["active"]),
        notes=r["notes"],
        tags=_flow_tags(conn, fid),
        weekend=Weekend(r["weekend"]),
        debt=_load_debt(conn, fid),
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


def get_flow(conn: sqlite3.Connection, flow_id: int) -> Flow:
    r = conn.execute("SELECT * FROM flow WHERE id = ?", (flow_id,)).fetchone()
    if r is None:
        raise CashError(f"no flow with id {flow_id}", "unknown_flow")
    return _row_to_flow(conn, r)


def resolve_flow(conn: sqlite3.Connection, ident: str | int) -> Flow:
    """Accept a (case-insensitive) unique name, or a numeric id."""
    s = str(ident).strip()
    r = conn.execute("SELECT * FROM flow WHERE name = ?", (s,)).fetchone()
    if r is not None:  # a name first, for older budgets that have all-digit names
        return _row_to_flow(conn, r)
    if s.isdigit():
        return get_flow(conn, int(s))
    raise CashError(f"no flow named {s!r}", "unknown_flow")


def list_flows(
    conn: sqlite3.Connection,
    *,
    kind: Kind | None = None,
    tag: str | None = None,
    include_inactive: bool = False,
) -> list[Flow]:
    sql = "SELECT f.* FROM flow f"
    where: list[str] = []
    args: list = []
    if tag is not None:
        sql += " JOIN flow_tag ft ON ft.flow_id = f.id JOIN tag t ON t.id = ft.tag_id"
        where.append("t.name = ?")
        args.append(normalize_tag(tag))
    if kind is not None:
        where.append("f.kind = ?")
        args.append(str(kind))
    if not include_inactive:
        where.append("f.active = 1")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY f.id"
    return [_row_to_flow(conn, r) for r in conn.execute(sql, args).fetchall()]


def check_name(name: str) -> str:
    """A flow's name, trimmed; numbers alone are refused because they read as ids."""
    n = str(name).strip()
    if not n:
        raise CashError("flow name must not be empty", "invalid_name")
    if n.isdigit():
        raise CashError(
            f"a flow name needs a letter in it ({n!r} would read as an id)", "invalid_name"
        )
    return n


def add_flow(
    conn: sqlite3.Connection,
    *,
    name: str,
    kind: Kind,
    amount_cents: int,
    rrule: str | None,
    dtstart: date,
    until: date | None = None,
    tags: list[str] | None = None,
    notes: str | None = None,
    active: bool = True,
    weekend: Weekend = Weekend.NONE,
    flow_id: int | None = None,
) -> Flow:
    name = check_name(name)
    if rrule is not None:
        rrule = validate_rrule(rrule)
    if until is not None and until < dtstart:
        raise CashError("until must not be before dtstart", "invalid_date")
    if conn.execute("SELECT 1 FROM flow WHERE name = ?", (name,)).fetchone():
        raise CashError(f"a flow named {name!r} already exists", "duplicate_flow")
    cur = conn.execute(
        "INSERT INTO flow(id, name, kind, amount_cents, rrule, dtstart, until, active, notes, "
        "weekend) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            flow_id,
            name,
            str(kind),
            amount_cents,
            rrule,
            iso(dtstart),
            iso(until) if until else None,
            1 if active else 0,
            notes,
            str(Weekend(weekend)),
        ),
    )
    assert cur.lastrowid is not None
    fid = cur.lastrowid
    if tags:
        set_flow_tags(conn, fid, tags)
    return get_flow(conn, fid)


_UNSET = object()


def update_flow(
    conn: sqlite3.Connection,
    flow_id: int,
    *,
    name: Any = _UNSET,
    kind: Any = _UNSET,
    amount_cents: Any = _UNSET,
    rrule: Any = _UNSET,
    dtstart: Any = _UNSET,
    until: Any = _UNSET,
    active: Any = _UNSET,
    notes: Any = _UNSET,
    weekend: Any = _UNSET,
) -> Flow:
    current = get_flow(conn, flow_id)
    sets: list[str] = []
    args: list = []
    if name is not _UNSET:
        n = check_name(name)
        dup = conn.execute(
            "SELECT id FROM flow WHERE name = ? AND id <> ?", (n, flow_id)
        ).fetchone()
        if dup:
            raise CashError(f"a flow named {n!r} already exists", "duplicate_flow")
        sets.append("name = ?")
        args.append(n)
    if kind is not _UNSET:
        if current.debt is not None and Kind(kind) != Kind.EXPENSE:
            raise CashError(
                "a flow with a debt record must remain an expense", "debt_requires_expense"
            )
        sets.append("kind = ?")
        args.append(str(Kind(kind)))
    if amount_cents is not _UNSET:
        sets.append("amount_cents = ?")
        args.append(int(amount_cents))
    if rrule is not _UNSET:
        sets.append("rrule = ?")
        args.append(validate_rrule(rrule) if rrule is not None else None)
    new_dtstart = current.dtstart if dtstart is _UNSET else dtstart
    new_until = current.until if until is _UNSET else until
    if new_until is not None and new_until < new_dtstart:
        raise CashError("until must not be before dtstart", "invalid_date")
    if dtstart is not _UNSET:
        sets.append("dtstart = ?")
        args.append(iso(dtstart))
    if until is not _UNSET:
        sets.append("until = ?")
        args.append(iso(until) if until else None)
    if active is not _UNSET:
        sets.append("active = ?")
        args.append(1 if active else 0)
    if notes is not _UNSET:
        sets.append("notes = ?")
        args.append(notes)
    if weekend is not _UNSET:
        sets.append("weekend = ?")
        args.append(str(Weekend(weekend)))
    if sets:
        args.append(flow_id)
        conn.execute(f"UPDATE flow SET {', '.join(sets)} WHERE id = ?", args)
    return get_flow(conn, flow_id)


def remove_flow(conn: sqlite3.Connection, flow_id: int) -> None:
    cur = conn.execute("DELETE FROM flow WHERE id = ?", (flow_id,))
    if cur.rowcount == 0:
        raise CashError(f"no flow with id {flow_id}", "unknown_flow")
    prune_unused_tags(conn)


# ---- debts --------------------------------------------------------------------------------


def set_debt(
    conn: sqlite3.Connection,
    flow_id: int,
    *,
    balance_cents: int,
    balance_as_of: date,
    annual_rate: Decimal,
    compounding: Compounding,
    day_count: DayCount = DayCount.ACT_365,
    capitalize_interest: bool | None = None,
    payment_mode: PaymentMode = PaymentMode.FIXED,
    payment_pct: Decimal | None = None,
    original_principal_cents: int | None = None,
    posting_day: int | None = None,
) -> Debt:
    flow = get_flow(conn, flow_id)
    if posting_day is None:
        posting_day = balance_as_of.day
    if not 1 <= posting_day <= 31:
        raise CashError("--posting-day must be between 1 and 31", "invalid_debt")
    if flow.kind != Kind.EXPENSE:
        raise CashError("a debt can only attach to an expense flow", "debt_requires_expense")
    if capitalize_interest is None:
        capitalize_interest = compounding != Compounding.SIMPLE
    if compounding == Compounding.SIMPLE and capitalize_interest:
        raise CashError(
            "simple interest never capitalizes; "
            "drop --capitalize or choose daily/monthly/continuous",
            "invalid_debt",
        )
    if (payment_mode == PaymentMode.PERCENT_OF_BALANCE) != (payment_pct is not None):
        raise CashError(
            "--payment-pct is required with (and only with) --payment-mode percent_of_balance",
            "invalid_debt",
        )
    conn.execute(
        "INSERT INTO debt(flow_id, original_principal_cents, balance_cents, balance_as_of, "
        "annual_rate, "
        "compounding, day_count, capitalize_interest, payment_mode, payment_pct, posting_day) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(flow_id) DO UPDATE SET "
        "original_principal_cents = excluded.original_principal_cents, "
        "balance_cents = excluded.balance_cents, balance_as_of = excluded.balance_as_of, "
        "annual_rate = excluded.annual_rate, compounding = excluded.compounding, "
        "day_count = excluded.day_count, capitalize_interest = excluded.capitalize_interest, "
        "payment_mode = excluded.payment_mode, payment_pct = excluded.payment_pct, "
        "posting_day = excluded.posting_day",
        (
            flow_id,
            original_principal_cents,
            balance_cents,
            iso(balance_as_of),
            rate_to_str(annual_rate),
            str(compounding),
            str(day_count),
            1 if capitalize_interest else 0,
            str(payment_mode),
            rate_to_str(payment_pct) if payment_pct is not None else None,
            posting_day,
        ),
    )
    debt = _load_debt(conn, flow_id)
    assert debt is not None
    return debt


def unset_debt(conn: sqlite3.Connection, flow_id: int) -> None:
    cur = conn.execute("DELETE FROM debt WHERE flow_id = ?", (flow_id,))
    if cur.rowcount == 0:
        raise CashError(f"flow {flow_id} has no debt record", "no_debt")


def add_event(
    conn: sqlite3.Connection,
    flow_id: int,
    *,
    type: EventType,
    date: date,
    rate: Decimal | None = None,
    amount_cents: int | None = None,
    notes: str | None = None,
    event_id: int | None = None,
) -> DebtEvent:
    if conn.execute("SELECT 1 FROM debt WHERE flow_id = ?", (flow_id,)).fetchone() is None:
        raise CashError(
            f"flow {flow_id} has no debt record; set one up with `bdbd debt set` first", "no_debt"
        )
    if type == EventType.SETTLE:
        raise CashError(
            "settle is a what-if only (use --settle or scenario debt_events); once the sale is "
            "real, record a payoff event or remove the flow",
            "invalid_event",
        )
    validate_event_fields(type, rate, amount_cents)
    cur = conn.execute(
        "INSERT INTO debt_event(id, flow_id, date, type, rate, amount_cents, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            flow_id,
            iso(date),
            str(type),
            rate_to_str(rate) if rate is not None else None,
            amount_cents,
            notes,
        ),
    )
    r = conn.execute("SELECT * FROM debt_event WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_to_event(r)


def validate_event_fields(type: EventType, rate: Decimal | None, amount_cents: int | None) -> None:
    if type == EventType.RATE_CHANGE:
        if rate is None or amount_cents is not None:
            raise CashError("rate_change needs --rate and no --amount", "invalid_event")
    elif type == EventType.PAYOFF:
        if rate is not None or amount_cents is not None:
            raise CashError("payoff takes neither --rate nor --amount", "invalid_event")
    else:
        if rate is not None or amount_cents is None:
            raise CashError(f"{type} needs --amount and no --rate", "invalid_event")
        if type == EventType.SETTLE and amount_cents < 0:
            raise CashError("settle proceeds must not be negative", "invalid_event")
        if type == EventType.EXTRA_PAYMENT and amount_cents <= 0:
            raise CashError("extra_payment amount must be positive", "invalid_event")
        if type == EventType.PAYMENT_CHANGE and amount_cents < 0:
            raise CashError("payment_change amount must not be negative", "invalid_event")


def remove_event(conn: sqlite3.Connection, event_id: int) -> None:
    cur = conn.execute("DELETE FROM debt_event WHERE id = ?", (event_id,))
    if cur.rowcount == 0:
        raise CashError(f"no debt event with id {event_id}", "unknown_event")


# ---- config -------------------------------------------------------------------------------

CONFIG_KEYS = {
    "weekly_spend": "everyday spending per week (groceries, incidentals), prorated daily in every "
    "projection unless --weekly-spend overrides it (money, e.g. 200)",
}


def config_get(conn: sqlite3.Connection, key: str) -> str | None:
    r = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else None


def config_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    if key not in CONFIG_KEYS:
        raise CashError(
            f"unknown config key {key!r}; known: {sorted(CONFIG_KEYS)}", "unknown_config"
        )
    conn.execute(
        "INSERT INTO config(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def config_unset(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM config WHERE key = ?", (key,))


def config_all(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM config ORDER BY key")
    }


# ---- export / import ----------------------------------------------------------------------

EXPORT_FORMAT = 1
EXPORT_KEY = "bdbd_export"


def export_all(conn: sqlite3.Connection, schema_version: int) -> dict:
    from bdbd.core.money import cents_to_str

    flows = []
    for f in list_flows(conn, include_inactive=True):
        item: dict = {
            "id": f.id,
            "name": f.name,
            "kind": str(f.kind),
            "amount": cents_to_str(f.amount_cents),
            "rrule": f.rrule,
            "dtstart": iso(f.dtstart),
            "until": iso(f.until) if f.until else None,
            "active": f.active,
            "notes": f.notes,
            "tags": list(f.tags),
            "weekend": str(f.weekend),
            "debt": None,
        }
        if f.debt:
            d = f.debt
            item["debt"] = {
                "balance": cents_to_str(d.balance_cents),
                "balance_as_of": iso(d.balance_as_of),
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
                "events": [
                    {
                        "id": e.id,
                        "date": iso(e.date),
                        "type": str(e.type),
                        "rate": rate_to_str(e.rate) if e.rate is not None else None,
                        "amount": (
                            cents_to_str(e.amount_cents) if e.amount_cents is not None else None
                        ),
                        "notes": e.notes,
                    }
                    for e in d.events
                ],
            }
        flows.append(item)
    return {
        EXPORT_KEY: EXPORT_FORMAT,
        "schema_version": schema_version,
        "config": config_all(conn),
        "flows": flows,
    }


def _problem(exc: Exception) -> str:
    """What was wrong with one item of a backup, in a few words."""
    if isinstance(exc, KeyError):
        return f"it has no {exc.args[0]!r}"
    if isinstance(exc, CashError):
        return exc.message
    return str(exc) or type(exc).__name__


def _items(payload: dict, key: str) -> list[dict]:
    value = payload.get(key) or []
    if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
        raise CashError(f"its {key!r} should be a list of objects", "invalid_import")
    return value


def import_rows(conn: sqlite3.Connection, payload: dict, *, keep_ids: bool) -> int:
    """Replace every flow, debt, tag and setting with a backup's; returns the number of flows.

    Runs no transaction of its own (bdbd.backup wraps it in one, with the balances). A bad
    item raises CashError('invalid_import') naming it, e.g. "flow 3 (Rent): it has no 'kind'".
    """
    from bdbd.core.money import parse_amount

    if payload.get(EXPORT_KEY) != EXPORT_FORMAT:
        raise CashError(
            f"not a bdbd export file (missing {EXPORT_KEY}: {EXPORT_FORMAT})", "invalid_import"
        )
    config = payload.get("config") or {}
    if not isinstance(config, dict):
        raise CashError("its 'config' should be an object", "invalid_import")
    flows = _items(payload, "flows")
    conn.execute("DELETE FROM flow")
    conn.execute("DELETE FROM tag")
    conn.execute("DELETE FROM config")
    for k, v in config.items():
        config_set(conn, k, str(v))
    for i, item in enumerate(flows, 1):
        label = f" ({item['name']})" if isinstance(item.get("name"), str) else ""
        try:
            f = add_flow(
                conn,
                name=item["name"],
                kind=Kind(item["kind"]),
                amount_cents=parse_amount(item["amount"]),
                rrule=item.get("rrule"),
                dtstart=parse_date(item["dtstart"]),
                until=parse_date(item["until"]) if item.get("until") else None,
                tags=item.get("tags") or [],
                notes=item.get("notes"),
                active=bool(item.get("active", True)),
                weekend=Weekend(item.get("weekend", "none")),
                flow_id=item.get("id") if keep_ids else None,
            )
            d = item.get("debt")
            if d:
                set_debt(
                    conn,
                    int(f.id),
                    balance_cents=parse_amount(d["balance"]),
                    balance_as_of=parse_date(d["balance_as_of"]),
                    annual_rate=parse_rate(d["annual_rate"]),
                    compounding=Compounding(d["compounding"]),
                    day_count=DayCount(d.get("day_count", "actual/365")),
                    capitalize_interest=d.get("capitalize_interest"),
                    payment_mode=PaymentMode(d.get("payment_mode", "fixed")),
                    payment_pct=parse_rate(d["payment_pct"]) if d.get("payment_pct") else None,
                    original_principal_cents=(
                        parse_amount(d["original_principal"])
                        if d.get("original_principal")
                        else None
                    ),
                    posting_day=d.get("posting_day"),
                )
                for e in d.get("events", []):
                    add_event(
                        conn,
                        int(f.id),
                        type=EventType(e["type"]),
                        date=parse_date(e["date"]),
                        rate=parse_rate(e["rate"]) if e.get("rate") is not None else None,
                        amount_cents=(
                            parse_amount(e["amount"], allow_negative=True)
                            if e.get("amount") is not None
                            else None
                        ),
                        notes=e.get("notes"),
                        event_id=e.get("id") if keep_ids else None,
                    )
        except (CashError, KeyError, TypeError, ValueError, AttributeError) as exc:
            raise CashError(f"flow {i}{label}: {_problem(exc)}", "invalid_import") from exc
    return len(flows)
