"""The open budget as the app sees it: every question and every change goes through here.

One `Session` owns the sqlite connection (main thread only), the what-if sandbox and a version
number. Views never open connections or build models themselves; they ask the session, which
answers with the what-if applied while its lens is on. Answers are memoized until the version
moves, and the version moves on every change: stored data, the sandbox, a reload or a new day.

Heavy queries (plan, earliest) must not touch the connection from a worker thread: take a
snapshot on the main thread (`flows()`, `model()`, `spec()`, `start()`, `weekly()`) and hand
those to the worker.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar, cast

from bdbd import ask, backup
from bdbd.budget import Budget, Problem, Recorded, Start
from bdbd.core import balance as balances
from bdbd.core import db, engine, repo
from bdbd.core.dates import today as today_fn
from bdbd.core.errors import CashError
from bdbd.core.models import (
    Compounding,
    DayCount,
    Debt,
    DebtEvent,
    EffectiveModel,
    EventType,
    Flow,
    Kind,
    PaymentMode,
    Tag,
    Weekend,
)
from bdbd.core.money import cents_to_str
from bdbd.core.queries.debt_schedule import debt_schedule
from bdbd.core.queries.summary import summary as summary_query
from bdbd.core.scenario import build_effective_model, resolve_placeholders
from bdbd.tui.scenario import Change, Scenario
from bdbd.ui.theme import cents_of, money, pct
from bdbd.words import describe, fmt_date, fmt_month, join

T = TypeVar("T")


# ── What forms hand the session ───────────────────────────────────────────────


@dataclass(frozen=True)
class DebtTerms:
    """Loan details as a form produces them, already parsed (mirrors `bdbd debt set`)."""

    balance_cents: int
    balance_as_of: date
    annual_rate: Decimal
    compounding: Compounding
    day_count: DayCount = DayCount.ACT_365
    capitalize_interest: bool | None = None  # None: yes, except for simple interest
    payment_mode: PaymentMode = PaymentMode.FIXED
    payment_pct: Decimal | None = None
    original_principal_cents: int | None = None
    posting_day: int | None = None  # None: the day of the balance date

    @classmethod
    def of(cls, debt: Debt) -> DebtTerms:
        """The terms of a stored debt, to prefill a form."""
        return cls(
            balance_cents=debt.balance_cents,
            balance_as_of=debt.balance_as_of,
            annual_rate=debt.annual_rate,
            compounding=debt.compounding,
            day_count=debt.day_count,
            capitalize_interest=debt.capitalize_interest,
            payment_mode=debt.payment_mode,
            payment_pct=debt.payment_pct,
            original_principal_cents=debt.original_principal_cents,
            posting_day=debt.posting_day,
        )

    def kwargs(self) -> dict[str, Any]:
        """Keyword arguments for `repo.set_debt`."""
        return {
            "balance_cents": self.balance_cents,
            "balance_as_of": self.balance_as_of,
            "annual_rate": self.annual_rate,
            "compounding": self.compounding,
            "day_count": self.day_count,
            "capitalize_interest": self.capitalize_interest,
            "payment_mode": self.payment_mode,
            "payment_pct": self.payment_pct,
            "original_principal_cents": self.original_principal_cents,
            "posting_day": self.posting_day,
        }


@dataclass(frozen=True)
class FlowDraft:
    """A flow as the flow form produces it: every value parsed (mirrors `bdbd add`/`edit`).

    `debt` is the optional loan section. On `update_flow`, `debt=None` stops tracking an
    existing loan (the payment stays as a plain expense), so prefill edits with `FlowDraft.of`.
    """

    name: str
    kind: Kind
    amount_cents: int
    rrule: str | None  # None: once, on dtstart
    dtstart: date
    until: date | None = None
    weekend: Weekend = Weekend.NONE
    tags: tuple[str, ...] = ()
    notes: str | None = None
    active: bool = True
    debt: DebtTerms | None = None
    payday: bool = False  # an income that starts a pay cycle on each of its dates

    @classmethod
    def of(cls, flow: Flow) -> FlowDraft:
        """A stored flow as a draft (change it with `dataclasses.replace`)."""
        return cls(
            name=flow.name,
            kind=flow.kind,
            amount_cents=flow.amount_cents,
            rrule=flow.rrule,
            dtstart=flow.dtstart,
            until=flow.until,
            weekend=flow.weekend,
            tags=flow.tags,
            notes=flow.notes,
            active=flow.active,
            debt=DebtTerms.of(flow.debt) if flow.debt else None,
            payday=flow.payday,
        )


@dataclass(frozen=True)
class Done:
    """What a change did: one line for the toast, and the flow it touched (if any)."""

    message: str
    flow: Flow | None = None


@dataclass(frozen=True)
class Expected:
    """What the budget expected a day's balance to be, carried from the balance before it."""

    previous: balances.Balance
    cents: int  # start of that day, before its scheduled items


# ── The session ───────────────────────────────────────────────────────────────


class Session:
    """The open budget, the what-if sandbox, and every question and change the app makes."""

    def __init__(self, path: Path, *, tidy: bool = True) -> None:
        """Open the budget at `path` (raises Problem/CashError/sqlite3.Error like the CLI)."""
        self.path = path
        self.tidy = tidy
        self.scenario = Scenario()
        self.tidied: list[str] = []
        self.version = 1
        self._memo: dict[tuple, Any] = {}
        self._memo_version = 0
        self._data_version: int | None = None
        self._file_id: tuple[int, int] | None = None
        self.budget = self._open(tidy=tidy)

    @classmethod
    def create(cls, path: Path, *, tidy: bool = True) -> Session:
        """Make a new, empty budget file at `path` and open it."""
        db.connect(path, create=True).close()
        return cls(path, tidy=tidy)

    def close(self) -> None:
        self.budget.close()

    @property
    def conn(self) -> sqlite3.Connection:
        return self.budget.conn

    @property
    def today(self) -> date:
        return self.budget.today

    def pop_tidied(self) -> list[str]:
        """What the last open tidied up (for toasts), forgetting it once it's been shown."""
        actions, self.tidied = self.tidied, []
        return actions

    # memo --------------------------------------------------------------------------

    def cached(self, key: tuple, compute: Callable[[], T]) -> T:
        """`compute()` once per version and key. Callers must not mutate what comes back."""
        if self._memo_version != self.version:
            self._memo.clear()
            self._memo_version = self.version
        if key not in self._memo:
            self._memo[key] = compute()
        return cast(T, self._memo[key])

    def bump(self) -> None:
        """Mark everything stale: views redraw and memoized answers recompute."""
        self.version += 1

    # ── The what-if sandbox ───────────────────────────────────────────────────────

    @property
    def lens_on(self) -> bool:
        """Whether answers include the what-if: the lens is on, something is on, and it fits."""
        s = self.scenario
        return s.enabled and s.any and not s.unpinned and self.scenario_error is None

    @property
    def scenario_error(self) -> str | None:
        """Why the sandbox can't be applied to this budget (e.g. its flow was deleted)."""
        return self.cached(("scenario_error",), self._scenario_error)

    def _scenario_error(self) -> str | None:
        if not self.scenario.any:
            return None
        try:
            spec = self.spec(placeholder=self.scenario.unpinned)
            spec = resolve_placeholders(spec, self.today)
            build_effective_model(self.flows(), spec, self.today)
        except CashError as exc:
            return exc.message
        return None

    def spec(self, base: date | None = None, *, placeholder: bool = False) -> dict | None:
        """The sandbox's enabled changes as a scenario dict, whether or not the lens is on.

        None when nothing is on. `placeholder=True` keeps '?' dates unresolved (for earliest),
        even when a date is pinned; otherwise an unpinned '?' raises Problem, like the CLI.
        """
        if not self.scenario.any:
            return None

        def build() -> dict | None:
            whatif = self.scenario.whatif()
            if placeholder:
                whatif = replace(whatif, on=None)
            return whatif.spec(
                today=self.today, base=base or self.today, allow_placeholder=placeholder
            )

        return self.cached(("spec", base, placeholder), build)

    def model(
        self, as_of: date | None = None, *, baseline: bool = False, include_inactive: bool = False
    ) -> EffectiveModel:
        """What the engine runs: the stored flows, with the what-if when the lens is on."""
        spec = self.spec() if self._lensed(baseline) else None
        day = as_of or self.today
        return self.cached(
            ("model", day, spec is not None, include_inactive),
            lambda: build_effective_model(
                self.flows(), spec, day, include_inactive=include_inactive
            ),
        )

    def weekly(self, model: EffectiveModel | None = None) -> int:
        """Everyday spending per week (a what-if can change it)."""
        model = model or self.model()
        if model.weekly_spend_cents is not None:
            return model.weekly_spend_cents
        return self.stored_weekly()

    def stored_weekly(self) -> int:
        """Everyday spending per week as saved in the budget (0 when not set)."""
        return self.cached(("stored_weekly",), self.budget.weekly_spend)

    def start(self, as_of: date | None = None) -> Start:
        """The balance a projection starts from (never scenario-dependent)."""
        day = as_of or self.today
        return self.cached(("start", day), lambda: self.budget.start(day))

    def check_change(self, change: Change) -> str | None:
        """Why `change` can't apply to this budget, or None when it can (for live forms)."""
        try:
            whatif = Scenario([change]).whatif()
            spec = whatif.spec(today=self.today, base=self.today, allow_placeholder=True)
            build_effective_model(self.flows(), resolve_placeholders(spec, self.today), self.today)
        except CashError as exc:
            return exc.message
        return None

    def add_change(self, change: Change) -> None:
        """Add a change to the sandbox (every sandbox edit unpins '?' and bumps the version)."""
        self._edit_changes(lambda cs: [*cs, change])

    def replace_change(self, index: int, change: Change) -> None:
        self._edit_changes(lambda cs: [change if i == index else c for i, c in enumerate(cs)])

    def remove_change(self, index: int) -> None:
        self._edit_changes(lambda cs: [c for i, c in enumerate(cs) if i != index])

    def toggle_change(self, index: int) -> None:
        """Switch one change on or off."""
        self._edit_changes(
            lambda cs: [
                replace(c, enabled=not c.enabled) if i == index else c for i, c in enumerate(cs)
            ]
        )

    def clear_changes(self) -> None:
        """Empty the sandbox (changes and any tried plan)."""
        self.scenario.extra, self.scenario.extra_label = None, ""
        self.scenario.extra_enabled = True
        self._edit_changes(lambda cs: [])

    def set_extra(self, extra: dict | None, label: str = "") -> None:
        """Try raw scenario JSON (a payoff plan's debt_events) alongside the changes."""
        self.scenario.extra, self.scenario.extra_label = extra, label if extra else ""
        self.scenario.extra_enabled = True
        self.scenario.pinned = None
        self.bump()

    def toggle_extra(self) -> None:
        """Switch the tried plan off or back on (it stays in the sandbox)."""
        self.scenario.extra_enabled = not self.scenario.extra_enabled
        self.scenario.pinned = None
        self.bump()

    def set_lens(self, on: bool) -> None:
        """Turn the what-if lens on or off (nothing in the sandbox changes)."""
        if self.scenario.enabled != on:
            self.scenario.enabled = on
            self.bump()

    def pin(self, day: date | None) -> None:
        """Resolve '?' dates to `day` (from an earliest result), or unpin with None."""
        if self.scenario.pinned != day:
            self.scenario.pinned = day
            self.bump()

    def _lensed(self, baseline: bool) -> bool:
        return self.lens_on and not baseline

    def _edit_changes(self, edit: Callable[[list[Change]], list[Change]]) -> None:
        self.scenario.changes = edit(list(self.scenario.changes))
        self.scenario.pinned = None  # a pinned date was found for the old set of changes
        self.bump()

    # ── Questions ─────────────────────────────────────────────────────────────────
    # Plain data from ask/core, with the what-if applied unless baseline=True.

    def flows(self, *, include_inactive: bool = True) -> list[Flow]:
        """The stored flows (paused ones too by default); never the what-if."""
        every = self.cached(("flows",), self.budget.flows)
        return every if include_inactive else [f for f in every if f.active]

    def flow(self, flow_id: int) -> Flow:
        """One stored flow by id (raises CashError when it's gone)."""
        for f in self.flows():
            if f.id == flow_id:
                return f
        raise CashError(f"there's no flow with id {flow_id} any more", "unknown_flow")

    def tags(self) -> list[Tag]:
        return self.cached(("tags",), lambda: repo.list_tags(self.conn))

    def overview(self, *, baseline: bool = False) -> ask.Picture:
        """Where you stand: the numbers `bdbd overview` gives."""
        model = self.model(baseline=baseline)
        return self.cached(
            ("overview", self._lensed(baseline)), lambda: ask.overview(self.budget, model)
        )

    def coming_up(self, *, baseline: bool = False) -> list[engine.LedgerEntry]:
        """Scheduled items until the next income (at least ten days ahead)."""
        return ask.coming_up(self.overview(baseline=baseline), self.today)

    def pay_cycles(self, *, baseline: bool = False) -> ask.PayCycles:
        """Every pay cycle from the one under way through a year ahead (`bdbd paydays`)."""
        model = self.model(baseline=baseline)
        return self.cached(
            ("pay_cycles", self._lensed(baseline)),
            lambda: ask.pay_cycles(self.budget, model=model),
        )

    def listing(self) -> ask.Listing:
        """Every stored flow (paused too) with its next date and monthly cost; never what-if."""
        return self.cached(("listing",), lambda: ask.listing(self.budget, include_inactive=True))

    def details(self, flow: Flow) -> ask.Details:
        """A flow's upcoming dates, monthly cost and (for a debt) its payoff."""
        return self.cached(
            ("details", flow.id), lambda: ask.details(self.budget, flow, lst=self.listing())
        )

    def debts(self, *, baseline: bool = False) -> list[ask.DebtRow]:
        """Every active debt: owed today, rate, payment, payoff date, interest to go."""
        model = self.model(baseline=baseline)
        return self.cached(("debts", self._lensed(baseline)), lambda: ask.debts(self.budget, model))

    def interest_to_go(self, key: object, *, baseline: bool = False) -> int | None:
        """Interest still to pay on a debt from today until it's paid off: the one measure
        the app uses, so owed today + interest to go = everything still to pay. None when it's
        never paid off at its payment (or isn't a debt)."""
        row = next((r for r in self.debts(baseline=baseline) if r.key == key), None)
        return row.interest if row is not None else None

    def growth(self, key: object, *, baseline: bool = False) -> int | None:
        """How much more is owed on a debt a year from now (for one that's never paid off)."""

        def compute() -> int | None:
            model = self.model(baseline=baseline, include_inactive=True)
            try:
                data, _ = debt_schedule(
                    model, key, as_of=self.today, until=self.today + timedelta(days=365)
                )
            except CashError:
                return None
            return cents_of(data["balance_at_until"]) - cents_of(data["balance_at_as_of"])

        return self.cached(("growth", key, self._lensed(baseline)), compute)

    def summary(self, *, include_inactive: bool = False) -> dict:
        """The stored budget's steady monthly and yearly money (`bdbd summary [--all]`)."""

        def compute() -> dict:
            model = self.model(baseline=True, include_inactive=include_inactive)
            data, _ = summary_query(model, as_of=self.today, by="both")
            return data

        return self.cached(("summary", include_inactive), compute)

    def calendar(self, month: date) -> tuple[list[ask.Day], Start]:
        """Every day of a month with its items, and projected balances from today on."""
        model = self.model()
        return self.cached(
            ("calendar", month.replace(day=1), self.lens_on),
            lambda: ask.calendar(self.budget, month, model=model),
        )

    def window(self, until: date, *, as_of: date | None = None) -> ask.Window:
        """A simulated stretch of days from the known (or unknown) balance."""
        day = as_of or self.today
        model = self.model(as_of=day)
        return self.cached(
            ("window", day, until, self.lens_on),
            lambda: ask.window(self.budget, until, as_of=day, model=model),
        )

    def tag_rows(self, *, baseline: bool = False) -> list[ask.TagRow]:
        """Every tag with its flows and monthly money."""
        model = self.model(baseline=baseline)
        return self.cached(
            ("tag_rows", self._lensed(baseline)), lambda: ask.tag_rows(self.budget, model)
        )

    def monthly_net(self) -> int:
        """What's left each month as the budget stands (no what-if): the toasts' yardstick."""
        return self.cached(("monthly_net",), lambda: ask.monthly_net(self.budget))

    def items_on(self, day: date) -> list[engine.LedgerEntry]:
        """Everything the stored budget schedules on `day` (for the balance dialog)."""
        return self.cached(("items_on", day), lambda: self.budget.items_on(day))

    def recorded(self) -> balances.Balance | None:
        """The newest remembered balance."""
        return self.cached(("recorded",), self.budget.recorded)

    def balance_history(self) -> list[balances.Balance]:
        """Every remembered balance, newest first."""
        return self.cached(("balance_history",), lambda: balances.history(self.conn))

    def expected(self, day: date) -> Expected | None:
        """What the budget expected `day` to start with, from the balance before it."""

        def compute() -> Expected | None:
            prev = balances.latest(self.conn, on_or_before=day - timedelta(days=1))
            if prev is None:
                return None
            return Expected(prev, self.budget.carry(prev.amount_cents, prev.as_of, day))

        return self.cached(("expected", day), compute)

    # ── Changes ───────────────────────────────────────────────────────────────────
    # Each one validates with the core (raising CashError with a message a person can read),
    # commits, bumps the version and says what it did.

    def add_flow(self, draft: FlowDraft) -> Done:
        """Add an income or expense (and its loan details)."""
        before = self.monthly_net()
        with self._write():
            f = repo.add_flow(
                self.conn,
                name=draft.name,
                kind=draft.kind,
                amount_cents=draft.amount_cents,
                rrule=draft.rrule,
                dtstart=draft.dtstart,
                until=draft.until,
                tags=list(draft.tags),
                notes=draft.notes,
                active=draft.active,
                weekend=draft.weekend,
                payday=draft.payday and draft.kind == Kind.INCOME,
            )
            if draft.debt is not None:
                self._set_debt(int(f.id), draft.debt)
        flow = repo.get_flow(self.conn, int(f.id))
        paused = ", paused" if not flow.active else ""
        return Done(
            f"Added {flow.name}{paused} · {describe_flow(flow)} · {self._net(before)}", flow
        )

    def current(self, opened: Flow) -> Flow:
        """The flow a form or a question was opened on, as it is on disk now.

        Raises CashError when it has gone, or was renamed or replaced meanwhile (another program
        can delete a flow and reuse its id), so nothing is written to the wrong flow.
        """
        try:
            now = repo.get_flow(self.conn, int(opened.id))
        except CashError:
            raise CashError(
                f"{opened.name} was deleted while you had it open, so nothing was saved",
                "unknown_flow",
            ) from None
        if now.name.lower() != opened.name.lower() or now.created_at != opened.created_at:
            raise CashError(
                f"{opened.name} was renamed or replaced while you had it open, so nothing was "
                "saved; open it again to see it as it is now",
                "stale_flow",
            )
        return now

    def update_flow(self, opened: Flow, draft: FlowDraft) -> Done:
        """Save what the person changed in a form opened on `opened`, and nothing else.

        Only the fields that differ from `opened` are written, onto the flow as it is now, so
        a change another program made meanwhile (an agent's edit, the tidy-up rolling a loan
        forward) survives unless the person changed that same field.
        """
        now = self.current(opened)
        base = FlowDraft.of(opened)
        changed = {
            f.name: getattr(draft, f.name)
            for f in fields(FlowDraft)
            if f.name != "debt" and getattr(draft, f.name) != getattr(base, f.name)
        }
        debt = _merged_debt(base.debt, draft.debt, FlowDraft.of(now).debt)
        debt_changed = debt != FlowDraft.of(now).debt
        if not changed and not debt_changed:
            return Done(f"Nothing changed in {now.name}", now)
        before = self.monthly_net()
        columns = {k: v for k, v in changed.items() if k != "tags"}
        with self._write():
            if debt is None and now.debt is not None:
                repo.unset_debt(self.conn, int(now.id))
            if columns:
                repo.update_flow(self.conn, int(now.id), **columns)
            if "tags" in changed:
                repo.set_flow_tags(self.conn, int(now.id), list(draft.tags))
                repo.prune_unused_tags(self.conn)
            if debt is not None and debt_changed:
                self._set_debt(int(now.id), debt)
        flow = repo.get_flow(self.conn, int(now.id))
        note = " · it had changed on disk too, so your edits went on top" if now != opened else ""
        return Done(f"Saved {flow.name} · {describe_flow(flow)} · {self._net(before)}{note}", flow)

    def remove_flow(self, opened: Flow) -> Done:
        """Delete a flow (with its loan details and events)."""
        flow = self.current(opened)
        before = self.monthly_net()
        with self._write():
            repo.remove_flow(self.conn, int(flow.id))
        return Done(f"Deleted {flow.name} · {self._net(before)}", flow)

    def set_active(self, opened: Flow, active: bool) -> Done:
        """Pause a flow (it stays saved but leaves every projection) or resume it."""
        now = self.current(opened)
        before = self.monthly_net()
        with self._write():
            flow = repo.update_flow(self.conn, int(now.id), active=active)
        verb = "Resumed" if active else "Paused"
        return Done(f"{verb} {flow.name} · {self._net(before)}", flow)

    def set_paydays(self, paydays: dict[int, bool]) -> Done:
        """Which incomes start pay cycles (`bdbd edit NAME --payday` for each)."""
        with self._write():
            for flow_id, on in paydays.items():
                if repo.get_flow(self.conn, flow_id).payday != on:
                    repo.update_flow(self.conn, flow_id, payday=on)
        names = [f.name for f in self.flows() if f.payday and f.kind == Kind.INCOME]
        if not names:
            return Done("No paydays now, so no pay cycles")
        return Done(f"Paydays: {join(names)} · each one starts a pay cycle")

    def set_debt(self, flow_id: int, terms: DebtTerms) -> Done:
        """Attach or change an expense's loan details (mirrors `bdbd debt set`)."""
        with self._write():
            self._set_debt(flow_id, terms)
        flow = repo.get_flow(self.conn, flow_id)
        owed = f"{money(terms.balance_cents)} owed at {pct(terms.annual_rate)}"
        return Done(f"{flow.name} is a debt · {owed} · {self._payoff(flow_id)}", flow)

    def unset_debt(self, opened: Flow) -> Done:
        """Stop tracking a debt; its payment stays as a plain expense."""
        flow_id = int(self.current(opened).id)
        with self._write():
            repo.unset_debt(self.conn, flow_id)
        flow = repo.get_flow(self.conn, flow_id)
        return Done(f"{flow.name} is a plain expense again · its payment stays", flow)

    def add_event(
        self,
        flow_id: int,
        type: EventType,
        day: date,
        *,
        rate: Decimal | None = None,
        amount_cents: int | None = None,
        notes: str | None = None,
    ) -> Done:
        """Record something that happened to a debt (mirrors `bdbd debt extra/rate/…`)."""
        before = self._payoff(flow_id)
        with self._write():
            ev = repo.add_event(
                self.conn,
                flow_id,
                type=type,
                date=day,
                rate=rate,
                amount_cents=amount_cents,
                notes=notes,
            )
        flow = repo.get_flow(self.conn, flow_id)
        return Done(
            f"{_event_phrase(flow, ev, self.today)} · {self._payoff(flow_id, before)}", flow
        )

    def remove_event(self, event_id: int) -> Done:
        """Delete a recorded debt event."""
        row = self.conn.execute(
            "SELECT flow_id FROM debt_event WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise CashError("that debt event is already gone", "unknown_event")
        flow_id = int(row["flow_id"])
        before = self._payoff(flow_id)
        with self._write():
            repo.remove_event(self.conn, event_id)
        flow = repo.get_flow(self.conn, flow_id)
        return Done(f"Deleted the event on {flow.name} · {self._payoff(flow_id, before)}", flow)

    def record_balance(self, cents: int, day: date, *, posted: bool) -> Recorded:
        """Remember what's in the account on `day` (posted: that day's items are in it)."""
        with self._write():
            return self.budget.record_balance(cents, day, posted=posted)

    def recorded_message(self, r: Recorded) -> str:
        """The toast after recording a balance, with the drift from what was expected."""
        day = r.balance.as_of
        when = "" if day == self.today else f" for {fmt_date(day, self.today, weekday=True)}"
        text = f"Recorded {money(r.typed_cents)}{when}"
        newest = balances.latest(self.conn)
        if newest is not None and newest.as_of > day:
            return (
                f"{text} · your newer balance from {fmt_date(newest.as_of, self.today)} comes first"
            )
        if r.expected_cents is None:
            return f"{text} · everything now starts from here"
        drift = r.balance.amount_cents - r.expected_cents
        if drift == 0:
            return f"{text} · right where the budget expected"
        side = "ahead of" if drift > 0 else "behind"
        return f"{text} · {money(abs(drift))} {side} what the budget expected"

    def forget_balances(self) -> int:
        """Forget every remembered balance; returns how many."""
        with self._write():
            return balances.forget(self.conn)

    def set_weekly_spend(self, cents: int | None) -> Done:
        """Save everyday spending per week, or clear it with None."""
        if cents is not None and cents < 0:
            raise CashError("everyday spending can't be negative", "invalid_amount")
        before = self.monthly_net()
        with self._write():
            if cents is None:
                repo.config_unset(self.conn, "weekly_spend")
            else:
                repo.config_set(self.conn, "weekly_spend", cents_to_str(cents))
        what = f"{money(cents)} a week" if cents else "off"
        return Done(f"Everyday spending is {what} · {self._net(before)}")

    def rename_tag(self, old: str, new: str) -> Done:
        with self._write():
            repo.rename_tag(self.conn, old, new)
        return Done(f"Renamed the tag {repo.normalize_tag(old)} to {repo.normalize_tag(new)}")

    def remove_tag(self, name: str) -> Done:
        with self._write():
            repo.remove_tag(self.conn, name)
        return Done(f"Took the tag {repo.normalize_tag(name)} off every flow")

    def export(self, path: Path) -> int:
        """Back up the whole budget as JSON (like `bdbd export -o`); returns the number of flows."""
        return backup.write(self.conn, path, budget=self.path, today=self.today)

    def backup_contents(self, path: Path) -> backup.Contents:
        """What the backup at `path` holds, fully checked (raises CashError when it's bad)."""
        return backup.check(backup.read(path))

    def replaced_by_import(self) -> list[str]:
        """What restoring a backup would replace ('15 flows', 'your balances', …)."""
        return backup.held(self.conn)

    def import_(self, path: Path, *, replace: bool) -> int:
        """Restore a backup (like `bdbd import`): all of it, or nothing; returns the flows."""
        try:
            return backup.restore(self.conn, backup.read(path), replace=replace)
        except sqlite3.Error as exc:
            raise self._db_error(exc) from exc
        finally:
            self.bump()

    # helpers for changes ---------------------------------------------------------

    @contextmanager
    def _write(self) -> Iterator[None]:
        """One transaction, bumping the version once it's committed.

        BEGIN IMMEDIATE takes the write lock up front, so a lock held by another program is
        waited out (sqlite's timeout) rather than failing halfway; a locked or read-only file
        raises CashError, which the app shows instead of crashing.
        """
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise self._db_error(exc) from exc
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException as exc:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise self._db_error(exc) from exc
            raise
        self.bump()

    def _db_error(self, exc: sqlite3.Error) -> CashError:
        """sqlite trouble (a lock, a read-only file) as a CashError a person can read."""
        code, message, hint = db.problem(exc, self.path)
        return CashError(f"{message}; {hint[0].lower()}{hint[1:]}" if hint else message, code)

    def _set_debt(self, flow_id: int, terms: DebtTerms) -> None:
        repo.set_debt(self.conn, flow_id, **terms.kwargs())

    def _net(self, before: int) -> str:
        """'monthly net +$1,621.04 → +$1,605.55' (or 'stays …' when it didn't move)."""
        after = self.monthly_net()
        if after == before:
            return f"monthly net stays {money(after, sign=True)}"
        return f"monthly net {money(before, sign=True)} → {money(after, sign=True)}"

    def _payoff(self, flow_id: int, before: str | None = None) -> str:
        """'paid off Feb 2030', with '(was Mar 2030)' when `before` differs."""
        row = next((r for r in self.debts(baseline=True) if r.key == flow_id), None)
        if row is None:
            text = "no longer tracked as a debt"
        elif row.paid_off_on is None:
            text = "never paid off at this payment"
        elif row.paid_off_on <= self.today:
            text = "paid off"
        else:
            text = f"paid off {fmt_month(row.paid_off_on)}"
        if before is not None and before != text and before.startswith("paid off "):
            text += f" (was {before.removeprefix('paid off ')})"
        return text

    # ── Staying current ───────────────────────────────────────────────────────────

    def check_disk(self) -> bool:
        """Reload when another process changed the file; True if it did."""
        if not self.path.exists():
            return False  # moved away: keep working with what's open
        try:
            moved = self._pragma_version(self.conn) != self._data_version
        except sqlite3.Error:
            moved = True
        if not moved and self._stat() == self._file_id:
            return False
        self.reload()
        return True

    def check_day(self) -> bool:
        """Reopen (and tidy) when the date has rolled over; True if it did."""
        if today_fn() == self.budget.today:
            return False
        self._reopen(tidy=self.tidy)
        return True

    def reload(self) -> None:
        """Reopen the file, keeping the sandbox, and redraw everything."""
        self._reopen(tidy=False)

    def _reopen(self, *, tidy: bool) -> None:
        fresh = self._open(tidy=tidy)
        old, self.budget = self.budget, fresh
        old.close()
        self.bump()

    def _open(self, *, tidy: bool) -> Budget:
        if not self.path.exists():
            raise Problem(f"there's no budget at {self.path}", "db_not_found")
        budget = Budget.open(self.path, tidy=tidy)
        self.tidied = [*self.tidied, *budget.tidied]
        self._data_version = self._pragma_version(budget.conn)
        self._file_id = self._stat()
        return budget

    @staticmethod
    def _pragma_version(conn: sqlite3.Connection) -> int:
        return int(conn.execute("PRAGMA data_version").fetchone()[0])

    def _stat(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return st.st_ino, st.st_dev


# ── Words for toasts ──────────────────────────────────────────────────────────


def describe_flow(flow: Flow) -> str:
    """'-$15.49 monthly on the 12th' or '+$2,650.00 every 2 weeks on Fri' (real minus)."""
    cents = flow.amount_cents if flow.kind == Kind.INCOME else -flow.amount_cents
    schedule = describe(flow.rrule, flow.dtstart, flow.until)
    if "=" not in schedule:  # a raw RRULE stays as it is
        schedule = schedule[:1].lower() + schedule[1:]
    return f"{money(cents, sign=True)} {schedule}"


def _event_phrase(flow: Flow, ev: DebtEvent, today: date) -> str:
    on = fmt_date(ev.date, today)
    match ev.type:
        case EventType.EXTRA_PAYMENT:
            return f"Recorded an extra {money(ev.amount_cents or 0)} on {flow.name} on {on}"
        case EventType.RATE_CHANGE:
            return f"{flow.name}'s rate is {pct(ev.rate or Decimal(0))} from {on}"
        case EventType.PAYMENT_CHANGE:
            return f"{flow.name}'s payment is {money(ev.amount_cents or 0)} from {on}"
        case EventType.BALANCE_ADJUSTMENT:
            amount = money(ev.amount_cents or 0, sign=True)
            return f"Adjusted {flow.name}'s balance by {amount} on {on}"
        case EventType.PAYOFF:
            return f"Recorded paying off {flow.name} on {on}"
    return f"Recorded {ev.type} on {flow.name}"


def _merged_debt(
    opened: DebtTerms | None, draft: DebtTerms | None, now: DebtTerms | None
) -> DebtTerms | None:
    """The loan terms to save: the person's changes (draft vs opened) applied onto `now`."""
    if draft is None:
        return None if opened is not None else now  # switched off, or never had one
    if opened is None or now is None:
        return draft  # a new loan (or the stored one vanished): take the form's terms
    changed = {
        f.name: getattr(draft, f.name)
        for f in fields(DebtTerms)
        if getattr(draft, f.name) != getattr(opened, f.name)
    }
    return replace(now, **changed) if changed else now
