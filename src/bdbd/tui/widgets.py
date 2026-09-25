"""Shared building blocks, so every view reads as part of one app.

- `View`: the base class of the six views (the app calls `refresh_view()`).
- `Panel`: a rounded box with a title; its border lights up while something inside has focus.
- `KeyValues`: the headline grid: FAINT label, bold value, then notes.
- `Chart`: a balance chart that fills its box, with an optional marker day.
- `RowList`: *the* list: aligned columns, group headers the cursor skips, Enter posts a key;
  `HeadedList` adds column headings that stay put.
- `Picker`: a row of choices drawn in a panel border (horizons, the two halves of a view);
  clicking one picks it.
- `Badge`, `EmptyState`, `keys_hint`, `panel_title` and cell helpers (`signed`, `balance`, …).
- `human_warning`: the core's warnings, said the way the app talks.
- `bdbd(widget)`: the running app, typed, for the session and the app's contract methods.
"""

from __future__ import annotations

import re
import textwrap
from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, ClassVar, Literal, cast

from rich.segment import Segment
from rich.text import Text
from textual import events
from textual.containers import Vertical
from textual.content import Content
from textual.css.styles import RulesMap
from textual.dom import DOMNode
from textual.geometry import Size
from textual.message import Message
from textual.reactive import reactive
from textual.strip import Strip
from textual.style import Style
from textual.visual import RenderOptions, Visual
from textual.widget import Widget
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from bdbd.ui.charts import balance_chart
from bdbd.ui.common import humanize
from bdbd.ui.theme import (
    ACCENT,
    AMBER,
    DARK,
    DOT,
    FAINT,
    GREEN,
    RED,
    balance_color,
    money,
)
from bdbd.words import fmt_date

if TYPE_CHECKING:
    from bdbd.tui.app import BdbdApp
    from bdbd.tui.session import Session


def bdbd(node: DOMNode) -> BdbdApp:
    """The running app, typed: `bdbd(self).session`, `bdbd(self).open_flow_card(id)`."""
    return cast("BdbdApp", node.app)


# ── Cells ─────────────────────────────────────────────────────────────────────
# Small styled pieces the views put in lists and grids, so money looks the same everywhere.

Cell = str | Text | Content


def signed(cents: int) -> Text:
    """Money with its direction: '+$2,650.00' in green for money in, '-$150.00' for out."""
    if cents > 0:
        return Text(money(cents, sign=True), style=GREEN)
    return Text(money(cents))


def balance(cents: int, low: int = 0) -> Text:
    """A balance colored red below zero, amber below `low` (a week of everyday spending)."""
    return Text(money(cents), style=balance_color(cents, low))


def day_cell(d: date, today: date) -> Text:
    """'Fri Oct 2' in FAINT, the way lists show dates."""
    return Text(fmt_date(d, today, weekday=True), style=FAINT)


def keys_hint(*pairs: tuple[str, str]) -> Text:
    """'a add your paycheck · b record your balance': keys in bold accent, words faint."""
    text = Text()
    for i, (key, words) in enumerate(pairs):
        if i:
            text.append(f"  {DOT}  ", style=FAINT)
        text.append(key, style=f"bold {ACCENT}")
        text.append(f" {words}", style=FAINT)
    return text


def panel_title(main: str, *extra: str) -> Content:
    """A border title: 'Coming up · balances include $175/week everyday' (extras faint)."""
    parts: list[str | tuple[str, str]] = [(f" {main}", "bold")]
    for e in extra:
        if e:
            parts.append((f" {DOT} {e}", FAINT))
    parts.append(" ")
    return Content.assemble(*parts)


def to_content(cell: Cell, style: str = "") -> Content:
    """Any cell as Content; `style` (a Rich style) applies to plain strings."""
    if isinstance(cell, Content):
        return cell
    if isinstance(cell, Text):
        return Content.from_rich_text(cell)
    return Content.from_rich_text(Text(cell, style=style)) if style else Content(cell)


# ── Warnings ──────────────────────────────────────────────────────────────────
# The core words its warnings for agents ("debt 'Card': scheduled payment 10.00 < period
# interest …"); these say the same things the way the app talks.

_KINDS = {"extra_payment": "extra payment", "payoff": "payoff", "settle": "sale"}
_WARNINGS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"^scenario: (?:end )?tag '(.+)' matched no active flow$"),
        "No active flow has the tag {0}, so that change does nothing",
    ),
    (
        re.compile(r"^scenario disables debt flow '(.+)' without a payoff/settle event"),
        "{0} just vanishes: nothing pays off what's owed on it",
    ),
    (
        re.compile(r"^scenario ends debt flow '(.+)' after (\S+) without a payoff/settle"),
        "{0}'s payments stop after {1}, but what's owed stays and keeps growing",
    ),
    (
        re.compile(r"^debt '(.+)': scheduled payment (\S+) < period interest (\S+) on (\S+);"),
        "{0}'s payment of {1} doesn't cover its interest ({2}) from {3}, so what's owed grows",
    ),
    (re.compile(r"^scenario\.\w+: no (?:scenario )?flow '(.+)'$"), "there's no flow called {0}"),
    (
        re.compile(r"^scenario\.debt_events: flow '(.+)' has no debt record$"),
        "{0} isn't a debt any more",
    ),
    (
        re.compile(r"^debt '(.+)': balance adjustment on (\S+) would go below.*ignored (\S+)\)"),
        "{0}'s correction on {1} would take it below zero, so {2} of it is ignored",
    ),
    (
        re.compile(r"^debt '(.+)': (\w+) on (\S+) predates the balance date (\S+);"),
        "{0}'s {kind} on {2} is before its balance date ({3}), so it's ignored",
    ),
    (re.compile(r"^debt '(.+)' was paid off on (\S+) \("), "{0} was paid off on {1}"),
    (
        re.compile(r"^debt '(.+)': balance date (\S+) is after as-of"),
        "{0}'s balance date ({1}) is still ahead: its payments count as plain bills until then",
    ),
    (re.compile(r"^debt '(.+)' is not paid off by (\S+);"), "{0} isn't paid off by {1}"),
    (
        re.compile(r"^no balance recorded: this projection starts from 0$"),
        "No balance recorded, so this starts from $0 · b to record it",
    ),
    (
        re.compile(r"^your balance was last recorded (.+); update it"),
        "Your balance was last recorded {0} · b to update it",
    ),
]


def human_warning(text: str) -> str:
    """A core warning or error, said the way the app talks: dates and money in words."""
    for pattern, words in _WARNINGS:
        if m := pattern.match(text):
            groups = m.groups()
            kind = _KINDS.get(groups[1], groups[1].replace("_", " ")) if "{kind}" in words else ""
            said = words.format(*groups, kind=kind)
            break
    else:
        said = text
    said = humanize(said)
    return said[:1].upper() + said[1:]


def warning_line(text: str) -> Text:
    """'! Credit card's payment of $10.00 doesn't cover its interest …' in amber."""
    return Text(f"! {human_warning(text)}", style=AMBER)


# ── The base view ─────────────────────────────────────────────────────────────


class View(Widget):
    """Base class of the six views (Overview, Calendar, Forecast, Budget, Debts, What if).

    A view lives in the app's ContentSwitcher and draws everything from the session in
    `refresh_view()`. The app calls it when the view is shown and the session's version has
    moved since it last drew (and at once when the view is visible), so keep it idempotent and
    let it read, never write. Optional hooks: `action_add()` decides what the global `a` adds
    here; `focus_default()` picks what gets focus when the view is shown.

    The app gives every view the class `-narrow` below 110 columns and `-short` below 32 rows,
    so a view's DEFAULT_CSS can stack or hide side panels: `&.-narrow #side { display: none; }`.
    """

    DEFAULT_CSS = """
    View {
        height: 1fr;
        width: 1fr;
    }
    """

    title: ClassVar[str] = ""
    """The tab label, e.g. 'Overview'."""

    drawn: int = 0
    """The session version this view last drew (kept by the app)."""

    @property
    def session(self) -> Session:
        return bdbd(self).session

    def refresh_view(self) -> None:
        """Redraw everything from the session."""
        raise NotImplementedError

    def focus_default(self) -> None:
        """Focus the view's first focusable (and shown) widget, or nothing when there's none."""
        for widget in self.screen.focus_chain:
            if self in widget.ancestors:
                widget.focus()
                return
        self.screen.set_focus(None)


# ── Panels and headline numbers ───────────────────────────────────────────────


class Picker:
    """A row of choices drawn in a panel's border, one of them chosen; a click picks one.

        horizon = Picker([(1, "1m"), (3, "3m"), (12, "1y")], chosen=3, on_pick=self._horizon)
        panel.set_subtitle(horizon, Content.styled(" · g any day", FAINT))

    Every picker in the app looks the same: the chosen choice as a small accent chip, the
    others faint, two spaces apart.
    """

    def __init__(
        self,
        choices: Sequence[tuple[Hashable, str]],
        *,
        chosen: Hashable | None,
        on_pick: Callable[[Hashable], object],
    ) -> None:
        self.choices = list(choices)
        self.chosen = chosen
        self.on_pick = on_pick

    def content(self) -> tuple[Content, list[tuple[int, int, Hashable]]]:
        """The choices as border text, and where each one is (start, end, value)."""
        parts: list[Content] = []
        spans: list[tuple[int, int, Hashable]] = []
        at = 0
        for i, (value, label) in enumerate(self.choices):
            if i:
                parts.append(Content(" "))
                at += 1
            chip = f" {label} "
            style = f"bold {DARK} on {ACCENT}" if value == self.chosen else FAINT
            parts.append(Content.styled(chip, style))
            spans.append((at, at + len(chip), value))
            at += len(chip)
        return Content.assemble(*parts), spans


BorderPart = str | Content | Picker
Pick = tuple[int, int, Picker, Hashable]  # a choice's cells in a border: start, end


class Panel(Vertical):
    """A rounded box with a title in its top border (and an optional one in the bottom).

    The border is `$border`; while something inside has focus it turns `$primary`. Headline
    panels (the key numbers of a view) pass a variant for a colored border: "accent" for money,
    "purple" for debts, "green"/"amber" for a verdict. They also get a line of breathing room.

        Panel(KeyValues(id="numbers"), title=panel_title("Where you stand"), variant="accent")

    `set_title`/`set_subtitle` take text and `Picker`s; clicking a picker's choice picks it.
    """

    DEFAULT_CSS = """
    Panel {
        height: auto;
        border: round $border;
        border-title-color: $foreground;
        border-subtitle-color: $text-muted;
        border-title-align: left;
        border-subtitle-align: right;
        padding: 0 1;
        background: $background;
        &:focus-within { border: round $primary; }
        &.-accent { border: round $primary; padding: 1 2; }
        &.-purple { border: round $secondary; padding: 1 2; }
        &.-green { border: round $success; padding: 1 2; }
        &.-amber { border: round $warning; padding: 1 2; }
    }
    """

    def __init__(
        self,
        *children: Widget,
        title: str | Content = "",
        subtitle: str | Content = "",
        variant: Literal["accent", "purple", "green", "amber"] | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(*children, id=id, classes=classes)
        self.border_title = title if isinstance(title, Content) or not title else f" {title} "
        self.border_subtitle = subtitle
        # what set_title/set_subtitle drew: (the label as Textual reports it back, the label,
        # where each picker choice is); Textual keeps labels as markup, so this is ours
        self._drawn: dict[str, tuple[str | None, Content, list[Pick]]] = {}
        self._fitted: tuple[str, tuple[str, ...]] | None = None  # see fit_title
        if variant:
            self.add_class(f"-{variant}")

    def fit_title(self, main: str, *extras: str) -> None:
        """A title that fits: 'Debts · $36,538.89 owed · debt-free Aug 2034' loses its extras,
        last first, while the panel is too narrow for them (and gets them back as it widens),
        rather than being cut off mid-word."""
        self._fitted = (main, tuple(e for e in extras if e))
        self._drawn.pop("top", None)
        self._refit()

    def _refit(self) -> None:
        if self._fitted is None:
            return
        main, extras = self._fitted
        room = self.outer_size.width - 6  # the corners, the edges and a space each side
        title = panel_title(main, *extras)
        for n in range(len(extras) - 1, -1, -1):
            if room <= 0 or title.cell_length <= room:  # before layout: all of it
                break
            title = panel_title(main, *extras[:n])
        self.border_title = title

    def on_resize(self) -> None:
        self._refit()

    def set_title(self, *parts: BorderPart) -> None:
        """The top border: text and pickers, left to right."""
        self._fitted = None
        label, picks = self._border(parts)
        self.border_title = label
        self._drawn["top"] = (self.border_title, label, picks)

    def set_subtitle(self, *parts: BorderPart) -> None:
        """The bottom border (right-aligned): text and pickers, left to right."""
        label, picks = self._border(parts)
        self.border_subtitle = label
        self._drawn["bottom"] = (self.border_subtitle, label, picks)

    @staticmethod
    def _border(parts: Sequence[BorderPart]) -> tuple[Content, list[Pick]]:
        pieces: list[Content] = []
        picks: list[Pick] = []
        at = 0
        for part in parts:
            if isinstance(part, Picker):
                content, spans = part.content()
                picks += [(at + a, at + b, part, value) for a, b, value in spans]
            else:
                content = part if isinstance(part, Content) else Content(part)
            pieces.append(content)
            at += content.cell_length
        return Content.assemble(*pieces), picks

    def on_click(self, event: events.Click) -> None:
        """A click on a picker's choice in either border picks it."""
        width, height = self.outer_size
        if event.y == 0:
            where, now = "top", self.border_title
        elif event.y == height - 1:
            where, now = "bottom", self.border_subtitle
        else:
            return
        drawn = self._drawn.get(where)
        if drawn is None or drawn[0] != now:  # never set with pickers, or replaced since
            return
        _, text, picks = drawn
        # where Textual draws a border label: after the corner, the edge and a space (left),
        # or before a space, the edge and the corner (right)
        start = 3 if where == "top" else width - 3 - text.cell_length
        for a, b, picker, value in picks:
            if start + a <= event.x < start + b:
                event.stop()
                picker.on_pick(value)
                return


class KeyValues(Widget):
    """The headline grid: a FAINT label, a bold right-aligned value, then notes.

        numbers.set_rows([
            ("Balance now", balance(407000), Text("est. from $4,120.00 on Sep 22", style=FAINT)),
            ("Lowest ahead", balance(159500), "Thu Oct 1 · next 90 days"),
        ])

    Columns are as wide as their widest cell, three spaces apart; each row's last cell takes
    what's left (with an ellipsis when it has to). Plain-string labels are FAINT, plain-string
    values bold; Text cells keep their own style.
    """

    DEFAULT_CSS = """
    KeyValues {
        height: auto;
        width: 1fr;
    }
    """

    GAP = 3

    def __init__(
        self, *, label_width: int = 0, id: str | None = None, classes: str | None = None
    ) -> None:
        """`label_width` keeps the label column at least that wide (to line up with a form)."""
        super().__init__(id=id, classes=classes)
        self.label_width = label_width
        self._rows: list[list[Text]] = []

    def set_rows(self, rows: Sequence[Sequence[str | Text]]) -> None:
        self._rows = [
            [
                cell
                if isinstance(cell, Text)
                else Text(cell, style=FAINT if i == 0 else "bold" if i == 1 else "")
                for i, cell in enumerate(row)
            ]
            for row in rows
        ]
        self.refresh(layout=True)

    def _widths(self) -> list[int]:
        rows = self._rows
        columns = max((len(r) for r in rows), default=0)
        widths = [
            max((r[i].cell_len for r in rows if i < len(r)), default=0) for i in range(columns)
        ]
        if widths:
            widths[0] = max(widths[0], self.label_width)
        return widths

    def get_content_width(self, container: Size, viewport: Size) -> int:
        """Its natural width (every cell in full), so `width: auto` fits the rows."""
        widths = self._widths()
        return sum(widths) + self.GAP * max(len(widths) - 1, 0)

    @classmethod
    def width_of(cls, rows: Sequence[Sequence[str | Text]]) -> int:
        """How wide a grid of these rows is with nothing cut short (before it's shown)."""
        lengths = [
            [Text(c).cell_len if isinstance(c, str) else c.cell_len for c in r] for r in rows
        ]
        columns = max((len(r) for r in lengths), default=0)
        widths = [max((r[i] for r in lengths if i < len(r)), default=0) for i in range(columns)]
        return sum(widths) + cls.GAP * max(columns - 1, 0)

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return len(self._rows)

    def render(self) -> Text:
        rows = self._rows
        if not rows:
            return Text()
        width = self.size.width or 1000
        widths = self._widths()
        lines = []
        for row in rows:
            line = Text(no_wrap=True, overflow="ellipsis")
            for i, cell in enumerate(row):
                if i:
                    line.append(" " * self.GAP)
                if i == 1:  # the value, right-aligned
                    line.append(" " * (widths[i] - cell.cell_len))
                    line.append_text(cell)
                elif i == len(row) - 1:  # the last note takes what's left
                    cell = cell.copy()
                    cell.truncate(max(0, width - line.cell_len), overflow="ellipsis")
                    line.append_text(cell)
                else:
                    line.append_text(cell)
                    line.append(" " * (widths[i] - cell.cell_len))
            lines.append(line)
        return Text("\n").join(lines)


class Badge(Static):
    """A small label on a colored pill: ' WHAT IF ', ' bdbd '."""

    DEFAULT_CSS = """
    Badge {
        width: auto;
        height: 1;
    }
    """

    def __init__(self, label: str, color: str = ACCENT, *, id: str | None = None) -> None:
        super().__init__(Text(f" {label} ", style=f"bold {DARK} on {color}"), id=id)


class EmptyState(Static):
    """A calm, centered message for a list or view with nothing in it yet.

    EmptyState("No flows yet.", "bdbd projects your balance from your incomes and bills.",
               keys=[("a", "add your paycheck"), ("b", "record your balance")])
    """

    DEFAULT_CSS = """
    EmptyState {
        width: 1fr;
        height: 1fr;
        content-align: center middle;
        text-align: center;
        padding: 1 2;
    }
    """

    def __init__(
        self,
        title: str = "",
        body: str = "",
        *,
        keys: Iterable[tuple[str, str]] = (),
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self.show(title, body, keys=keys)

    def show(self, title: str, body: str = "", *, keys: Iterable[tuple[str, str]] = ()) -> None:
        """Change the message."""
        text = Text(justify="center")
        if title:
            text.append(title, style="bold")
        if body:
            lines = "\n".join(textwrap.fill(line, 64) for line in body.splitlines())
            text.append(("\n\n" if title else "") + lines, style=FAINT)
        pairs = list(keys)
        if pairs:
            text.append("\n\n")
            text.append_text(keys_hint(*pairs))
        self.update(text)


# ── Charts ────────────────────────────────────────────────────────────────────


class Chart(Widget):
    """End-of-day balances as columns rising from zero, filling the widget.

    The last row is the date axis. `marker` lights up one day's column and labels it on the
    axis (the Forecast view moves it with its cursor).

        chart.set_series(window.run.daily, floor=weekly)
        chart.marker = date(2026, 12, 12)
    """

    DEFAULT_CSS = """
    Chart {
        height: 1fr;
        min-height: 3;
        text-wrap: nowrap;
        text-overflow: clip;
    }
    """

    marker: reactive[date | None] = reactive(None)

    def __init__(
        self, *, color: str = ACCENT, id: str | None = None, classes: str | None = None
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._series: list[tuple[date, int]] = []
        self._color = color
        self._floor: int | None = None
        self._below = RED

    def set_series(
        self,
        series: Sequence[tuple[date, int]],
        *,
        color: str | None = None,
        floor: int | None = None,
        marker: date | None = None,
        below: str = RED,
    ) -> None:
        """Draw these (date, cents) points; columns under `floor` (or zero) are `below`."""
        self._series = list(series)
        self._color = color or self._color
        self._floor = floor
        self._below = below
        self.marker = marker
        self.refresh()

    def render(self) -> Text:
        width, height = self.content_size
        if not self._series or height < 2 or width < 12:
            return Text()
        lines = balance_chart(
            self._series,
            width,
            height=height - 1,
            color=self._color,
            floor=self._floor,
            marker=self.marker,
            below=self._below,
        )
        return Text("\n").join(lines)


# ── RowList: the one list ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Column:
    """How one RowList column lays out.

    Columns are as wide as their widest cell. One `flex` column (usually the name) takes the
    leftover width, so right-aligned money lines up at the list's right edge, and it gives
    width back first (with an ellipsis) when the list is narrow.
    """

    align: Literal["left", "right"] = "left"
    flex: bool = False
    min: int = 6  # a flex column never gets narrower than this
    max: int | None = None  # a fixed column never gets wider than this (ellipsis)
    style: str = ""  # a Rich style for plain-string cells, e.g. FAINT for dates


class Row:
    """A selectable line of a RowList: its cells, and the key Enter reports (a flow id, …)."""

    __slots__ = ("cells", "key")

    def __init__(self, *cells: Cell, key: Hashable) -> None:
        self.cells = cells
        self.key = key


class Header:
    """A group heading the cursor skips ('Income'), with optional text on the right."""

    __slots__ = ("right", "title")

    def __init__(self, title: Cell, right: Cell = "") -> None:
        self.title = title
        self.right = right


Item = Row | Header | None  # None is a blank separator line


class _Line(Visual):
    """One RowList line, laid out when it's drawn so its columns follow the list's width."""

    def __init__(self, owner: RowList, index: int) -> None:
        self.owner = owner
        self.index = index

    def render_strips(
        self, width: int, height: int | None, style: Style, options: RenderOptions
    ) -> list[Strip]:
        line = self.owner._layout(self.index, width)
        return line.render_strips(width, 1, style, options)

    def get_optimal_width(self, rules: RulesMap, container_width: int) -> int:
        return self.owner._natural_width()

    def get_height(self, rules: RulesMap, width: int) -> int:
        return 1


class RowList(OptionList):
    """The one list primitive: every list in the app is a RowList.

    Aligned columns (money right-aligned), group headers the cursor skips, blank separator
    lines, a cursor that doesn't wrap and stays on the same key across redraws, mouse clicks,
    scrolling. A click moves the cursor (whatever shows the row beside the list follows); Enter
    or a double click posts `RowList.Selected` with the row's key. Moving the cursor posts
    `RowList.Highlighted`.

        rows = RowList(
            Column(style=FAINT),                # date
            Column(flex=True),                  # name
            Column(align="right"),              # amount
            id="upcoming",
        )
        rows.set_rows([
            Header("Income", "+$5,760.07/mo"),
            Row("Fri Oct 2", "Paycheck", signed(265000), key=1),
            None,                               # a blank line
            Header("Expenses"),
            Row("Thu Oct 1", "Rent", signed(-215000), key=3),
        ])

        def on_row_list_selected(self, event: RowList.Selected) -> None:
            bdbd(self).open_flow_card(event.key)
    """

    DEFAULT_CSS = """
    RowList {
        height: auto;
        max-height: 100%;
        border: none;
        padding: 0;
        background: transparent;
        scrollbar-size-vertical: 1;
        &:focus {
            border: none;
            background-tint: transparent;
        }
        & > .option-list--option { padding: 0 1 0 0; }
        & > .option-list--option-disabled { color: $foreground; }
        & > .option-list--option-hover { background: $boost; }
        & > .option-list--option-highlighted {
            color: $foreground;
            background: $primary 10%;
            text-style: none;
        }
        &:focus > .option-list--option-highlighted {
            color: $foreground;
            background: $primary 22%;
            text-style: none;
        }
        & > .row-list--cursor { color: $primary 45%; }
        &:focus > .row-list--cursor { color: $primary; }
    }
    """

    COMPONENT_CLASSES: ClassVar[set[str]] = {*OptionList.COMPONENT_CLASSES, "row-list--cursor"}

    class Selected(Message):
        """Enter (or a click) on a row."""

        def __init__(self, row_list: RowList, key: Hashable, row: Row) -> None:
            super().__init__()
            self.row_list = row_list
            self.key = key
            self.row = row

        @property
        def control(self) -> RowList:
            return self.row_list

    class Highlighted(Selected):
        """The cursor moved to a row."""

    def __init__(
        self,
        *columns: Column,
        gap: int = 2,
        empty: str = "",
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self.columns = columns or (Column(flex=True),)
        self.gap = gap
        self.empty = empty
        self._items: list[Item] = []
        self._cells: list[tuple[Content, ...] | None] = []
        self._natural: list[int] = [0] * len(self.columns)
        if empty:
            self.set_rows([])

    # public API --------------------------------------------------------------------

    def set_rows(self, items: Iterable[Item]) -> None:
        """Show these rows, keeping the cursor on the same key (or near where it was)."""
        key, index = self.key, self.highlighted
        self._items = list(items)
        self._cells = [
            tuple(to_content(c, col.style) for c, col in zip(it.cells, self.columns, strict=False))
            if isinstance(it, Row)
            else None
            for it in self._items
        ]
        self._natural = [
            max((c[i].cell_length for c in self._cells if c and i < len(c)), default=0)
            for i in range(len(self.columns))
        ]
        options = [
            Option(_Line(self, i), disabled=not isinstance(it, Row))
            for i, it in enumerate(self._items)
        ]
        if not options and self.empty:
            options = [Option(Content.styled(self.empty, FAINT), disabled=True)]
        self.set_options(options)
        self.highlighted = self._restore(key, index)

    @property
    def items(self) -> list[Item]:
        """What's shown, headers and blanks included."""
        return self._items

    @property
    def key(self) -> Hashable | None:
        """The key of the row under the cursor, if any."""
        row = self.current
        return row.key if row else None

    @property
    def current(self) -> Row | None:
        """The row under the cursor, if any."""
        i = self.highlighted
        if i is None or i >= len(self._items):
            return None
        item = self._items[i]
        return item if isinstance(item, Row) else None

    def select_key(self, key: Hashable) -> bool:
        """Move the cursor to the row with this key; False when there's none."""
        for i, item in enumerate(self._items):
            if isinstance(item, Row) and item.key == key:
                self.highlighted = i
                return True
        return False

    # cursor --------------------------------------------------------------------------

    def _restore(self, key: Hashable | None, index: int | None) -> int | None:
        rows = [i for i, it in enumerate(self._items) if isinstance(it, Row)]
        if not rows:
            return None
        if key is not None:
            for i in rows:
                item = self._items[i]
                if isinstance(item, Row) and item.key == key:
                    return i
        if index is None:
            return rows[0]
        return next((i for i in rows if i >= index), rows[-1])

    def _step(self, direction: int) -> None:
        if self.highlighted is None:
            self.action_first()
            return
        i = self.highlighted + direction
        while 0 <= i < len(self._items):
            if isinstance(self._items[i], Row):
                self.highlighted = i
                return
            i += direction
        if direction < 0:
            self.scroll_home(animate=False)  # show the header above the first row
        else:
            self.scroll_end(animate=False)

    def action_cursor_down(self) -> None:
        self._step(1)

    def action_cursor_up(self) -> None:
        self._step(-1)

    def action_page_down(self) -> None:
        super().action_page_down()
        self._onto_row(1)

    def action_page_up(self) -> None:
        super().action_page_up()
        self._onto_row(-1)

    def _onto_row(self, direction: int) -> None:
        """After a jump, keep the cursor on a row (a page can land on a header, or nothing)."""
        if isinstance(self.current, Row):
            return
        rows = [i for i, it in enumerate(self._items) if isinstance(it, Row)]
        if not rows:
            return
        at = self.highlighted if self.highlighted is not None else (0 if direction < 0 else -1)
        ahead = [i for i in rows if (i > at if direction > 0 else i < at)]
        if ahead:
            self.highlighted = ahead[0] if direction > 0 else ahead[-1]
        else:
            self.highlighted = rows[0] if direction < 0 else rows[-1]

    async def _on_click(self, event: events.Click) -> None:
        """One click moves the cursor; a double click opens the row, like enter."""
        if event.chain >= 2:
            return  # OptionList's own handler, next in line, selects it
        event.prevent_default()
        option = event.style.meta.get("option")
        if option is not None and not self._options[option].disabled:
            self.highlighted = option

    def watch_highlighted(self, highlighted: int | None) -> None:
        if highlighted is None or highlighted >= len(self._items):
            return
        item = self._items[highlighted]
        if not isinstance(item, Row):
            return
        if all(not isinstance(it, Row) for it in self._items[:highlighted]):
            self.scroll_home(animate=False, immediate=True)
        self.scroll_to_highlight()
        self.post_message(self.Highlighted(self, item.key, item))

    def action_select(self) -> None:
        row = self.current
        if row is not None:
            self.post_message(self.Selected(self, row.key, row))

    # layout --------------------------------------------------------------------------

    def _natural_width(self) -> int:
        return sum(self._natural) + self.gap * (len(self.columns) - 1)

    def _widths(self, width: int) -> list[int]:
        cols = self.columns
        widths = [
            n if c.flex or c.max is None else min(n, c.max)
            for n, c in zip(self._natural, cols, strict=True)
        ]
        flex = [i for i, c in enumerate(cols) if c.flex]
        if flex:
            fixed = sum(w for i, w in enumerate(widths) if i not in flex)
            spare = width - fixed - self.gap * (len(cols) - 1)
            share, extra = divmod(max(spare, 0), len(flex))
            for k, i in enumerate(flex):
                widths[i] = max(cols[i].min, share + (1 if k < extra else 0))
        return widths

    def _layout(self, index: int, width: int) -> Content:
        item = self._items[index]
        if item is None:
            return Content("")
        if isinstance(item, Header):
            right = to_content(item.right, FAINT)
            title = to_content(item.title).stylize_before("bold")
            room = width - right.cell_length - (2 if right.cell_length else 0)
            title = title.truncate(max(room, 0), ellipsis=True)
            gap = " " * max(0, width - title.cell_length - right.cell_length)
            return Content.assemble(title, gap, right)
        cells = self._cells[index] or ()
        widths = self._widths(width)
        parts: list[Content | str] = []
        for i, (col, w) in enumerate(zip(self.columns, widths, strict=True)):
            cell = cells[i] if i < len(cells) else Content("")
            cell = cell.truncate(w, ellipsis=True)
            pad = " " * (w - cell.cell_length)
            if i:
                parts.append(" " * self.gap)
            parts.extend([pad, cell] if col.align == "right" else [cell, pad])
        return Content.assemble(*parts).truncate(width, ellipsis=True)

    # the cursor bar in the left gutter -------------------------------------------------

    def _get_left_gutter_width(self) -> int:
        return 1

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        try:
            index = self._lines[self.scroll_offset.y + y][0]
        except IndexError:
            return strip
        mark = " "
        style = self.get_visual_style("option-list--option").rich_style
        if index == self.highlighted and isinstance(self.current, Row):
            mark = "▌"
            style = self.get_component_rich_style("row-list--cursor")
        return Strip([Segment(mark, style), *strip], strip.cell_length + 1)


class Heads(Widget):
    """The column headings of a `HeadedList`, lined up with its columns as it scrolls."""

    DEFAULT_CSS = """
    Heads {
        height: 1;
        text-wrap: nowrap;
        text-overflow: clip;
    }
    """

    def __init__(self, table: HeadedList) -> None:
        super().__init__()
        self.table = table

    def render(self) -> Content:
        return self.table.heading()


class HeadedList(RowList):
    """A RowList with faint column headings above it that stay put as it scrolls.

    Yield its `heads` just before it:

        table = HeadedList(Column(), Column(align="right"), heads=["Date", "Payment"])
        yield table.heads
        yield table
    """

    def __init__(
        self,
        *columns: Column,
        heads: Sequence[str],
        gap: int = 2,
        empty: str = "",
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        self.head_text = list(heads)
        self.heads = Heads(self)
        super().__init__(*columns, gap=gap, empty=empty, id=id, classes=classes)

    def set_rows(self, items: Iterable[Item], heads: Sequence[str] | None = None) -> None:
        """Show these rows (and new headings), keeping the cursor on the same key."""
        if heads is not None:
            self.head_text = list(heads)
        super().set_rows(items)
        self._natural = [
            max(n, len(h)) for n, h in zip(self._natural, self.head_text, strict=False)
        ]
        self.heads.display = any(isinstance(it, Row) for it in self.items)
        if self.is_mounted:  # once the rows are laid out (a scrollbar may have come or gone)
            self.call_after_refresh(self.heads.refresh)

    def heading(self) -> Content:
        """The headings laid out on the columns the rows use at the current width."""
        width = self.scrollable_content_region.width - 2  # the cursor gutter, the right pad
        if width <= 0:
            return Content("")
        parts: list[Content | str] = [" "]
        for i, (col, w) in enumerate(zip(self.columns, self._widths(width), strict=True)):
            head = self.head_text[i] if i < len(self.head_text) else ""
            cell = Content(head).truncate(w, ellipsis=True)
            pad = " " * (w - cell.cell_length)
            if i:
                parts.append(" " * self.gap)
            parts.extend([pad, cell] if col.align == "right" else [cell, pad])
        return Content.assemble(*parts).stylize(FAINT)

    def on_resize(self) -> None:
        self.heads.refresh()
