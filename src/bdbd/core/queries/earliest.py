"""Earliest date: the first date on which a '?'-dated what-if keeps the balance above a floor.

The scenario must use the `?` date placeholder (e.g. `--settle "Car loan:29500@?"`). Every
candidate date from `first` to `last` is substituted in turn and projected; a candidate is
feasible when the measured quantity (end-of-day balance, or spare balance) never drops below
`floor` on any day from the candidate date to the end of the horizon. Days before the candidate
are not checked: what happens before the decision is the same whatever the date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from bdbd.core import engine
from bdbd.core.dates import daterange
from bdbd.core.errors import CashError
from bdbd.core.models import EffectiveModel, Flow
from bdbd.core.money import cents_to_str
from bdbd.core.queries.project import SpareCalculator
from bdbd.core.scenario import (
    build_effective_model,
    has_placeholder,
    placeholder_dates,
    resolve_placeholders,
)

SCENARIO_KINDS = {"extra_payment", "payoff", "settle"}
MEASURES = ("balance", "spare")


def _candidates(first: date, last: date, step: int, weekdays_only: bool) -> list[date]:
    return [
        d
        for i, d in enumerate(daterange(first, last))
        if i % step == 0 and (not weekdays_only or d.weekday() < 5)
    ]


@dataclass
class _Trial:
    day: date
    model: EffectiveModel
    result: engine.SimResult
    calc: SpareCalculator
    min_day: date
    min_cents: int  # of the measured quantity, on min_day

    def min_after(self) -> dict:
        bal = dict(self.result.daily)[self.min_day]
        return {
            "date": self.min_day.isoformat(),
            "balance": cents_to_str(bal),
            "spare": cents_to_str(self.calc.spare_cents(self.min_day, bal)),
        }


def earliest(
    all_flows: list[Flow],
    spec: dict | None,
    *,
    as_of: date,
    until: date,
    starting_balance_cents: int,
    floor_cents: int,
    measure: str = "balance",
    first: date | None = None,
    last: date | None = None,
    step: int = 1,
    weekdays_only: bool = False,
    weekly_spend_cents: int = 0,
    verbose: bool = False,
) -> tuple[dict, list[str]]:
    if not has_placeholder(spec):
        raise CashError(
            "nothing in the scenario depends on the date: use the '?' placeholder in at least "
            'one date, e.g. --add-expense "Flight:550@?" or --settle "Car loan:29500@?"',
            "usage",
        )
    if measure not in MEASURES:
        raise CashError(f"--measure must be one of {MEASURES}", "usage")
    if step < 1:
        raise CashError("--step must be at least 1", "usage")
    first = first or as_of
    last = last or until
    if not (as_of <= first <= last <= until):
        raise CashError(
            f"candidate range {first}..{last} must lie within the horizon {as_of}..{until}",
            "usage",
        )
    candidates = _candidates(first, last, step, weekdays_only)
    if not candidates:
        raise CashError("no candidate dates in the range (weekdays only?)", "usage")

    def trial(day: date) -> _Trial:
        model = build_effective_model(all_flows, resolve_placeholders(spec, day), as_of)
        weekly = (
            model.weekly_spend_cents if model.weekly_spend_cents is not None else weekly_spend_cents
        )
        result = engine.run(
            model,
            as_of=as_of,
            until=until,
            starting_balance_cents=starting_balance_cents,
            weekly_spend_cents=weekly,
        )
        calc = SpareCalculator(model, as_of, until, weekly)
        after = [(d, b) for d, b in result.daily if d >= day]
        if measure == "spare":
            after = [(d, calc.spare_cents(d, b)) for d, b in after]
        lo_d, lo_v = min(after, key=lambda t: (t[1], t[0]))
        return _Trial(day, model, result, calc, lo_d, lo_v)

    found: _Trial | None = None
    feasible_through: date | None = None
    broke_on: date | None = None
    last_infeasible: _Trial | None = None
    best_infeasible: _Trial | None = None
    checked = 0
    prev: date | None = None
    for day in candidates:
        t = trial(day)
        checked += 1
        if t.min_cents >= floor_cents:
            if found is None:
                found, feasible_through = t, day
            elif prev == feasible_through:
                feasible_through = day
        elif found is not None:
            broke_on = day  # first infeasible date after the answer bounds feasible_through
            break
        else:
            last_infeasible = t
            if best_infeasible is None or t.min_cents > best_infeasible.min_cents:
                best_infeasible = t
        prev = day

    def miss(t: _Trial) -> dict:
        return {
            "date": t.day.isoformat(),
            "min_after": t.min_after(),
            "shortfall": cents_to_str(floor_cents - t.min_cents),
        }

    data: dict = {
        "as_of": as_of.isoformat(),
        "until": until.isoformat(),
        "starting_balance": cents_to_str(starting_balance_cents),
        "weekly_spend": cents_to_str(weekly_spend_cents),
        "floor": cents_to_str(floor_cents),
        "measure": measure,
        "candidates": {
            "from": first.isoformat(),
            "before": last.isoformat(),
            "step_days": step,
            "weekdays_only": weekdays_only,
            "checked": checked,
        },
        "status": "found" if found else "none_in_range",
        "date": found.day.isoformat() if found else None,
        "feasible_through": feasible_through.isoformat() if feasible_through else None,
        "result": None,
        "last_infeasible": miss(last_infeasible) if last_infeasible else None,
        "best_infeasible": miss(best_infeasible) if best_infeasible and not found else None,
    }
    shown = found or last_infeasible
    if found:
        r, calc, day = found.result, found.calc, found.day
        bal = dict(r.daily)
        dates = placeholder_dates(spec, day)
        entries = [
            e
            for e in r.ledger
            if e.date in dates
            and (e.kind in SCENARIO_KINDS or (isinstance(e.key, str) and e.key.startswith("s:")))
        ]
        data["result"] = {
            "date": day.isoformat(),
            "balance_on_date": cents_to_str(bal[day]),
            "spare_on_date": cents_to_str(calc.spare_cents(day, bal[day])),
            "min_after": found.min_after(),
            "headroom": cents_to_str(found.min_cents - floor_cents),
            "ending_balance": cents_to_str(r.ending_balance_cents),
            "spare_balance": cents_to_str(calc.spare_cents(until, r.ending_balance_cents)),
            "placeholder_entries": [e.to_json() for e in entries],
        }
        if verbose:
            data["result"]["debts"] = [d.to_json() for d in r.debts.values()]
    data["scenario"] = (shown.model.scenario or {}).get("name") if shown else None
    data["scenario_applied"] = shown.model.applied if shown else []
    warnings = list(dict.fromkeys(shown.result.warnings)) if shown else []
    if found and broke_on:
        warnings.append(
            f"feasible from {data['date']} through {feasible_through}, but {broke_on} fails the "
            f"{measure} floor again (a later bill lands before the next income); the earliest "
            "date is not a one-way door"
        )
    if not found:
        warnings.append(
            f"no date between {first} and {last} keeps the {measure} at or above "
            f"{cents_to_str(floor_cents)} through {until}; best_infeasible is the closest miss"
        )
    return data, warnings
