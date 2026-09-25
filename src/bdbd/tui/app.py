"""The app shell: top bar and tabs, the what-if banner, the six views, keys, palette, watcher.

Views reach everything through `BdbdApp` (typed via `widgets.bdbd(self)`): the session, and the
contract methods below (`show_view`, `open_flow_card`, `add_flow`, `confirm`, `changed`, …).
The single-key global shortcuts live on `MainScreen`, not the App, so they never fire inside a
dialog; ctrl+q quits and ctrl+p opens the command palette everywhere.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from functools import partial
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import ActiveBinding, Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Center, Horizontal, Vertical
from textual.keys import format_key
from textual.notifications import SeverityLevel
from textual.screen import Screen
from textual.theme import Theme
from textual.widget import Widget
from textual.widgets import ContentSwitcher, Footer, Static

from bdbd.backup import is_budget
from bdbd.core.errors import CashError
from bdbd.core.models import Flow, Kind
from bdbd.tui import flow_form, modals
from bdbd.tui.forms import Action, ConfirmScreen, FormScreen, PromptScreen
from bdbd.tui.session import Done, Session, describe_flow
from bdbd.tui.text import sentence
from bdbd.tui.views.budget import BudgetView
from bdbd.tui.views.calendar import CalendarView
from bdbd.tui.views.debts import DebtsView
from bdbd.tui.views.forecast import ForecastView
from bdbd.tui.views.overview import OverviewView
from bdbd.tui.views.whatif import WhatIfView
from bdbd.tui.widgets import Badge, View, bdbd, human_warning
from bdbd.ui.common import humanize, tilde
from bdbd.ui.theme import (
    ACCENT,
    AMBER,
    BACKGROUND_HEX,
    BORDER,
    DARK,
    DOT,
    FAINT,
    FOREGROUND,
    GREEN,
    PANEL_BG,
    PURPLE,
    RED,
    SURFACE,
    muted,
)
from bdbd.words import fmt_date

THEME = Theme(
    name="bdbd",
    primary=ACCENT,
    secondary=PURPLE,
    accent=ACCENT,
    success=GREEN,
    warning=AMBER,
    error=RED,
    foreground=FOREGROUND,
    background=BACKGROUND_HEX,
    surface=SURFACE,
    panel=PANEL_BG,
    dark=True,
    variables={
        "border": BORDER,
        "border-blurred": BORDER,
        "text-muted": FAINT,
        "faint": FAINT,
        "boost": "#ffffff08",
        "footer-background": BACKGROUND_HEX,
        "footer-key-foreground": ACCENT,
        "footer-description-foreground": FAINT,
        "footer-item-background": BACKGROUND_HEX,
        "block-cursor-background": ACCENT,
        "block-cursor-foreground": BACKGROUND_HEX,
        "input-cursor-background": FOREGROUND,
        "input-selection-background": f"{ACCENT} 35%",
        "scrollbar": muted(ACCENT, 0.35),
        "scrollbar-hover": muted(ACCENT, 0.6),
        "scrollbar-active": ACCENT,
        "scrollbar-background": BACKGROUND_HEX,
        "scrollbar-background-hover": BACKGROUND_HEX,
        "scrollbar-background-active": BACKGROUND_HEX,
        "scrollbar-corner-color": BACKGROUND_HEX,
    },
)

VIEWS: list[tuple[str, type[View]]] = [
    ("overview", OverviewView),
    ("calendar", CalendarView),
    ("forecast", ForecastView),
    ("budget", BudgetView),
    ("debts", DebtsView),
    ("whatif", WhatIfView),
]
MIN_SIZE = (80, 24)
TIMES = "\u00d7"
NARROW = 110  # columns: below this the main screen gets the -narrow class
SHORT = 32  # rows: below this it gets -short
COMPACT = 100  # columns: below this the footer keeps the view's keys and drops the globals
# globals the compact footer leaves to help (?), so the view's own keys and ? and q fit
COMPACT_HIDDEN = {"app.record_balance", "app.show_view('overview')", "app.what_if"}


# ── The top bar ───────────────────────────────────────────────────────────────


class ViewTab(Static):
    """One tab: '1 Overview'. Clicking it shows the view."""

    def __init__(self, number: int, name: str, label: str) -> None:
        super().__init__(id=f"tab-{name}", classes="view-tab")
        self.number, self.view_name, self.label = number, name, label

    def on_mount(self) -> None:
        self.draw()

    def draw(self) -> None:
        active = self.has_class("-active")
        self.update(
            Text.assemble(
                (f"{self.number} ", ACCENT if active else FAINT),
                (self.label, f"bold {FOREGROUND}" if active else FAINT),
            )
        )

    def on_click(self) -> None:
        bdbd(self).show_view(self.view_name)


class TabRule(Widget):
    """The line under the top bar, lit in the accent color under the active tab."""

    DEFAULT_CSS = """
    TabRule { height: 1; }
    """

    def render(self) -> Text:
        width = self.size.width
        line = Text("─" * width, style=BORDER)
        tabs = self.screen.query(".view-tab.-active")
        tab = tabs.first(ViewTab) if tabs else None
        if tab is not None and tab.region.width:
            start = max(0, tab.region.x - self.region.x)
            end = min(width, start + tab.region.width)
            line = Text.assemble(
                ("─" * start, BORDER), ("━" * (end - start), ACCENT), ("─" * (width - end), BORDER)
            )
        return line


class Banner(Widget):
    """The what-if banner: the changes being tried, and how to leave or edit them.

    The sentences give way (with an ellipsis) before the keys at the end do.
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._message = Text()
        self._tail = Text()

    def draw(self, session: Session, searching: str | None = None) -> None:
        """`searching`: what What if's earliest-date search is doing about an unpinned '?'
        ("searching", "none" when no date works, "unpinned" when the person unpinned it)."""
        s = session.scenario
        self.display = s.enabled and s.any
        if not self.display:
            return
        if session.scenario_error:
            self._message = Text(
                f"This what-if doesn't fit your budget any more: "
                f"{human_warning(session.scenario_error)}",
                style=AMBER,
            )
            self._tail = Text(f"6 edit {DOT} w off", style=FAINT)
        elif s.unpinned and searching == "none":
            self._message = Text("No date in the next year works for the what-if", style=AMBER)
            self._tail = Text(f"other views show your budget as it is {DOT} 6 edit", style=FAINT)
        elif s.unpinned and searching == "unpinned":
            self._message = Text("The what-if's date is unpinned")
            self._tail = Text(f"other views show your budget as it is {DOT} 6 pin", style=FAINT)
        elif s.unpinned:
            self._message = Text("Finding the earliest date for the what-if…")
            self._tail = Text("other views show your budget as it is until then", style=FAINT)
        else:
            self._message = Text(f" {DOT} ".join(s.sentences(session.today, short=True)))
            self._tail = Text(f"nothing is saved {DOT} w off {DOT} 6 edit", style=FAINT)
        self.refresh()

    def render(self) -> Text:
        badge = Text(" WHAT IF ", style=f"bold {DARK} on {ACCENT}")
        width = self.size.width
        tail = self._tail
        if self._message.cell_len + tail.cell_len + 16 > width:  # keep the keys, drop the words
            tail = Text(tail.plain.replace(f"nothing is saved {DOT} ", ""), style=FAINT)
        message = self._message.copy()
        message.truncate(max(width - badge.cell_len - tail.cell_len - 6, 0), overflow="ellipsis")
        gap = " " * max(2, width - badge.cell_len - message.cell_len - tail.cell_len - 2)
        return Text.assemble(badge, "  ", message, gap, tail)


class TooSmall(Static):
    """Shown instead of everything when the terminal is smaller than 80 by 24."""


# ── Screens ───────────────────────────────────────────────────────────────────


class MainScreen(Screen):
    """The screen hosting the views. Its single-key shortcuts don't reach dialogs."""

    AUTO_FOCUS = ""  # never Textual's first-focusable guess: it can pick a hidden view's list
    BINDINGS: ClassVar = [
        Binding("b", "app.record_balance", "balance"),
        Binding("a", "add", "add"),
        Binding("1", "app.show_view('overview')", "views", key_display="1-6"),
        Binding("2", "app.show_view('calendar')", "Calendar", show=False),
        Binding("3", "app.show_view('forecast')", "Forecast", show=False),
        Binding("4", "app.show_view('budget')", "Budget", show=False),
        Binding("5", "app.show_view('debts')", "Debts", show=False),
        Binding("6", "app.show_view('whatif')", "What if", show=False),
        Binding("w", "app.what_if", "what-if"),
        Binding("question_mark", "app.help", "help"),
        Binding("colon", "app.command_palette", "commands", show=False),
        Binding("comma", "app.open_settings", "settings", show=False),
        Binding("ctrl+r", "app.reload", "reload", show=False),
        Binding("q", "app.ask_quit", "quit"),
    ]

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Badge("bdbd", id="brand")
            with Horizontal(id="tabs"):
                for i, (name, cls) in enumerate(VIEWS, start=1):
                    yield ViewTab(i, name, cls.title)
            yield Static(id="meta")
        yield TabRule(id="tabrule")
        yield Banner(id="banner")
        with ContentSwitcher(id="views", initial="overview"):
            for name, cls in VIEWS:
                yield cls(id=name)
        yield TooSmall(f"Make the window a bit bigger (80{TIMES}24)", id="too-small")
        yield Footer()

    @property
    def switcher(self) -> ContentSwitcher:
        return self.query_one("#views", ContentSwitcher)

    @property
    def current_view(self) -> View:
        return self.query_one(f"#{self.switcher.current or 'overview'}", View)

    @property
    def active_bindings(self) -> dict[str, ActiveBinding]:
        """What the footer shows (key handling doesn't use this): narrow windows leave the
        globals that help lists to help, so the view's keys, ? and q still fit."""
        shown = super().active_bindings
        if not self.has_class("-compact"):
            return shown
        return {k: b for k, b in shown.items() if b.binding.action not in COMPACT_HIDDEN}

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Behind the too-small notice nothing is visible, so only quitting and help work."""
        if self.has_class("-too-small"):
            return action in ("ask_quit", "help", "quit")
        return True

    def on_resize(self, event: events.Resize) -> None:
        w, h = event.size
        small = w < MIN_SIZE[0] or h < MIN_SIZE[1]
        if small and not self.has_class("-too-small"):
            self.set_focus(None)  # keys mustn't act on a view nobody can see
        self.set_class(small, "-too-small")
        self.set_class(w < NARROW, "-narrow")
        self.set_class(h < SHORT, "-short")
        if self.has_class("-compact") != (w < COMPACT):
            self.set_class(w < COMPACT, "-compact")
            self.query_one(Footer).show_command_palette = w >= COMPACT
            self.refresh_bindings()
        for view in self.query(View):
            view.set_class(w < NARROW, "-narrow")
            view.set_class(h < SHORT, "-short")
        self.query_one(TabRule).refresh()
        if not small:
            self.call_after_refresh(self.keep_focus)

    def on_screen_resume(self) -> None:
        """Back from a dialog, help or the palette: focus goes to the visible view."""
        self.call_after_refresh(self.keep_focus)

    def keep_focus(self) -> None:
        """Give focus to the visible view when it's nowhere in it (e.g. after a dialog, or
        when the view hid its focused widget)."""
        if self.has_class("-too-small") or self.app.screen is not self:
            return
        view = self.current_view
        focused = self.focused
        if focused is None or not (focused is view or view in focused.ancestors):
            view.focus_default()

    def draw_chrome(self, session: Session, searching: str | None = None) -> None:
        """The top bar's meta, the tabs and the banner."""
        current = self.switcher.current
        for tab in self.query(ViewTab):
            tab.set_class(tab.view_name == current, "-active")
            tab.draw()
        today = session.today
        meta = f"{session.path.name} {DOT} {fmt_date(today, today, weekday=True)}"
        self.query_one("#meta", Static).update(Text(meta, style=FAINT))
        self.query_one(Banner).draw(session, searching)
        self.call_after_refresh(self.query_one(TabRule).refresh)

    def action_add(self) -> None:
        """The global `a`: the view decides what it adds, else a flow."""
        add = getattr(self.current_view, "action_add", None)
        if callable(add):
            add()
        else:
            bdbd(self).add_flow()


class WelcomeScreen(Screen):
    """No budget file yet: offer to create one."""

    BINDINGS: ClassVar = [
        Binding("escape,q", "app.quit", "quit", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    def compose(self) -> ComposeResult:
        with Center(), Vertical(id="welcome-box"):
            yield Badge("bdbd")
            yield Static(
                Text.assemble(
                    ("\nbdbd keeps your budget in one file.\n", "bold"),
                    ("There isn't one at ", FAINT),
                    (tilde(self.path), FOREGROUND),
                    (" yet.", FAINT),
                ),
                id="welcome-text",
            )
            with Horizontal(id="welcome-buttons"):
                yield Action("Create it", id="create", variant="primary")
                yield Action("Quit", id="quit")

    def on_mount(self) -> None:
        self.query_one("#create", Action).focus()

    def on_action_pressed(self, event: Action.Pressed) -> None:
        if event.action.id == "create":
            bdbd(self).create_budget()
        else:
            self.app.exit()


class ProblemScreen(Screen):
    """The budget can't be opened (newer schema, not a budget file): say so, quit on any key."""

    def __init__(self, message: str, path: Path | None = None) -> None:
        super().__init__()
        self.message = message
        self.path = path

    def compose(self) -> ComposeResult:
        where = (
            Text.assemble(
                ("\n\nIt's at ", FAINT),
                (tilde(self.path), FOREGROUND),
                (". To open another budget, run ", FAINT),
                ("bdbd --db PATH", FOREGROUND),
                (" or set ", FAINT),
                ("BDBD_DB", FOREGROUND),
                (".", FAINT),
            )
            if self.path is not None
            else Text()
        )
        with Center(), Vertical(id="problem-box"):
            yield Badge("bdbd", AMBER)
            yield Static(
                Text.assemble(
                    ("\nbdbd couldn't open your budget.\n", "bold"),
                    (self.message, AMBER),
                    where,
                    ("\n\nPress any key to quit.", FAINT),
                )
            )

    def on_key(self, event: events.Key) -> None:
        event.stop()
        self.app.exit(return_code=1)


# ── The command palette ───────────────────────────────────────────────────────


class Commands(Provider):
    """What ctrl+p (or :) offers: views, the main actions, and every flow by name."""

    def _commands(self) -> list[tuple[str, Callable[[], object], str]]:
        """(name, what it does, a line of help) for every command."""
        app = self.app
        assert isinstance(app, BdbdApp)
        commands: list[tuple[str, Callable[[], object], str]] = [
            (f"Go to {cls.title}", partial(app.show_view, name), f"press {i}")
            for i, (name, cls) in enumerate(VIEWS, start=1)
        ]
        commands += [
            ("Record your balance", app.record_balance, "press b"),
            ("Add an income", partial(app.add_flow, income=True), "a paycheck, a refund…"),
            ("Add an expense", app.add_flow, "a bill, a subscription…"),
            ("Add a debt", partial(app.add_flow, loan=True), "a loan or a card"),
            ("Add a what-if", app.add_what_if, "try something without saving it"),
            ("Settings…", app.open_settings, "everyday spending, the file, backups · press ,"),
            ("Everyday spending…", app.open_settings, "in settings · press ,"),
            ("Export a backup…", app.export_backup, "the whole budget as JSON"),
            ("Import a backup…", app.import_backup, "restore a JSON backup"),
            ("Reload from disk", app.action_reload, "ctrl+r"),
            ("Help", app.action_help, "press ?"),
            ("Quit", app.action_ask_quit, "press q"),
        ]
        for f in app.session.flows():
            paused = "paused · " if not f.active else ""
            show = partial(app.open_flow_card, int(f.id))
            commands.append((f"{f.name} — show", show, paused + describe_flow(f)))
        return commands

    async def discover(self) -> Hits:
        for name, callback, help_ in self._commands():
            if not name.endswith(" — show"):
                yield DiscoveryHit(name, callback, help=help_)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, callback, help_ in self._commands():
            score = max(matcher.match(name), matcher.match(help_) * 0.5)  # names count most
            if score > 0:
                yield Hit(score, matcher.highlight(name), callback, help=help_)


# ── The app ───────────────────────────────────────────────────────────────────


class BdbdApp(App[None]):
    """bdbd: a budget in your terminal."""

    CSS_PATH = "app.tcss"
    TITLE = "bdbd"
    COMMANDS: ClassVar = {Commands}
    NOTIFICATION_TIMEOUT: ClassVar[float] = 6
    BINDINGS: ClassVar = [
        Binding("ctrl+q", "quit", "quit", show=False, priority=True),
        Binding("ctrl+p", "command_palette", "commands", show=False, priority=True),
    ]
    WATCH_SECONDS = 2.0

    session: Session

    def __init__(self, path: Path, *, tidy: bool = True) -> None:
        super().__init__()
        self.path = path
        self.tidy = tidy
        self.toasts: list[str] = []  # every toast shown, newest last (handy in tests)
        self._watch_error: str | None = None
        self.register_theme(THEME)
        self.theme = "bdbd"

    # starting up -------------------------------------------------------------------

    def on_mount(self) -> None:
        if not self.path.exists():
            self.push_screen(WelcomeScreen(self.path))
            return
        try:
            self.session = Session(self.path, tidy=self.tidy)
        except (CashError, sqlite3.Error) as exc:
            self.push_screen(ProblemScreen(sentence(str(exc)), self.path))
            return
        self._start()

    def create_budget(self) -> None:
        """The welcome screen's Create it: make the file and open the (empty) overview."""
        try:
            self.session = Session.create(self.path, tidy=self.tidy)
        except (CashError, sqlite3.Error, OSError) as exc:
            self.switch_screen(ProblemScreen(sentence(str(exc))))
            return
        self.pop_screen()
        self._start()

    def _start(self) -> None:
        self.push_screen(MainScreen())
        self.call_after_refresh(self._opened)
        self.set_interval(self.WATCH_SECONDS, self._watch, name="watch")

    def _opened(self) -> None:
        self.show_view("overview")
        self._toast_tidied()

    def _toast_tidied(self) -> None:
        for action in self.session.pop_tidied():
            self.notify(f"Tidied up: {humanize(action)}", markup=False)

    @property
    def main(self) -> MainScreen | None:
        """The main screen, once the budget is open."""
        for screen in self.screen_stack:
            if isinstance(screen, MainScreen):
                return screen
        return None

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Behind the too-small notice only quitting and help work (the main screen's own
        check doesn't see its `app.` keys, like 1-6 and b)."""
        main = self.main
        if main is not None and self.screen is main and main.has_class("-too-small"):
            return action in ("ask_quit", "help", "quit")
        return True

    # staying current ---------------------------------------------------------------

    def _watch(self) -> None:
        """Every two seconds: a new day, or another process changed the file."""
        if self.main is None:
            return
        try:
            if self.session.check_day():
                self._toast_tidied()
                for screen in self.screen_stack:
                    if isinstance(screen, FormScreen):
                        screen.new_day()  # 'today' in any open form means the new day now
                self.refresh_views()
            elif self.session.check_disk():
                self.notify("Your budget changed on disk, so bdbd reloaded it.")
                self.refresh_views()
        except (CashError, sqlite3.Error) as exc:
            if str(exc) != self._watch_error:  # say it once, not every two seconds
                self.notify(f"Couldn't reload your budget: {exc}", severity="error", markup=False)
            self._watch_error = str(exc)
        else:
            self._watch_error = None

    def action_reload(self) -> None:
        """ctrl+r: read the file again."""
        try:
            self.session.reload()
        except (CashError, sqlite3.Error) as exc:
            self.notify(f"Couldn't reload your budget: {exc}", severity="error", markup=False)
            return
        self.notify("Reloaded your budget from disk.")
        self.refresh_views()

    # the contract with the views ---------------------------------------------------

    def refresh_views(self) -> None:
        """Redraw the visible view (if the session moved on), the banner and the top bar."""
        main = self.main
        if main is None:
            return
        whatif = main.query_one(WhatIfView)
        whatif.keep_searching()  # an unpinned '?' is looked for whichever view is showing
        main.draw_chrome(self.session, whatif.search_state)
        self._sync(main.current_view)
        main.call_after_refresh(main.keep_focus)

    def _sync(self, view: View) -> None:
        if view.drawn != self.session.version:
            view.drawn = self.session.version
            view.refresh_view()

    def show_view(self, name: str) -> None:
        """Show a view: "overview" | "calendar" | "forecast" | "budget" | "debts" | "whatif"."""
        main = self.main
        if main is None:
            return
        main.switcher.current = name
        whatif = main.query_one(WhatIfView)
        main.draw_chrome(self.session, whatif.search_state)
        view = main.current_view
        self._sync(view)
        view.focus_default()

    def action_show_view(self, name: str) -> None:
        self.show_view(name)

    def open_flow_card(self, flow_id: int) -> None:
        """The flow's details with its actions (edit, pause, delete, loan terms)."""
        modals.open_flow_card(self, flow_id)

    def add_flow(
        self, *, income: bool | None = None, loan: bool = False, starts: date | None = None
    ) -> None:
        """Open the flow form for a new income or expense (loan=True: a debt).

        With `income` left out, it's an income while the budget has none yet (the empty
        views say "add your paycheck"), and an expense after that.
        """
        if income is None:
            income = not loan and not any(f.kind == Kind.INCOME for f in self.session.flows())
        flow_form.open_add(self, income=income, loan=loan, starts=starts)

    def edit_flow(self, flow_id: int, *, loan: bool = False) -> None:
        """Open the flow form on an existing flow (loan=True: open at the loan section)."""
        flow_form.open_edit(self, flow_id, loan=loan)

    def delete_flow(self, flow_id: int) -> None:
        """Ask, then delete a flow."""
        try:
            flow = self.session.flow(flow_id)
        except CashError as exc:
            self.notify(sentence(exc.message), severity="error", markup=False)
            return
        extra = " Its loan details and recorded events go too." if flow.debt else ""
        self.confirm(
            f"Delete {flow.name}?",
            f"It disappears from every projection. This can't be undone.{extra}",
            lambda: self.apply(lambda: self.session.remove_flow(flow)),
        )

    def toggle_paused(self, flow_id: int) -> None:
        """Pause a flow, or resume a paused one."""
        try:
            flow: Flow = self.session.flow(flow_id)
        except CashError as exc:
            self.notify(sentence(exc.message), severity="error", markup=False)
            return
        self.apply(lambda: self.session.set_active(flow, not flow.active))

    def record_balance(self) -> None:
        """The balance dialog (b)."""
        self.push_screen(modals.BalanceScreen())

    def action_record_balance(self) -> None:
        self.record_balance()

    def open_settings(self) -> None:
        """Settings (,): everyday spending, the file, backups."""
        modals.open_settings(self)

    def action_open_settings(self) -> None:
        self.open_settings()

    def confirm(
        self,
        title: str,
        message: str,
        on_yes: Callable[[], object],
        *,
        yes: str = "Delete",
        danger: bool = True,
    ) -> None:
        """Ask before doing something; `on_yes` runs only on yes."""

        def answered(result: bool | None) -> None:
            if result:
                on_yes()

        self.push_screen(ConfirmScreen(title, message, yes=yes, danger=danger), answered)

    def prompt(
        self,
        title: str,
        label: str,
        on_submit: Callable[[str], None],
        *,
        value: str = "",
        preview: Callable[[str], tuple[bool, str]] | None = None,
    ) -> None:
        """Ask for one line of text, with a live preview; `on_submit` gets what was typed."""

        def answered(result: str | None) -> None:
            if result is not None:
                on_submit(result)

        self.push_screen(PromptScreen(title, label, value=value, preview=preview), answered)

    def changed(self, message: str) -> None:
        """After a change: one toast saying what happened, and a redraw."""
        self.notify(message, markup=False)
        self.refresh_views()

    def apply(self, change: Callable[[], Done]) -> Done | None:
        """Run a session change: toast and redraw on success, an error toast on CashError."""
        try:
            done = change()
        except CashError as exc:
            self.notify(sentence(exc.message), severity="error", markup=False)
            self.refresh_views()
            return None
        except sqlite3.Error as exc:  # the session turns these into CashError; just in case
            self.notify(f"Couldn't save that: {exc}", severity="error", markup=False)
            self.refresh_views()
            return None
        self.changed(done.message)
        return done

    # more actions ------------------------------------------------------------------

    def action_what_if(self) -> None:
        """w: the what-if on/off; with nothing to try, start a new change."""
        session = self.session
        s = session.scenario
        if not s.changes and s.extra is None:
            self.add_what_if()
            return
        session.set_lens(not s.enabled)
        if not s.enabled:
            self.notify("What-if off · every view shows your budget as it is")
        elif session.lens_on:
            self.notify("What-if on · every view shows the what-if")
        elif not s.any:
            self.notify("What-if on, but every change in it is switched off (6 to switch one on)")
        elif session.scenario_error:
            self.notify("What-if on, but it doesn't fit your budget any more (6 to fix it)")
        else:
            self.notify("What-if on · finding its earliest date first (6 to see it)")
        self.refresh_views()

    def action_ask_quit(self) -> None:
        """q: quit, asking first when there's a what-if that would be lost."""
        s = self.session.scenario if hasattr(self, "session") else None
        if s is None or (not s.changes and s.extra is None):
            self.exit()
            return
        self.confirm(
            "Quit and drop the what-if?",
            "It isn't saved anywhere, so its changes go when bdbd closes.",
            self.exit,
            yes="Quit",
        )

    def add_what_if(self) -> None:
        """Open the What if view and its add-a-change form."""
        self.show_view("whatif")
        main = self.main
        add = getattr(main.current_view, "action_add", None) if main else None
        if callable(add):
            add()

    def action_help(self) -> None:
        main = self.main
        self.push_screen(modals.HelpScreen(main.current_view if main else None))

    def action_command_palette(self) -> None:
        if isinstance(self.screen, MainScreen):
            super().action_command_palette()

    def export_backup(self) -> None:
        """Ask where, then write a JSON backup (like `bdbd export -o`)."""
        default = f"~/bdbd-backup-{self.session.today.isoformat()}.json"

        def preview(text: str) -> tuple[bool, str]:
            path = Path(text.strip()).expanduser()
            if not text.strip():
                return False, "Where to save it, e.g. " + default
            if is_budget(path, self.session.path):
                return False, "That's your budget file; pick another name"
            if path.is_dir():
                return False, "That's a folder; add a file name"
            if not path.parent.is_dir():
                return False, f"There's no folder at {tilde(path.parent)}"
            if path.exists():
                return True, f"Replaces the file at {tilde(path)}"
            return True, f"Saves to {tilde(path)}"

        def export(text: str) -> None:
            path = Path(text.strip()).expanduser()
            try:
                n = self.session.export(path)
            except CashError as exc:
                self.notify(sentence(exc.message), severity="error", markup=False)
                return
            flows = f"{n} flow{'s' if n != 1 else ''}"
            self.notify(f"Saved a backup of {flows} to {tilde(path)}", markup=False)

        self.prompt("Export a backup", "Save to", export, value=default, preview=preview)

    def import_backup(self) -> None:
        """Pick a backup and restore it (the same dialog as in settings)."""
        modals.open_import(self)

    # looks -------------------------------------------------------------------------

    def get_key_display(self, binding: Binding) -> str:
        """Keys the way the footer and help name them: 'ctrl+p', 'enter', '?'."""
        if binding.key_display:
            return binding.key_display
        *mods, key = binding.key.split("+")
        return "+".join([*mods, "enter" if key == "enter" else format_key(key)])

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        self.toasts.append(message)
        super().notify(message, title=title, severity=severity, timeout=timeout, markup=markup)
