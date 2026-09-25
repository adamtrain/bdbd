"""Opening a budget and asking it questions: the glue between the engine and the views."""

from __future__ import annotations

import difflib
import re
import sqlite3
from dataclasses import dataclass, field, fields, replace
from datetime import date, timedelta
from pathlib import Path

from bdbd.core import balance as balances
from bdbd.core import db, engine, repo
from bdbd.core.cleanup import run_cleanup
from bdbd.core.dates import month_start
from bdbd.core.dates import today as today_fn
from bdbd.core.errors import CashError
from bdbd.core.models import EffectiveModel, Flow
from bdbd.core.money import parse_amount
from bdbd.core.scenario import (
    build_effective_model,
    has_placeholder,
    load_scenario,
    resolve_placeholders,
)
from bdbd.core.shortcuts import merge_scenarios, scenario_from_flags
from bdbd.words import fmt_month, parse_day


class Problem(CashError):
    """A CashError with a hint for the person at the keyboard."""

    def __init__(self, message: str, code: str = "error", hint: str | None = None):
        super().__init__(message, code)
        self.hint = hint


# ── What-ifs ──────────────────────────────────────────────────────────────────

_DATED = {  # flag -> whether its @DATE part is required
    "stop": True,
    "stop_tag": True,
    "payoff": True,
    "settle": True,
    "extra_payment": True,
    "set_payment": True,
    "rate_change": True,
    "add_income": True,
    "add_expense": True,
    "set_amount": False,
}
_PLACEHOLDER = re.compile(r"^\?(?:[+-]\d+)?$")


@dataclass
class WhatIf:
    """What-if shortcut flags and scenario JSON, layered over the budget. Never stored."""

    disable: list[str] = field(default_factory=list)
    disable_tag: list[str] = field(default_factory=list)
    enable: list[str] = field(default_factory=list)
    stop: list[str] = field(default_factory=list)
    stop_tag: list[str] = field(default_factory=list)
    payoff: list[str] = field(default_factory=list)
    settle: list[str] = field(default_factory=list)
    extra_payment: list[str] = field(default_factory=list)
    set_payment: list[str] = field(default_factory=list)
    rate_change: list[str] = field(default_factory=list)
    add_income: list[str] = field(default_factory=list)
    add_expense: list[str] = field(default_factory=list)
    set_amount: list[str] = field(default_factory=list)
    scenario: str | None = None
    scenario_json: str | None = None
    on: str | None = None

    @property
    def active(self) -> bool:
        return any(getattr(self, f.name) for f in fields(self) if f.name != "on")

    def _normalized(self, today: date, base: date) -> WhatIf:
        """The same flags with human dates ('nov 13', 'fri') rewritten as ISO dates."""
        changes: dict[str, list[str]] = {}
        for name in _DATED:
            out = []
            for spec in getattr(self, name):
                head, at, when = spec.rpartition("@")
                if at and not _PLACEHOLDER.match(when.strip()):
                    when = parse_day(when, today=today, base=base).isoformat()
                out.append(f"{head}@{when}" if at else spec)
            changes[name] = out
        return replace(self, **changes)

    def spec(self, *, today: date, base: date, allow_placeholder: bool = False) -> dict | None:
        """The scenario these flags describe, with any '?' date pinned by --on."""
        flags = self._normalized(today, base)
        loaded = load_scenario(self.scenario, self.scenario_json)
        spec = merge_scenarios(loaded, scenario_from_flags(flags))
        if self.on:
            if not has_placeholder(spec):
                raise Problem("--on was given, but no what-if is dated '?'", "usage")
            return resolve_placeholders(spec, parse_day(self.on, today=today, base=base))
        if has_placeholder(spec) and not allow_placeholder:
            raise Problem(
                "a what-if is dated '?'",
                "usage",
                hint="Pin it with [bold]--on DATE[/], or let [bold]bdbd earliest[/] find the date.",
            )
        return spec


# ── Balances ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Start:
    """The balance a projection starts from (cash on hand before `as_of`'s scheduled items)."""

    cents: int
    as_of: date
    source: str  # "given" | "recorded" | "carried" | "none"
    recorded: balances.Balance | None = None

    @property
    def known(self) -> bool:
        return self.source != "none"


@dataclass(frozen=True)
class Recorded:
    """The outcome of `bdbd balance AMOUNT`."""

    balance: balances.Balance
    typed_cents: int
    posted: list[engine.LedgerEntry]  # today's items already in the typed amount
    previous: balances.Balance | None
    expected_cents: int | None  # what the budget predicted from the previous balance


# ── The budget ────────────────────────────────────────────────────────────────


@dataclass
class Budget:
    conn: sqlite3.Connection
    path: Path
    today: date
    tidied: list[str] = field(default_factory=list)

    @classmethod
    def open(cls, path: Path, *, tidy: bool = True) -> Budget:
        try:
            conn = db.connect(path)
        except CashError as exc:
            if exc.code == "db_not_found":
                raise Problem(
                    str(exc),
                    exc.code,
                    hint="Start one with [bold]bdbd init[/], or point [bold]BDBD_DB[/] or "
                    "[bold]--db[/] at an existing budget.",
                ) from exc
            raise
        today = today_fn()
        budget = cls(conn, path, today)
        if tidy:
            budget.tidied = budget.tidy()
        return budget

    def close(self) -> None:
        self.conn.close()

    def tidy(self) -> list[str]:
        """The prior-month cleanup, plus forgetting balances recorded before this month."""
        actions = run_cleanup(self.conn, self.today)
        gone = balances.prune(self.conn, month_start(self.today))
        if gone:
            actions.append(
                f"forgot {len(gone)} balance{'s' if len(gone) != 1 else ''} recorded before "
                f"{fmt_month(month_start(self.today))}"
            )
        return actions

    # flows ---------------------------------------------------------------------

    def flows(self, *, include_inactive: bool = True) -> list[Flow]:
        return repo.list_flows(self.conn, include_inactive=include_inactive)

    def find(self, ident: str) -> Flow:
        try:
            return repo.resolve_flow(self.conn, ident)
        except CashError as exc:
            if exc.code != "unknown_flow":
                raise
            names = [f.name for f in self.flows()]
            close = difflib.get_close_matches(ident.lower(), [n.lower() for n in names], 3, 0.5)
            matches = [n for n in names if n.lower() in close]
            contains = [n for n in names if ident.lower() in n.lower() and n not in matches]
            options = (matches + contains)[:3]
            hint = None
            if options:
                hint = "Did you mean " + " or ".join(f"[bold]{n}[/]" for n in options) + "?"
            elif names:
                hint = "See them all with [bold]bdbd ls[/]."
            raise Problem(f"there's no flow called {ident!r}", "unknown_flow", hint) from exc

    def weekly_spend(self) -> int:
        stored = repo.config_get(self.conn, "weekly_spend")
        return parse_amount(stored) if stored else 0

    def model(
        self,
        spec: dict | None = None,
        *,
        as_of: date | None = None,
        include_inactive: bool = False,
    ) -> EffectiveModel:
        return build_effective_model(
            self.flows(), spec, as_of or self.today, include_inactive=include_inactive
        )

    def weekly_for(self, model: EffectiveModel, override: int | None = None) -> int:
        if override is not None:
            return override
        if model.weekly_spend_cents is not None:
            return model.weekly_spend_cents
        return self.weekly_spend()

    # balances ------------------------------------------------------------------

    def recorded(self, on_or_before: date | None = None) -> balances.Balance | None:
        return balances.latest(self.conn, on_or_before=on_or_before)

    def carry(self, cents: int, since: date, until: date) -> int:
        """Start-of-day balance on `until`, given the start-of-day balance on `since`."""
        if until <= since:
            return cents
        res = engine.run(
            self.model(as_of=since),
            as_of=since,
            until=until,
            starting_balance_cents=cents,
            weekly_spend_cents=self.weekly_spend(),
        )
        due = sum(e.delta_cents for e in res.ledger if e.date == until and e.kind != "lifestyle")
        return res.ending_balance_cents - due

    def start(self, as_of: date | None = None, *, given: int | None = None) -> Start:
        as_of = as_of or self.today
        if given is not None:
            return Start(given, as_of, "given")
        rec = self.recorded(on_or_before=as_of)
        if rec is None:
            return Start(0, as_of, "none")
        if rec.as_of == as_of:
            return Start(rec.amount_cents, as_of, "recorded", rec)
        return Start(self.carry(rec.amount_cents, rec.as_of, as_of), as_of, "carried", rec)

    def items_on(self, day: date) -> list[engine.LedgerEntry]:
        """Everything scheduled on `day` (no lifestyle spend)."""
        res = engine.run(self.model(as_of=day), as_of=day, until=day)
        return [e for e in res.ledger if e.kind != "lifestyle"]

    def record_balance(self, cents: int, as_of: date, *, posted: bool) -> Recorded:
        if as_of > self.today:
            raise Problem("a balance can't be recorded for a future date", "invalid_date")
        items = self.items_on(as_of) if posted else []
        stored = cents - sum(e.delta_cents for e in items)
        previous = balances.latest(self.conn, on_or_before=as_of - timedelta(days=1))
        expected = self.carry(previous.amount_cents, previous.as_of, as_of) if previous else None
        saved = balances.record(self.conn, stored, as_of)
        return Recorded(saved, cents, items, previous, expected)
