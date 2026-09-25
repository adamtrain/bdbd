"""Shared fixtures. "Today" is pinned to 2026-09-16, the date the hand-checked numbers use."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from bdbd import cli
from bdbd.core import db, repo
from bdbd.core.models import Compounding, Kind, Weekend

TODAY = date(2026, 9, 16)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setenv("BDBD_TODAY", TODAY.isoformat())
    monkeypatch.setenv("COLUMNS", "100")
    monkeypatch.setenv("NO_COLOR", "1")
    for var in ("BDBD_DB", "BDBD_AGENT", "BDBD_CURRENCY"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "budget.sqlite"


@pytest.fixture
def conn(db_path) -> Iterator[sqlite3.Connection]:
    c = db.connect(db_path, create=True)
    yield c
    c.close()


def add_sample_budget(conn: sqlite3.Connection) -> None:
    """Salary / Rent / Car loan / Car insurance: the budget the reference numbers use."""
    repo.add_flow(
        conn,
        name="Salary",
        kind=Kind.INCOME,
        amount_cents=250000,
        rrule="FREQ=WEEKLY;INTERVAL=2",
        dtstart=date(2026, 9, 18),
        tags=["job"],
    )
    repo.add_flow(
        conn,
        name="Rent",
        kind=Kind.EXPENSE,
        amount_cents=300000,
        rrule="FREQ=MONTHLY;BYMONTHDAY=1",
        dtstart=date(2026, 10, 1),
        tags=["housing"],
    )
    loan = repo.add_flow(
        conn,
        name="Car loan",
        kind=Kind.EXPENSE,
        amount_cents=38666,
        rrule="FREQ=MONTHLY;BYMONTHDAY=1",
        dtstart=date(2026, 11, 1),
        tags=["car", "loan"],
    )
    repo.set_debt(
        conn,
        int(loan.id),
        balance_cents=2000000,
        balance_as_of=date(2026, 10, 1),
        annual_rate=Decimal("0.06"),
        compounding=Compounding.MONTHLY,
    )
    repo.add_flow(
        conn,
        name="Car insurance",
        kind=Kind.EXPENSE,
        amount_cents=12000,
        rrule="FREQ=MONTHLY;BYMONTHDAY=15",
        dtstart=date(2026, 10, 15),
        tags=["car"],
        weekend=Weekend.NONE,
    )


@pytest.fixture
def budget(conn) -> sqlite3.Connection:
    add_sample_budget(conn)
    return conn


# ── Running the real command ──────────────────────────────────────────────────

Run = Callable[..., Any]


def _invoke(argv: list[str]) -> int:
    try:
        cli.main(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


@pytest.fixture
def agent(db_path, capsys) -> Run:
    """Run bdbd in agent mode against a fresh budget; returns the parsed envelope."""

    def run(*argv: str, ok: bool = True) -> dict:
        code = _invoke(["--agent", "--db", str(db_path), *argv])
        out = capsys.readouterr().out
        env = json.loads(out)
        if ok:
            assert env["ok"], env
            assert code == 0
        else:
            assert not env["ok"], env
            assert code in (1, 2)
        return env

    run("init")
    return run


@pytest.fixture
def human(db_path, capsys) -> Run:
    """Run bdbd the way a person does; returns (exit code, everything printed)."""

    def run(*argv: str) -> tuple[int, str]:
        code = _invoke(["--db", str(db_path), *argv])
        captured = capsys.readouterr()
        return code, captured.out + captured.err

    return run


@pytest.fixture
def reference(agent) -> Run:
    """The reference budget, entered through the CLI in plain English."""
    agent("add", "Salary", "2500", "--income", "--when", "every 2 weeks", "--from",
          "2026-09-18", "--tag", "job")  # fmt: skip
    agent("add", "Rent", "3000", "monthly", "on", "the", "1st", "--from", "2026-10-01",
          "--tag", "housing")  # fmt: skip
    agent("add", "Car loan", "386.66", "monthly on the 1st", "--from", "2026-11-01",
          "--tag", "car,loan")  # fmt: skip
    agent("debt", "set", "Car loan", "--balance", "20000", "--as-of", "2026-10-01",
          "--rate", "6%", "--compounding", "monthly")  # fmt: skip
    agent("add", "Car insurance", "120", "monthly on the 15th", "--from", "2026-10-15",
          "--tag", "car")  # fmt: skip
    return agent
