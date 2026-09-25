from datetime import date
from decimal import Decimal

from bdbd.core.dates import daterange
from bdbd.core.debt import DebtState, pmt
from bdbd.core.models import Compounding, DayCount, Debt
from bdbd.core.money import dec_to_str


def mk(comp, dc=DayCount.ACT_365, cap=True, balance=2000000, as_of=date(2026, 10, 1)):
    return DebtState(
        Debt(
            flow_id=1,
            balance_cents=balance,
            balance_as_of=as_of,
            annual_rate=Decimal("0.06"),
            compounding=comp,
            day_count=dc,
            capitalize_interest=cap,
        )
    )


def test_single_day_accruals():
    st = mk(Compounding.DAILY)
    i = st.accrue(date(2026, 10, 2))
    assert str(i).startswith("3.2876712")
    assert dec_to_str(i) == "3.29"
    st = mk(Compounding.DAILY, DayCount.ACT_360)
    assert dec_to_str(st.accrue(date(2026, 10, 2))) == "3.33"
    st = mk(Compounding.SIMPLE, cap=False)
    st.accrue(date(2026, 10, 2))
    assert st.principal == Decimal("20000.00") and dec_to_str(st.accrued) == "3.29"


def test_no_accrual_on_or_before_anchor():
    st = mk(Compounding.DAILY)
    assert st.accrue(date(2026, 10, 1)) == 0
    assert st.accrue(date(2026, 9, 30)) == 0


def test_30_360_full_month_is_100():
    for start, end in [
        (date(2026, 10, 1), date(2026, 11, 1)),
        (date(2027, 1, 31), date(2027, 2, 28)),
        (date(2028, 1, 31), date(2028, 2, 29)),
        (date(2027, 2, 28), date(2027, 3, 31)),
    ]:
        st = mk(Compounding.DAILY, DayCount.THIRTY_360, cap=False, as_of=start)
        total = sum(st.accrue(d) for d in daterange(start, end))
        assert dec_to_str(total) == "100.00", (start, end)


def test_monthly_posting_dates_clamp():
    st = mk(Compounding.MONTHLY, as_of=date(2027, 1, 31))
    posts = [d for d in daterange(date(2027, 1, 31), date(2027, 4, 30)) if st.is_posting_day(d)]
    assert posts == [date(2027, 2, 28), date(2027, 3, 31), date(2027, 4, 30)]


def test_final_payment_capped_and_zeroed():
    st = mk(Compounding.MONTHLY, balance=38638)
    st.principal = Decimal("386.3812")
    res = st.apply_payment(38666, date(2031, 10, 1))
    assert res.cash_cents == 38638 and res.paid_off and st.principal == 0 and st.is_paid_off


def test_negative_amortization_state():
    st = mk(Compounding.MONTHLY)
    st.accrue(date(2026, 11, 1))
    res = st.apply_payment(5000, date(2026, 11, 1))
    assert dec_to_str(res.balance_after) == "20050.00"
    assert res.interest_cents == 5000 and res.principal_cents == 0


def test_pmt():
    assert pmt(2000000, Decimal("0.06"), 60) == 38666
    assert pmt(30000000, Decimal("0.065"), 360) == 189620
    assert pmt(120000, Decimal("0"), 12) == 10000


def test_balance_adjustment_clamps_and_reopens():
    st = mk(Compounding.MONTHLY, balance=10000)
    assert st.adjust_balance(-20000) == Decimal("100.00")
    assert st.principal == 0
    st.paid_off_on = date(2026, 10, 5)
    st.adjust_balance(5000)
    assert not st.is_paid_off and st.principal == Decimal("50.00")


def test_posting_day_overrides_anchor_day():
    from bdbd.core.models import Debt as _Debt

    st = DebtState(
        _Debt(
            flow_id=1,
            balance_cents=2000000,
            balance_as_of=date(2026, 9, 16),
            annual_rate=Decimal("0.06"),
            compounding=Compounding.MONTHLY,
            posting_day=15,
        )
    )
    posts = [d for d in daterange(date(2026, 9, 16), date(2026, 11, 30)) if st.is_posting_day(d)]
    assert posts == [date(2026, 10, 15), date(2026, 11, 15)]
