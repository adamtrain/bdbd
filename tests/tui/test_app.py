"""The app shell and the Overview, driven the way a person would (Textual's Pilot)."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Input

from bdbd import ask
from bdbd.budget import Budget
from bdbd.core import balance as balances
from bdbd.core import repo
from bdbd.core.models import Kind
from bdbd.tui.app import Banner, BdbdApp, Commands, MainScreen, WelcomeScreen
from bdbd.tui.forms import ConfirmScreen, PromptScreen
from bdbd.tui.modals import BalanceScreen, HelpScreen
from bdbd.tui.scenario import Change
from bdbd.tui.views.overview import OverviewView
from bdbd.tui.widgets import Column, Header, Row, RowList
from bdbd.ui.theme import MINUS, money

from .conftest import SIZE, screen_text, widget_text


def _main(app: BdbdApp) -> MainScreen:
    assert isinstance(app.screen, MainScreen)
    return app.screen


# ── The shell ─────────────────────────────────────────────────────────────────


async def test_number_keys_and_clicks_switch_views(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        main = _main(app)
        assert main.switcher.current == "overview"
        await pilot.press("4")
        assert main.switcher.current == "forecast"
        assert main.query_one("#tab-forecast").has_class("-active")
        assert not main.query_one("#tab-overview").has_class("-active")
        await pilot.click("#tab-debts")
        assert main.switcher.current == "debts"
        await pilot.press("1")
        assert main.switcher.current == "overview"


async def test_the_banner_follows_the_sandbox(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        banner = app.screen.query_one(Banner)
        assert not banner.display
        app.session.add_change(Change("settle", "Car loan", "13000", "2026-11-01"))
        app.refresh_views()
        await pilot.pause()
        assert banner.display
        text = widget_text(banner)
        assert "WHAT IF" in text and "Sell Car loan for $13,000 on Nov 1" in text
        assert "w off" in text
        await pilot.press("w")  # the lens off
        assert not banner.display and not app.session.lens_on
        await pilot.press("w")
        assert banner.display and app.session.lens_on
        app.session.clear_changes()
        app.refresh_views()
        await pilot.pause()
        assert not banner.display


async def test_keys_typed_into_a_dialog_stay_in_it(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        got: list[str] = []
        app.prompt("Name it", "Name", got.append)
        await pilot.pause()
        assert isinstance(app.screen, PromptScreen)
        await pilot.press("q", "1", "b", "a", "w", "comma", "question_mark")
        assert app.screen.query_one(Input).value == "q1baw,?"
        assert isinstance(app.screen, PromptScreen)  # no balance dialog, no help, no quit
        assert app.is_running
        await pilot.press("enter")
        assert got == ["q1baw,?"]
        main = _main(app)
        assert main.switcher.current == "overview"
        assert not app.session.scenario.changes


async def test_keys_in_the_balance_dialog_stay_in_it(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("b")
        assert isinstance(app.screen, BalanceScreen)
        await pilot.press("q", "3", "b")
        assert app.screen.query_one(Input).value == "q3b"
        assert isinstance(app.screen, BalanceScreen)
        await pilot.press("escape")
        assert isinstance(app.screen, MainScreen)


async def test_help_lists_keys_and_reading(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        assert isinstance(app.screen, HelpScreen)
        text = screen_text(app)
        assert "Everywhere" in text and "record your balance" in text
        assert "Reading the numbers" in text and "Spare" in text
        await pilot.press("escape")
        assert isinstance(app.screen, MainScreen)


async def test_the_palette_offers_bdbd_commands_only(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        provider = Commands(app.screen)
        names = [name for name, _, _ in provider._commands()]
        assert "Record your balance" in names and "Quit" in names
        assert "Rent — show" in names
        assert not any("theme" in n.lower() for n in names)
        assert {Commands} == BdbdApp.COMMANDS


async def test_delete_asks_first(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        gym = next(int(f.id) for f in app.session.flows() if f.name == "Gym")
        app.delete_flow(gym)
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("enter")  # focus starts on Keep
        assert "Gym" in {f.name for f in app.session.flows()}
        app.delete_flow(gym)
        await pilot.pause()
        await pilot.press("right", "enter")
        assert "Gym" not in {f.name for f in app.session.flows()}
        assert app.toasts[-1].startswith("Deleted Gym · monthly net")


async def test_too_small(make_app) -> None:
    app = make_app()
    async with app.run_test(size=(70, 20)) as pilot:
        await pilot.pause()
        main = _main(app)
        assert main.has_class("-too-small")
        assert "Make the window a bit bigger" in screen_text(app)
        toasts = list(app.toasts)
        await pilot.press("4", "b", "w", "a", "comma", "down")  # nothing hidden moves
        assert app.screen is main and main.switcher.current == "overview"
        assert main.focused is None and app.toasts == toasts
        await pilot.press("question_mark")  # help still opens
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        await pilot.resize_terminal(*SIZE)
        await pilot.pause()
        assert not main.has_class("-too-small")
        focused = main.focused
        assert focused is not None and main.current_view in focused.ancestors


# ── Opening ───────────────────────────────────────────────────────────────────


async def test_welcome_when_there_is_no_budget(tmp_path: Path) -> None:
    path = tmp_path / "new" / "budget.sqlite"
    app = BdbdApp(path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, WelcomeScreen)
        text = screen_text(app)
        assert "bdbd keeps your budget in one file" in text and "There isn't one at" in text
        await pilot.press("enter")
        await pilot.pause()
        assert path.exists()
        assert isinstance(app.screen, MainScreen)
        assert "Welcome to bdbd." in screen_text(app)


async def test_an_unreadable_file_says_so(tmp_path: Path) -> None:
    path = tmp_path / "budget.sqlite"
    path.write_text("not a database")
    app = BdbdApp(path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert "couldn't open your budget" in screen_text(app)
        await pilot.press("x")
        await pilot.pause()
    assert app.return_code == 1


async def test_empty_budget(make_app, empty_file: Path) -> None:
    app = make_app(empty_file)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        overview = app.screen.query_one(OverviewView)
        assert overview.has_class("-empty")
        text = screen_text(app)
        assert "Welcome to bdbd." in text and "add your paycheck" in text


# ── The Overview ──────────────────────────────────────────────────────────────


async def test_overview_shows_the_numbers_bdbd_overview_gives(make_app, budget_file) -> None:
    budget = Budget.open(budget_file)
    try:
        p = ask.overview(budget)
        upcoming = ask.coming_up(p, budget.today)
    finally:
        budget.close()
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        headline = widget_text(app.screen.query_one(OverviewView).query_one("#headline"))
        assert money(p.start.cents) in headline  # $4,070.00
        assert p.spare is not None
        assert money(int(p.spare["spare_balance"].replace(".", ""))) in headline  # $1,595.00
        assert p.low_point is not None
        assert f"Lowest ahead         {money(p.low_point[1])}" in headline
        assert f"net {money(p.monthly_net, sign=True)}/mo" in headline
        rows = widget_text(app.screen.query_one("#upcoming"))
        for e in upcoming:
            assert e.name in rows and money(e.delta_cents, sign=True).lstrip("+") in rows
        debts = widget_text(app.screen.query_one("#debts-panel"))
        for d in p.debts:
            assert d.name in debts and money(d.balance) in debts


async def test_overview_without_a_balance(make_app, tmp_path: Path) -> None:
    from .conftest import build_budget

    path = build_budget(tmp_path / "nb.sqlite", with_balance=False)
    app = make_app(path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        overview = app.screen.query_one(OverviewView)
        assert overview.has_class("-no-balance")
        headline = widget_text(app.screen.query_one(OverviewView).query_one("#headline"))
        assert "unknown" in headline and "press b to tell bdbd" in headline


async def test_overview_shows_what_if_deltas_and_marks(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.session.add_change(Change("add_expense", "Flight", "650", "2026-09-29"))
        app.refresh_views()
        await pilot.pause()
        headline = widget_text(app.screen.query_one(OverviewView).query_one("#headline"))
        assert f"{MINUS}$650.00 vs now" in headline  # spare and lowest both drop
        assert "↳ Flight" in widget_text(app.screen.query_one("#upcoming"))


async def test_enter_on_coming_up_opens_the_flow_card(make_app) -> None:
    app = make_app()
    opened: list[int] = []
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.open_flow_card = opened.append  # type: ignore[method-assign]
        rows = app.screen.query_one("#upcoming", RowList)
        assert rows.has_focus
        await pilot.press("down", "enter")  # past the blank line between weeks
        rent = next(int(f.id) for f in app.session.flows() if f.name == "Rent")
        assert opened == [rent]


# ── Changing things ───────────────────────────────────────────────────────────


async def test_recording_a_balance(make_app, budget_file: Path) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("b")
        await pilot.press(*"3920")
        await pilot.press("enter")  # to "On"
        await pilot.press("enter")  # the last field saves
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
        assert app.toasts[-1] == "Recorded $3,920.00 · $150.00 behind what the budget expected"
        assert "$3,920.00" in widget_text(app.screen.query_one(OverviewView).query_one("#headline"))
    conn = sqlite3.connect(budget_file)
    conn.row_factory = sqlite3.Row
    try:
        latest = balances.latest(conn)
    finally:
        conn.close()
    assert latest is not None
    assert (latest.as_of, latest.amount_cents) == (date(2026, 9, 24), 392000)


async def test_a_bad_amount_is_refused(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("b", *"abc", "ctrl+s")
        assert isinstance(app.screen, BalanceScreen)
        assert "isn't an amount of money" in screen_text(app)


async def test_a_change_on_disk_reloads(make_app, budget_file: Path) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        conn = sqlite3.connect(budget_file)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            repo.add_flow(
                conn,
                name="Birthday gift",
                kind=Kind.EXPENSE,
                amount_cents=8000,
                rrule=None,
                dtstart=date(2026, 9, 28),
            )
            conn.commit()
        finally:
            conn.close()
        app._watch()  # what the 2-second timer does
        await pilot.pause()
        assert app.toasts[-1] == "Your budget changed on disk, so bdbd reloaded it."
        assert "Birthday gift" in widget_text(app.screen.query_one("#upcoming"))


async def test_tidy_up_toasts_on_open(make_app, monkeypatch) -> None:
    monkeypatch.setenv("BDBD_TODAY", "2026-11-02")  # October's one-offs are over
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        tidied = [t for t in app.toasts if t.startswith("Tidied up: ")]
        assert any("Tax refund" in t and "Oct 20, 2026" in t for t in tidied)
        assert not any("2026-10-20" in t for t in tidied)  # dates the way people write them


async def test_ctrl_r_reloads(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        v = app.session.version
        await pilot.press("ctrl+r")
        assert app.session.version > v
        assert app.toasts[-1] == "Reloaded your budget from disk."


async def test_ctrl_q_quits_even_inside_a_dialog(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("b")
        assert isinstance(app.screen, BalanceScreen)
        await pilot.press("ctrl+q")
        await pilot.pause()
    assert not app.is_running


# ── The shared list ───────────────────────────────────────────────────────────


class ListApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.selected: list[object] = []

    def compose(self) -> ComposeResult:
        yield RowList(Column(), Column(flex=True), Column(align="right"), empty="Nothing here.")

    def on_row_list_selected(self, event: RowList.Selected) -> None:
        self.selected.append(event.key)


async def test_row_list_aligns_skips_headers_and_keeps_its_place() -> None:
    app = ListApp()
    async with app.run_test(size=(40, 8)) as pilot:
        rows = app.query_one(RowList)
        assert "Nothing here." in widget_text(rows)
        rows.set_rows(
            [
                Header("Income", "+$1.00/mo"),
                Row("Oct 2", "Paycheck", "+$1.00", key=1),
                None,
                Header("Expenses"),
                Row("Oct 1", "Rent", "-$2,150.00", key=2),
                Row("Oct 3", "Gym", "-$39.00", key=3),
            ]
        )
        rows.focus()
        await pilot.pause()
        assert rows.key == 1
        lines = widget_text(rows).splitlines()
        assert lines[0].startswith(" Income") and lines[0].endswith("+$1.00/mo")
        money = [line for line in lines if line.rstrip().endswith(("0", "00"))]
        assert len({len(line.rstrip()) for line in money}) == 1  # right-aligned money
        await pilot.press("down")  # past the blank line and the header
        assert rows.key == 2
        await pilot.press("down", "down")
        assert rows.key == 3  # no wrapping at the end
        await pilot.press("up", "up", "up")
        assert rows.key == 1
        await pilot.press("down", "enter")
        assert app.selected == [2]
        rows.set_rows([Row("Oct 1", "Rent", "-$2,150.00", key=2), Row("x", "New", "1", key=9)])
        assert rows.key == 2  # the cursor stays on the same thing
        assert rows.select_key(9) and rows.current is not None
