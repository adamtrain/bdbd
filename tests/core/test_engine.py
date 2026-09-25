from datetime import date
from decimal import Decimal

from bdbd.core import engine
from bdbd.core.debt import pmt
from bdbd.core.models import (
    Compounding,
    DayCount,
    Debt,
    DebtEvent,
    EffectiveFlow,
    EffectiveModel,
    EventType,
    Kind,
)
from bdbd.core.money import cents_to_str, dec_to_str


def loan(
    comp=Compounding.MONTHLY,
    cap=True,
    dc=DayCount.ACT_365,
    events=(),
    amount=38666,
    balance=2000000,
    rate="0.06",
    as_of=date(2026, 10, 1),
    dtstart=date(2026, 11, 1),
):
    d = Debt(
        flow_id=1,
        balance_cents=balance,
        balance_as_of=as_of,
        annual_rate=Decimal(rate),
        compounding=comp,
        day_count=dc,
        capitalize_interest=cap,
        events=tuple(events),
    )
    return EffectiveFlow(
        key=1,
        name="Car loan",
        kind=Kind.EXPENSE,
        tags=frozenset({"car"}),
        rrule="FREQ=MONTHLY;BYMONTHDAY=1",
        dtstart=dtstart,
        until=None,
        base_cents=amount,
        debt=d,
    )


def schedule(flow, as_of=date(2026, 10, 1), until=date(2045, 1, 1)):
    r = engine.run(
        EffectiveModel(flows=[flow]), as_of=as_of, until=until, stop_when_debts_paid=True
    )
    return r, r.debts[1].rows


def rowtuple(r):
    j = r.to_json()
    return j["interest"], j["principal"], j["balance"]


REFERENCE = {
    (Compounding.MONTHLY, True): (
        ("100.00", "286.66", "19713.34"),
        ("98.57", "288.09", "19425.25"),
        60,
        "386.38",
        "2031-10-01",
        "3199.32",
    ),
    (Compounding.DAILY, True): (
        ("102.17", "284.49", "19715.51"),
        ("97.46", "289.20", "19426.31"),
        61,
        "12.17",
        "2031-11-01",
        "3211.77",
    ),
    (Compounding.SIMPLE, False): (
        ("101.92", "284.74", "19715.26"),
        ("97.23", "289.43", "19425.82"),
        61,
        "2.67",
        "2031-11-01",
        "3202.27",
    ),
    (Compounding.CONTINUOUS, True): (
        ("102.18", "284.48", "19715.52"),
        ("97.47", "289.19", "19426.33"),
        61,
        "12.50",
        "2031-11-01",
        "3212.10",
    ),
}


def test_reference_tables_all_methods():
    for (comp, cap), (r1, r2, n, last, payoff, total) in REFERENCE.items():
        r, rows = schedule(loan(comp, cap))
        assert rowtuple(rows[0]) == r1, comp
        assert rowtuple(rows[1]) == r2, comp
        assert len(rows) == n, comp
        assert cents_to_str(rows[-1].payment_cents) == last, comp
        assert r.debts[1].paid_off_on.isoformat() == payoff, comp
        assert dec_to_str(r.debts[1].interest_in_window) == total, comp
        # invariant: payments == principal + interest, to the cent
        paid = sum(x.payment_cents for x in rows)
        assert cents_to_str(paid) == f"{Decimal(20000) + Decimal(total):.2f}", comp
        assert r.warnings == []


def test_monthly_row3_and_row60():
    _, rows = schedule(loan())
    assert rowtuple(rows[2]) == ("97.13", "289.53", "19135.71")
    j = rows[59].to_json()
    assert j == {
        "n": 60,
        "date": "2031-10-01",
        "payment": "386.38",
        "interest": "1.92",
        "principal": "384.46",
        "balance": "0.00",
        "kind": "scheduled",
    }


def test_mortgage_sanity():
    p = pmt(30000000, Decimal("0.065"), 360)
    assert p == 189620
    f = loan(balance=30000000, rate="0.065", amount=p)
    r, rows = schedule(f, until=date(2080, 1, 1))
    assert rowtuple(rows[0]) == ("1625.00", "271.20", "299728.80")
    assert len(rows) in (360, 361)  # rounding the level payment down leaves a tiny 361st payment
    assert abs(r.debts[1].interest_in_window - Decimal(382632)) < 10


def test_warm_up_rolls_forward():
    r = engine.run(EffectiveModel(flows=[loan()]), as_of=date(2027, 1, 15), until=date(2027, 3, 1))
    assert dec_to_str(r.debts[1].balance_at_as_of) == "19135.71"
    after = [x for x in r.debts[1].rows if not x.before_as_of]
    assert after[0].date == date(2027, 2, 1) and cents_to_str(after[0].interest_cents) == "95.68"
    split = r.ledger[0].debt
    assert len(r.ledger) == 2 and split is not None and split.interest_cents == 9568


def test_extra_payment_and_rate_change():
    evs = [DebtEvent(None, 1, date(2026, 10, 15), EventType.EXTRA_PAYMENT, amount_cents=500000)]
    r, rows = schedule(loan(events=evs), until=date(2027, 1, 1))
    assert rows[0].kind == "extra" and rows[0].payment_cents == 500000
    assert cents_to_str(rows[1].interest_cents) == "75.00"
    ledger_extra = [e for e in r.ledger if e.kind == "extra_payment"]
    assert len(ledger_extra) == 1 and ledger_extra[0].delta_cents == -500000
    evs = [DebtEvent(None, 1, date(2026, 12, 1), EventType.RATE_CHANGE, rate=Decimal("0.05"))]
    r, rows = schedule(loan(events=evs), until=date(2027, 1, 1))
    assert cents_to_str(rows[1].interest_cents) == "82.14"


def test_payoff_event_and_skipped_payments():
    evs = [
        DebtEvent(None, 1, date(2026, 10, 15), EventType.PAYOFF),
        DebtEvent(None, 1, date(2026, 12, 1), EventType.EXTRA_PAYMENT, amount_cents=100),
    ]
    r = engine.run(
        EffectiveModel(flows=[loan(events=evs)]), as_of=date(2026, 10, 1), until=date(2027, 3, 1)
    )
    assert [e.kind for e in r.ledger] == ["payoff"]
    assert r.ledger[0].amount_cents == 2000000
    assert r.debts[1].paid_off_on == date(2026, 10, 15)


def test_payoff_before_as_of_warns():
    evs = [DebtEvent(None, 1, date(2026, 10, 15), EventType.PAYOFF)]
    r = engine.run(
        EffectiveModel(flows=[loan(events=evs)]), as_of=date(2027, 1, 1), until=date(2027, 3, 1)
    )
    assert r.ledger == []
    assert any("before as-of" in w for w in r.warnings)


def test_negative_amortization_warning():
    r, rows = schedule(loan(amount=5000), until=date(2026, 12, 1))
    assert rows[0].to_json()["balance"] == "20050.00"
    assert any("negative amortization" in w for w in r.warnings)


def test_dormant_debt():
    f = loan(as_of=date(2027, 1, 1))
    r = engine.run(EffectiveModel(flows=[f]), as_of=date(2026, 9, 16), until=date(2027, 2, 1))
    kinds = [(e.date.isoformat(), e.kind, e.debt is None) for e in r.ledger]
    assert kinds[:2] == [("2026-11-01", "debt_payment", True), ("2026-12-01", "debt_payment", True)]
    assert kinds[2] == (
        "2027-01-01",
        "debt_payment",
        True,
    )  # payment on the balance date is dormant too
    assert kinds[3][0] == "2027-02-01" and kinds[3][2] is False
    split = r.ledger[3].debt
    assert split is not None and split.interest_cents == 10000
    assert any("after as-of" in w for w in r.warnings)


def test_balance_adjustment():
    evs = [
        DebtEvent(None, 1, date(2026, 11, 15), EventType.BALANCE_ADJUSTMENT, amount_cents=-25000)
    ]
    _, rows = schedule(loan(events=evs), until=date(2027, 1, 1))
    assert rows[1].to_json()["interest"] == "97.32"  # (19713.34 - 250) * 0.005


def test_payment_change_event():
    evs = [DebtEvent(None, 1, date(2026, 12, 1), EventType.PAYMENT_CHANGE, amount_cents=45000)]
    _, rows = schedule(loan(events=evs), until=date(2027, 1, 1))
    assert rows[0].payment_cents == 38666 and rows[1].payment_cents == 45000


def test_performance_30_years():
    import time

    flows = [loan(balance=30000000, rate="0.065", amount=189620)]
    flows.append(
        EffectiveFlow(
            key=2,
            name="Salary",
            kind=Kind.INCOME,
            tags=frozenset(),
            rrule="FREQ=WEEKLY",
            dtstart=date(2026, 10, 2),
            until=None,
            base_cents=100000,
        )
    )
    t = time.time()
    r = engine.run(EffectiveModel(flows=flows), as_of=date(2026, 10, 1), until=date(2056, 10, 1))
    assert time.time() - t < 3
    assert len(r.daily) == (date(2056, 10, 1) - date(2026, 10, 1)).days + 1
