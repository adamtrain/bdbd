"""Adding and editing a flow, with its optional loan section.

One form does both, and it mirrors the JSON commands so the app and an agent can't disagree:

- Adding is `bdbd add`: the schedule is the When words plus " from STARTS" and " until ENDS".
- Editing is `bdbd edit`: When is only read again if the person changed its words (they're
  prefilled with `describe()`, which drops the start date, so re-reading an unchanged
  "every 2 weeks on Fri" would move the pay week); new words keep the stored start date as
  their anchor unless Starts says otherwise. Ends counts only when it changed (cleared is
  `--no-until`). Only the fields the person changed are saved, onto the flow as it is on disk
  then (`Session.update_flow`), so a change another program made meanwhile survives.
- The loan section is `bdbd debt set` (`repo.set_debt`), capitalize default included: "Usual"
  means yes, except for simple interest. Turning it off on a debt asks first, then stops
  tracking it (`repo.unset_debt`); the payment stays as a plain expense.

The app opens it with `open_add(app, income=…, loan=…, starts=…)` and `open_edit(app, id)`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.suggester import Suggester
from textual.widgets import Static

from bdbd import ask
from bdbd.core.errors import CashError
from bdbd.core.models import (
    Compounding,
    DayCount,
    Debt,
    Flow,
    Kind,
    PaymentMode,
    Weekend,
)
from bdbd.core.queries.debt_schedule import debt_schedule
from bdbd.core.queries.summary import summary
from bdbd.core.recurrence import occurrences
from bdbd.core.scenario import build_effective_model
from bdbd.tui.forms import (
    Action,
    ChoiceField,
    Field,
    FormScreen,
    Parsed,
    SwitchField,
    TextField,
    parse_date_text,
    parse_money,
    parse_rate_text,
    parse_tags_text,
    parse_text,
)
from bdbd.tui.session import DebtTerms, FlowDraft, describe_flow
from bdbd.tui.text import sentence
from bdbd.tui.widgets import bdbd
from bdbd.ui.common import next_date
from bdbd.ui.theme import AMBER, DOT, FAINT, GREEN, RED, cents_of, money, pct, plural
from bdbd.words import (
    SCHEDULE_EXAMPLES,
    Schedule,
    describe,
    fmt_date,
    fmt_month,
    ordinal,
    parse_day,
    parse_schedule,
    relative,
)

if TYPE_CHECKING:
    from bdbd.tui.app import BdbdApp


# ── Choices ───────────────────────────────────────────────────────────────────

KINDS = [(Kind.EXPENSE, "Money out"), (Kind.INCOME, "Money in")]
KIND_NOTES = {
    Kind.EXPENSE: "A bill, a subscription, a loan payment",
    Kind.INCOME: "A paycheck, a refund, a side gig",
}
WEEKENDS = [
    (Weekend.NONE, "Stay put"),
    (Weekend.NEXT, "Move to Monday"),
    (Weekend.PREVIOUS, "Move to Friday"),
]
WEEKEND_NOTES = {
    Weekend.NONE: "Weekend dates stay where they are",
    Weekend.NEXT: "Weekend dates move to Monday, like an ACH pull",
    Weekend.PREVIOUS: "Weekend dates move to the Friday before",
}
WEEKEND_WORDS = {Weekend.NONE: "stay put", Weekend.NEXT: "→Mon", Weekend.PREVIOUS: "→Fri"}
INTERESTS = [
    (Compounding.SIMPLE, "Simple"),
    (Compounding.DAILY, "Daily"),
    (Compounding.MONTHLY, "Monthly"),
    (Compounding.CONTINUOUS, "Continuous"),
]
INTEREST_NOTES = {
    Compounding.SIMPLE: "Car and student loans: it never compounds",
    Compounding.DAILY: "Credit cards: compounds every day",
    Compounding.MONTHLY: "Mortgages: interest posts once a month",
    Compounding.CONTINUOUS: "Compounds continuously (rare)",
}
DAY_COUNTS = [
    (DayCount.ACT_365, "Actual/365"),
    (DayCount.ACT_360, "Actual/360"),
    (DayCount.THIRTY_360, "30/360"),
]
DAY_COUNT_NOTES = {
    DayCount.ACT_365: "Most loans: a day's interest is the rate ÷ 365",
    DayCount.ACT_360: "Some loans: the rate ÷ 360, a little more interest",
    DayCount.THIRTY_360: "Every month counts as 30 days",
}
CAPITALIZE = [(None, "Usual"), (True, "Yes"), (False, "No")]
PAYMENT_MODES = [
    (PaymentMode.FIXED, "Fixed"),
    (PaymentMode.INTEREST_ONLY, "Interest only"),
    (PaymentMode.PERCENT_OF_BALANCE, "Share of balance"),
]
PAYMENT_NOTES = {
    PaymentMode.FIXED: "The amount above, every time",
    PaymentMode.INTEREST_ONLY: "Only the interest, so the balance stays put",
    PaymentMode.PERCENT_OF_BALANCE: "A share of the balance, or the amount if larger",
}
NEW_ID = 0  # the would-be flow's id in previews (stored ids start at 1)


# ── Opening it ────────────────────────────────────────────────────────────────


def open_add(
    app: BdbdApp, *, income: bool = False, loan: bool = False, starts: date | None = None
) -> None:
    """Push the flow form for a new flow (loan=True: an expense with the loan section on)."""
    app.push_screen(FlowForm(income=income and not loan, loan=loan, starts=starts))


def open_edit(app: BdbdApp, flow_id: int, *, loan: bool = False) -> None:
    """Push the flow form for an existing flow (loan=True: open at the loan section)."""
    try:
        flow = app.session.flow(flow_id)
    except CashError as exc:
        app.notify(sentence(exc.message), severity="error", markup=False)
        return
    if loan and flow.kind == Kind.INCOME:
        app.notify("Only money going out can be a loan or a card.", markup=False)
        return
    app.push_screen(FlowForm(flow=flow, loan=loan))


# ── Pieces ────────────────────────────────────────────────────────────────────


class TagSuggester(Suggester):
    """Completes the tag being typed (after the last comma) from the tags already in use."""

    def __init__(self, tags: Iterable[str]) -> None:
        super().__init__(use_cache=False, case_sensitive=False)
        self.tags = list(tags)

    async def get_suggestion(self, value: str) -> str | None:
        head, _, last = value.rpartition(",")
        stem = last.lstrip()
        if not stem:
            return None
        taken = {t.strip() for t in head.split(",")}
        for tag in self.tags:
            if tag.startswith(stem) and tag != stem and tag not in taken:
                return value + tag[len(stem) :]
        return None


class CheckedChoice(ChoiceField):
    """A ChoiceField whose preview (and validity) depends on the rest of the form."""

    def __init__(
        self,
        key: str,
        label: str,
        choices: Sequence[tuple[Any, str]],
        value: Any = None,
        *,
        check: Callable[[Any], Parsed],
    ) -> None:
        super().__init__(key, label, choices, value)
        self.check = check

    def _parse(self, text: str) -> Parsed:
        return self.check(self.choices[self.selected][0])


@dataclass(frozen=True)
class Outlook:
    """A debt's payoff as the form would leave it."""

    paid_off: date | None
    payments: int
    interest: int


# ── The form ──────────────────────────────────────────────────────────────────


class FlowForm(FormScreen[None]):
    """Add or edit an income or expense, and (for an expense) its loan or card details."""

    DEFAULT_CSS = """
    FlowForm {
        & #dialog { width: 76; }
        & #field-when > .field--preview { height: 2; }
        & #field-loan { margin-top: 1; }
        & #loan, #more { height: auto; }
        & #more-toggle {
            margin: 0 0 1 15;
            padding: 0 1;
            background: transparent;
            color: $text-muted;
        }
        & #more-toggle:hover { color: $foreground; background: $boost; }
        & #more-toggle:focus { color: $foreground; background: $primary 18%; text-style: none; }
        & #more { display: none; }
        & #more.-open { display: block; }
    }
    """

    def __init__(
        self,
        *,
        flow: Flow | None = None,
        income: bool = False,
        loan: bool = False,
        starts: date | None = None,
    ) -> None:
        super().__init__()
        self.flow = flow
        self.focus_loan = loan and flow is not None
        self._last: tuple[Any, Any] | None = None  # (money of a draft, (net, outlook))
        f = flow
        debt = f.debt if f else None
        self.initial: dict[str, Any] = {
            "kind": f.kind if f else (Kind.INCOME if income else Kind.EXPENSE),
            "name": f.name if f else "",
            "amount": money(f.amount_cents, symbol=False) if f else "",
            "when": describe(f.rrule, f.dtstart) if f else "",
            "starts": fmt_date(f.dtstart) if f else (fmt_date(starts) if starts else ""),
            "ends": fmt_date(f.until) if f and f.until else "",
            "weekend": f.weekend if f else Weekend.NONE,
            "tags": ", ".join(f.tags) if f else "",
            "notes": (f.notes or "") if f else "",
            "active": f.active if f else True,
            "loan": debt is not None or (loan and (f is None or f.kind == Kind.EXPENSE)),
            "balance": money(debt.balance_cents, symbol=False) if debt else "",
            "as_of": fmt_date(debt.balance_as_of) if debt else "today",
            "rate": _rate_text(debt.annual_rate) if debt else "",
            "compounding": debt.compounding if debt else Compounding.SIMPLE,
            "day_count": debt.day_count if debt else DayCount.ACT_365,
            "capitalize": _capitalize_choice(debt) if debt else None,
            "payment_mode": debt.payment_mode if debt else PaymentMode.FIXED,
            "payment_pct": _rate_text(debt.payment_pct) if debt and debt.payment_pct else "",
            "principal": (
                money(debt.original_principal_cents, symbol=False)
                if debt and debt.original_principal_cents is not None
                else ""
            ),
            "posting_day": (
                str(debt.posting_day)
                if debt and debt.posting_day and debt.posting_day != debt.balance_as_of.day
                else ""
            ),
        }

    # ── Fields ────────────────────────────────────────────────────────────────────

    def fields(self) -> ComposeResult:
        i = self.initial

        def today() -> date:  # read on every parse: a form left open past midnight moves on
            return self.session.today

        tags = sorted(self.session.tags(), key=lambda t: (-t.flow_count, t.name))
        yield ChoiceField("kind", "Kind", KINDS, i["kind"], notes=KIND_NOTES)
        yield TextField(
            "name",
            "Name",
            i["name"],
            parse=self._parse_name,
            placeholder="Netflix, Rent, Paycheck…",
        )
        yield TextField(
            "amount", "Amount", i["amount"], parse=self._parse_amount, placeholder="15.49"
        )
        yield TextField(
            "when", "When", i["when"], parse=self._parse_when, placeholder=SCHEDULE_EXAMPLES[0]
        )
        blank = "Blank: keeps its start date" if self.flow else "Blank: the next date it happens"
        yield TextField(
            "starts",
            "Starts",
            i["starts"],
            parse=parse_date_text(today, prefer="nearest", optional=True, blank=blank),
            placeholder="optional",
        )
        yield TextField(
            "ends",
            "Ends",
            i["ends"],
            parse=parse_date_text(today, optional=True, blank="Blank: it keeps going"),
            placeholder="optional",
        )
        yield ChoiceField("weekend", "Weekends", WEEKENDS, i["weekend"], notes=WEEKEND_NOTES)
        yield TextField(
            "tags",
            "Tags",
            i["tags"],
            parse=self._parse_tags,
            placeholder="optional, comma-separated",
            suggester=TagSuggester(t.name for t in tags),
        )
        yield TextField(
            "notes",
            "Notes",
            i["notes"],
            parse=parse_text(required=False),
            placeholder="optional",
        )
        if self.flow is not None:
            yield SwitchField(
                "active",
                "Status",
                i["active"],
                yes="Active",
                no="Paused",
                notes={
                    True: "In every projection",
                    False: "Saved, but left out of every projection",
                },
            )
        yield SwitchField(
            "loan",
            "Loan or card",
            i["loan"],
            notes={
                True: "bdbd tracks what's owed and when it's paid off",
                False: "Switch on if this pays off a loan or a card",
            },
        )
        with Vertical(id="loan"):
            yield TextField(
                "balance",
                "Balance owed",
                i["balance"],
                parse=parse_money(example="14,860.00"),
                placeholder="what you still owe",
            )
            yield TextField(
                "as_of",
                "As of",
                i["as_of"],
                parse=self._parse_as_of,
                placeholder="today",
            )
            yield TextField(
                "rate",
                "Rate",
                i["rate"],
                parse=parse_rate_text(example="6.49%"),
                placeholder="6.49%",
            )
            yield ChoiceField(
                "compounding", "Interest", INTERESTS, i["compounding"], notes=INTEREST_NOTES
            )
            more = i["day_count"] != DayCount.ACT_365 or any(
                i[k] not in ("", None, PaymentMode.FIXED)
                for k in ("capitalize", "payment_mode", "principal", "posting_day")
            )
            yield Action(_more_label(more), id="more-toggle")
            with Vertical(id="more", classes="-open" if more else ""):
                yield ChoiceField(
                    "day_count", "Day count", DAY_COUNTS, i["day_count"], notes=DAY_COUNT_NOTES
                )
                yield CheckedChoice(
                    "capitalize",
                    "Capitalize",
                    CAPITALIZE,
                    i["capitalize"],
                    check=self._check_capitalize,
                )
                yield ChoiceField(
                    "payment_mode", "Payment", PAYMENT_MODES, i["payment_mode"], notes=PAYMENT_NOTES
                )
                yield TextField(
                    "payment_pct",
                    "Minimum %",
                    i["payment_pct"],
                    parse=self._parse_share,
                    placeholder="2%",
                )
                yield TextField(
                    "principal",
                    "Borrowed",
                    i["principal"],
                    parse=parse_money(optional=True, example="18,000.00"),
                    placeholder="optional",
                )
                yield TextField(
                    "posting_day",
                    "Posting day",
                    i["posting_day"],
                    parse=self._parse_posting_day,
                    placeholder="optional",
                )

    def on_mount(self) -> None:
        # FormScreen.on_mount runs after this one (Textual calls both): it draws the summary
        # and focuses the first field, so the name (or the balance) is focused after it.
        self._show_dependents()
        for key in ("when", "amount"):
            self.field(key).reparse()  # now that every field they read is there
        self.query_one("#save", Action).update("Save" if self.flow else "Add")
        self._retitle()
        self.call_after_refresh(self._focus_start)

    def _focus_start(self) -> None:
        if self.focus_loan:
            self.field("balance").focus_field()
            self.call_after_refresh(self.field("loan").scroll_visible, top=True, animate=False)
        else:
            self.field("name").focus_field()

    # ── Reading fields ────────────────────────────────────────────────────────────

    def _text(self, key: str) -> str:
        """A text field's words (its prefill while the form is still being built)."""
        try:
            return self.field(key).text
        except NoMatches:
            return str(self.initial.get(key) or "")

    def _choice(self, key: str) -> Any:
        """A choice field's value (its prefill while the form is still being built)."""
        try:
            f = self.field(key)
        except NoMatches:
            return self.initial[key]
        assert isinstance(f, ChoiceField)
        return f.choices[f.selected][0]

    def _changed(self, key: str, text: str | None = None) -> bool:
        """Whether a text field's words differ from what it was prefilled with."""
        words = self._text(key) if text is None else text
        return _norm(words) != _norm(str(self.initial[key]))

    def _date(self, key: str, prefer: str = "future") -> date | None:
        """A date field's day; None when it's blank or doesn't read."""
        text = self._text(key).strip()
        if not text:
            return None
        try:
            return parse_day(text, today=self.session.today, prefer=prefer)
        except CashError:
            return None

    # ── Parsers ───────────────────────────────────────────────────────────────────

    def _parse_name(self, text: str) -> Parsed:
        name = text.strip()
        if not name:
            return Parsed.bad("A name, e.g. Netflix or Rent")
        if name.isdigit():
            return Parsed.bad("A name with a letter in it (a number alone reads as an id)")
        own = self.flow.id if self.flow else None
        for f in self.session.flows():
            if f.name.lower() == name.lower() and f.id != own:
                return Parsed.bad(f"There's already a flow called {f.name}; try another name")
        return Parsed.good(name)

    def _parse_amount(self, text: str) -> Parsed:
        parsed = parse_money(example="15.49")(text)
        if not parsed.ok:
            return parsed
        income = self._choice("kind") == Kind.INCOME
        loan = not income and self._choice("loan")
        cents = parsed.value
        signed = money(cents if income else -cents, sign=True)
        return Parsed.good(cents, f"{signed} each {'payment' if loan else 'time'}")

    def _parse_when(self, text: str) -> Parsed:
        if not text.strip():
            example = "once" if self._text("starts").strip() else SCHEDULE_EXAMPLES[0]
            return Parsed.bad(f"When it happens, e.g. {example}")
        try:
            sched = self._schedule(text)
        except CashError as exc:
            problem = sentence(exc.message)
            if exc.code == "invalid_schedule":  # one short example, not the core's list
                problem = problem.split("; try")[0]
                problem += f"; try {SCHEDULE_EXAMPLES[0]} or {SCHEDULE_EXAMPLES[4]}"
            return Parsed.bad(problem)
        weekend = self._choice("weekend")
        return Parsed.good(sched, _schedule_preview(sched, self.session.today, weekend))

    def _parse_tags(self, text: str) -> Parsed:
        try:
            parsed = parse_tags_text()(text)
        except CashError as exc:
            return Parsed.bad(sentence(exc.message))
        own = self.flow.id if self.flow else None
        words = []
        for t in parsed.value:
            others = sum(1 for f in self.session.flows() if t in f.tags and f.id != own)
            if not others and not any(t == x.name for x in self.session.tags()):
                words.append(f"{t} (new)")
            elif others:
                words.append(f"{t} (also on {plural(others, 'other flow')})")
            else:
                words.append(f"{t} (only here)")
        return Parsed.good(parsed.value, f" {DOT} ".join(words))

    def _parse_as_of(self, text: str) -> Parsed:
        today = self.session.today
        if not text.strip():
            return Parsed.good(None, f"Blank: today, {fmt_date(today, today, weekday=True)}")
        parsed = parse_date_text(today, prefer="nearest")(text)
        if not parsed.ok:
            return parsed
        day: date = parsed.value  # after that day's payment, like `bdbd debt set --as-of`
        when = f"{fmt_date(day, today, weekday=True)} {DOT} {relative(day, today)}"
        return Parsed.good(day, f"{when} {DOT} after that day's payment")

    def _parse_share(self, text: str) -> Parsed:
        parsed = parse_rate_text(example="2%")(text)
        if not parsed.ok:
            return Parsed.bad("The minimum as a share of the balance, e.g. 2%")
        return Parsed.good(parsed.value, f"At least {pct(parsed.value)} of the balance")

    def _parse_posting_day(self, text: str) -> Parsed:
        s = text.strip().lower()
        if not s:
            day = self._date("as_of", "nearest") or self.session.today
            return Parsed.good(None, f"Blank: the {ordinal(day.day)}, the day of the balance date")
        m = re.fullmatch(r"(?:the )?(\d{1,2})(?:st|nd|rd|th)?", s)
        if not m or not 1 <= int(m.group(1)) <= 31:
            return Parsed.bad("A day of the month from 1 to 31, e.g. 15")
        n = int(m.group(1))
        return Parsed.good(n, f"Interest posts on the {ordinal(n)} of each month")

    def _check_capitalize(self, value: bool | None) -> Parsed:
        simple = self._choice("compounding") == Compounding.SIMPLE
        if value is None:
            if simple:
                return Parsed.good(None, "Usual: simple interest never joins the balance")
            return Parsed.good(None, "Usual: unpaid interest joins the balance")
        if value and simple:
            return Parsed.bad("Simple interest never capitalizes; pick Usual")
        if value:
            return Parsed.good(True, "Unpaid interest joins the balance")
        return Parsed.good(False, "Unpaid interest waits apart and earns none")

    # ── The schedule (mirrors bdbd add / bdbd edit) ──────────────────────────────

    def _schedule(self, when: str) -> Schedule:
        """The rrule, start and end the form would save (raises CashError)."""
        today = self.session.today
        starts = self._text("starts").strip()
        ends = self._text("ends").strip()
        starts_ok = not starts or self._date("starts", "nearest") is not None
        ends_ok = not ends or self._date("ends") is not None
        if self.flow is None:  # bdbd add: one phrase
            phrase = when.strip()
            if starts and starts_ok:
                phrase += f" from {starts}"
            if ends and ends_ok:
                phrase += f" until {ends}"
            return parse_schedule(phrase, today=today)
        f = self.flow
        rrule, dtstart, until = f.rrule, f.dtstart, f.until
        if self._changed("when", when):  # bdbd edit --when W --from S (or the stored start)
            anchor = starts if starts and starts_ok else f.dtstart.isoformat()
            sched = parse_schedule(f"{when.strip()} from {anchor}", today=today)
            rrule, dtstart = sched.rrule, sched.dtstart
            if sched.until is not None:
                until = sched.until
        elif starts and starts_ok:  # bdbd edit --from S
            dtstart = parse_day(starts, today=today, prefer="nearest")
        if self._changed("ends") and ends_ok:  # --until E, or --no-until when cleared
            until = self._date("ends") if ends else None
        if until is not None and until < dtstart:
            raise CashError(
                f"it would end ({fmt_date(until, today)}) before it starts "
                f"({fmt_date(dtstart, today)})",
                "invalid_date",
            )
        return Schedule(rrule, dtstart, until)

    # ── Showing and hiding ───────────────────────────────────────────────────────

    def changed(self, field: Field) -> None:
        key = field.key
        if key in ("starts", "ends", "weekend"):
            self.field("when").reparse()
        elif key == "as_of":
            self.field("posting_day").reparse()
        elif key == "compounding":
            self.field("capitalize").reparse()
        if key in ("kind", "loan"):
            self.field("amount").reparse()
            self._retitle()
        if key in ("kind", "loan", "compounding", "payment_mode"):
            self._show_dependents()

    def _show_dependents(self) -> None:
        expense = self._choice("kind") == Kind.EXPENSE
        self.field("loan").display = expense
        self.query_one("#loan", Vertical).display = expense and bool(self._choice("loan"))
        self.field("posting_day").display = self._choice("compounding") == Compounding.MONTHLY
        percent = self._choice("payment_mode") == PaymentMode.PERCENT_OF_BALANCE
        self.field("payment_pct").display = percent

    def _retitle(self) -> None:
        dialog = self.query_one("#dialog")
        if self.flow is not None:
            dialog.border_title = f" Edit {self.flow.name} "
        elif self._choice("kind") == Kind.INCOME:
            dialog.border_title = " Add an income "
        elif self._choice("loan"):
            dialog.border_title = " Add a debt "
        else:
            dialog.border_title = " Add an expense "

    def pressed(self, action: Action) -> None:
        if action.id == "more-toggle":
            more = self.query_one("#more", Vertical)
            opening = not more.has_class("-open")
            more.set_class(opening, "-open")
            action.update(_more_label(opening))
            if opening:
                self.call_after_refresh(more.scroll_visible)

    # ── What it would mean ────────────────────────────────────────────────────────

    def counted(self) -> list[Field]:
        """The fields that make the flow: the shown ones, and loan terms tucked under More."""
        fields = self.visible_fields()
        if self.query_one("#loan", Vertical).display:
            fields += [f for f in self.query("#more Field").results(Field) if f.display]
        return fields

    def draft(self) -> FlowDraft | None:
        """The flow as the form stands, or None while a field that counts doesn't read."""
        fields = self.counted()
        if any(not f.parsed.ok for f in fields):
            return None
        v = {f.key: f.parsed.value for f in fields}
        sched: Schedule = v["when"]
        kind: Kind = v["kind"]
        return FlowDraft(
            name=v["name"],
            kind=kind,
            amount_cents=v["amount"],
            rrule=sched.rrule,
            dtstart=sched.dtstart,
            until=sched.until,
            weekend=v["weekend"],
            tags=v["tags"],
            notes=v["notes"],
            active=v.get("active", True),
            debt=self._terms(v) if kind == Kind.EXPENSE and v.get("loan") else None,
        )

    def _terms(self, v: dict[str, Any]) -> DebtTerms:
        """The loan section as `bdbd debt set` would take it.

        Editing, a term the person didn't touch keeps its stored value exactly (the form shows
        '6.49%' for 0.0649, 'Usual' for the default capitalizing, a blank posting day for the
        balance date's day), so saving changes only what they changed.
        """
        old = self.flow.debt if self.flow else None

        def kept(key: str, value: Any, stored: Any) -> Any:
            if old is None:
                return value
            touched = (
                self._choice(key) != self.initial[key]
                if isinstance(self.field(key), ChoiceField)
                else self._changed(key)
            )
            return value if touched else stored

        percent = v.get("payment_mode") == PaymentMode.PERCENT_OF_BALANCE
        return DebtTerms(
            balance_cents=kept("balance", v["balance"], old and old.balance_cents),
            balance_as_of=kept(
                "as_of", v["as_of"] or self.session.today, old and old.balance_as_of
            ),
            annual_rate=kept("rate", v["rate"], old and old.annual_rate),
            compounding=kept("compounding", v["compounding"], old and old.compounding),
            day_count=kept(
                "day_count", v.get("day_count", DayCount.ACT_365), old and old.day_count
            ),
            capitalize_interest=kept(
                "capitalize", v.get("capitalize"), old and old.capitalize_interest
            ),
            payment_mode=kept(
                "payment_mode", v.get("payment_mode", PaymentMode.FIXED), old and old.payment_mode
            ),
            payment_pct=(
                kept("payment_pct", v.get("payment_pct"), old and old.payment_pct)
                if percent
                else None
            ),
            original_principal_cents=kept(
                "principal", v.get("principal"), old and old.original_principal_cents
            ),
            posting_day=kept("posting_day", v.get("posting_day"), old and old.posting_day),
        )

    def _consequence(self, draft: FlowDraft | None) -> tuple[int, Outlook | None]:
        """(monthly net after, the debt's payoff) for a draft, remembered while it's the same."""
        key = replace(draft, name="", tags=(), notes=None) if draft else None  # money only
        if self._last is not None and self._last[0] == key:
            return self._last[1]
        flows = [f for f in self.session.flows() if self.flow is None or f.id != self.flow.id]
        if draft is not None:
            flows.append(self._as_flow(draft))
        today = self.session.today
        model = build_effective_model(flows, None, today)
        data, _ = summary(model, as_of=today, by="flow")
        net = cents_of(data["net"]["net_monthly"]) - ask.everyday_monthly(
            self.session.stored_weekly()
        )
        outlook = None
        if draft is not None and draft.debt is not None:
            fid = self.flow.id if self.flow else NEW_ID
            everything = build_effective_model(flows, None, today, include_inactive=True)
            info, _ = debt_schedule(
                everything,
                fid,
                as_of=today,
                until=today + timedelta(days=ask.DEBT_HORIZON_DAYS),
                max_rows=0,
            )
            paid_off = date.fromisoformat(info["payoff_date"]) if info["payoff_date"] else None
            interest = cents_of(info["total_paid_remaining"]) - cents_of(info["balance_at_as_of"])
            outlook = Outlook(paid_off, info["payments_remaining"], interest)
        result = (net, outlook)
        self._last = (key, result)
        return result

    def _as_flow(self, draft: FlowDraft) -> Flow:
        """The draft as a stored flow would look (for previews; nothing is saved)."""
        fid = self.flow.id if self.flow else NEW_ID
        debt = None
        if draft.debt is not None:
            t = draft.debt
            events = self.flow.debt.events if self.flow and self.flow.debt else ()
            debt = Debt(
                flow_id=fid,
                balance_cents=t.balance_cents,
                balance_as_of=t.balance_as_of,
                annual_rate=t.annual_rate,
                compounding=t.compounding,
                day_count=t.day_count,
                capitalize_interest=(
                    t.compounding != Compounding.SIMPLE
                    if t.capitalize_interest is None
                    else t.capitalize_interest
                ),
                payment_mode=t.payment_mode,
                payment_pct=t.payment_pct,
                original_principal_cents=t.original_principal_cents,
                posting_day=t.posting_day or t.balance_as_of.day,
                events=events,
            )
        return Flow(
            id=fid,
            name=draft.name,
            kind=draft.kind,
            amount_cents=draft.amount_cents,
            rrule=draft.rrule,
            dtstart=draft.dtstart,
            until=draft.until,
            active=draft.active,
            notes=draft.notes,
            tags=draft.tags,
            weekend=draft.weekend,
            debt=debt,
        )

    def summary(self) -> Text | None:
        before = self.session.monthly_net()
        draft = self.draft()
        text = Text("Monthly net ", style=FAINT)
        if draft is None:
            text.append(money(before, sign=True), style=FAINT)
            return text
        after, outlook = self._consequence(draft)
        if after == before:
            text.append(f"stays {money(after, sign=True)}", style=FAINT)
        else:
            text.append(money(before, sign=True))
            text.append(" → ", style=FAINT)
            text.append(money(after, sign=True), style=f"bold {GREEN if after >= 0 else RED}")
        if outlook is not None:
            text.append("\n")
            text.append_text(_outlook_text(outlook, self.session.today))
        return text

    # ── Saving ────────────────────────────────────────────────────────────────────

    def save(self, values: dict[str, Any]) -> None:
        draft = self.draft()
        if draft is None:  # a loan term under a closed More doesn't read: show it
            bad = [f for f in self.counted() if not f.parsed.ok]
            more = self.query_one("#more", Vertical)
            more.add_class("-open")
            self.query_one("#more-toggle", Action).update(_more_label(True))
            for f in bad:
                f.touch()
            if bad:
                self.call_after_refresh(bad[0].focus_field)
            self.app.bell()
            return
        old = self.flow
        if old is not None and old.debt is not None and draft.debt is None:
            events = len(old.debt.events)
            extra = f" and its {plural(events, 'recorded event')}" if events else ""
            app = bdbd(self)
            app.confirm(
                f"Stop tracking {old.name} as a debt?",
                f"bdbd forgets its loan details{extra}. The payment stays as a plain expense.",
                lambda: app.call_later(self._commit, draft),
                yes="Stop tracking",
            )
            return
        self._commit(draft)

    def _commit(self, draft: FlowDraft) -> None:
        app = bdbd(self)
        s = self.session
        before = s.monthly_net()
        opened = self.flow
        try:
            if opened is None:
                done = s.add_flow(draft)
                old = None
            else:
                if draft == FlowDraft.of(opened):
                    self.dismiss(None)
                    app.notify(f"Nothing changed in {opened.name}", markup=False)
                    return
                old = s.current(opened)  # as it is on disk now
                was = _payoff_word(s, old.id) if old.debt is not None else None
                done = s.update_flow(opened, draft)
        except CashError as exc:
            self._error(exc)
            return
        flow = done.flow
        assert flow is not None
        after = s.monthly_net()
        if old is None:
            message = _added_message(s, flow, before, after)
        else:
            message = _updated_message(s, old, flow, before, after, was)
            if old != opened:
                message += f" {DOT} it had changed on disk too, so your edits went on top"
        self.dismiss(None)
        app.changed(message)

    def _error(self, exc: CashError) -> None:
        summary = self.query_one("#summary", Static)
        summary.update(Text(f"✗ {sentence(exc.message)}", style=RED))
        summary.add_class("-error")
        summary.display = True


# ── Words ─────────────────────────────────────────────────────────────────────


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _rate_text(rate: Decimal) -> str:
    """A stored rate as it's typed, exactly: Decimal('0.0649') -> '6.49%'."""
    percent = (rate * 100).normalize()
    return f"{percent:f}%"


def _capitalize_choice(debt: Debt) -> bool | None:
    """Usual (None) when the stored flag is what `debt set` would pick by itself."""
    usual = debt.compounding != Compounding.SIMPLE
    return None if debt.capitalize_interest == usual else debt.capitalize_interest


def _more_label(open_: bool) -> str:
    return "▾ Fewer loan terms" if open_ else "▸ More loan terms"


def _schedule_preview(sched: Schedule, today: date, weekend: Weekend = Weekend.NONE) -> Text:
    """Two lines: 'Monthly on the 12th, until Dec 31, 2027' and the next three dates (after
    the weekend rule moves them, as the flow will)."""
    words = describe(sched.rrule, sched.dtstart, sched.until)
    if sched.until is not None and sched.rrule is not None:
        words += f", until {fmt_date(sched.until, today)}"
    ahead = occurrences(
        sched.rrule, sched.dtstart, sched.until, today, today + timedelta(days=800), weekend
    )
    dates = ahead[:3]
    if dates and sched.rrule is None:
        second = f"{fmt_date(dates[0], today, weekday=True)} {DOT} {relative(dates[0], today)}"
    elif dates:
        when = ", ".join(fmt_date(d, today, weekday=True) for d in dates)
        second = f"next {when}"
    elif sched.rrule is None:
        second = f"that was {relative(sched.dtstart, today)}, so nothing is ahead"
    else:
        second = "nothing ahead: it has ended"
    return Text(f"{words}\n{second}")


def _outlook_text(outlook: Outlook, today: date) -> Text:
    if outlook.paid_off is None:
        return Text("Never paid off at this payment; raise the amount", style=AMBER)
    if outlook.paid_off <= today:
        return Text("Paid off already", style=GREEN)
    return Text.assemble(
        ("Paid off ", FAINT),
        (fmt_month(outlook.paid_off), "bold"),
        (f" {DOT} ", FAINT),
        (plural(outlook.payments, "payment"), FAINT),
        (f" {DOT} {money(outlook.interest)} interest to go", FAINT),
    )


def _net(before: int, after: int) -> str:
    if after == before:
        return f"monthly net stays {money(after, sign=True)}"
    return f"monthly net {money(before, sign=True)} → {money(after, sign=True)}"


def _payoff_word(s: Any, flow_id: Any) -> str | None:
    """'Feb 2030' for a stored debt ('never' when it isn't paid off), None for no debt."""
    row = next((r for r in s.debts(baseline=True) if r.key == flow_id), None)
    if row is None:
        return None
    return fmt_month(row.paid_off_on) if row.paid_off_on else "never"


def _added_message(s: Any, flow: Flow, before: int, after: int) -> str:
    """'Added Netflix · -$15.49 monthly on the 12th · next Mon Oct 12 · monthly net …'."""
    today = s.today
    parts = [f"Added {flow.name}" + ("" if flow.active else ", paused"), describe_flow(flow)]
    nxt = next_date(flow, today) if flow.active else None
    if nxt is not None and flow.rrule is not None:
        parts.append(f"next {fmt_date(nxt, today, weekday=True)}")
    if flow.debt is not None:
        payoff = _payoff_word(s, flow.id)
        parts.append(
            "never paid off at this payment" if payoff == "never" else f"paid off {payoff}"
        )
    parts.append(_net(before, after))
    return f" {DOT} ".join(parts)


def _flow_words(f: Flow) -> dict[str, str]:
    """A flow's fields the way a toast names them."""
    return {
        "name": f.name,
        "kind": "money in" if f.kind == Kind.INCOME else "money out",
        "amount": money(f.amount_cents),
        "schedule": describe(f.rrule, f.dtstart),
        "starts": fmt_date(f.dtstart),
        "ends": fmt_date(f.until) if f.until else "never",
        "weekends": WEEKEND_WORDS.get(f.weekend, str(f.weekend)),
        "tags": ", ".join(sorted(f.tags)) or "none",
    }


def _debt_words(d: Debt) -> dict[str, str]:
    return {
        "balance owed": money(d.balance_cents),
        "as of": fmt_date(d.balance_as_of),
        "rate": _rate_text(d.annual_rate),
        "interest": str(d.compounding),
        "day count": str(d.day_count),
        "unpaid interest": "capitalizes" if d.capitalize_interest else "never capitalizes",
        "payment": str(d.payment_mode).replace("_", " "),
        "minimum": pct(d.payment_pct) if d.payment_pct is not None else "none",
        "borrowed": money(d.original_principal_cents)
        if d.original_principal_cents is not None
        else "not set",
        "posting day": f"the {ordinal(d.posting_day)}" if d.posting_day else "not set",
    }


def _changes(old: Flow, new: Flow) -> list[str]:
    """What an edit changed, in words: ['amount $2,150.00 → $2,200.00', 'paused']."""
    changes: list[str] = []
    a, b = _flow_words(old), _flow_words(new)
    changes += [f"{k} {a[k]} → {b[k]}" for k in a if a[k] != b[k]]
    if (old.notes or "") != (new.notes or ""):
        changes.append("notes cleared" if not new.notes else "notes updated")
    if old.active != new.active:
        changes.append("resumed" if new.active else "paused")
    if old.debt is None and new.debt is not None:
        d = new.debt
        changes.append(f"now a debt: {money(d.balance_cents)} owed at {pct(d.annual_rate)}")
    elif old.debt is not None and new.debt is None:
        changes.append("no longer a debt")
    elif old.debt is not None and new.debt is not None:
        da, db = _debt_words(old.debt), _debt_words(new.debt)
        changes += [f"{k} {da[k]} → {db[k]}" for k in da if da[k] != db[k]]
    return changes


def _updated_message(s: Any, old: Flow, new: Flow, before: int, after: int, was: str | None) -> str:
    """'Updated Rent · amount $2,150.00 → $2,200.00 · monthly net …'."""
    parts = [f"Updated {new.name}", *_changes(old, new)]
    now = _payoff_word(s, new.id) if new.debt is not None else None
    if now is not None and was is not None and now != was:
        parts.append(f"paid off {was} → {now}")
    elif now is not None and was is None:
        parts.append("never paid off at this payment" if now == "never" else f"paid off {now}")
    if after != before:
        parts.append(_net(before, after))
    return f" {DOT} ".join(parts)
