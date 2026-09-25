"""Plain data models mirroring the sqlite schema."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum


class Kind(StrEnum):
    INCOME = "income"
    EXPENSE = "expense"


class Compounding(StrEnum):
    SIMPLE = "simple"
    DAILY = "daily"
    MONTHLY = "monthly"
    CONTINUOUS = "continuous"


class DayCount(StrEnum):
    ACT_365 = "actual/365"
    ACT_360 = "actual/360"
    THIRTY_360 = "30/360"


class PaymentMode(StrEnum):
    FIXED = "fixed"
    INTEREST_ONLY = "interest_only"
    PERCENT_OF_BALANCE = "percent_of_balance"


class Weekend(StrEnum):
    """What happens when an occurrence lands on a Saturday or Sunday."""

    NONE = "none"  # it happens on the weekend day
    NEXT = "next"  # moves to the following Monday (typical ACH pull)
    PREVIOUS = "previous"  # moves to the preceding Friday


class EventType(StrEnum):
    RATE_CHANGE = "rate_change"
    BALANCE_ADJUSTMENT = "balance_adjustment"
    EXTRA_PAYMENT = "extra_payment"
    PAYMENT_CHANGE = "payment_change"
    PAYOFF = "payoff"
    SETTLE = "settle"  # scenario-only: clear the debt on DATE with `amount` of proceeds applied


@dataclass(frozen=True)
class DebtEvent:
    id: int | None
    flow_id: int | str
    date: date
    type: EventType
    rate: Decimal | None = None
    amount_cents: int | None = None
    notes: str | None = None


@dataclass(frozen=True)
class Debt:
    flow_id: int | str
    balance_cents: int
    balance_as_of: date
    annual_rate: Decimal
    compounding: Compounding
    day_count: DayCount = DayCount.ACT_365
    capitalize_interest: bool = True
    payment_mode: PaymentMode = PaymentMode.FIXED
    payment_pct: Decimal | None = None
    original_principal_cents: int | None = None
    posting_day: int | None = None  # monthly compounding: day-of-month interest posts (clamped)
    events: tuple[DebtEvent, ...] = ()


@dataclass(frozen=True)
class Flow:
    id: int | str
    name: str
    kind: Kind
    amount_cents: int
    rrule: str | None
    dtstart: date
    until: date | None = None
    active: bool = True
    notes: str | None = None
    tags: tuple[str, ...] = ()
    weekend: Weekend = Weekend.NONE
    debt: Debt | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def is_debt(self) -> bool:
        return self.debt is not None

    @property
    def one_off(self) -> bool:
        return self.rrule is None


@dataclass
class Tag:
    id: int
    name: str
    flow_count: int = field(default=0)


# ---- effective model (DB flows + scenario overlay), consumed by the engine -------------

FlowKey = int | str  # DB id, or "s:<n>" for scenario-added flows


@dataclass(frozen=True)
class AmountSegment:
    from_date: date | None  # None = from the beginning
    until: date | None  # inclusive; None = forever
    cents: int


@dataclass(frozen=True)
class EffectiveFlow:
    key: FlowKey
    name: str
    kind: Kind
    tags: frozenset[str]
    rrule: str | None
    dtstart: date
    until: date | None
    base_cents: int
    segments: tuple[AmountSegment, ...] = ()
    debt: Debt | None = None
    origin: str = "db"  # "db" | "scenario"
    weekend: Weekend = Weekend.NONE

    def amount_on(self, day: date) -> int:
        cents = self.base_cents
        for seg in self.segments:  # later segments override earlier ones
            if (seg.from_date is None or day >= seg.from_date) and (
                seg.until is None or day <= seg.until
            ):
                cents = seg.cents
        return cents


@dataclass
class EffectiveModel:
    flows: list[EffectiveFlow]
    applied: list[str] = field(default_factory=list)  # informational notes about the overlay
    warnings: list[str] = field(default_factory=list)
    scenario: dict | None = None
    weekly_spend_cents: int | None = None  # scenario override of --weekly-spend
