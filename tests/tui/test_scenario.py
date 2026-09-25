"""The what-if sandbox as data: sentences, and the same flags the JSON commands take."""

from __future__ import annotations

import json
from datetime import date

import pytest

from bdbd.budget import WhatIf
from bdbd.tui.scenario import KINDS, Change, Scenario, placeholder_offset

TODAY = date(2026, 9, 24)


@pytest.mark.parametrize(
    ("change", "sentence"),
    [
        (Change("payoff", "Car loan", when="2026-11-01"), "Pay off Car loan on Sun Nov 1"),
        (
            Change("settle", "Car loan", "13000", "2026-11-01"),
            "Sell Car loan for $13,000 on Sun Nov 1",
        ),
        (
            Change("extra_payment", "Credit card", "500.50", "2026-10-09"),
            "Pay $500.50 extra on Credit card on Fri Oct 9",
        ),
        (
            Change("set_payment", "Car loan", "450", "2026-12-05"),
            "Change Car loan's payment to $450 from Sat Dec 5",
        ),
        (
            Change("rate_change", "Credit card", "5.5%", "2026-12-01"),
            "Change Credit card's rate to 5.5% from Tue Dec 1",
        ),
        (
            Change("add_income", "Bonus", "1200", "2026-12-18"),
            "Bonus brings in $1,200 on Fri Dec 18",
        ),
        (
            Change("add_expense", "Flight", "650", "?"),
            "Flight costs $650 on the earliest date that works",
        ),
        (Change("set_amount", "Rent", "2300"), "Rent becomes $2,300"),
        (Change("set_amount", "Rent", "2300", "2027-01-01"), "Rent becomes $2,300 from Fri Jan 1"),
        (Change("stop", "Gym", when="2026-11-01"), "Stop Gym after Sun Nov 1"),
        (
            Change("stop_tag", "car", when="2026-11-01"),
            "Stop everything tagged car after Sun Nov 1",
        ),
        (Change("disable", "Streaming"), "Leave out Streaming"),
        (Change("disable_tag", "fun"), "Leave out everything tagged fun"),
        (Change("enable", "Gym"), "Bring back Gym"),
        (Change("everyday", amount="150"), "Everyday spending at $150 a week"),
    ],
)
def test_sentences(change: Change, sentence: str) -> None:
    assert Scenario([change]).sentence(change, TODAY) == sentence


def test_every_kind_has_a_sentence_of_its_own() -> None:
    labels = {Scenario().sentence(Change(k, "X", "1", "2026-11-01"), TODAY) for k in KINDS}
    assert len(labels) == len(KINDS)


def test_short_sentences_drop_the_weekday() -> None:
    s = Scenario([Change("settle", "Car loan", "13000", "2026-11-01")])
    assert s.sentences(TODAY, short=True) == ["Sell Car loan for $13,000 on Nov 1"]


def test_placeholders_and_pinning() -> None:
    later = Change("add_expense", "Hotel", "300", "?+3")
    s = Scenario([Change("add_expense", "Flight", "650", "?"), later])
    assert s.placeholders and s.unpinned
    assert s.sentence(later, TODAY) == "Hotel costs $300 3 days after the earliest date that works"
    s.pinned = date(2026, 10, 16)
    assert not s.unpinned
    assert s.sentence(later, TODAY) == "Hotel costs $300 on Mon Oct 19 (earliest)"
    assert s.whatif().on == "2026-10-16"
    assert placeholder_offset("?-2") == -2
    assert placeholder_offset("2026-10-16") is None


def test_whatif_matches_the_cli_flags() -> None:
    s = Scenario(
        [
            Change("settle", "Car loan", "13000", "2026-11-01"),
            Change("stop_tag", "car", when="2026-11-01"),
            Change("disable", "Gym"),
            Change("set_amount", "Rent", "2300"),
            Change("add_expense", "Flight", "650", "2026-10-16", enabled=False),
        ]
    )
    w = s.whatif()
    cli = WhatIf(
        settle=["Car loan:13000@2026-11-01"],
        stop_tag=["car@2026-11-01"],
        disable=["Gym"],
        set_amount=["Rent:2300"],
    )
    assert w.spec(today=TODAY, base=TODAY) == cli.spec(today=TODAY, base=TODAY)
    assert w.add_expense == []  # switched off


def test_everyday_and_extra_become_scenario_json() -> None:
    extra = {"debt_events": [{"flow": "Car loan", "type": "extra_payment", "date": "2026-11-05",
                              "amount": "300"}]}  # fmt: skip
    s = Scenario(
        [Change("everyday", amount="120"), Change("everyday", amount="150")],
        extra=extra,
        extra_label="Put $300 extra a month toward debts",
    )
    loaded = json.loads(s.whatif().scenario_json or "{}")
    assert loaded["weekly_spend"] == "150"  # the last one wins
    assert loaded["debt_events"] == extra["debt_events"]
    assert s.sentences(TODAY)[-1] == "Put $300 extra a month toward debts"
    assert s.any and not Scenario().any


def test_copy_is_a_snapshot() -> None:
    s = Scenario([Change("disable", "Gym")])
    snap = s.copy()
    s.changes.append(Change("disable", "Rent"))
    assert len(snap.changes) == 1
