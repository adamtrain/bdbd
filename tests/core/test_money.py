from decimal import Decimal

import pytest

from bdbd.core.errors import CashError
from bdbd.core.money import cents_to_str, parse_amount, parse_rate, rate_to_str


def test_parse_amount_forms():
    assert parse_amount("$1,234.56") == 123456
    assert parse_amount("386.66") == 38666
    assert parse_amount("1,234") == 123400
    assert parse_amount(20) == 2000
    assert parse_amount("0") == 0


def test_negative_rejected_unless_allowed():
    with pytest.raises(CashError) as e:
        parse_amount("-5")
    assert e.value.code == "negative_amount"
    assert parse_amount("-5", allow_negative=True) == -500


def test_invalid_amount():
    with pytest.raises(CashError) as e:
        parse_amount("abc")
    assert e.value.code == "invalid_amount"


def test_cents_to_str():
    assert cents_to_str(38638) == "386.38"
    assert cents_to_str(-5) == "-0.05"
    assert cents_to_str(0) == "0.00"


def test_rates():
    assert parse_rate("6%") == Decimal("0.06")
    assert parse_rate("0.065") == Decimal("0.065")
    assert rate_to_str(Decimal("0.0600")) == "0.06"
    assert rate_to_str(Decimal("0")) == "0.0"
    with pytest.raises(CashError):
        parse_rate("x")
