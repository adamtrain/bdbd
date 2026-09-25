"""Projection: cash balance over a horizon given a starting balance."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from bdbd.core import engine
from bdbd.core.dates import is_month_end, month_end
from bdbd.core.models import EffectiveModel, Kind
from bdbd.core.money import cents_to_str, dec_to_str


def series_rows(result: engine.SimResult, granularity: str) -> list[dict]:
    """Balance rows: daily, or as_of + every month end + until."""
    daily = result.daily
    if not daily:
        return []
    if granularity == "daily":
        points = [d for d, _ in daily]
    else:
        points = [result.as_of]
        d = month_end(result.as_of)
        while d < result.until:
            if d > result.as_of:
                points.append(d)
            d = month_end(d + timedelta(days=1))  # next month's end
        if result.until != result.as_of and (
            not is_month_end(result.until) or points[-1] != result.until
        ):
            points.append(result.until)
    bal = dict(daily)
    rows = []
    li = 0
    ledger = result.ledger
    prev = None
    for p in points:
        inc = exp = 0
        while li < len(ledger) and ledger[li].date <= p:
            e = ledger[li]
            if prev is None or e.date > prev:
                if e.delta_cents > 0:
                    inc += e.delta_cents
                else:
                    exp -= e.delta_cents
            li += 1
        rows.append(
            {
                "date": p.isoformat(),
                "balance": cents_to_str(bal[p]),
                "income": cents_to_str(inc),
                "expense": cents_to_str(exp),
                "net": cents_to_str(inc - exp),
            }
        )
        prev = p
    return rows


def extremes(result: engine.SimResult) -> tuple[dict, dict]:
    lo = min(result.daily, key=lambda t: (t[1], t[0]))
    hi = max(result.daily, key=lambda t: (t[1], -t[0].toordinal()))
    return (
        {"date": lo[0].isoformat(), "balance": cents_to_str(lo[1])},
        {"date": hi[0].isoformat(), "balance": cents_to_str(hi[1])},
    )


SPARE_LOOKAHEAD_DAYS = 400


class SpareCalculator:
    """What's spare on a date: the balance that day plus everything that comes and goes after
    it until the next payday (bills, everyday spending, and any other money coming in, each on
    its day), so what's left the day before that payday.

    A payday is an income flagged as one (it starts a pay cycle); a budget with none counts to
    its next income of any kind. One extended simulation answers any date in the window.
    """

    def __init__(
        self, model: EffectiveModel, as_of: date, until: date, weekly_spend_cents: int = 0
    ):
        ext = engine.run(
            model,
            as_of=as_of,
            until=until + timedelta(days=SPARE_LOOKAHEAD_DAYS),
            weekly_spend_cents=weekly_spend_cents,
        )
        self.ledger = ext.ledger
        self.horizon = ext.until
        self._paying = {f.key for f in model.flows if f.payday and f.kind == Kind.INCOME}

    def _is_payday(self, e: engine.LedgerEntry) -> bool:
        return e.delta_cents > 0 and (not self._paying or e.key in self._paying)

    def _window(self, day: date) -> tuple[engine.LedgerEntry | None, list[engine.LedgerEntry]]:
        """The next payday after `day` (None: none in the horizon), and everything before it."""
        nxt = next((e for e in self.ledger if e.date > day and self._is_payday(e)), None)
        cutoff = nxt.date if nxt else None
        between = [e for e in self.ledger if e.date > day and (cutoff is None or e.date < cutoff)]
        return nxt, between

    def spare_cents(self, day: date, balance_cents: int) -> int:
        """Spare balance in cents (no payday ahead: everything left in the horizon counts)."""
        _, between = self._window(day)
        return balance_cents + sum(e.delta_cents for e in between)

    def compute(self, day: date, balance_cents: int) -> dict:
        nxt, between = self._window(day)
        out = [e for e in between if e.delta_cents < 0]
        money_in = [e for e in between if e.delta_cents > 0]
        committed = sum(-e.delta_cents for e in out)
        lifestyle = sum(-e.delta_cents for e in out if e.kind == "lifestyle")
        income = sum(e.delta_cents for e in money_in)

        def item(e: engine.LedgerEntry) -> dict:
            return {
                "date": e.date.isoformat(),
                "name": e.name,
                "amount": cents_to_str(abs(e.delta_cents)),
            }

        return {
            "date": day.isoformat(),
            "balance": cents_to_str(balance_cents),
            "next_payday": (
                {
                    "date": nxt.date.isoformat(),
                    "name": nxt.name,
                    "amount": cents_to_str(nxt.delta_cents),
                }
                if nxt
                else None
            ),
            "committed_total": cents_to_str(committed),
            "committed_lifestyle": cents_to_str(lifestyle),
            "committed_before_payday": [item(e) for e in out if e.kind != "lifestyle"],
            "income_before_payday": [item(e) for e in money_in],
            "income_total": cents_to_str(income),
            "spare_balance": cents_to_str(balance_cents + income - committed) if nxt else None,
        }


def project(
    model: EffectiveModel,
    *,
    as_of: date,
    until: date,
    starting_balance_cents: int,
    granularity: str = "monthly",
    include_ledger: bool = False,
    include_spare: bool = True,
    weekly_spend_cents: int = 0,
    verbose: bool = False,
) -> tuple[dict, list[str]]:
    if model.weekly_spend_cents is not None:
        weekly_spend_cents = model.weekly_spend_cents
    result = engine.run(
        model,
        as_of=as_of,
        until=until,
        starting_balance_cents=starting_balance_cents,
        weekly_spend_cents=weekly_spend_cents,
    )
    lo, hi = extremes(result)
    series = series_rows(result, granularity)
    spare: dict | None = None
    warnings = list(result.warnings)
    if include_spare:
        calc = SpareCalculator(model, as_of, until, weekly_spend_cents)
        bal = dict(result.daily)
        for row in series:
            d = date.fromisoformat(row["date"])
            row["spare"] = calc.compute(d, bal[d])["spare_balance"]
        spare = calc.compute(until, result.ending_balance_cents)
        if spare["next_payday"] is None:
            warnings.append(
                f"no payday found within {SPARE_LOOKAHEAD_DAYS} days after {until}; "
                "spare_balance is null"
            )
    data = {
        "as_of": as_of.isoformat(),
        "until": until.isoformat(),
        "starting_balance": cents_to_str(starting_balance_cents),
        "weekly_spend": cents_to_str(weekly_spend_cents),
        "lifestyle_total": cents_to_str(
            result.flows_used[engine.LIFESTYLE_KEY].total_cents
            if engine.LIFESTYLE_KEY in result.flows_used
            else 0
        ),
        "ending_balance": cents_to_str(result.ending_balance_cents),
        "spare_balance": spare["spare_balance"] if spare else None,
        "spare": spare,
        "min_balance": lo,
        "max_balance": hi,
        "totals": {
            "income": cents_to_str(result.income_cents),
            "expense": cents_to_str(result.expense_cents),
            "net": cents_to_str(result.income_cents - result.expense_cents),
            "interest_paid": cents_to_str(result.interest_paid_cents),
            "principal_paid": cents_to_str(result.principal_paid_cents),
        },
        "series": series,
    }
    if verbose:
        data["debts"] = [d.to_json() for d in result.debts.values()]
        data["flows_used"] = [u.to_json() for u in result.flows_used.values()]
    else:
        data["debts"] = [
            {
                "flow": d.key,
                "name": d.name,
                "balance_at_until": dec_to_str(d.balance_at_until),
                "paid_off_on": d.paid_off_on.isoformat() if d.paid_off_on else None,
            }
            for d in result.debts.values()
        ]
    if include_ledger:
        data["ledger"] = [e.to_json() for e in result.ledger if verbose or e.kind != "lifestyle"]
        if not verbose and weekly_spend_cents:
            data["ledger_note"] = (
                "lifestyle rows omitted (see lifestyle_total); --verbose shows them"
            )
    data["total_debt_at_until"] = dec_to_str(
        sum((d.balance_at_until for d in result.debts.values()), start=Decimal(0))
    )
    return data, warnings
