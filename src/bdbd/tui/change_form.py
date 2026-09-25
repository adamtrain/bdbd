"""Add or edit a what-if change: first what kind of change, then only the fields it needs.

The first rows choose the change: About (Debts, One-offs, Flows, Tags, Everyday) and, within
it, what to try (Pay off, Sell, Extra payment, …). The rest follow the kind: what it applies to
(a debt, a flow, a tag, a paused flow, or a new item's name), completed from what's in the
budget as you type; an amount or a rate; and a date in words, or '?' to let the What if view
find the earliest date that works ('?+3' three days after it). Every field reads live and is
checked the way the core checks it, so a saved change always applies to the budget.

Nothing here touches the budget file: saving puts the change in the session's sandbox.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable
from dataclasses import replace
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.css.query import NoMatches
from textual.suggester import Suggester
from textual.widgets import Static

from bdbd import ask
from bdbd.core.errors import CashError
from bdbd.core.models import Flow
from bdbd.core.money import cents_to_str, parse_amount
from bdbd.core.repo import normalize_tag
from bdbd.tui.forms import (
    ChoiceField,
    Field,
    FormScreen,
    Parsed,
    TextField,
    day_preview,
    parse_money,
    parse_rate_text,
)
from bdbd.tui.scenario import KINDS, Change, Scenario, placeholder_offset
from bdbd.tui.session import describe_flow
from bdbd.tui.widgets import bdbd, human_warning
from bdbd.ui.theme import ACCENT, AMBER, FOREGROUND, GREEN, money, pct
from bdbd.words import fmt_date, fmt_month, join, parse_day

# ── What can be tried ─────────────────────────────────────────────────────────

GROUPS: list[tuple[str, str]] = [
    ("debts", "Debts"),
    ("oneoffs", "One-offs"),
    ("flows", "Flows"),
    ("tags", "Tags"),
    ("everyday", "Everyday"),
]
GROUP_NOTES = {
    "debts": "The loans and cards bdbd tracks",
    "oneoffs": "Money in or out, once, only in the what-if",
    "flows": "Your incomes and bills",
    "tags": "Every flow with a tag at once",
    "everyday": "Groceries and incidentals, a little each day",
}
GROUP_KINDS: dict[str, list[tuple[str, str]]] = {
    "debts": [
        ("payoff", "Pay off"),
        ("settle", "Sell"),
        ("extra_payment", "Extra"),
        ("set_payment", "Payment"),
        ("rate_change", "Rate"),
    ],
    "oneoffs": [("add_expense", "Money out"), ("add_income", "Money in")],
    "flows": [
        ("set_amount", "New amount"),
        ("stop", "Stop"),
        ("disable", "Leave out"),
        ("enable", "Bring back"),
    ],
    "tags": [("stop_tag", "Stop"), ("disable_tag", "Leave out")],
    "everyday": [("everyday", "Per week")],
}
GROUP_OF = {kind: group for group, kinds in GROUP_KINDS.items() for kind, _ in kinds}
KIND_NOTES = {
    "payoff": "Pay the whole balance on the day; the payments stop",
    "settle": "Sell something and clear its loan with what it fetches",
    "extra_payment": "A one-off payment on top of the usual one",
    "set_payment": "A new regular payment from a date",
    "rate_change": "A new yearly rate from a date",
    "add_expense": "Money out, once: a flight, a repair, a gift",
    "add_income": "Money in, once: a bonus, a sale, a refund",
    "set_amount": "A flow's new amount, from a date or from today",
    "stop": "No more of a flow after a date",
    "disable": "Leave a flow out, as if it weren't in your budget",
    "enable": "Bring a paused flow back into every projection",
    "stop_tag": "No more of anything with a tag after a date",
    "disable_tag": "Leave out everything with a tag",
    "everyday": "Different everyday spending per week",
}
TARGET_LABELS = {
    "debt": "Debt",
    "name": "Name",
    "flow": "Flow",
    "tag": "Tag",
    "paused": "Paused flow",
}
WHEN_LABELS = {"set_payment": "From", "rate_change": "From", "set_amount": "From"}
WHEN_LABELS |= {"stop": "After", "stop_tag": "After"}
DATE_HINT = "nov 1, fri, in 3 weeks or 2026-11-01"
EARLIEST = "the earliest date that works"
PREVIEW_WIDTH = 54  # a preview line's room in the dialog


def open_add(app: Any) -> None:
    """Push the form for a new change."""
    app.push_screen(ChangeForm())


def open_edit(app: Any, index: int) -> None:
    """Push the form on the sandbox's change number `index`."""
    changes = app.session.scenario.changes
    if 0 <= index < len(changes):
        app.push_screen(ChangeForm(index, changes[index]))


def earliest_words(offset: int) -> str:
    """'? = let bdbd find the earliest date that works', '3 days after the earliest …'."""
    if offset == 0:
        return f"? = let bdbd find {EARLIEST}"
    n = abs(offset)
    return f"{n} day{'s' if n != 1 else ''} {'after' if offset > 0 else 'before'} {EARLIEST}"


def resolve(text: str, names: list[str]) -> tuple[str | None, list[str]]:
    """The name `text` means: an exact match (any case), or the only name it starts.

    Returns (name, []) when it's clear, else (None, the names it could start).
    """
    t = text.strip().casefold()
    if not t:
        return None, []
    exact = [n for n in names if n.casefold() == t]
    if exact:
        return exact[0], []
    starts = [n for n in names if n.casefold().startswith(t)]
    if len(starts) == 1:
        return starts[0], []
    return None, starts


class NameSuggester(Suggester):
    """Completes a name from a list the form keeps current (it changes with the kind)."""

    def __init__(self, names: Callable[[], list[str]]) -> None:
        super().__init__(use_cache=False, case_sensitive=False)
        self.names = names

    async def get_suggestion(self, value: str) -> str | None:
        if not value.strip():
            return None
        for name in self.names():
            if name.casefold().startswith(value) and name.casefold() != value:
                return name
        return None


# ── The form ──────────────────────────────────────────────────────────────────


class ChangeForm(FormScreen[Change]):
    """Add a change to the what-if, or edit one (nothing is saved to the budget)."""

    DEFAULT_CSS = """
    ChangeForm {
        & #dialog { width: 76; }
        & #field-target { margin-top: 1; }
    }
    """

    TITLE: ClassVar[str] = "Add a what-if"
    SAVE: ClassVar[str] = "Add"

    def __init__(self, index: int | None = None, change: Change | None = None) -> None:
        """A new change, or (with both) the sandbox's change number `index`."""
        super().__init__()
        self.index = index
        self.original = change if index is not None else None
        kind = self.original.kind if self.original else "add_expense"
        self._kind = kind
        self._typed: dict[str, str] = {}  # what was typed per target type, to bring back
        self._target_type = KINDS[kind].target
        self._amount_type = _amount_type(kind)

    # ── Fields ────────────────────────────────────────────────────────────────────

    def fields(self) -> ComposeResult:
        o, kind = self.original, self._kind
        group = GROUP_OF[kind]
        yield ChoiceField("about", "About", GROUPS, group, notes=GROUP_NOTES)
        for g, kinds in GROUP_KINDS.items():
            chosen = kind if g == group else kinds[0][0]
            yield ChoiceField(f"kind-{g}", "Try", kinds, chosen, notes=KIND_NOTES)
        spec = KINDS[kind]
        yield TextField(
            "target",
            TARGET_LABELS.get(spec.target or "", "Name"),
            o.target if o else "",
            parse=self._parse_target,
            placeholder=self._target_placeholder(),
            suggester=NameSuggester(self.target_names),
        )
        yield TextField(
            "amount",
            spec.amount or "Amount",
            _amount_text(o) if o else "",
            parse=self._parse_amount,
            placeholder=self._amount_placeholder(),
        )
        yield TextField(
            "when",
            WHEN_LABELS.get(kind, "On"),
            _when_text(o) if o else "",
            parse=self._parse_when,
            placeholder="nov 1, in 3 weeks, or ?",
        )

    def on_mount(self) -> None:
        # FormScreen.on_mount runs after this one: it focuses the first shown field.
        if self.original is not None:
            self.query_one("#dialog").border_title = " Change this what-if "
            self.query_one("#save", Static).update("Save")
        self._show(self._kind)
        if self.original is not None:
            self.call_after_refresh(self._focus_first_box)

    def _focus_first_box(self) -> None:
        for f in self.visible_fields():
            if isinstance(f, TextField):
                f.focus_field()
                return

    # ── The kind ──────────────────────────────────────────────────────────────────

    @property
    def kind(self) -> str:
        """The kind the choices point at now."""
        group = self._choice("about", GROUP_OF[self._kind])
        kinds = GROUP_KINDS[group]
        if len(kinds) == 1:
            return kinds[0][0]
        return self._choice(f"kind-{group}", kinds[0][0])

    def _choice(self, key: str, default: str) -> str:
        try:
            f = self.field(key)
        except NoMatches:
            return default
        assert isinstance(f, ChoiceField)
        return f.choices[f.selected][0]

    def changed(self, field: Field) -> None:
        if field.key == "about" or field.key.startswith("kind-"):
            kind = self.kind
            if kind != self._kind:
                self._kind = kind
                self._show(kind)
        elif field.key == "target":
            self.field("amount").reparse()  # its preview mentions what it applies to

    def _show(self, kind: str) -> None:
        """Show the fields `kind` needs, labeled for it, and read them again."""
        spec = KINDS[kind]
        group = GROUP_OF[kind]
        for g, kinds in GROUP_KINDS.items():
            self.field(f"kind-{g}").display = g == group and len(kinds) > 1
        target = self.field("target")
        assert isinstance(target, TextField)
        target.display = spec.target is not None
        if spec.target is not None:
            self._relabel(target, TARGET_LABELS[spec.target])
            target.input.placeholder = self._target_placeholder()
            self._carry(target, self._target_type, spec.target)
            self._target_type = spec.target
        amount = self.field("amount")
        assert isinstance(amount, TextField)
        amount.display = spec.amount is not None
        if spec.amount is not None:
            self._relabel(amount, spec.amount)
            amount.input.placeholder = self._amount_placeholder()
            kind_of_amount = _amount_type(kind)
            self._carry(amount, f"amount-{self._amount_type}", f"amount-{kind_of_amount}")
            self._amount_type = kind_of_amount
        when = self.field("when")
        when.display = spec.date != "none"
        self._relabel(when, WHEN_LABELS.get(kind, "On"))
        for f in (target, amount, when):
            f.reparse()

    def _relabel(self, field: Field, label: str) -> None:
        field.label = label
        field.query_one(".field--label", Static).update(label)

    def _carry(self, field: TextField, old: str | None, new: str) -> None:
        """Keep what's typed when it still reads for the new kind; else bring back what was
        typed for that kind before (or start blank)."""
        if old == new:
            return
        text = field.text
        if old is not None:
            self._typed[old] = text
        if new in self._typed:
            text = self._typed[new]
        elif text.strip() and not field.parse(text).ok:
            text = ""
        if text != field.text:
            field.text = text

    # ── What there is to pick from ────────────────────────────────────────────────

    def _flows(self) -> list[Flow]:
        return self.session.flows()

    def target_names(self) -> list[str]:
        """What the target field completes from, for the kind chosen now."""
        match KINDS[self.kind].target:
            case "debt":
                return [f.name for f in self._flows() if f.debt is not None]
            case "flow":
                flows = self._flows()
                return [f.name for f in flows if f.active] + [f.name for f in flows if not f.active]
            case "paused":
                return [f.name for f in self._flows() if not f.active]
            case "tag":
                return [t.name for t in self.session.tags() if t.flow_count]
        return []

    def _target_placeholder(self) -> str:
        target = KINDS[self._kind].target
        if target == "name":
            return "Flight, Bonus, New laptop…"
        names = self.target_names()
        if not names:
            return ""
        return ", ".join(names[:2]) + ("…" if len(names) > 2 else "")

    def _amount_placeholder(self) -> str:
        return {
            "rate": "5.5%",
            "everyday": money(self.session.stored_weekly() or 17500, symbol=False),
        }.get(_amount_type(self._kind), "650")

    def _target_flow(self) -> Flow | None:
        """The stored flow the target field names (None for a tag, a new name or no match)."""
        if KINDS[self.kind].target not in ("debt", "flow", "paused"):
            return None
        try:
            text = self.field("target").text
        except NoMatches:
            return None
        name, _ = resolve(text, self.target_names())
        return next((f for f in self._flows() if f.name == name), None)

    # ── Parsing ───────────────────────────────────────────────────────────────────

    def _parse_target(self, text: str) -> Parsed:
        target = KINDS[self.kind].target
        if target == "name":
            name = text.strip()
            if not name:
                return Parsed.bad("A name for it, e.g. Flight")
            return Parsed.good(name, "Only in the what-if, never saved")
        if target == "tag":
            return self._parse_tag(text)
        names = self.target_names()
        what = {"debt": "debt", "flow": "flow", "paused": "paused flow"}[target or "flow"]
        if not names:
            return Parsed.bad(
                {
                    "debt": "You don't track any debts; add one in Debts (5)",
                    "paused": "Nothing in your budget is paused",
                }.get(target or "", "Your budget has no flows yet")
            )
        if not text.strip():
            return Parsed.bad(f"Which {what}? {_options(names)}")
        name, starts = resolve(text, names)
        if name is None:
            if starts:
                return Parsed.bad(f"{_options(starts, 'or')}?")
            return Parsed.bad(self._unknown(text.strip(), what, names))
        flow = next(f for f in self._flows() if f.name == name)
        return Parsed.good(name, self._flow_note(flow, text))

    def _unknown(self, text: str, what: str, names: list[str]) -> str:
        """'Rent isn't a debt; try Car loan …' or 'No flow called …; did you mean …?'."""
        flows = self._flows()
        other, _ = resolve(text, [f.name for f in flows])
        if other is not None:
            flow = next(f for f in flows if f.name == other)
            if what == "debt":
                return f"{other} isn't a debt; try {_options(names, 'or')}"
            if what == "paused" and flow.active:
                return f"{other} isn't paused; try {_options(names, 'or')}"
        close = difflib.get_close_matches(text.casefold(), [n.casefold() for n in names], 3, 0.5)
        near = [n for n in names if n.casefold() in close]
        if near:
            return f"No {what} called {text!r}; did you mean {_options(near, 'or')}?"
        return f"No {what} called {text!r}; try {_options(names, 'or')}"

    def _flow_note(self, flow: Flow, typed: str) -> str:
        """What the picked flow is: '$14,907.56 owed today at 6.49% · paid off Feb 2030'."""
        name = f"{flow.name} · " if flow.name.casefold() != typed.strip().casefold() else ""
        if not flow.active:
            if self.kind == "enable":
                return f"{name}{describe_flow(flow)}"
            return f"{name}Paused in your budget, so this changes nothing on its own"
        if GROUP_OF.get(self.kind) == "debts":
            row = next((r for r in self.session.debts(baseline=True) if r.key == flow.id), None)
            if row is not None:
                owed = f"{name}{money(row.balance)} owed today at {pct(row.rate)}"
                paid = f" · paid off {fmt_month(row.paid_off_on)}" if row.paid_off_on else ""
                return owed + paid if len(owed + paid) <= PREVIEW_WIDTH else owed
        return f"{name}{describe_flow(flow)}"

    def _parse_tag(self, text: str) -> Parsed:
        names = self.target_names()
        if not names:
            return Parsed.bad("None of your flows has a tag yet")
        if not text.strip():
            return Parsed.bad(f"Which tag? {_options(names)}")
        name, starts = resolve(normalize_tag(text), names)
        if name is None:
            if starts:
                return Parsed.bad(f"{_options(starts, 'or')}?")
            return Parsed.bad(f"No flow has the tag {text.strip()!r}; try {_options(names, 'or')}")
        tagged = [f for f in self._flows() if name in f.tags]
        active = [f.name for f in tagged if f.active]
        lead = f"{name} · " if name != text.strip().casefold() else ""
        if not active:
            return Parsed.good(name, f"{lead}Only paused flows have it, so this changes nothing")
        return Parsed.good(name, f"{lead}{_names(active)}")

    def _parse_amount(self, text: str) -> Parsed:
        kind = self.kind
        if _amount_type(kind) == "rate":
            parsed = parse_rate_text(example="5.5%")(text)
            flow = self._target_flow()
            if parsed.ok and flow is not None and flow.debt is not None:
                return Parsed.good(
                    parsed.value, f"{parsed.preview} · now {pct(flow.debt.annual_rate)}"
                )
            return parsed
        example = {"settle": "13,000", "everyday": "200", "add_income": "1,200"}.get(kind, "650")
        parsed = parse_money(zero=kind in ("settle", "set_payment", "set_amount", "everyday"),
                             example=example)(text)  # fmt: skip
        if not parsed.ok:
            return parsed
        cents: int = parsed.value
        return Parsed.good(cents, self._amount_note(kind, cents))

    def _amount_note(self, kind: str, cents: int) -> str | Text:
        """'$13,000.00 for it · $14,297.54 owed today', '$200.00 a week · now …'."""
        flow = self._target_flow()
        shown = money(cents)
        match kind:
            case "add_income":
                return Text.assemble((money(cents, sign=True), GREEN), ", once")
            case "add_expense":
                return f"{money(-cents)}, once"
            case "everyday":
                monthly = money(ask.everyday_monthly(cents))
                now = self.session.stored_weekly()
                return f"{shown} a week · about {monthly} a month · now {money(now)}"
            case "extra_payment":
                return f"{shown} on top of the usual payment"
        if flow is None:
            return shown
        match kind:
            case "settle":
                row = next((r for r in self.session.debts(baseline=True) if r.key == flow.id), None)
                if row is None:
                    return shown
                return f"{shown} for it · {money(row.balance)} owed today"
            case "set_payment" | "set_amount":
                return f"{shown} each time · now {money(flow.amount_cents)}"
        return shown

    def _parse_when(self, text: str) -> Parsed:
        s = text.strip()
        today = self.session.today
        if not s:
            if KINDS[self.kind].date == "optional":
                return Parsed.good("", "Blank: from today")
            return Parsed.bad("A date like nov 1, or ? to let bdbd find the earliest that works")
        squeezed = s.replace(" ", "")
        offset = placeholder_offset(squeezed)
        if offset is not None:
            return Parsed.good(squeezed, earliest_words(offset))
        if squeezed.startswith("?"):
            return Parsed.bad("? alone, or ?+3 / ?-2 for days after or before the earliest")
        try:
            day = parse_day(s, today=today, prefer="future")
        except CashError:
            return Parsed.bad(f"Try {DATE_HINT}, or ?")
        if day < today:
            return Parsed.bad(
                f"{fmt_date(day, today, weekday=True)} has passed; pick today or later"
            )
        return Parsed.good(day.isoformat(), day_preview(day, today))

    # ── The change ────────────────────────────────────────────────────────────────

    def draft(self) -> Change | None:
        """The change the fields describe, or None while one doesn't read."""
        fields = self.visible_fields()
        if not all(f.parsed.ok for f in fields):
            return None
        values = {f.key: f.parsed.value for f in fields}
        kind = self.kind
        spec = KINDS[kind]
        amount = values.get("amount") if spec.amount else None
        if amount is None:
            amount_text = ""
        elif _amount_type(kind) == "rate":
            amount_text = self.field("amount").text.strip()
        else:
            amount_text = cents_to_str(amount)
        return Change(
            kind=kind,
            target=(values.get("target") or "") if spec.target else "",
            amount=amount_text,
            when=(values.get("when") or "") if spec.date != "none" else "",
        )

    def summary(self) -> Text:
        change = self.draft()
        if change is None:
            return Text("")
        problem = self.session.check_change(change)
        if problem:
            return Text(f"! {human_warning(problem)}", style=AMBER)
        sentence = Scenario().sentence(change, self.session.today)
        return Text.assemble(("↳ ", ACCENT), (sentence, FOREGROUND))

    def save(self, values: dict[str, Any]) -> None:
        change = self.draft()
        if change is None:  # every field reads by now; this is belt and braces
            raise CashError("something above doesn't read yet", "invalid_change")
        problem = self.session.check_change(change)
        if problem:
            raise CashError(human_warning(problem), "invalid_change")
        s = self.session
        sentence = Scenario().sentence(change, s.today)
        if self.index is None or self.original is None:
            s.add_change(change)
            verb = "Trying"
        else:
            change = replace(change, enabled=self.original.enabled)
            s.replace_change(self.index, change)
            verb = "Now trying"
        tail = " · finding the earliest date" if change.placeholder and change.enabled else ""
        bdbd(self).changed(f"{verb}: {sentence}{tail} · nothing is saved")
        self.dismiss(change)


# ── Pieces ────────────────────────────────────────────────────────────────────


def _amount_type(kind: str) -> str:
    if kind == "rate_change":
        return "rate"
    if kind == "everyday":
        return "everyday"
    return "money"


def _amount_text(change: Change) -> str:
    """The amount as the form shows it again: '13,000.00', or the rate as typed."""
    if not change.amount or change.kind == "rate_change":
        return change.amount
    try:
        return money(parse_amount(change.amount), symbol=False)
    except CashError:
        return change.amount


def _when_text(change: Change) -> str:
    """The date as the form shows it again: 'Nov 1, 2026', or '?' as it was."""
    day = change.day
    return fmt_date(day) if day else change.when


def _options(names: list[str], word: str = "or") -> str:
    """'Car loan, Student loan or Credit card' (the first few)."""
    shown = names[:3]
    if len(names) > 3:
        return ", ".join(shown) + " …"
    if len(shown) <= 2:
        return f" {word} ".join(shown)
    return ", ".join(shown[:-1]) + f" {word} {shown[-1]}"


def _names(names: list[str]) -> str:
    """'Car insurance, Car loan and Car registration' (or '… and 3 more')."""
    if len(names) > 4:
        return ", ".join(names[:3]) + f" and {len(names) - 3} more"
    return join(names)
