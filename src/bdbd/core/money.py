"""Money and rate parsing/formatting. Money is integer cents; rates are Decimal."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from bdbd.core.errors import CashError

CENT = Decimal("0.01")
_AMOUNT_RE = re.compile(r"^[+-]?\$?[0-9][0-9,]*(\.[0-9]*)?$|^[+-]?\$?\.[0-9]+$")


def D(value: int | str | float | Decimal) -> Decimal:
    """Build a Decimal without ever going through binary float for str/int."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(value)


def q(value: Decimal) -> Decimal:
    """Quantize to cents, ROUND_HALF_UP."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def parse_amount(text: str | int | float | Decimal, *, allow_negative: bool = False) -> int:
    """Parse '386.66', '1,234', '$1,234.56', '-5' into integer cents."""
    if isinstance(text, bool):
        raise CashError("amount must be a number", "invalid_amount")
    if isinstance(text, int | float | Decimal):
        dec = D(text)
    else:
        s = str(text).strip().replace(" ", "")
        if not _AMOUNT_RE.match(s):
            raise CashError(f"invalid amount {text!r}", "invalid_amount")
        s = s.replace("$", "").replace(",", "")
        try:
            dec = Decimal(s)
        except InvalidOperation as exc:
            raise CashError(f"invalid amount {text!r}", "invalid_amount") from exc
    cents = q(dec) * 100
    if cents != cents.to_integral_value():
        raise CashError(f"invalid amount {text!r}", "invalid_amount")
    if cents < 0 and not allow_negative:
        raise CashError(
            f"amount {text!r} is negative; amounts are always positive "
            "(the flow kind determines the sign)",
            "negative_amount",
        )
    return int(cents)


def cents_to_decimal(cents: int) -> Decimal:
    return Decimal(cents).scaleb(-2)


def cents_to_str(cents: int) -> str:
    """Integer cents -> '386.66' / '-386.66'."""
    return f"{cents_to_decimal(cents):.2f}"


def dec_to_str(value: Decimal) -> str:
    """Unrounded Decimal money -> '386.66'."""
    return f"{q(value):.2f}"


def parse_rate(text: str | Decimal | float) -> Decimal:
    """Parse '0.06', '6%', '6.5 %' into a Decimal fraction (0.06)."""
    if isinstance(text, Decimal):
        return text
    if isinstance(text, float):
        return D(text)
    s = str(text).strip().replace(" ", "")
    pct = s.endswith("%")
    if pct:
        s = s[:-1]
    try:
        dec = Decimal(s)
    except InvalidOperation as exc:
        raise CashError(f"invalid rate {text!r}", "invalid_rate") from exc
    if pct:
        dec = dec / 100
    if dec < 0:
        raise CashError(f"rate {text!r} must not be negative", "invalid_rate")
    return dec


def rate_to_str(rate: Decimal) -> str:
    s = format(rate.normalize(), "f")
    return s if "." in s else s + ".0"
