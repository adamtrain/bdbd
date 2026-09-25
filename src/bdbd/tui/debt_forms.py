"""Debt dialogs, and the words and numbers the Debts view shares with them.

- `EventForm` (r in the Debts view): record an extra payment, a new rate, a new payment, a
  balance correction or a payoff, like `bdbd debt extra|rate|payment|adjust|payoff`.
- `PlanScreen` (p): an extra amount each month toward debts, one debt at a time, like
  `bdbd plan --extra A --strategy S --from D`. It works the plan out in a thread (from a
  snapshot, never the sqlite connection) and can put it in the what-if sandbox.
- `outlook`, `schedule`: a debt as `bdbd show NAME` and `bdbd debt schedule NAME --all`
  report it (its words, like `terms_words`, are in tui/text.py).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static
from textual.worker import Worker, WorkerState

from bdbd import ask
from bdbd.ask import DEBT_HORIZON_DAYS
from bdbd.core.dates import add_months
from bdbd.core.errors import CashError
from bdbd.core.models import Debt, DebtEvent, EffectiveModel, EventType, Flow, FlowKey
from bdbd.core.queries.debt_schedule import debt_schedule
from bdbd.core.queries.plan import plan as plan_query
from bdbd.core.scenario import build_effective_model
from bdbd.tui.forms import (
    Action,
    ChoiceField,
    Field,
    FormNote,
    FormScreen,
    Parsed,
    Parser,
    TextField,
    parse_date_text,
    parse_money,
    parse_rate_text,
    parse_text,
)
from bdbd.tui.text import DEBOUNCE, NEVER_PAID, SPINNER, sentence
from bdbd.tui.widgets import KeyValues, bdbd, human_warning
from bdbd.ui import charts
from bdbd.ui.common import humanize
from bdbd.ui.theme import (
    AMBER,
    DARK,
    DOT,
    FAINT,
    GREEN,
    PURPLE,
    RED,
    balance_color,
    cents_of,
    money,
    money_short,
    pct,
)
from bdbd.words import fmt_date, fmt_month, months_apart, span

if TYPE_CHECKING:
    from bdbd.tui.session import Session

PLAN_MONTHS = 600  # `bdbd plan`'s default horizon: fifty years
SCHEDULE_MONTHS = 600  # `bdbd debt schedule`'s default horizon


# ── A debt in words and numbers ───────────────────────────────────────────────


def outlook(s: Session, key: FlowKey) -> dict | None:
    """A debt's outlook as `bdbd show NAME` gives it (debt_outlook), with the what-if applied.

    Owed today (`balance_at_as_of`), `payoff_date`, `payments_remaining`,
    `total_paid_remaining` and `total_interest_remaining`; None when the debt isn't there.
    """

    def compute() -> dict | None:
        try:
            data, _ = debt_schedule(
                s.model(include_inactive=True),
                key,
                as_of=s.today,
                until=s.today + timedelta(days=DEBT_HORIZON_DAYS),
                max_rows=0,
            )
        except CashError:
            return None
        return data

    return s.cached(("debts.outlook", key, s.lens_on), compute)


def schedule(s: Session, key: FlowKey) -> dict | None:
    """Every payment ahead, as `bdbd debt schedule NAME --all` gives it (what-if applied)."""

    def compute() -> dict | None:
        try:
            data, _ = debt_schedule(
                s.model(include_inactive=True),
                key,
                as_of=s.today,
                until=add_months(s.today, SCHEDULE_MONTHS),
                max_rows=None,
            )
        except CashError:
            return None
        return data

    return s.cached(("debts.schedule", key, s.lens_on), compute)


def outlook_with(s: Session, key: FlowKey, event: DebtEvent | None) -> dict | None:
    """The stored debt's outlook with one more event: what recording it would do."""
    model = s.model(baseline=True, include_inactive=True)
    flow = next((f for f in model.flows if f.key == key and f.debt is not None), None)
    if flow is None or flow.debt is None:
        return None
    if event is not None:
        flow = replace(flow, debt=replace(flow.debt, events=(*flow.debt.events, event)))
    try:
        data, _ = debt_schedule(
            EffectiveModel(flows=[flow]),
            key,
            as_of=s.today,
            until=s.today + timedelta(days=DEBT_HORIZON_DAYS),
            max_rows=0,
        )
    except CashError:
        return None
    return data


def payoff_day(data: dict | None) -> date | None:
    """The payoff date in an outlook, if it's paid off."""
    if not data or not data["payoff_date"]:
        return None
    return date.fromisoformat(data["payoff_date"])


# ── Recording an event ────────────────────────────────────────────────────────

EVENT_CHOICES: list[tuple[EventType, str]] = [
    (EventType.EXTRA_PAYMENT, "Extra"),
    (EventType.RATE_CHANGE, "Rate"),
    (EventType.PAYMENT_CHANGE, "Payment"),
    (EventType.BALANCE_ADJUSTMENT, "Correction"),
    (EventType.PAYOFF, "Paid off"),
]
EVENT_NOTES: dict[Any, str] = {
    EventType.EXTRA_PAYMENT: "Paid on top of the regular payment, off the balance",
    EventType.RATE_CHANGE: "The yearly rate from that day on",
    EventType.PAYMENT_CHANGE: "The regular payment from that day on",
    EventType.BALANCE_ADJUSTMENT: "A fee adds to what's owed; a refund (-40) takes off",
    EventType.PAYOFF: "The whole balance paid that day; the payments stop",
}
AMOUNT_FIELD: dict[EventType, str | None] = {  # the field each kind of event asks for
    EventType.EXTRA_PAYMENT: "extra",
    EventType.RATE_CHANGE: "rate",
    EventType.PAYMENT_CHANGE: "payment",
    EventType.BALANCE_ADJUSTMENT: "change",
    EventType.PAYOFF: None,
}


def _worded(parse: Parser, words: Callable[[Any], str]) -> Parser:
    """A parser whose preview, when the text reads, is `words(value)`."""

    def wrapped(text: str) -> Parsed:
        got = parse(text)
        return Parsed.good(got.value, words(got.value)) if got.ok else got

    return wrapped


def _parse_change(text: str) -> Parsed:
    """A balance correction: '+25' or '25' adds, '-40' takes off; zero changes nothing."""
    got = parse_money(negative=True, example="25 or -40")(text)
    if got.ok and got.value == 0:
        return Parsed.bad("A correction needs an amount, e.g. 25 (a fee) or -40 (a refund)")
    if not got.ok:
        return got
    cents: int = got.value
    if cents > 0:
        return Parsed.good(cents, f"{money(cents, sign=True)} added to what's owed")
    return Parsed.good(cents, f"{money(-cents)} taken off what's owed")


def _parse_on(today: Callable[[], date], anchor: date) -> Parser:
    """The event's day in words (like `--on`, the nearest one), not before the balance date."""
    parse = parse_date_text(today, prefer="nearest")

    def wrapped(text: str) -> Parsed:
        got = parse(text)
        if got.ok and got.value < anchor:
            now = today()
            return Parsed.bad(
                f"That's before the balance you gave ({fmt_date(anchor, now)}), "
                f"so it's already in it; try {fmt_date(anchor, now)} or later"
            )
        return got

    return wrapped


class EventForm(FormScreen[None]):
    """r in the Debts view: something that happened to a debt (it changes the schedule)."""

    TITLE = "Record an event"
    SAVE = "Record"

    DEFAULT_CSS = """
    EventForm {
        & #dialog { width: 78; }
    }
    """

    def __init__(self, flow: Flow, kind: EventType = EventType.EXTRA_PAYMENT) -> None:
        super().__init__()
        assert flow.debt is not None
        self.flow = flow
        self.debt: Debt = flow.debt
        self.kind = kind
        self._before: dict | None = None

    def intro(self) -> ComposeResult:
        s = self.session
        self._before = outlook_with(s, self.flow.id, None)
        owed = cents_of(self._before["balance_at_as_of"]) if self._before else 0
        off = payoff_day(self._before)
        when = f"paid off {fmt_month(off)}" if off else f"paid off {NEVER_PAID}"
        yield FormNote(
            Text.assemble(
                (self.flow.name, "bold"),
                (
                    f"  {money(owed)} owed today {DOT} {pct(self.debt.annual_rate)} {DOT} {when}",
                    FAINT,
                ),
            )
        )

    def fields(self) -> ComposeResult:
        payment = self.flow.amount_cents
        yield ChoiceField("type", "What happened", EVENT_CHOICES, self.kind, notes=EVENT_NOTES)
        yield TextField(
            "extra",
            "Extra paid",
            parse=_worded(
                parse_money(zero=False, example="500"), lambda c: f"{money(c)} off the balance"
            ),
        )
        yield TextField(
            "rate",
            "New rate",
            parse=parse_rate_text(example="5.5%"),
            placeholder=f"now {pct(self.debt.annual_rate)}",
        )
        yield TextField(
            "payment",
            "New payment",
            parse=_worded(
                parse_money(example="450"),
                lambda c: f"{money(c)} each payment, instead of {money(payment)}",
            ),
            placeholder=f"now {money(payment)}",
        )
        yield TextField("change", "Change by", parse=_parse_change)
        yield TextField(
            "on",
            "On",
            "today",
            parse=_parse_on(lambda: self.session.today, self.debt.balance_as_of),
        )
        yield TextField("notes", "Notes", parse=parse_text(required=False), placeholder="optional")

    def on_mount(self) -> None:  # FormScreen.on_mount runs too (Textual calls each class's)
        self._show_amount()
        if key := AMOUNT_FIELD[self.kind]:  # most of the time it's just the amount
            self.call_after_refresh(self.field(key).focus_field)

    def changed(self, field: Field) -> None:
        if field.key == "type" and field.parsed.ok:
            self.kind = field.parsed.value
            self._show_amount()

    def _show_amount(self) -> None:
        wanted = AMOUNT_FIELD[self.kind]
        for key in AMOUNT_FIELD.values():
            if key is not None:
                self.field(key).display = key == wanted

    def _event(self) -> DebtEvent | None:
        """The event as the fields stand, or None while one of them doesn't read."""
        day = self.value("on")
        key = AMOUNT_FIELD[self.kind]
        value = self.value(key) if key else None
        if day is None or (key is not None and value is None):
            return None
        return DebtEvent(
            id=None,
            flow_id=self.flow.id,
            date=day,
            type=self.kind,
            rate=value if self.kind == EventType.RATE_CHANGE else None,
            amount_cents=value if key not in (None, "rate") else None,
        )

    def summary(self) -> Text:
        """What recording it does to the payoff date and the interest to go."""
        ev = self._event()
        before = self._before
        if ev is None or before is None:
            return Text("Fill in the fields to see what it changes.", style=FAINT)
        after = outlook_with(self.session, self.flow.id, ev)
        return payoff_change(before, after, self.session.today)

    def save(self, values: dict[str, Any]) -> None:
        key = AMOUNT_FIELD[self.kind]
        value = values[key] if key else None
        done = self.session.add_event(
            int(self.flow.id),
            self.kind,
            values["on"],
            rate=value if self.kind == EventType.RATE_CHANGE else None,
            amount_cents=value if key not in (None, "rate") else None,
            notes=values["notes"],
        )
        self.dismiss(None)
        bdbd(self).changed(done.message)


def payoff_change(before: dict, after: dict | None, today: date) -> Text:
    """'Paid off Feb 2030 → Nov 2029 · $175.36 less interest' (what an event would do)."""
    if after is None:
        return Text("")
    was, now = payoff_day(before), payoff_day(after)
    interest = cents_of(after["total_interest_remaining"]) - cents_of(
        before["total_interest_remaining"]
    )
    text = Text()
    if now is None:
        text.append(f"Then it's paid off {NEVER_PAID}", style=AMBER)
    elif now <= today:
        text.append("Paid off", style=GREEN)
        text.append(f" {fmt_date(now, today)}", style="bold")
    elif was == now or (was and fmt_month(was) == fmt_month(now)):
        text.append(f"Still paid off {fmt_month(now)}", style=FAINT)
    else:
        text.append("Paid off ", style=FAINT)
        if was:
            text.append(f"{fmt_month(was)} → ", style=FAINT)
        text.append(fmt_month(now), style=f"bold {GREEN if was is None or now < was else AMBER}")
    if interest:
        side = "less" if interest < 0 else "more"
        text.append(f"  {DOT}  {money(abs(interest))} {side} interest", style=FAINT)
    elif now is not None and now > today:
        text.append(f"  {DOT}  the same interest", style=FAINT)
    return text


# ── The payoff plan ───────────────────────────────────────────────────────────

STRATEGIES: list[tuple[Any, str]] = [
    ("avalanche", "Highest rate first"),
    ("snowball", "Smallest balance first"),
]
STRATEGY_NOTES: dict[Any, str] = {
    "avalanche": "Avalanche: the least interest overall",
    "snowball": "Snowball: a debt gone as soon as possible",
}
STRATEGY_WORDS = {"avalanche": "highest rate first", "snowball": "smallest balance first"}


@dataclass
class PlanMemory:
    """What the plan dialog last held, so it opens the same way next time (per app session)."""

    extra: str = "200"
    strategy: str = "avalanche"
    start: str = "today"


@dataclass(frozen=True)
class PlanInputs:
    """A snapshot of the budget for the plan's worker thread (no sqlite in here)."""

    model: EffectiveModel
    today: date
    until: date
    start_cents: int | None  # None: no balance known, so no cash check (like `bdbd plan`)
    weekly: int
    what_if: bool  # the sandbox's changes are in the model
    interest_now: int | None  # interest to go from today without the plan (None: a debt never)

    @classmethod
    def of(cls, s: Session) -> PlanInputs:
        model, what_if = plan_model(s)
        start = s.start()
        rows = ask.debts(s.budget, model)
        interest = (
            None if any(r.interest is None for r in rows) else sum(r.interest or 0 for r in rows)
        )
        return cls(
            model=model,
            today=s.today,
            until=add_months(s.today, PLAN_MONTHS),
            start_cents=start.cents if start.known else None,
            weekly=s.weekly(model),
            what_if=what_if,
            interest_now=interest,
        )


def plan_model(s: Session) -> tuple[EffectiveModel, bool]:
    """The budget a plan works on: what the views will show once it's tried.

    Trying a plan turns the what-if on, so the sandbox's enabled changes are in (`bdbd plan
    --settle …`) even while it's off; a payoff plan already there isn't (the new one replaces
    it). No changes, or changes that don't fit: the stored budget (`bdbd plan`).
    """
    sandbox = s.scenario.copy()
    sandbox.extra, sandbox.extra_label = None, ""
    if not sandbox.any or sandbox.unpinned or s.scenario_error:
        return s.model(baseline=True), False

    def build() -> EffectiveModel:
        spec = sandbox.whatif().spec(today=s.today, base=s.today)
        return build_effective_model(s.flows(), spec, s.today)

    return s.cached(("debts.plan_model",), build), True


def run_plan(
    inputs: PlanInputs, extra: int, strategy: str, start: date | None
) -> tuple[dict, list[str]]:
    """`bdbd plan --extra EXTRA --strategy STRATEGY [--from START]` on the snapshot."""
    return plan_query(
        inputs.model,
        as_of=inputs.today,
        until=inputs.until,
        extra_cents=extra,
        start=start,
        strategy=strategy,
        starting_balance_cents=inputs.start_cents,
        weekly_spend_cents=inputs.weekly,
    )


def plan_sentence(extra: int, strategy: str, start: date, today: date) -> str:
    """'Put $300 extra a month toward debts, highest rate first' (the what-if banner's words)."""
    text = f"Put {money_short(extra)} extra a month toward debts, {STRATEGY_WORDS[strategy]}"
    if start > today:
        text += f", from {fmt_date(start, today)}"
    return text


class Timeline(Widget):
    """The payoff timeline: a bar per debt, solid with the plan, dotted on as scheduled."""

    DEFAULT_CSS = """
    Timeline {
        height: auto;
        text-wrap: nowrap;
        text-overflow: clip;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._spans: list[charts.Span] = []
        self._start = self._end = date.min

    def show(self, spans: list[charts.Span], start: date, end: date) -> None:
        self._spans, self._start, self._end = spans, start, end
        self.refresh(layout=True)

    def get_content_height(self, container: Any, viewport: Any, width: int) -> int:
        return len(self._spans) + 1 if self._spans else 0

    def render(self) -> Text:
        if not self._spans or self.size.width < 20:
            return Text()
        lines = charts.timeline(self._spans, self._start, self._end, self.size.width)
        return Text("\n").join(lines)


class PlanScreen(FormScreen[None]):
    """p in the Debts view: how fast the debts go with an extra amount each month."""

    TITLE = "Payoff plan"
    SAVE = "Try it in What if"

    DEFAULT_CSS = """
    PlanScreen {
        & #dialog { width: 96; }
        & _FormBody { max-height: 100%; }
        & #plan-result { height: auto; }
        & #plan-verdict { height: 1; margin-top: 1; }
        & #plan-numbers { margin-top: 1; }
        & #plan-timeline-title { height: 1; margin-top: 1; }
        & #plan-order { height: auto; margin-top: 1; }
        & #plan-warnings { height: auto; margin-top: 1; color: $warning; }
        & .-hidden { display: none; }
    }
    """

    def __init__(self, memory: PlanMemory | None = None) -> None:
        super().__init__()
        self.memory = memory or PlanMemory()
        self.inputs: PlanInputs | None = None
        self.result: tuple[dict, list[str]] | None = None  # the latest plan, for its key
        self.result_key: tuple | None = None
        self._requested: tuple | None = None  # the key the worker is (or was last) asked for
        self._timer: Timer | None = None
        self._spin: Timer | None = None
        self._frame = 0
        self._try_when_ready = False  # enter came before the plan did

    # the form ------------------------------------------------------------------------

    def intro(self) -> ComposeResult:
        yield FormNote(
            "Each month the extra goes to one debt at a time; once it's paid off, "
            "its payment joins the extra for the next."
        )

    def fields(self) -> ComposeResult:
        today = self.session.today
        m = self.memory
        yield TextField(
            "extra",
            "Extra a month",
            m.extra,
            parse=_worded(parse_money(example="200"), self._extra_words),
        )
        yield ChoiceField("strategy", "Pay off", STRATEGIES, m.strategy, notes=dict(STRATEGY_NOTES))
        yield TextField(
            "start",
            "Starting",
            m.start,
            parse=parse_date_text(today, prefer="future", past=False),
            placeholder="today",
        )
        yield Vertical(
            Static(id="plan-verdict"),
            KeyValues(id="plan-numbers", label_width=16),
            Static(id="plan-timeline-title"),
            Timeline(id="plan-timeline"),
            Static(id="plan-order"),
            Static(id="plan-warnings"),
            id="plan-result",
        )

    def _extra_words(self, cents: int) -> str:
        scheduled = sum(r.monthly for r in self.session.debts())
        if not cents:
            return f"Nothing extra: only freed-up payments roll on ({money(scheduled)} a month now)"
        return f"{money(cents)} a month on top of {money(scheduled)} in payments"

    def summary(self) -> Text:
        return Text("What if shows it in every view; nothing is saved.", style=FAINT)

    def on_mount(self) -> None:  # FormScreen.on_mount runs too (Textual calls each class's)
        dialog = self.query_one("#dialog")
        s = self.session
        self.inputs = PlanInputs.of(s)
        extra = f" {DOT} with the what-if" if self.inputs.what_if else ""
        dialog.border_title = f" Payoff plan{extra} "
        dialog.border_subtitle = " ctrl+s try it in What if · esc close "
        self.query_one("#cancel", Action).update("Close")
        self._spin = self.set_interval(0.08, self._tick, pause=True)
        self._run()

    def changed(self, field: Field) -> None:
        m = self.memory
        if isinstance(field, TextField) and field.key == "extra":
            m.extra = field.text
        elif isinstance(field, TextField) and field.key == "start":
            m.start = field.text
        elif field.key == "strategy" and field.parsed.ok:
            m.strategy = field.parsed.value
        if field.key in ("extra", "strategy", "start"):
            self._later()

    # working it out --------------------------------------------------------------------

    def _key(self) -> tuple | None:
        """(extra cents, strategy, start day) as the fields stand; None while one doesn't read."""
        extra, strategy, start = self.value("extra"), self.value("strategy"), self.value("start")
        if extra is None or strategy is None or start is None:
            return None
        return extra, strategy, start

    def _later(self) -> None:
        """Work it out again once the typing stops for a moment."""
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        key = self._key()
        if key is None:
            self._requested = None
            self._busy(False)
            self._verdict(Text("Fix the fields above to see the plan.", style=AMBER))
            return
        if key == self._requested:
            return
        self._timer = self.set_timer(DEBOUNCE, self._run)

    def _run(self) -> None:
        key = self._key()
        if key is None or self.inputs is None:
            return
        if key == self.result_key and self.result is not None:  # typed back to what's shown
            self._requested = key
            self._busy(False)
            self._draw(self.result)
            return
        self._requested = key
        self._busy(True)
        self._work(key, self.inputs)

    @work(thread=True, exclusive=True, group="debts-plan", exit_on_error=False)
    def _work(self, key: tuple, inputs: PlanInputs) -> tuple[tuple, tuple[dict, list[str]] | str]:
        """The plan, from the snapshot (no sqlite in here)."""
        extra, strategy, start = key
        try:
            return key, run_plan(inputs, extra, strategy, start)
        except CashError as exc:
            return key, exc.message

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group != "debts-plan":
            return
        if event.state == WorkerState.ERROR:
            self._busy(False)
            self._verdict(Text("Couldn't work the plan out.", style=RED))
            return
        if event.state != WorkerState.SUCCESS or not event.worker.result:
            return
        key, answer = event.worker.result
        if key != self._requested:
            return  # the fields moved on while it worked
        self._busy(False)
        if isinstance(answer, str):
            self.result, self.result_key = None, None
            self._try_when_ready = False
            self._verdict(Text(sentence(humanize(answer)), style=AMBER))
            self._show_details(False)
            return
        self.result, self.result_key = answer, key
        self._draw(answer)
        if self._try_when_ready:
            self._try_when_ready = False
            self.action_save()

    def _busy(self, busy: bool) -> None:
        if self._spin is None:
            return
        if busy:
            self._frame = 0
            self._tick()
            self._spin.resume()
        else:
            self._spin.pause()

    def _tick(self) -> None:
        self._frame += 1
        glyph = SPINNER[self._frame % len(SPINNER)]
        self._verdict(Text.assemble((glyph, PURPLE), ("  Working it out…", FAINT)))

    def _verdict(self, text: Text) -> None:
        self.query_one("#plan-verdict", Static).update(text)

    def _show_details(self, shown: bool) -> None:
        for id_ in ("plan-numbers", "plan-timeline-title", "plan-timeline", "plan-order"):
            self.query_one(f"#{id_}").set_class(not shown, "-hidden")

    # drawing ---------------------------------------------------------------------------

    def _draw(self, answer: tuple[dict, list[str]]) -> None:
        data, warnings = answer
        today = self.session.today
        self._verdict(plan_verdict(data))
        inputs = self.inputs
        self.query_one("#plan-numbers", KeyValues).set_rows(
            plan_numbers(
                data, today, inputs.weekly if inputs else 0, inputs.interest_now if inputs else None
            )
        )
        start = date.fromisoformat(data["start"])
        spans, end = plan_spans(data)
        self.query_one("#plan-timeline-title", Static).update(
            Text.assemble(
                ("Payoff order", "bold"),
                (f"  {DOT}  solid with the plan, dotted as scheduled", FAINT),
            )
        )
        self.query_one("#plan-timeline", Timeline).show(spans, start, end)
        self.query_one("#plan-order", Static).update(plan_table(data))
        box = self.query_one("#plan-warnings", Static)
        box.update(Text("\n".join(f"! {human_warning(w)}" for w in warnings), style=AMBER))
        box.display = bool(warnings)
        self._show_details(True)
        order = " → ".join(s["name"] for s in data["steps"])
        strategy = self.field("strategy")
        if isinstance(strategy, ChoiceField) and data["strategy"] in STRATEGY_NOTES:
            strategy.notes = {**STRATEGY_NOTES, data["strategy"]: order}
            strategy.reparse()

    # trying it -------------------------------------------------------------------------

    def save(self, values: dict[str, Any]) -> None:
        key = self._key()
        if key is not None and (self.result is None or key != self.result_key):
            self._try_when_ready = True  # tried as soon as it's worked out
            self._run()
            return
        if self.result is None or key is None:
            raise CashError("there's no plan to try yet; fix the fields above")
        data, _ = self.result
        extra, strategy, start = key
        s = self.session
        today = s.today
        s.set_extra(data["plan_scenario"], plan_sentence(extra, strategy, start, today))
        s.set_lens(True)
        self.dismiss(None)
        bdbd(self).changed(tried_message(data))


def tried_message(data: dict) -> str:
    """The toast after trying a plan: 'Trying the plan in What if · debt-free Aug 2029 …'."""
    text = "Trying the payoff plan"
    if data["debt_free_on"]:
        text += f" {DOT} debt-free {fmt_month(date.fromisoformat(data['debt_free_on']))}"
        if base := data["baseline"]["debt_free_on"]:
            text += f" instead of {fmt_month(date.fromisoformat(base))}"
    return text + f" {DOT} w turns it off"


def plan_verdict(data: dict) -> Text:
    """The badge: 'Debt-free Aug 2029' and how much sooner that is."""
    if data["status"] != "debt_free" or not data["debt_free_on"]:
        return Text.assemble(
            (" Not debt-free within 50 years ", f"bold {DARK} on {AMBER}"),
            ("  a bigger extra would get there", FAINT),
        )
    free = date.fromisoformat(data["debt_free_on"])
    text = Text.assemble((f" Debt-free {fmt_month(free)} ", f"bold {DARK} on {GREEN}"), "  ")
    base = data["baseline"]["debt_free_on"]
    if base:
        was = date.fromisoformat(base)
        sooner = months_apart(free, was)
        if sooner > 0:
            text.append(f"{span(sooner)} sooner", style="bold")
            text.append(f" than {fmt_month(was)}", style=FAINT)
        else:
            text.append(f"about when it would be anyway ({fmt_month(was)})", style=FAINT)
    else:
        text.append("instead of never at today's payments", style=FAINT)
    return text


def plan_numbers(
    data: dict, today: date, weekly: int, interest_now: int | None = None
) -> list[list[str | Text]]:
    """Paying in total, interest saved and the cash check, as KeyValues rows.

    Interest to go is measured from today (as in the Debts view): as it is, `interest_now`;
    with the plan, that less what the plan saves.
    """
    outlay = data["monthly_outlay"]
    saved = cents_of(data["interest_saved"])
    if interest_now is None:
        interest_note = "less interest than paying as scheduled"
    else:
        interest_note = f"{money(interest_now - saved)} to go instead of {money(interest_now)}"
    rows: list[list[str | Text]] = [
        [
            "Paying in total",
            money(cents_of(outlay["with_extra"])),
            Text(f"a month, instead of {money(cents_of(outlay['scheduled_payments']))}", FAINT),
        ],
        [
            "Interest saved",
            Text(money(saved), style=f"bold {GREEN if saved > 0 else ''}".strip()),
            Text(interest_note, FAINT),
        ],
    ]
    cash = data["cash_check"]
    if cash is None:
        rows.append(
            [
                "Lowest balance",
                Text("unknown", style=f"bold {AMBER}"),
                Text("record your balance (b) to check the plan fits", AMBER),
            ]
        )
        return rows
    low = cash["min_balance_after_start"]
    cents = cents_of(low["balance"])
    day = fmt_date(date.fromisoformat(low["date"]), today, weekday=True)
    if cash["affordable"]:
        note = Text(f"{day} {DOT} the plan fits your budget", FAINT)
    else:
        note = Text.assemble((day, FAINT), (f" {DOT} you'd run short", f"bold {RED}"))
    rows.append(
        ["Lowest balance", Text(money(cents), style=f"bold {balance_color(cents, weekly)}"), note]
    )
    return rows


def plan_spans(data: dict) -> tuple[list[charts.Span], date]:
    """The timeline's bars (solid to the plan's payoff, dotted to the scheduled one) and end."""
    start = date.fromisoformat(data["start"])
    spans, ends = [], [start + timedelta(days=30)]
    for s in data["steps"]:
        off = date.fromisoformat(s["paid_off_on"]) if s["paid_off_on"] else None
        was = date.fromisoformat(s["baseline_paid_off_on"]) if s["baseline_paid_off_on"] else None
        spans.append(charts.Span(s["name"], start, off, was, PURPLE))
        ends += [d for d in (off, was) if d]
    return spans, max(ends)


def plan_table(data: dict) -> Table:
    """The order: each debt, what it's paid while it's the target, and when it's gone."""
    table = Table(box=None, pad_edge=False, padding=(0, 2), header_style=FAINT, show_edge=False)
    table.add_column("#", justify="right", style=FAINT, no_wrap=True)
    table.add_column("Debt", no_wrap=True, overflow="ellipsis", max_width=24)
    table.add_column("Rate", justify="right", style=FAINT, no_wrap=True)
    table.add_column("Owed", justify="right", no_wrap=True)
    table.add_column("Paying", justify="right", no_wrap=True)
    table.add_column("Paid off", no_wrap=True)
    table.add_column("Interest", justify="right", style=PURPLE, no_wrap=True)
    for s in data["steps"]:
        off = date.fromisoformat(s["paid_off_on"]) if s["paid_off_on"] else None
        paying = s.get("payment_during")
        table.add_row(
            str(s["order"]),
            s["name"],
            pct(s["annual_rate"]),
            money(cents_of(s["balance_at_start"])),
            Text.assemble(money(cents_of(paying)), ("/mo", FAINT))
            if paying
            else Text("on its own", style=FAINT),
            Text(fmt_month(off)) if off else Text("not paid off", style=AMBER),
            money(cents_of(s["interest_paid"])),
        )
    return table
