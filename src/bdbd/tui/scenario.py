"""The what-if sandbox as data: changes a person tries out without saving anything.

A `Scenario` is a list of `Change`s plus the lens switch. It turns into the same `WhatIf` flags
the JSON commands take, so the app and `bdbd project --settle …` always agree. Nothing here is
ever written to the budget file; the scenario lives for one app session.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import NamedTuple

from bdbd.budget import WhatIf
from bdbd.core.errors import CashError
from bdbd.core.money import parse_amount, parse_rate
from bdbd.core.shortcuts import merge_scenarios
from bdbd.ui.theme import money_short, pct
from bdbd.words import fmt_date


class KindSpec(NamedTuple):
    """How a kind of change is asked for: its menu label, what it targets, its fields."""

    label: str
    target: str | None  # "debt" | "name" | "flow" | "tag" | "paused" | None (no target)
    amount: str | None  # the amount field's label, or None when there's no amount
    date: str  # "required" | "optional" | "none"


KINDS: dict[str, KindSpec] = {
    "payoff": KindSpec("Pay off a debt", "debt", None, "required"),
    "settle": KindSpec("Sell something and clear its loan", "debt", "Sale proceeds", "required"),
    "extra_payment": KindSpec("Make an extra debt payment", "debt", "Extra amount", "required"),
    "set_payment": KindSpec("Change a debt's payment", "debt", "New payment", "required"),
    "rate_change": KindSpec("Change a debt's rate", "debt", "New rate", "required"),
    "add_income": KindSpec("One-off money in", "name", "Amount", "required"),
    "add_expense": KindSpec("One-off money out", "name", "Amount", "required"),
    "set_amount": KindSpec("Change a flow's amount", "flow", "New amount", "optional"),
    "stop": KindSpec("Stop a flow after a date", "flow", None, "required"),
    "stop_tag": KindSpec("Stop a tag after a date", "tag", None, "required"),
    "disable": KindSpec("Leave a flow out", "flow", None, "none"),
    "disable_tag": KindSpec("Leave a tag out", "tag", None, "none"),
    "enable": KindSpec("Bring back a paused flow", "paused", None, "none"),
    "everyday": KindSpec("Different everyday spending", None, "Per week", "none"),
}

_PLACEHOLDER = re.compile(r"^\?(?:([+-])(\d+))?$")


def placeholder_offset(when: str) -> int | None:
    """Days after the earliest date for '?', '?+3', '?-2'; None for a real date or ''."""
    m = _PLACEHOLDER.match(when.strip())
    if not m:
        return None
    return 0 if m.group(2) is None else int(m.group(2)) * (1 if m.group(1) == "+" else -1)


@dataclass(frozen=True)
class Change:
    """One thing to try: sell the car, stop a bill, a one-off expense, …"""

    kind: str  # a key of KINDS
    target: str = ""  # flow name, tag, or the new item's name
    amount: str = ""  # as typed: "13000", "5.5%", "150"
    when: str = ""  # ISO date, "?" (or "?+N"/"?-N"), or "" when the kind has no date
    enabled: bool = True

    @property
    def spec(self) -> KindSpec:
        return KINDS[self.kind]

    @property
    def placeholder(self) -> bool:
        """Dated '?': the earliest date that works (found by the What if view)."""
        return placeholder_offset(self.when) is not None

    @property
    def day(self) -> date | None:
        """The change's date, when it has a real one."""
        if not self.when or self.placeholder:
            return None
        return date.fromisoformat(self.when)

    def flag(self) -> str:
        """The shortcut flag value: 'TARGET:AMOUNT@WHEN', 'TARGET@WHEN' or 'TARGET'."""
        head = f"{self.target}:{self.amount}" if self.spec.amount else self.target
        return f"{head}@{self.when}" if self.when else head


@dataclass
class Scenario:
    """The sandbox: the changes being tried, whether the lens is on, and a pinned '?' date."""

    changes: list[Change] = field(default_factory=list)
    enabled: bool = True  # the lens (w toggles it)
    pinned: date | None = None  # the date '?' resolves to (set from an earliest result)
    extra: dict | None = None  # raw scenario JSON from "try this plan" (debt_events)
    extra_label: str = ""  # the sentence for `extra`, e.g. "Put $300 extra a month toward debts"
    extra_enabled: bool = True  # a tried plan can be switched off, like a change

    @property
    def plan(self) -> dict | None:
        """The tried plan's scenario JSON while it's switched on."""
        return self.extra if self.extra_enabled else None

    @property
    def active(self) -> list[Change]:
        """The changes that are switched on."""
        return [c for c in self.changes if c.enabled]

    @property
    def any(self) -> bool:
        """Whether there's anything switched on to try (changes or a plan)."""
        return bool(self.active) or self.plan is not None

    @property
    def placeholders(self) -> bool:
        """Whether an enabled change is dated '?'."""
        return any(c.placeholder for c in self.active)

    @property
    def unpinned(self) -> bool:
        """An enabled '?' date that no earliest result has pinned yet."""
        return self.placeholders and self.pinned is None

    def copy(self) -> Scenario:
        """A snapshot that later edits to this one won't touch (for workers)."""
        return replace(self, changes=list(self.changes))

    def whatif(self) -> WhatIf:
        """The same flags `bdbd project --settle … --stop …` would take."""
        whatif = WhatIf(on=self.pinned.isoformat() if self.pinned and self.placeholders else None)
        loaded = self.plan
        for c in self.active:
            if c.kind == "everyday":  # the last one wins
                loaded = merge_scenarios(loaded, {"weekly_spend": c.amount})
            else:
                getattr(whatif, c.kind).append(c.flag())
        whatif.scenario_json = json.dumps(loaded) if loaded else None
        return whatif

    # sentences -------------------------------------------------------------------

    def sentences(self, today: date, *, short: bool = False) -> list[str]:
        """Every enabled change (and a tried plan) as a short sentence."""
        out = [self.sentence(c, today, short=short) for c in self.active]
        if self.plan is not None:
            out.append(self.extra_label or "A payoff plan")
        return out

    def sentence(self, change: Change, today: date, *, short: bool = False) -> str:
        """'Sell Car loan for $13,000 on Sun Nov 1' (short=True drops the weekday)."""
        t = change.target
        amt = _amount(change)

        def when(prep: str = "on") -> str:
            return self._when(change, today, short, prep)

        match change.kind:
            case "payoff":
                return f"Pay off {t} {when()}"
            case "settle":
                return f"Sell {t} for {amt} {when()}"
            case "extra_payment":
                return f"Pay {amt} extra on {t} {when()}"
            case "set_payment":
                return f"Change {t}'s payment to {amt} {when('from')}"
            case "rate_change":
                return f"Change {t}'s rate to {amt} {when('from')}"
            case "add_income":
                return f"{t} brings in {amt} {when()}"
            case "add_expense":
                return f"{t} costs {amt} {when()}"
            case "set_amount":
                return f"{t} becomes {amt} {when('from')}".rstrip()
            case "stop":
                return f"Stop {t} {when('after')}"
            case "stop_tag":
                return f"Stop everything tagged {t} {when('after')}"
            case "disable":
                return f"Leave out {t}"
            case "disable_tag":
                return f"Leave out everything tagged {t}"
            case "enable":
                return f"Bring back {t}"
            case "everyday":
                return f"Everyday spending at {amt} a week"
        return KINDS[change.kind].label

    def _when(self, change: Change, today: date, short: bool, prep: str) -> str:
        """'on Sun Nov 1', 'on the earliest date that works', 'after Fri Oct 16 (earliest)'."""
        offset = placeholder_offset(change.when)
        if offset is None:
            day = change.day
            return f"{prep} {fmt_date(day, today, weekday=not short)}" if day else ""
        if self.pinned is not None:
            day = self.pinned + timedelta(days=offset)
            return f"{prep} {fmt_date(day, today, weekday=not short)} (earliest)"
        if offset == 0:
            return f"{prep} the earliest date that works"
        n = abs(offset)
        side = "after" if offset > 0 else "before"
        return f"{n} day{'s' if n != 1 else ''} {side} the earliest date that works"


def _amount(change: Change) -> str:
    """The typed amount as the views show money ('$13,000') or rates ('5.5%')."""
    if not change.amount:
        return ""
    try:
        if change.kind == "rate_change":
            return pct(parse_rate(change.amount))
        return money_short(parse_amount(change.amount))
    except CashError:
        return change.amount
