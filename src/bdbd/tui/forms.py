"""Dialogs that ask for things: the FormScreen base, its fields, and confirm/prompt dialogs.

A form is a centered panel of labeled fields. Under each field a live preview line says what
was understood (faint) or, once the field has been visited, what's wrong and an example that
works (amber). Save is refused, with focus on the first bad field, until every field reads.

Keys: tab/shift+tab or up/down move between fields, enter moves on (and saves from the last
field), ctrl+s saves, esc cancels. Building one:

    class WeeklyForm(FormScreen[None]):
        TITLE = "Everyday spending"

        def fields(self) -> ComposeResult:
            yield TextField("weekly", "Per week", value="175", parse=parse_money())

        def summary(self) -> str:
            cents = self.value("weekly")
            return f"About {money(everyday_monthly(cents))} a month" if cents is not None else ""

        def save(self, values: dict[str, Any]) -> None:
            bdbd(self).apply(lambda: self.session.set_weekly_spend(values["weekly"]))
            self.dismiss(None)

A `save` that raises CashError keeps the dialog open and shows the message at the bottom.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, ClassVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widget import Widget
from textual.widgets import Input, Static

from bdbd.core import repo
from bdbd.core.errors import CashError
from bdbd.core.money import parse_amount, parse_rate
from bdbd.tui.text import sentence
from bdbd.tui.widgets import bdbd
from bdbd.ui.theme import (
    ACCENT,
    AMBER,
    BACKGROUND_HEX,
    FAINT,
    FOREGROUND,
    MINUS,
    RED,
    money,
    muted,
    pct,
)
from bdbd.words import fmt_date, parse_day, relative

if TYPE_CHECKING:
    from bdbd.tui.session import Session


# ── Parsing what's typed ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Parsed:
    """What a field made of its text: whether it reads, a preview line, and the value."""

    ok: bool
    preview: str | Text = ""  # the meaning when ok; the problem (and an example) when not
    value: Any = None

    @classmethod
    def good(cls, value: Any, preview: str | Text = "") -> Parsed:
        return cls(True, preview, value)

    @classmethod
    def bad(cls, problem: str) -> Parsed:
        return cls(False, problem)


Parser = Callable[[str], Parsed]


def parse_text(*, required: bool = True, what: str = "A name") -> Parser:
    """Trimmed text; blank is None when not required."""

    def parse(text: str) -> Parsed:
        s = text.strip()
        if not s:
            return Parsed.bad(f"{what} is needed") if required else Parsed.good(None)
        return Parsed.good(s)

    return parse


def parse_money(
    *, negative: bool = False, optional: bool = False, zero: bool = True, example: str = "15.49"
) -> Parser:
    """An amount of money in cents ('1,250.00', '$15', '-40' when negative=True)."""

    def parse(text: str) -> Parsed:
        s = text.strip()
        if not s:
            return Parsed.good(None) if optional else Parsed.bad(f"An amount, e.g. {example}")
        whole, dot, cents_part = s.partition(".")
        if dot and len(cents_part.strip()) > 2:  # 175.00200 is a typo, not $175
            return Parsed.bad(f"That has too many decimals; try {whole}.{cents_part[:2]}")
        try:
            cents = parse_amount(s, allow_negative=negative)
        except CashError as exc:
            if exc.code == "negative_amount":
                return Parsed.bad(f"No minus sign needed; try {s.lstrip('-' + MINUS)}")
            return Parsed.bad(f"That isn't an amount of money; try {example}")
        if not zero and cents == 0:
            return Parsed.bad("It needs to be more than zero")
        return Parsed.good(cents, money(cents))

    return parse


def parse_rate_text(*, optional: bool = False, example: str = "6.49%") -> Parser:
    """An annual rate: '6.49%' or '0.0649'."""

    def parse(text: str) -> Parsed:
        s = text.strip()
        if not s:
            return Parsed.good(None) if optional else Parsed.bad(f"A yearly rate, e.g. {example}")
        try:
            rate = parse_rate(s)
        except CashError:
            return Parsed.bad(f"That isn't a rate; try {example} or 0.0649")
        if rate >= 1 and not s.endswith("%"):
            return Parsed.bad(f"That would be {pct(rate)}; add a % sign, e.g. {s}%")
        return Parsed.good(rate, f"{pct(rate)} a year")

    return parse


def parse_date_text(
    today: date | Callable[[], date],
    *,
    prefer: str = "future",
    optional: bool = False,
    blank: str = "",
    past: bool = True,
    future: bool = True,
) -> Parser:
    """A day in words ('oct 15', 'fri', 'in 3 weeks', '2026-10-15').

    `today` may be a callable (e.g. `lambda: session.today`), read on every parse, so a form
    left open past midnight takes 'today' as the new day. `blank` is the preview when an
    optional field is left empty ('the next one'). `past` or `future` False refuses days on
    that side of today.
    """
    examples = (
        "today, yesterday, sep 22 or mon"
        if not future
        else "dec 12, fri, +3m or eom"
        if not past
        else "oct 15, fri, +2w or 2026-10-15"
    )

    def parse(text: str) -> Parsed:
        now = today if isinstance(today, date) else today()
        s = text.strip()
        if not s:
            if optional:
                return Parsed.good(None, blank)
            return Parsed.bad(f"A date, e.g. {examples}")
        try:
            day = parse_day(s, today=now, prefer=prefer)
        except CashError:
            return Parsed.bad(f"Try {examples}")
        if not past and day < now:
            return Parsed.bad(f"{fmt_date(day, now, weekday=True)} has passed; pick today or later")
        if not future and day > now:
            return Parsed.bad(f"{fmt_date(day, now, weekday=True)} hasn't happened yet")
        return Parsed.good(day, day_preview(day, now))

    return parse


def day_preview(day: date, today: date) -> str:
    """'Fri Oct 2, 2026 · in 8 days'."""
    return f"{fmt_date(day, weekday=True)} · {relative(day, today)}"


def parse_tags_text() -> Parser:
    """'car, insurance' -> ('car', 'insurance')."""

    def parse(text: str) -> Parsed:
        tags = tuple(dict.fromkeys(repo.normalize_tag(t) for t in text.split(",") if t.strip()))
        return Parsed.good(tags, " · ".join(tags) if tags else "")

    return parse


# ── Fields ────────────────────────────────────────────────────────────────────


class Field(Widget):
    """A labeled row of a FormScreen with a live preview line under it.

    Subclasses provide `text` (what's entered) and focus handling; the field keeps `parsed`
    current and posts `Field.Changed` whenever it changes.
    """

    DEFAULT_CSS = """
    Field {
        height: auto;
        width: 1fr;
        & > .field--row { height: 1; width: 1fr; }
        & .field--label { width: 16; color: $text-muted; }
        & > .field--preview { height: 1; margin-left: 16; color: $text-muted; }
        &.-bad.-touched > .field--preview { color: $warning; }
        &:focus-within .field--label { color: $foreground; }
    }
    """

    class Changed(Message):
        """The field's value (or validity) changed."""

        def __init__(self, field: Field) -> None:
            super().__init__()
            self.field = field

        @property
        def control(self) -> Field:
            return self.field

    class Submitted(Message):
        """Enter in a field that isn't a text box (a text box posts Input.Submitted)."""

        def __init__(self, field: Field) -> None:
            super().__init__()
            self.field = field

        @property
        def control(self) -> Field:
            return self.field

    def __init__(self, key: str, label: str, *, parse: Parser | None = None) -> None:
        super().__init__(id=f"field-{key}")
        self.key = key
        self.label = label
        self.parse = parse or (lambda text: Parsed.good(text.strip()))
        self.parsed = Parsed.good(None)
        self.touched = False

    @property
    def text(self) -> str:
        raise NotImplementedError

    @property
    def value(self) -> Any:
        return self.parsed.value

    def focus_field(self) -> None:
        self.focus()

    def reparse(self) -> None:
        """Parse the current text again (e.g. when another field it depends on changed)."""
        self.parsed = self.parse(self.text)
        self.set_class(not self.parsed.ok, "-bad")
        preview = self.query_one(".field--preview", Static)
        preview.update(self.parsed.preview)
        self.post_message(self.Changed(self))

    def touch(self) -> None:
        """Show problems in amber from now on (after a visit, or a refused save)."""
        self.touched = True
        self.add_class("-touched")

    def on_descendant_blur(self) -> None:
        self.touch()

    def on_blur(self) -> None:
        self.touch()

    def on_mount(self) -> None:
        self.reparse()


class TextField(Field):
    """A one-line text box: `parse` turns the text into a value and a preview line.

    TextField("amount", "Amount", value="15.49", parse=parse_money())
    """

    DEFAULT_CSS = """
    TextField Input {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0 1;
        background: $panel;
        &:focus { background: $primary 18%; border: none; }
        & > .input--placeholder { color: $foreground 30%; }
    }
    """

    def __init__(
        self,
        key: str,
        label: str,
        value: str = "",
        *,
        parse: Parser | None = None,
        placeholder: str = "",
        suggester: Suggester | None = None,
    ) -> None:
        super().__init__(key, label, parse=parse)
        self._value = value
        self._placeholder = placeholder
        self._suggester = suggester

    def compose(self) -> ComposeResult:
        with Horizontal(classes="field--row"):
            yield Static(self.label, classes="field--label")
            yield Input(  # focus selects the text, so typing replaces a prefilled value
                self._value,
                placeholder=self._placeholder,
                suggester=self._suggester,
                compact=True,
            )
        yield Static(classes="field--preview")

    @property
    def input(self) -> Input:
        return self.query_one(Input)

    @property
    def text(self) -> str:
        return self.input.value if self.is_mounted else self._value

    @text.setter
    def text(self, value: str) -> None:
        self.input.value = value

    def focus_field(self) -> None:
        self.input.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self.reparse()


CHOSEN = f"bold {FOREGROUND} on {muted(ACCENT, 0.35)}"  # the chosen segment, without focus


class ChoiceField(Field, can_focus=True):
    """A segmented choice, moved with left/right (or a click): 'Money out │ Money in'.

        ChoiceField("kind", "Kind", [(Kind.EXPENSE, "Money out"), (Kind.INCOME, "Money in")])

    `notes` gives a preview line per value (e.g. what a weekend rule does).
    """

    DEFAULT_CSS = """
    ChoiceField {
        & > .field--row { height: 1; }
    }
    """

    BINDINGS: ClassVar = [
        Binding("left", "move(-1)", "Previous", show=False),
        Binding("right", "move(1)", "Next", show=False),
        Binding("enter", "submit", "Next field", show=False),
    ]

    selected: reactive[int] = reactive(0)

    def __init__(
        self,
        key: str,
        label: str,
        choices: Sequence[tuple[Any, str]],
        value: Any = None,
        *,
        notes: dict[Any, str] | None = None,
    ) -> None:
        super().__init__(key, label, parse=self._parse)
        self.choices = list(choices)
        self.notes = notes or {}
        values = [v for v, _ in self.choices]
        self.set_reactive(ChoiceField.selected, values.index(value) if value in values else 0)
        self._spans: list[tuple[int, int]] = []

    def compose(self) -> ComposeResult:
        with Horizontal(classes="field--row"):
            yield Static(self.label, classes="field--label")
            yield Static(classes="choice--options")
        yield Static(classes="field--preview")

    @property
    def text(self) -> str:
        return str(self.selected)

    def _parse(self, text: str) -> Parsed:
        value = self.choices[self.selected][0]
        return Parsed.good(value, self.notes.get(value, ""))

    def action_submit(self) -> None:
        self.post_message(self.Submitted(self))

    def action_move(self, step: int) -> None:
        self.selected = max(0, min(len(self.choices) - 1, self.selected + step))

    def select(self, value: Any) -> None:
        """Pick the choice with this value."""
        self.selected = [v for v, _ in self.choices].index(value)

    def watch_selected(self) -> None:
        if self.is_mounted:
            self._draw()
            self.reparse()

    def on_focus(self) -> None:
        self._draw()

    def on_blur(self) -> None:
        self.touch()
        self._draw()

    def on_mount(self) -> None:
        self._draw()  # Field.on_mount (reparse) runs after this one

    def _draw(self) -> None:
        parts: list[str | tuple[str, str]] = []
        self._spans = []
        x = 0
        for i, (_, label) in enumerate(self.choices):
            if i:
                parts.append((" │ ", FAINT))
                x += 3
            chip = f" {label} "
            if i == self.selected:
                # screen.focused, not has_focus: on_focus/on_blur run before has_focus moves
                focused = self.is_attached and self.screen.focused is self
                style = f"bold {BACKGROUND_HEX} on {ACCENT}" if focused else CHOSEN
            else:
                style = FAINT
            parts.append((chip, style))
            self._spans.append((x, x + len(chip)))
            x += len(chip)
        self.query_one(".choice--options", Static).update(Content.assemble(*parts))

    def on_click(self, event: events.Click) -> None:
        options = self.query_one(".choice--options", Static)
        offset = event.get_content_offset(options)
        if offset is None:
            return
        for i, (a, b) in enumerate(self._spans):
            if a <= offset.x < b:
                self.selected = i


class SwitchField(ChoiceField):
    """A yes/no switch: space, left/right or a click flips it.

    SwitchField("posted", "Already in it?", True, notes={True: "…", False: "…"})
    """

    BINDINGS: ClassVar = [Binding("space", "flip", "Flip", show=False)]

    def __init__(
        self,
        key: str,
        label: str,
        value: bool = False,
        *,
        yes: str = "Yes",
        no: str = "No",
        notes: dict[Any, str] | None = None,
    ) -> None:
        super().__init__(key, label, [(True, yes), (False, no)], value, notes=notes)

    def action_flip(self) -> None:
        self.selected = 1 - self.selected


class FormNote(Static):
    """A faint line of explanation (or a subheading) between fields."""

    DEFAULT_CSS = """
    FormNote {
        height: auto;
        color: $text-muted;
        margin: 0 0 1 0;
    }
    """


# ── Buttons ───────────────────────────────────────────────────────────────────


class Action(Static, can_focus=True):
    """A small button: enter, space or a click presses it. Variants: primary, danger."""

    DEFAULT_CSS = """
    Action {
        width: auto;
        height: 1;
        padding: 0 2;
        margin-left: 1;
        background: $panel;
        color: $foreground;
        &:hover { background: $primary 25%; }
        &:focus { background: $primary; color: $background; text-style: bold; }
        &.-primary { background: $primary 30%; text-style: bold; }
        &.-primary:focus { background: $primary; color: $background; }
        &.-danger { color: $error; }
        &.-danger:focus { background: $error; color: $background; text-style: bold; }
    }
    """

    BINDINGS: ClassVar = [Binding("enter,space", "press", "Press", show=False)]

    class Pressed(Message):
        """The button was pressed."""

        def __init__(self, action: Action) -> None:
            super().__init__()
            self.action = action

        @property
        def control(self) -> Action:
            return self.action

    def __init__(self, label: str, *, id: str | None = None, variant: str | None = None) -> None:
        super().__init__(label, id=id, classes=f"-{variant}" if variant else None)

    def action_press(self) -> None:
        self.post_message(self.Pressed(self))

    def on_click(self) -> None:
        self.action_press()


# ── The form screen ───────────────────────────────────────────────────────────


class _FormBody(VerticalScroll, can_focus=False, inherit_bindings=False):
    """The scrolling column of fields (without the scroll keys, so up/down move focus)."""


class Dialog[T](ModalScreen[T]):
    """Base for bdbd's dialogs: a centered panel over the dimmed app."""

    DEFAULT_CSS = """
    Dialog {
        align: center middle;
        background: $background 70%;
        & > #dialog {
            width: 66;
            max-width: 100%;
            height: auto;
            max-height: 100%;
            border: round $primary;
            border-title-color: $foreground;
            border-title-style: bold;
            border-subtitle-color: $text-muted;
            background: $surface;
            padding: 1 2;
        }
    }
    """

    @property
    def session(self) -> Session:
        return bdbd(self).session


class FormScreen[T](Dialog[T]):
    """A dialog of labeled fields with live previews, a consequence line and Save/Cancel.

    Override `fields()` (yield Field widgets, FormNotes, …), `save(values)`, and optionally
    `intro()` (widgets above the fields), `summary()` (the consequence line) and
    `changed(field)` (react to a field, e.g. show or hide others with `field.display`).
    Hidden fields are skipped when checking and saving.
    """

    DEFAULT_CSS = """
    FormScreen {
        & _FormBody { height: auto; }
        & #summary { height: auto; margin-top: 1; color: $text-muted; }
        & #summary.-error { color: $error; }
        & #buttons { height: 1; margin-top: 1; }
        & #buttons > .spacer { width: 1fr; }
    }
    """

    BINDINGS: ClassVar = [
        Binding("ctrl+s", "save", "Save", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("down", "app.focus_next", "Next field", show=False),
        Binding("up", "app.focus_previous", "Previous field", show=False),
    ]

    TITLE: ClassVar[str] = ""
    SAVE: ClassVar[str] = "Save"

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog") as dialog:
            dialog.border_title = f" {self.TITLE} " if self.TITLE else ""
            dialog.border_subtitle = " ctrl+s save · esc cancel "
            yield from self.intro()
            with _FormBody():
                yield from self.fields()
            yield Static(id="summary")
            with Horizontal(id="buttons"):
                yield from self.extra_actions()
                yield Static(classes="spacer")
                yield Action("Cancel", id="cancel")
                yield Action(self.SAVE, id="save", variant="primary")

    # to override -------------------------------------------------------------------

    def intro(self) -> ComposeResult:
        """Widgets above the fields (nothing by default)."""
        yield from ()

    def fields(self) -> ComposeResult:
        """The form's fields, in order."""
        yield from ()

    def summary(self) -> str | Text | None:
        """The consequence line, recomputed after every change (read `self.value(key)`)."""
        return None

    def changed(self, field: Field) -> None:
        """A field changed (show or hide dependent fields here)."""

    def extra_actions(self) -> ComposeResult:
        """More buttons, shown at the left of Cancel/Save (handle them in `pressed`)."""
        yield from ()

    def pressed(self, action: Action) -> None:
        """One of `extra_actions` (or an Action in the fields) was pressed."""

    def save(self, values: dict[str, Any]) -> None:
        """Every visible field reads: commit and `self.dismiss(result)`, or raise CashError."""
        raise NotImplementedError

    # helpers -----------------------------------------------------------------------

    def field(self, key: str) -> Field:
        return self.query_one(f"#field-{key}", Field)

    def value(self, key: str) -> Any:
        """A field's parsed value (None when it doesn't read yet)."""
        f = self.field(key)
        return f.parsed.value if f.parsed.ok else None

    def visible_fields(self) -> list[Field]:
        return [f for f in self.query(Field) if f.display and all(a.display for a in f.ancestors)]

    def values(self) -> dict[str, Any]:
        return {f.key: f.parsed.value for f in self.visible_fields()}

    # behaviour -----------------------------------------------------------------------

    def on_mount(self) -> None:
        fields = self.visible_fields()
        if fields:
            fields[0].focus_field()
        self._update_summary()
        self._fit()

    def on_resize(self) -> None:
        self._fit()

    def _fit(self) -> None:
        """Let the fields scroll so the summary and the buttons always fit on screen."""
        body = self.query_one(_FormBody)
        dialog = self.query_one("#dialog")
        # everything but the fields, added up (the dialog's own height is capped by the
        # screen, so it can't say how much it needs)
        rest = dialog.styles.gutter.height + dialog.styles.margin.height
        for child in dialog.children:
            if child is not body and child.display:
                rest += child.outer_size.height + child.styles.margin.height
        body.styles.max_height = max(4, self.size.height - rest)

    def new_day(self) -> None:
        """The date rolled over while the form was open: read every field again."""
        for f in self.query(Field):
            f.reparse()
        self._update_summary()

    def on_field_changed(self, event: Field.Changed) -> None:
        if self.is_mounted:
            self.changed(event.field)
            self._update_summary()

    def _update_summary(self) -> None:
        summary = self.query_one("#summary", Static)
        summary.remove_class("-error")
        try:
            text = self.summary()
        except CashError as exc:
            text = Text(sentence(exc.message), style=AMBER)
        summary.update(text or "")
        # a form with a summary keeps its line, so the dialog doesn't jump as you type
        summary.display = bool(text) or type(self).summary is not FormScreen.summary
        self.call_after_refresh(self._fit)  # a longer summary leaves the fields less room

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._advance()

    def on_field_submitted(self, event: Field.Submitted) -> None:
        event.stop()
        self._advance()

    def _advance(self) -> None:
        """Enter: move to the next field, or save from the last one."""
        fields = self.visible_fields()
        focused = self.focused
        current = next(
            (f for f in fields if focused is not None and (focused is f or f in focused.ancestors)),
            None,
        )
        if current is None or current is fields[-1]:
            self.action_save()
        else:
            fields[fields.index(current) + 1].focus_field()

    def action_save(self) -> None:
        bad = [f for f in self.visible_fields() if not f.parsed.ok]
        if bad:
            for f in bad:
                f.touch()
            bad[0].focus_field()
            self.app.bell()
            return
        try:
            self.save(self.values())
        except CashError as exc:
            summary = self.query_one("#summary", Static)
            summary.update(Text(f"✗ {sentence(exc.message)}", style=RED))
            summary.add_class("-error")
            self.call_after_refresh(self._fit)
            summary.display = True

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_action_pressed(self, event: Action.Pressed) -> None:
        event.stop()
        if event.action.id == "save":
            self.action_save()
        elif event.action.id == "cancel":
            self.action_cancel()
        else:
            self.pressed(event.action)


# ── Confirm and prompt ────────────────────────────────────────────────────────


class ConfirmScreen(Dialog[bool]):
    """'Delete Netflix?' with one sentence of consequence; focus starts on the safe button."""

    DEFAULT_CSS = """
    ConfirmScreen {
        & #dialog { width: 58; }
        & #message { height: auto; color: $text-muted; }
        & #buttons { height: 1; margin-top: 1; align-horizontal: right; }
        & .-danger-dialog { border: round $error; }
    }
    """

    BINDINGS: ClassVar = [
        Binding("escape", "answer(False)", "Keep", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def __init__(
        self,
        title: str,
        message: str,
        *,
        yes: str = "Delete",
        no: str = "Keep",
        danger: bool = True,
    ) -> None:
        super().__init__()
        self.title_text = title
        self.message = message
        self.yes, self.no, self.danger = yes, no, danger

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="-danger-dialog" if self.danger else "") as dialog:
            dialog.border_title = f" {self.title_text} "
            yield Static(self.message, id="message")
            with Horizontal(id="buttons"):
                yield Action(self.no, id="no")
                yield Action(self.yes, id="yes", variant="danger" if self.danger else "primary")

    def on_mount(self) -> None:
        self.query_one("#no" if self.danger else "#yes", Action).focus()

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)

    def on_action_pressed(self, event: Action.Pressed) -> None:
        event.stop()
        self.dismiss(event.action.id == "yes")


class PromptScreen(Dialog[str | None]):
    """One input with a live preview line; enter submits when the preview says it reads.

    `preview(text)` returns (ok, line): the line shows faint when ok, amber when not.
    """

    DEFAULT_CSS = """
    PromptScreen {
        & #dialog { width: 60; }
    }
    """

    BINDINGS: ClassVar = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(
        self,
        title: str,
        label: str,
        *,
        value: str = "",
        preview: Callable[[str], tuple[bool, str]] | None = None,
    ) -> None:
        super().__init__()
        self.title_text = title
        self.label = label
        self.initial = value
        self.preview = preview

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog") as dialog:
            dialog.border_title = f" {self.title_text} "
            dialog.border_subtitle = " enter ok · esc cancel "
            yield TextField("answer", self.label, self.initial, parse=self._parse)

    def _parse(self, text: str) -> Parsed:
        if self.preview is None:
            return Parsed.good(text)
        ok, line = self.preview(text)
        return Parsed(ok, line, text)

    def on_mount(self) -> None:
        self.query_one(TextField).focus_field()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        field = self.query_one(TextField)
        if not field.parsed.ok:
            field.touch()
            self.app.bell()
            return
        self.dismiss(field.text)

    def action_cancel(self) -> None:
        self.dismiss(None)
