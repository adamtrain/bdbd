"""The remembered cash balance.

The table lives outside the numbered migrations: it is created the first time a balance is
recorded, and adding it never changes the schema version, so a budget file stays readable by
tools that only know the numbered schema.

One entry per date; recording a balance for a date that already has one replaces it. Older entries
are kept so a new balance can be compared with what the budget expected since the previous one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from bdbd.core.dates import iso, parse_date

TABLE = "bdbd_balance"

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
  id           INTEGER PRIMARY KEY,
  as_of        TEXT    NOT NULL UNIQUE
                       CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  amount_cents INTEGER NOT NULL,
  recorded_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
)
"""


@dataclass(frozen=True)
class Balance:
    as_of: date
    amount_cents: int
    recorded_at: str


def _exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
    ).fetchone()
    return row is not None


def _row(r: sqlite3.Row) -> Balance:
    return Balance(parse_date(r["as_of"]), int(r["amount_cents"]), r["recorded_at"])


def history(conn: sqlite3.Connection) -> list[Balance]:
    """Every remembered balance, newest first."""
    if not _exists(conn):
        return []
    rows = conn.execute(f"SELECT * FROM {TABLE} ORDER BY as_of DESC").fetchall()
    return [_row(r) for r in rows]


def latest(conn: sqlite3.Connection, *, on_or_before: date | None = None) -> Balance | None:
    if not _exists(conn):
        return None
    if on_or_before is None:
        r = conn.execute(f"SELECT * FROM {TABLE} ORDER BY as_of DESC LIMIT 1").fetchone()
    else:
        r = conn.execute(
            f"SELECT * FROM {TABLE} WHERE as_of <= ? ORDER BY as_of DESC LIMIT 1",
            (iso(on_or_before),),
        ).fetchone()
    return _row(r) if r else None


def record(conn: sqlite3.Connection, amount_cents: int, as_of: date) -> Balance:
    conn.execute(DDL)
    conn.execute(
        f"INSERT INTO {TABLE}(as_of, amount_cents) VALUES (?, ?) "
        "ON CONFLICT(as_of) DO UPDATE SET amount_cents = excluded.amount_cents, "
        "recorded_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')",
        (iso(as_of), amount_cents),
    )
    r = conn.execute(f"SELECT * FROM {TABLE} WHERE as_of = ?", (iso(as_of),)).fetchone()
    return _row(r)


def forget(conn: sqlite3.Connection, as_of: date | None = None) -> int:
    """Forget one date's balance, or every balance when `as_of` is None."""
    if not _exists(conn):
        return 0
    if as_of is None:
        return conn.execute(f"DELETE FROM {TABLE}").rowcount
    return conn.execute(f"DELETE FROM {TABLE} WHERE as_of = ?", (iso(as_of),)).rowcount


def prune(conn: sqlite3.Connection, before: date) -> list[Balance]:
    """Drop balances dated before `before`, except the newest one overall. Returns what went."""
    if not _exists(conn):
        return []
    newest = latest(conn)
    gone = [b for b in history(conn) if b.as_of < before and b != newest]
    for b in gone:
        conn.execute(f"DELETE FROM {TABLE} WHERE as_of = ?", (iso(b.as_of),))
    return gone


def replace_all(conn: sqlite3.Connection, entries: list[tuple[date, int]]) -> None:
    """Restore balances from a backup (used by import)."""
    conn.execute(DDL)
    conn.execute(f"DELETE FROM {TABLE}")
    for as_of, cents in entries:
        conn.execute(f"INSERT INTO {TABLE}(as_of, amount_cents) VALUES (?, ?)", (iso(as_of), cents))
