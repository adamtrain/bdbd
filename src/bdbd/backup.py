"""Backups: the whole budget as one JSON file, and restoring one.

`bdbd export`/`bdbd import` and the app's settings share this, so a backup made either way
restores either way. A backup is checked in full (by restoring it into a scratch database)
before anything is written, and restoring replaces flows, settings and balances in one
transaction: all of it or none of it.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from bdbd.core import balance as balances
from bdbd.core import db, repo
from bdbd.core.errors import CashError
from bdbd.core.money import cents_to_str, parse_amount


def payload(conn: sqlite3.Connection, today: date) -> dict:
    """The budget as a backup: every flow with its debt, the settings and the balances."""
    data = repo.export_all(conn, db.current_version(conn))
    data["balances"] = [
        {"as_of": h.as_of.isoformat(), "balance": cents_to_str(h.amount_cents)}
        for h in balances.history(conn)
    ]
    data["exported_at"] = today.isoformat()
    return data


def is_budget(path: Path, budget: Path) -> bool:
    """Whether `path` is the budget file itself (or one of sqlite's files beside it)."""
    budget = budget.expanduser().resolve()
    target = path.expanduser().resolve()
    if target in {
        budget,
        *(budget.with_name(budget.name + s) for s in ("-wal", "-shm", "-journal")),
    }:
        return True
    try:
        return target.exists() and budget.exists() and target.samefile(budget)
    except OSError:
        return False


def write(conn: sqlite3.Connection, path: Path, *, budget: Path, today: date) -> int:
    """Save a backup at `path`, never over the budget itself; returns the number of flows."""
    path = path.expanduser()
    if is_budget(path, budget):
        raise CashError(
            f"{path} is your budget file; save the backup under another name", "file_error"
        )
    if path.is_dir():
        raise CashError(f"{path} is a folder; add a file name", "file_error")
    if not path.parent.is_dir():
        raise CashError(f"there's no folder at {path.parent}", "file_not_found")
    data = payload(conn, today)
    tmp: Path | None = None
    try:  # write beside it, then swap it in, so a failed write never leaves half a file
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        tmp = Path(name)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data, indent=2) + "\n")
        tmp.replace(path)
    except OSError as exc:
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink()
        raise CashError(f"couldn't write {path}: {exc.strerror or exc}", "file_error") from exc
    return len(data["flows"])


def read(path: Path) -> dict:
    """The backup in a file: what `bdbd export -o` writes, or the envelope `bdbd export` prints."""
    path = path.expanduser()
    if not path.is_file():
        raise CashError(f"there's no file at {path}", "file_not_found")
    try:
        data = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError) as exc:
        raise CashError(f"couldn't read {path.name} as text", "invalid_import") from exc
    except json.JSONDecodeError as exc:
        raise CashError(
            f"{path.name} isn't valid JSON ({exc.msg}, line {exc.lineno})", "invalid_import"
        ) from exc
    if isinstance(data, dict) and data.get("ok") is True and isinstance(data.get("data"), dict):
        data = data["data"]  # `bdbd export > file` saves the whole envelope
    if not isinstance(data, dict) or data.get(repo.EXPORT_KEY) != repo.EXPORT_FORMAT:
        raise CashError(f"{path.name} isn't a bdbd backup", "invalid_import")
    return data


@dataclass(frozen=True)
class Contents:
    """What a backup holds, once it has been checked."""

    flows: int
    balances: int
    exported_at: date | None


def check(data: dict) -> Contents:
    """Check every part of a backup by restoring it into a scratch database."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        db.migrate(conn)
        n = _restore(conn, data, keep_ids=True)
        held = len(balances.history(conn))
    finally:
        conn.close()
    try:
        exported = date.fromisoformat(str(data.get("exported_at")))
    except ValueError:
        exported = None
    return Contents(n, held, exported)


def held(conn: sqlite3.Connection) -> list[str]:
    """What restoring a backup would replace, in words ('15 flows', 'your balances', …)."""
    flows = conn.execute("SELECT count(*) FROM flow").fetchone()[0]
    parts = [f"{flows} flow{'s' if flows != 1 else ''}"] if flows else []
    if balances.history(conn):
        parts.append("your balances")
    if repo.config_all(conn):
        parts.append("your settings")
    return parts


def restore(conn: sqlite3.Connection, data: dict, *, replace: bool) -> int:
    """Make the budget the backup: flows, settings and balances together, or nothing.

    Without `replace`, only a budget with nothing in it can be restored into.
    """
    check(data)  # a bad backup never touches the budget
    there = held(conn)
    if there and not replace:
        raise CashError(
            f"the budget already has {', '.join(there)}; pass --replace to overwrite it",
            "db_not_empty",
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        n = _restore(conn, data, keep_ids=True)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return n


def _restore(conn: sqlite3.Connection, data: dict, *, keep_ids: bool) -> int:
    n = repo.import_rows(conn, data, keep_ids=keep_ids)
    raw = data.get("balances") or []
    if not isinstance(raw, list):
        raise CashError("its 'balances' should be a list", "invalid_import")
    entries = []
    for i, x in enumerate(raw, 1):
        try:
            entries.append(
                (date.fromisoformat(x["as_of"]), parse_amount(x["balance"], allow_negative=True))
            )
        except (CashError, KeyError, TypeError, ValueError) as exc:
            what = f"it has no {exc.args[0]!r}" if isinstance(exc, KeyError) else str(exc)
            raise CashError(f"balance {i}: {what}", "invalid_import") from exc
    balances.replace_all(conn, entries)
    return n
