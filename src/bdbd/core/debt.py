"""Pure debt math: interest accrual per compounding method, payment application, PMT."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from bdbd.core.dates import days_in_month
from bdbd.core.models import Compounding, DayCount, Debt, PaymentMode
from bdbd.core.money import CENT, D, q

_ONE = Decimal(1)
_ZERO = Decimal(0)


def day_weight(day: date, basis: DayCount) -> Decimal:
    """Fraction-of-a-day weight for accrual. 30/360 makes every month worth 30 days."""
    if basis != DayCount.THIRTY_360:
        return _ONE
    if day.day == 31:
        return _ZERO
    if day.month == 2:
        dim = days_in_month(day.year, 2)
        if day.day == dim:
            return Decimal(31 - dim)
    return _ONE


def basis_days(basis: DayCount) -> Decimal:
    return Decimal(365) if basis == DayCount.ACT_365 else Decimal(360)


def pmt(principal_cents: int, annual_rate: Decimal, n_periods: int) -> int:
    """Level monthly payment (cents) that amortizes principal over n periods."""
    if n_periods <= 0:
        raise ValueError("n_periods must be positive")
    p = D(principal_cents).scaleb(-2)
    i = annual_rate / 12
    payment = p / n_periods if i == 0 else p * i / (1 - (1 + i) ** (-n_periods))
    return int(payment.quantize(CENT, rounding=ROUND_HALF_UP) * 100)


@dataclass(frozen=True)
class PaymentResult:
    cash_cents: int
    interest_cents: int
    principal_cents: int
    balance_after: Decimal
    paid_off: bool
    interest_exact: Decimal = _ZERO  # unrounded interest portion, for exact totals


class DebtState:
    """Mutable state of one debt while the engine rolls it forward."""

    def __init__(self, debt: Debt, name: str = ""):
        self.name = name
        self.principal: Decimal = D(debt.balance_cents).scaleb(-2)
        self.accrued: Decimal = _ZERO  # uncapitalized interest owed
        self.cap_since_payment: Decimal = _ZERO  # capitalized since last payment (reporting)
        self.rate: Decimal = debt.annual_rate
        self.compounding = debt.compounding
        self.day_count = debt.day_count
        self.capitalize = bool(debt.capitalize_interest) and debt.compounding != Compounding.SIMPLE
        self.payment_mode = debt.payment_mode
        self.payment_pct = debt.payment_pct
        self.anchor: date = debt.balance_as_of
        self.posting_day: int = debt.posting_day or debt.balance_as_of.day
        self.paid_off_on: date | None = None
        self.total_interest: Decimal = _ZERO
        self.scheduled_override_cents: int | None = None
        self.interest_since_payment: Decimal = _ZERO
        self.negative_amortization_warned = False

    # -- observations -------------------------------------------------------------------
    def owed(self) -> Decimal:
        return self.principal + self.accrued

    def owed_cents(self) -> int:
        return int(q(self.owed()) * 100)

    @property
    def is_paid_off(self) -> bool:
        return self.paid_off_on is not None

    # -- accrual ------------------------------------------------------------------------
    def is_posting_day(self, day: date) -> bool:
        """Monthly compounding posts on each monthly anniversary of the balance date."""
        if day <= self.anchor:
            return False
        return day.day == min(self.posting_day, days_in_month(day.year, day.month))

    def accrue(self, day: date) -> Decimal:
        """Accrue interest for the day ending at `day`. Returns the amount accrued."""
        if self.is_paid_off or day <= self.anchor:
            return _ZERO
        p = self.principal
        if self.compounding == Compounding.MONTHLY:
            if not self.is_posting_day(day):
                return _ZERO
            interest = p * self.rate / 12
        else:
            w = day_weight(day, self.day_count)
            if w == 0:
                return _ZERO
            frac = self.rate * w / basis_days(self.day_count)
            if self.compounding == Compounding.CONTINUOUS:
                interest = p * (frac.exp() - 1)
            else:  # simple or daily
                interest = p * frac
        if interest == 0:
            return _ZERO
        self.total_interest += interest
        self.interest_since_payment += interest
        if self.capitalize:
            self.principal += interest
            self.cap_since_payment += interest
        else:
            self.accrued += interest
        return interest

    # -- payments -----------------------------------------------------------------------
    def scheduled_payment_cents(self, flow_amount_cents: int) -> int:
        base = (
            self.scheduled_override_cents
            if self.scheduled_override_cents is not None
            else flow_amount_cents
        )
        if self.payment_mode == PaymentMode.INTEREST_ONLY:
            return int(q(self.accrued + self.cap_since_payment) * 100)
        if self.payment_mode == PaymentMode.PERCENT_OF_BALANCE and self.payment_pct is not None:
            pct_amount = int(q(self.owed() * self.payment_pct) * 100)
            return max(base, pct_amount)
        return base

    def apply_payment(self, cents: int, day: date) -> PaymentResult:
        """Pay up to `cents`, capped at the amount owed. Zeroes the debt exactly on payoff."""
        owed_c = self.owed_cents()
        if cents >= owed_c:
            cash = owed_c
            interest_owed = self.accrued + self.cap_since_payment
            interest_c = min(cash, int(q(interest_owed) * 100))
            self.principal = _ZERO
            self.accrued = _ZERO
            self.cap_since_payment = _ZERO
            self.interest_since_payment = _ZERO
            self.paid_off_on = day
            return PaymentResult(
                cash,
                interest_c,
                cash - interest_c,
                _ZERO,
                True,
                min(D(cash).scaleb(-2), interest_owed),
            )
        amount = D(cents).scaleb(-2)
        interest_owed = self.accrued + self.cap_since_payment
        interest_part = min(amount, interest_owed)
        take_accrued = min(amount, self.accrued)
        self.accrued -= take_accrued
        self.principal -= amount - take_accrued
        self.cap_since_payment = _ZERO
        self.interest_since_payment = _ZERO
        interest_c = int(q(interest_part) * 100)
        return PaymentResult(
            cents, interest_c, cents - interest_c, self.owed(), False, interest_part
        )

    # -- events -------------------------------------------------------------------------
    def set_rate(self, rate: Decimal) -> None:
        self.rate = rate

    def adjust_balance(self, cents: int) -> Decimal:
        """Add signed cents to principal. Returns the clamped-off amount (0 if none)."""
        delta = D(cents).scaleb(-2)
        new = self.principal + delta
        clamped = _ZERO
        if new < 0:
            clamped = -new
            new = _ZERO
        self.principal = new
        if cents > 0 and self.is_paid_off:
            self.paid_off_on = None
        return clamped

    def set_scheduled_override(self, cents: int) -> None:
        self.scheduled_override_cents = cents
