"""Dialogs opened from anywhere: the balance dialog, help, the flow card and settings."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from bdbd import ask
from bdbd.core.engine import LedgerEntry
from bdbd.core.errors import CashError
from bdbd.core.models import Flow, Kind
from bdbd.tui.cards import Card, flow_lines
from bdbd.tui.forms import (
    Action,
    Dialog,
    Field,
    FormNote,
    FormScreen,
    Parsed,
    SwitchField,
    TextField,
    parse_date_text,
    parse_money,
)
from bdbd.tui.text import sentence
from bdbd.tui.widgets import KeyValues, bdbd
from bdbd.ui.common import tilde
from bdbd.ui.theme import (
    ACCENT,
    AMBER,
    DOT,
    FAINT,
    GREEN,
    PURPLE,
    RED,
    balance_color,
    money,
    plural,
)
from bdbd.words import describe, fmt_date, join, relative

if TYPE_CHECKING:
    from bdbd.tui.app import BdbdApp


# ── Record your balance ───────────────────────────────────────────────────────


class BalanceScreen(FormScreen[None]):
    """b: record what's in the account; the toast says how far it drifted from the budget."""

    TITLE = "Record your balance"

    DEFAULT_CSS = """
    BalanceScreen {
        & #balance-now { margin-bottom: 1; }
        & #history { height: auto; margin-bottom: 1; display: none; }
        & #history.-open { display: block; }
        & #history-list { height: auto; max-height: 10; }
        & #forget { margin: 1 0 0 0; }
        & #items { margin: 0 0 0 16; }
    }
    """

    def intro(self) -> ComposeResult:
        s = self.session
        rec, start = s.recorded(), s.start()
        now = KeyValues(label_width=13, id="balance-now")  # values line up with the fields
        if rec is None:
            yield FormNote(
                "bdbd doesn't know your balance yet. Tell it what's in your account and every "
                "projection starts there."
            )
        else:
            rows: list[tuple[str | Text, ...]] = [
                (
                    "Recorded",
                    Text(money(rec.amount_cents), style="bold"),
                    Text(
                        f"{fmt_date(rec.as_of, s.today, weekday=True)} {DOT} "
                        f"{relative(rec.as_of, s.today)}",
                        style=FAINT,
                    ),
                )
            ]
            if rec.as_of != s.today:
                color = balance_color(start.cents, s.stored_weekly())
                rows.append(
                    (
                        "Today",
                        Text(money(start.cents), style=f"bold {color}"),
                        Text("estimated, before today's items", style=FAINT),
                    )
                )
            now.set_rows(rows)
            yield now
        with Vertical(id="history"):
            yield Static(id="history-list")
            yield Action("Forget every balance", id="forget", variant="danger")

    def fields(self) -> ComposeResult:
        yield TextField(
            "amount",
            "Amount",
            parse=parse_money(negative=True, example="3,980.00"),
            placeholder="what your bank shows",
        )
        yield TextField(
            "on",
            "On",
            "today",
            parse=parse_date_text(lambda: self.session.today, prefer="past", future=False),
        )
        yield FormNote(id="items")
        yield SwitchField(
            "posted",
            "Already in it?",
            True,
            notes={
                True: "Yes, the amount already includes them",
                False: "No, they still have to come off",
            },
        )

    def extra_actions(self) -> ComposeResult:
        if self.session.balance_history():
            yield Action("History", id="show-history")

    def on_mount(self) -> None:  # FormScreen.on_mount runs too (Textual calls each class's)
        self._show_items()

    # the day's items -----------------------------------------------------------------

    def _items(self) -> list[LedgerEntry]:
        day = self.value("on")
        return self.session.items_on(day) if isinstance(day, date) else []

    def _show_items(self) -> None:
        items = self._items()
        note = self.query_one("#items", FormNote)
        switch = self.field("posted")
        note.display = switch.display = bool(items)
        if items:
            day = self.value("on")
            when = "Today" if day == self.session.today else fmt_date(day, self.session.today)
            listed = Text(f"{when} has ", style=FAINT)
            for i, e in enumerate(items):
                if i:
                    listed.append(", ", style=FAINT)
                listed.append(e.name)
                color = GREEN if e.delta_cents > 0 else ""
                listed.append(f" {money(e.delta_cents, sign=True)}", style=color)
            listed.append(" scheduled.", style=FAINT)
            note.update(listed)

    def changed(self, field: Field) -> None:
        if field.key == "on" and self.is_mounted:
            self._show_items()

    # what it would mean --------------------------------------------------------------

    def _stored(self) -> int | None:
        """The start-of-day balance this would store (like Budget.record_balance)."""
        cents, day = self.value("amount"), self.value("on")
        if cents is None or day is None:
            return None
        items = self._items()
        posted = bool(self.value("posted")) and bool(items)
        return cents - sum(e.delta_cents for e in items) if posted else cents

    def summary(self) -> Text | None:
        stored, day = self._stored(), self.value("on")
        if stored is None or day is None:
            return None
        newer = self.session.recorded()
        if newer is not None and newer.as_of > day:
            since = fmt_date(newer.as_of, self.session.today)
            return Text(f"Your newer balance from {since} still comes first.", style=FAINT)
        expected = self.session.expected(day)
        if expected is None:
            return Text("Every projection will start from here.", style=FAINT)
        since = fmt_date(expected.previous.as_of, self.session.today)
        text = Text(f"Expected {money(expected.cents)} from {since}'s balance", style=FAINT)
        drift = stored - expected.cents
        if drift == 0:
            text.append(" · right on it", style=GREEN)
        else:
            text.append(f" {DOT} ", style=FAINT)
            text.append(money(abs(drift)), style=f"bold {GREEN if drift > 0 else AMBER}")
            text.append(" ahead" if drift > 0 else " behind", style=FAINT)
        return text

    def save(self, values: dict[str, Any]) -> None:
        items = self._items()
        posted = bool(values.get("posted")) and bool(items)
        recorded = self.session.record_balance(values["amount"], values["on"], posted=posted)
        app = bdbd(self)
        self.dismiss(None)
        app.changed(self.session.recorded_message(recorded))

    # history -------------------------------------------------------------------------

    def pressed(self, action: Action) -> None:
        if action.id == "show-history":
            self._toggle_history()
        elif action.id == "forget":
            self._forget()

    def _toggle_history(self) -> None:
        history = self.query_one("#history", Vertical)
        opening = not history.has_class("-open")
        history.set_class(opening, "-open")
        if opening:
            s = self.session
            table = Table.grid(padding=(0, 3))
            table.add_column(style=FAINT, no_wrap=True)
            table.add_column(justify="right", no_wrap=True)
            table.add_column(style=FAINT, no_wrap=True)
            for b in s.balance_history():
                table.add_row(
                    fmt_date(b.as_of, s.today, weekday=True),
                    money(b.amount_cents),
                    relative(b.as_of, s.today),
                )
            self.query_one("#history-list", Static).update(table)

    def _forget(self) -> None:
        n = len(self.session.balance_history())
        app = bdbd(self)

        def forget() -> None:
            gone = self.session.forget_balances()
            self.dismiss(None)
            app.changed(f"Forgot {gone} recorded balance{'s' if gone != 1 else ''}")

        app.confirm(
            "Forget every balance?",
            f"bdbd forgets all {n} recorded balance{'s' if n != 1 else ''}, and projections "
            "start from nothing until you record a new one.",
            forget,
            yes="Forget",
        )


# ── Help ──────────────────────────────────────────────────────────────────────

GLOBAL_KEYS = [
    ("1-7", "switch views"),
    ("b", "record your balance"),
    ("a", "add to this view"),
    ("w", "the what-if on or off"),
    ("ctrl+p  :", "commands, flows by name"),
    (",", "settings and backups"),
    ("ctrl+r", "reload from disk"),
    ("?", "this help"),
    ("q  ctrl+q", "quit"),
]
LIST_KEYS = [
    ("↑ ↓", "move in a list"),
    ("enter", "open the selected row"),
    ("click", "select · double click opens"),
    ("tab", "the next field or panel"),
    ("esc", "close a dialog"),
]
READING = [
    ("Balance", "Cash on hand before today's scheduled items. Press b to record it; everything "
     "starts from the latest one, carried forward day by day."),
    ("Spare", "The balance plus everything coming and going before your next payday: bills "
     "and everyday spending out, any other money in on its day. What you'll have left the "
     "day before it."),
    ("Everyday spending", "Groceries and incidentals, charged a little each day in every "
     "projection. Change it in settings (,)."),
    ("Lowest", "The lowest end-of-day balance ahead: the day to watch."),
    ("Pay cycle", "From a payday to the day before the next one. What's free to spend or save "
     "is the paycheck less the cycle's bills and everyday spending."),
    ("◆", "A debt payment. Its interest and principal split comes from the loan's own terms."),
    ("→Mon", "This flow's weekend dates move to Monday, the way an ACH pull does (→Fri: to "
     "Friday)."),
    ("↳", "Only in the what-if: it isn't in your budget."),
]  # fmt: skip


class HelpScreen(Dialog[None]):
    """?: every key, this view's keys, and how to read the numbers."""

    DEFAULT_CSS = """
    HelpScreen {
        & #dialog { width: 86; }
        & VerticalScroll { height: auto; max-height: 100%; }
        & .help-section { height: auto; margin-bottom: 1; }
        & #help-keys { height: auto; }
        & #help-keys > * { width: 1fr; height: auto; }
    }
    """

    BINDINGS: ClassVar = [
        Binding("escape,question_mark,q", "close", "Close", show=False),
    ]

    def __init__(self, view: Widget | None) -> None:
        super().__init__()
        self.view = view

    def compose(self) -> ComposeResult:
        app: BdbdApp = bdbd(self)
        with Vertical(id="dialog") as dialog:
            dialog.border_title = " Help "
            dialog.border_subtitle = " esc close "
            with VerticalScroll():
                with Horizontal(id="help-keys"):
                    yield Static(_keys("Everywhere", GLOBAL_KEYS), classes="help-section")
                    with Vertical():
                        view_keys = self._view_keys(app)
                        if view_keys and self.view is not None:
                            title = getattr(self.view, "title", "") or "This view"
                            yield Static(_keys(title, view_keys), classes="help-section")
                        yield Static(
                            _keys("In lists and dialogs", LIST_KEYS), classes="help-section"
                        )
                yield Static(_reading(), classes="help-section")

    def _view_keys(self, app: BdbdApp) -> list[tuple[str, str]]:
        """The view's keys that work right now (a view's other half has its own), with the
        keys for one action on one line ('e  enter  edit')."""
        view = self.view
        if view is None:
            return []
        out: list[tuple[str, str]] = []
        seen: dict[str, int] = {}
        for binding in Binding.make_bindings(getattr(type(view), "BINDINGS", [])):
            if not binding.description or binding.system:
                continue
            if view.check_action(binding.action, ()) is False:
                continue  # not here, not now (e.g. Tags keys while Flows shows)
            key = app.get_key_display(binding)
            if binding.action in seen:  # 'e' and 'enter' for the same thing
                i = seen[binding.action]
                out[i] = (f"{out[i][0]}  {key}", out[i][1])
                continue
            seen[binding.action] = len(out)
            out.append((key, binding.description))
        return out

    def action_close(self) -> None:
        self.dismiss(None)


def _keys(title: str, rows: list[tuple[str, str]]) -> Table:
    table = Table.grid(padding=(0, 2))
    table.title = Text(title, style="bold")
    table.title_justify = "left"
    table.add_column(width=10, no_wrap=True)
    table.add_column()
    for key, words in rows:
        table.add_row(Text(key, style=f"bold {ACCENT}"), Text(words))
    return table


def _reading() -> Table:
    table = Table.grid(padding=(0, 2))
    table.title = Text("Reading the numbers", style="bold")
    table.title_justify = "left"
    table.add_column(width=18, no_wrap=True)
    table.add_column()
    for name, words in READING:
        style = f"bold {PURPLE}" if name == "◆" else f"bold {ACCENT}" if name == "↳" else "bold"
        table.add_row(Text(name, style=style), Text(words, style=FAINT))
    colors = Text.assemble(
        ("Balances turn ", FAINT),
        ("amber", AMBER),
        (" below one week of everyday spending, and ", FAINT),
        ("red", RED),
        (" below zero. Money in is ", FAINT),
        ("green", GREEN),
        (".", FAINT),
    )
    table.add_row(Text("Colors", style="bold"), colors)
    return table


# ── The flow card ─────────────────────────────────────────────────────────────
# Enter on a flow anywhere opens FlowCard; its body is the same Card the Budget view shows.


class KeyStrip(Static):
    """A line of clickable keys: 'e edit · space pause · esc close' (a click runs the action).

    Actions run on `target` (the screen by default), e.g. a view that shows the strip.
    """

    DEFAULT_CSS = """
    KeyStrip {
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    """

    def __init__(self, *, target: Widget | None = None, id: str | None = None) -> None:
        super().__init__(id=id)
        self.target = target
        self._spans: list[tuple[int, int, str]] = []

    def set_keys(self, keys: list[tuple[str, str, str]]) -> None:
        """(key, words, action) for each key."""
        text, x = Text(), 0
        self._spans = []
        for i, (key, words, action) in enumerate(keys):
            if i:
                text.append(f"  {DOT}  ", style=FAINT)
                x += 5
            text.append(key, style=f"bold {ACCENT}")
            text.append(f" {words}", style=FAINT)
            width = len(key) + 1 + len(words)
            self._spans.append((x, x + width, action))
            x += width
        self.update(text)

    async def on_click(self, event: events.Click) -> None:
        offset = event.get_content_offset(self)
        if offset is None:
            return
        for start, end, action in self._spans:
            if start <= offset.x < end:
                event.stop()
                await (self.target or self.screen).run_action(action)


class FlowCard(Dialog[None]):
    """Enter on a flow: its details, and e edit · space pause · x delete · d loan terms."""

    DEFAULT_CSS = """
    FlowCard {
        & #dialog { width: 64; }
        & #card-body { height: auto; }
        & #card-body Card { max-width: 100%; }
        & #card-keys { margin-top: 1; }
    }
    """

    BINDINGS: ClassVar = [
        Binding("e", "edit", "edit"),
        Binding("space", "pause", "pause or resume"),
        Binding("x,delete", "delete", "delete"),
        Binding("d", "loan", "loan terms"),
        Binding("escape", "close", "close"),
    ]

    def __init__(self, flow_id: int) -> None:
        super().__init__()
        self.flow_id = flow_id
        self.drawn = 0
        self._gone = False  # the flow went away; close once this card is on top again
        self._dismissed = False

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog") as dialog:
            dialog.border_subtitle = " esc close "
            with VerticalScroll(id="card-body"):
                yield Card(id="details")
            yield KeyStrip(id="card-keys")

    def on_mount(self) -> None:
        self.redraw()
        self.set_interval(1.0, self._check, name="card-watch")
        self.call_after_refresh(self._fit)

    def on_resize(self) -> None:
        self._fit()

    def _fit(self) -> None:
        body = self.query_one("#card-body", VerticalScroll)
        body.styles.max_height = max(4, self.size.height - 8)

    def on_screen_resume(self) -> None:
        self._check()

    def _check(self) -> None:
        """Redraw after a change (an edit on top, a pause, another process); close if gone.

        Only while this card is the top screen: under an edit form it waits, so it never
        closes the form (dismiss pops whatever screen is on top).
        """
        if self._dismissed or self.app.screen is not self:
            return
        if self._gone or self.drawn != self.session.version:
            self.redraw()

    @property
    def flow(self) -> Flow | None:
        try:
            return self.session.flow(self.flow_id)
        except CashError:
            return None

    def redraw(self) -> None:
        flow = self.flow
        if flow is None:
            self._gone = True
            if self.app.screen is self and not self._dismissed:
                self._dismissed = True
                self.dismiss(None)
            return
        self.drawn = self.session.version
        self.query_one("#dialog").border_title = f" {flow.name} "
        self.query_one(Card).show(flow_lines(self.session, flow, dates=6))
        keys = [("e", "edit", "edit"), ("space", "resume" if not flow.active else "pause", "pause")]
        keys.append(("x", "delete", "delete"))
        if flow.kind == Kind.EXPENSE:
            keys.append(("d", "loan terms" if flow.debt else "make it a debt", "loan"))
        self.query_one(KeyStrip).set_keys(keys)  # esc close is in the border

    def action_edit(self) -> None:
        bdbd(self).edit_flow(self.flow_id)

    def action_pause(self) -> None:
        bdbd(self).toggle_paused(self.flow_id)
        self._check()

    def action_delete(self) -> None:
        bdbd(self).delete_flow(self.flow_id)

    def action_loan(self) -> None:
        flow = self.flow
        if flow is not None and flow.kind == Kind.EXPENSE:
            bdbd(self).edit_flow(self.flow_id, loan=True)
        else:
            self.app.bell()

    def action_close(self) -> None:
        if not self._dismissed:
            self._dismissed = True
            self.dismiss(None)


def open_flow_card(app: BdbdApp, flow_id: int) -> None:
    """Push the FlowCard for `flow_id` (an error toast when the flow is gone)."""
    try:
        app.session.flow(flow_id)
    except CashError as exc:
        app.notify(sentence(exc.message), severity="error", markup=False)
        return
    app.push_screen(FlowCard(flow_id))


# ── Paydays ───────────────────────────────────────────────────────────────────


class PaydaysForm(FormScreen[None]):
    """p in Paydays: which incomes start a pay cycle (`bdbd edit NAME --payday`)."""

    TITLE = "Paydays"

    def intro(self) -> ComposeResult:
        yield FormNote(
            "Each date of a payday starts a pay cycle, which runs to the day before the next "
            "one. Your paycheck is one; a refund or a gift isn't (its money still counts in the "
            "cycle it lands in)."
        )

    def fields(self) -> ComposeResult:
        for f in self._incomes():
            when = describe(f.rrule, f.dtstart, f.until) + ("" if f.active else " (paused)")
            yield SwitchField(
                f"payday-{f.id}",
                f.name,
                f.payday,
                notes={
                    True: f"{when} {DOT} starts a pay cycle",
                    False: f"{when} {DOT} doesn't start one",
                },
            )

    def _incomes(self) -> list[Flow]:
        return [f for f in self.session.flows() if f.kind == Kind.INCOME]

    def summary(self) -> str | Text | None:
        on = [f.name for f in self._incomes() if self.value(f"payday-{f.id}")]
        if not on:
            return Text("No paydays: there won't be any pay cycles", style=AMBER)
        return f"Pay cycles start on each date of {join(on)}"

    def save(self, values: dict[str, Any]) -> None:
        flags = {int(k.removeprefix("payday-")): bool(v) for k, v in values.items()}
        bdbd(self).apply(lambda: self.session.set_paydays(flags))
        self.dismiss(None)


def open_paydays(app: BdbdApp) -> None:
    """Choose the incomes that start pay cycles (a toast when there's no income yet)."""
    if not any(f.kind == Kind.INCOME for f in app.session.flows()):
        app.notify("Add your paycheck first: a gives you a new income.", markup=False)
        return
    app.push_screen(PaydaysForm())


# ── Settings ──────────────────────────────────────────────────────────────────


class SettingsScreen(FormScreen[None]):
    """,: everyday spending, where the budget lives, and backups."""

    TITLE = "Settings"

    DEFAULT_CSS = """
    SettingsScreen {
        & #dialog { width: 72; }
        & .settings--heading { color: $foreground; text-style: bold; margin: 0; }
        & .settings--row { height: auto; margin-bottom: 1; }
        & .settings--label { width: 16; color: $text-muted; }
        & .settings--value { width: 1fr; color: $text-muted; }
        & .settings--row Action { margin: 0 2 0 0; }
        & #field-weekly { margin-bottom: 1; }
    }
    """

    def fields(self) -> ComposeResult:
        s = self.session
        weekly = s.stored_weekly()
        yield FormNote("Everyday spending", classes="settings--heading")
        yield FormNote("Groceries and incidentals, charged a little each day in every projection.")
        yield TextField(
            "weekly",
            "Per week",
            money(weekly, symbol=False) if weekly else "",
            parse=self._parse_weekly,
            placeholder="175.00",
        )
        yield FormNote("Your budget", classes="settings--heading")
        with Horizontal(classes="settings--row"):
            yield Static("File", classes="settings--label")
            yield Static(tilde(s.path), classes="settings--value", id="budget-path")
        with Horizontal(classes="settings--row", id="backups"):
            yield Static("Backups", classes="settings--label")
            yield Action("Export a backup…", id="export")
            yield Action("Import a backup…", id="import")

    def _parse_weekly(self, text: str) -> Parsed:
        parsed = parse_money(optional=True, example="175")(text)
        if not parsed.ok:
            return parsed
        cents = parsed.value
        if not cents:
            return Parsed.good(cents, "Nothing: projections leave out everyday spending")
        monthly = ask.everyday_monthly(cents)
        return Parsed.good(cents, f"{money(cents)} a week {DOT} about {money(monthly)} a month")

    def summary(self) -> Text | None:
        before = self.session.monthly_net()
        cents = self.value("weekly")
        text = Text("Monthly net ", style=FAINT)
        if not self.field("weekly").parsed.ok:
            text.append(money(before, sign=True), style=FAINT)
            return text
        old = ask.everyday_monthly(self.session.stored_weekly())
        after = before + old - ask.everyday_monthly(cents or 0)
        if after == before:
            text.append(f"stays {money(after, sign=True)}", style=FAINT)
        else:
            text.append(money(before, sign=True))
            text.append(" → ", style=FAINT)
            text.append(money(after, sign=True), style=f"bold {GREEN if after >= 0 else RED}")
        return text

    def save(self, values: dict[str, Any]) -> None:
        cents = values["weekly"]
        app = bdbd(self)
        self.dismiss(None)
        if (cents or 0) != self.session.stored_weekly():
            app.apply(lambda: self.session.set_weekly_spend(cents))

    def pressed(self, action: Action) -> None:
        app = bdbd(self)
        if action.id == "export":
            app.export_backup()
        elif action.id == "import":

            def imported(done: bool | None) -> None:
                if done:
                    self.dismiss(None)

            app.push_screen(ImportScreen(), imported)


def open_settings(app: BdbdApp) -> None:
    """Push the SettingsScreen."""
    app.push_screen(SettingsScreen())


class ImportScreen(FormScreen[bool]):
    """Restore a backup like `bdbd import FILE [--replace]`; replacing anything asks first."""

    TITLE = "Import a backup"
    SAVE = "Import"

    DEFAULT_CSS = """
    ImportScreen {
        & #dialog { width: 72; }
    }
    """

    def fields(self) -> ComposeResult:
        s = self.session
        there = s.replaced_by_import()
        what = join(there)
        yield TextField(
            "file",
            "File",
            parse=self._parse_file,
            placeholder=f"~/bdbd-backup-{s.today.isoformat()}.json",
        )
        yield SwitchField(
            "replace",
            "Replace",
            bool(there),
            yes="Replace everything",
            no="No",
            notes={
                True: f"Replaces {what}" if there else "Fills your empty budget from the backup",
                False: f"Only into an empty budget; yours has {what}"
                if there
                else "Fills your empty budget from the backup",
            },
        )

    def _parse_file(self, text: str) -> Parsed:
        if not text.strip():
            return Parsed.bad("A backup made by bdbd, e.g. ~/bdbd-backup.json")
        path = Path(text.strip()).expanduser()
        try:
            held = self.session.backup_contents(path)
        except CashError as exc:
            return Parsed.bad(sentence(exc.message))
        what = f"{plural(held.flows, 'flow')} and {plural(held.balances, 'balance')}"
        if held.exported_at is not None:
            what += f", backed up {fmt_date(held.exported_at, self.session.today, weekday=True)}"
        return Parsed.good(path, what)

    def save(self, values: dict[str, Any]) -> None:
        path: Path = values["file"]
        replace = bool(values["replace"])
        there = self.session.replaced_by_import()
        if there and not replace:
            raise CashError(
                f"your budget already has {join(there)}; switch Replace on to overwrite them",
                "db_not_empty",
            )
        if not there:
            self._restore(path, replace)
            return
        app = bdbd(self)
        app.confirm(
            "Replace your budget?",
            f"The backup replaces {join(there)}. This can't be undone.",
            lambda: app.call_later(self._restore, path, replace),
            yes="Replace",
        )

    def _restore(self, path: Path, replace: bool) -> None:
        app = bdbd(self)
        try:
            n = self.session.import_(path, replace=replace)
        except CashError as exc:
            summary = self.query_one("#summary", Static)
            summary.update(Text(f"✗ {sentence(exc.message)}", style=RED))
            summary.add_class("-error")
            summary.display = True
            app.refresh_views()
            return
        self.dismiss(True)
        app.changed(f"Restored {plural(n, 'flow')} from {path.name}")


def open_import(app: BdbdApp) -> None:
    """Push the import dialog (a file and a Replace switch)."""
    app.push_screen(ImportScreen())
