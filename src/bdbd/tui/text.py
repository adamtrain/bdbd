"""Words and small styled pieces the whole app shares, so a thing reads the same everywhere.

A debt event, a loan's terms, "never, at this payment", a what-if's "+$120.00 vs now": each is
worded once, here, and every view, card and dialog uses it.
"""

from __future__ import annotations

from rich.text import Text

from bdbd.core.engine import LedgerEntry
from bdbd.core.models import Compounding, DayCount, Debt, DebtEvent, EventType, PaymentMode
from bdbd.ui.theme import AMBER, DOT, FAINT, balance_color, money, pct
from bdbd.words import ordinal

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
DEBOUNCE = 0.3  # seconds of quiet typing before a slow answer is worked out again
STALE_DAYS = 14  # a recorded balance older than this gets a nudge to update it

DEBT_KINDS = frozenset({"debt_payment", "extra_payment", "payoff", "settle"})  # ledger kinds
NEVER_PAID = "never, at this payment"

EVENT_WORDS = {  # a recorded (or tried) debt event
    EventType.EXTRA_PAYMENT: "Extra payment",
    EventType.RATE_CHANGE: "New rate",
    EventType.PAYMENT_CHANGE: "New payment",
    EventType.BALANCE_ADJUSTMENT: "Balance correction",
    EventType.PAYOFF: "Paid off",
    EventType.SETTLE: "Sold",
}

PAYMENT_KINDS = {  # a row of an amortization schedule
    "scheduled": "",
    "extra": "extra payment",
    "payoff": "paid off",
    "settle": "sold",
}


def sentence(text: str) -> str:
    """The text with a capital first letter (core messages start lower-case)."""
    return text[:1].upper() + text[1:] if text else text


def bold_balance(cents: int, low: int = 0) -> Text:
    """A balance in bold, red below zero and amber below `low` (a week of everyday spending)."""
    return Text(money(cents), style=f"bold {balance_color(cents, low)}")


def versus(now: int | None, before: int | None) -> Text:
    """'+$120.00 vs now' or 'same as now' (faint): how far the what-if moved a number."""
    if now is None or before is None:
        return Text("")
    if now == before:
        return Text("same as now", style=FAINT)
    return Text(f"{money(now - before, sign=True)} vs now", style=FAINT)


def no_balance_note() -> Text:
    """What views say while no balance is recorded (they project from $0)."""
    return Text(f"No balance recorded, so this starts from $0 {DOT} b to record it", style=AMBER)


def ledger_key(e: LedgerEntry) -> tuple:
    """What identifies a ledger item across two runs (as it is vs the what-if)."""
    return (e.date, e.key, e.kind, e.delta_cents)


def what_if_only(e: LedgerEntry, baseline: frozenset[tuple] | set[tuple] | None) -> bool:
    """Whether a ledger item is there only because of the what-if (it gets a ↳)."""
    if isinstance(e.key, str) and e.key.startswith("s:"):
        return True
    return baseline is not None and ledger_key(e) not in baseline


def event_words(ev: DebtEvent) -> str:
    """'Extra payment', 'New rate', … for a recorded debt event."""
    return EVENT_WORDS.get(ev.type, str(ev.type).replace("_", " ").capitalize())


def event_amount(ev: DebtEvent) -> str:
    """'$500.00', '5.5%', '+$25.00' (a correction keeps its sign), or '' for a payoff."""
    if ev.rate is not None:
        return pct(ev.rate)
    if ev.amount_cents is None:
        return ""
    return money(ev.amount_cents, sign=ev.type == EventType.BALANCE_ADJUSTMENT)


def terms_words(debt: Debt, *, lines: bool = False) -> str:
    """How a debt's interest works: 'simple interest, accrued daily, never compounds'.

    `lines=True` puts one idea on each line, for a narrow card.
    """
    match debt.compounding:
        case Compounding.SIMPLE:
            parts = ["simple interest, accrued daily", "never compounds"]
        case Compounding.DAILY:
            parts = ["compounds daily"]
        case Compounding.MONTHLY:
            day = ordinal(debt.posting_day or debt.balance_as_of.day)
            parts = [f"compounds monthly, posted on the {day}"]
        case _:
            parts = ["compounds continuously"]
    if debt.payment_mode == PaymentMode.INTEREST_ONLY:
        parts.append("interest-only payments")
    elif debt.payment_mode == PaymentMode.PERCENT_OF_BALANCE and debt.payment_pct is not None:
        parts.append(f"pays the larger of the amount or {pct(debt.payment_pct)} of the balance")
    if debt.day_count != DayCount.ACT_365:
        parts.append(f"counts days {debt.day_count}")
    return ("\n" if lines else ", ").join(parts)
