"""The Budget view (4): Flows and Tags, driven the way a person would.

Every number is checked against the JSON command that answers the same question
(`bdbd ls --all`, `bdbd summary`, `bdbd overview`, `bdbd tags`, `bdbd spend`, `bdbd show`).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.content import Content
from textual.widgets import Input

from bdbd import cli
from bdbd.core import db, repo
from bdbd.core.models import Flow
from bdbd.tui.app import BdbdApp, MainScreen
from bdbd.tui.forms import ConfirmScreen, PromptScreen
from bdbd.tui.modals import HelpScreen
from bdbd.tui.scenario import Change
from bdbd.tui.views.budget import BudgetView, FilterInput
from bdbd.tui.widgets import Header, Row, RowList
from bdbd.ui.theme import MINUS, money
from bdbd.words import fmt_month

from .conftest import SIZE, build_budget, screen_text, widget_text

# ── Helpers ───────────────────────────────────────────────────────────────────


def run_json(path: Path, capsys: Any, *argv: str) -> dict:
    """A JSON command against `path`, its data block."""
    try:
        cli.main(["--db", str(path), "--no-tidy", *argv])
    except SystemExit as exc:
        assert not exc.code
    env = json.loads(capsys.readouterr().out)
    assert env["ok"], env
    return env["data"]


def cents(text: str) -> int:
    return int(Decimal(text) * 100)


def plain(cell: object) -> str:
    return cell.plain if isinstance(cell, (Text, Content)) else str(cell)


def stored(path: Path) -> list[Flow]:
    conn = db.connect(path)
    try:
        return repo.list_flows(conn, include_inactive=True)
    finally:
        conn.close()


def stored_tags(path: Path) -> set[str]:
    conn = db.connect(path)
    try:
        return {t.name for t in repo.list_tags(conn)}
    finally:
        conn.close()


async def open_budget(app: BdbdApp, pilot: Any) -> BudgetView:
    await pilot.pause()
    await pilot.press("4")
    await pilot.pause()
    return app.screen.query_one(BudgetView)


def flow_rows(view: BudgetView) -> RowList:
    return view.query_one("#flow-list", RowList)


def tag_rows(view: BudgetView) -> RowList:
    return view.query_one("#tag-list", RowList)


def names(rows: RowList) -> list[str]:
    return [plain(i.cells[0]).removesuffix(" ◆") for i in rows.items if isinstance(i, Row)]


def flow_id(app: BdbdApp, name: str) -> int:
    return next(int(f.id) for f in app.session.flows() if f.name == name)


async def select(view: BudgetView, pilot: Any, name: str) -> None:
    """Move the cursor down to the row called `name`, the way a person would."""
    rows = view.query_one("#tag-list" if view.mode == "tags" else "#flow-list", RowList)
    rows.focus()
    for _ in range(len(rows.items)):
        current = rows.current
        if current is not None and plain(current.cells[0]).removesuffix(" ◆") == name:
            return
        await pilot.press("down")
    raise AssertionError(f"no row called {name}")


# ── Flows ─────────────────────────────────────────────────────────────────────


async def test_flows_are_grouped_by_next_date_with_bdbd_ls_numbers(
    make_app, budget_file, capsys
) -> None:
    ls = {f["name"]: f for f in run_json(budget_file, capsys, "ls", "--all")["flows"]}
    net = run_json(budget_file, capsys, "summary")["net"]
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_budget(app, pilot)
        rows = flow_rows(view)
        assert rows.has_focus
        headers = [i for i in rows.items if isinstance(i, Header)]
        assert [plain(h.title) for h in headers] == ["Income  2 flows", "Expenses  13 flows"]
        # the group totals are `bdbd summary`'s monthly in and bills
        income, bills = cents(net["income_monthly"]), cents(net["expense_monthly"])
        assert plain(headers[0].right) == f"a month  {money(income, sign=True)}"
        assert plain(headers[1].right) == f"a month  {money(-bills)}"
        listed = names(rows)
        assert listed[:2] == ["Paycheck", "Tax refund"]
        for group in (listed[:2], listed[2:]):  # each group soonest first, like the old bdbd ls
            assert group == sorted(group, key=lambda n: (ls[n]["next"], n))
        assert set(listed) == set(ls)
        for row in (i for i in rows.items if isinstance(i, Row)):
            f = ls[plain(row.cells[0]).removesuffix(" ◆")]
            sign = 1 if f["kind"] == "income" else -1
            assert row.key == f["id"]
            assert plain(row.cells[0]).endswith(" ◆") == (f["debt"] is not None)
            assert plain(row.cells[1]) == money(sign * cents(f["amount"]), sign=True)
            assert plain(row.cells[2]).startswith(f["schedule"])
            monthly = cents(f["monthly"])
            per_month = money(sign * monthly, sign=True) if monthly else "one-off"
            assert plain(row.cells[4]) == per_month
        text = widget_text(rows)
        assert "Renters insurance" in text  # names fit beside the other columns at 120x36
        assert "Monthly on the 1st →Mon" in text


async def test_the_headline_is_bdbd_overviews_month(make_app, budget_file, capsys) -> None:
    monthly = run_json(budget_file, capsys, "overview")["monthly"]
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_budget(app, pilot)
        text = widget_text(view.query_one("#budget-totals"))
        assert f"{money(cents(monthly['income']), sign=True)} in" in text
        assert f"{money(-cents(monthly['bills']))} bills" in text
        assert f"{money(-cents(monthly['everyday']))} everyday" in text
        assert f"{money(cents(monthly['net']), sign=True)} left over" in text
        assert "Everyday spending $175/week · s to change" in text


async def test_the_card_follows_the_cursor(make_app, budget_file, capsys) -> None:
    show = run_json(budget_file, capsys, "show", "Car loan")
    by_flow = run_json(budget_file, capsys, "summary", "--all")["by_flow"]
    annual = next(r["annual"] for r in by_flow if r["name"] == "Car loan")
    debts = run_json(budget_file, capsys, "debts")["debts"]
    interest = next(cents(d["interest_remaining"]) for d in debts if d["name"] == "Car loan")
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_budget(app, pilot)
        assert "Paycheck" in widget_text(view.query_one("#flow-side"))
        await select(view, pilot, "Car loan")
        card = widget_text(view.query_one("#flow-side"))
        outlook = show["debt_outlook"]
        assert "Car loan ◆" in card and "Money out · a debt ◆" in card
        assert cents(show["monthly"]) == 41237  # a monthly flow: its month is its amount,
        assert "Per month" not in card and money(-41237) in card  # said once
        assert money(-cents(annual)) in card  # `bdbd summary --all`'s annual, not 12 x monthly
        owed = cents(outlook["balance_at_as_of"])
        assert money(owed) in card  # owed today
        assert fmt_month(date.fromisoformat(outlook["payoff_date"])) in card
        assert money(interest) in card  # interest to go, as `bdbd debts` says
        assert money(owed + interest) in card  # in all, still to pay
        assert str(outlook["payments_remaining"]) in card
        assert "simple interest," in card and "accrued daily" in card  # wrapped, not cut
        assert "weekend dates move to" in card and "Monday" in card


async def test_space_pauses_and_resumes_through_the_ui(make_app, budget_file) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_budget(app, pilot)
        rent = flow_id(app, "Rent")
        await select(view, pilot, "Rent")
        await pilot.press("space")
        await pilot.pause()
        assert not next(f for f in stored(budget_file) if f.id == rent).active
        assert app.toasts[-1] == "Paused Rent · monthly net +$1,621.04 → +$3,771.04"
        rows = flow_rows(view)
        assert rows.key == rent  # the cursor followed Rent into Paused
        assert [plain(h.title) for h in rows.items if isinstance(h, Header)][-1] == "Paused  1 flow"
        assert f"{MINUS}$1,228.09 bills" in widget_text(view.query_one("#budget-totals"))
        assert "paused" in widget_text(view.query_one("#flow-side"))
        await pilot.press("space")
        await pilot.pause()
        assert next(f for f in stored(budget_file) if f.id == rent).active
        assert rows.key == rent


async def test_x_deletes_after_asking(make_app, budget_file) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_budget(app, pilot)
        await select(view, pilot, "Gym")
        await pilot.press("x")
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("enter")  # Keep
        assert "Gym" in {f.name for f in stored(budget_file)}
        await pilot.press("delete", "right", "enter")
        await pilot.pause()
        assert "Gym" not in {f.name for f in stored(budget_file)}
        assert "Gym" not in names(flow_rows(view))
        assert flow_rows(view).current is not None  # the cursor lands on a neighbour


async def test_the_cursor_stays_on_its_flow_when_the_budget_changes(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_budget(app, pilot)
        await select(view, pilot, "Streaming")
        app.apply(
            lambda: app.session.remove_flow(app.session.flow(flow_id(app, "Rent")))
        )  # a row above it goes
        await pilot.pause()
        current = flow_rows(view).current
        assert current is not None and plain(current.cells[0]) == "Streaming"


async def test_e_enter_and_d_open_the_flow_form(make_app) -> None:
    app = make_app()
    opened: list[tuple[int, bool]] = []
    async with app.run_test(size=SIZE) as pilot:
        app.edit_flow = lambda fid, *, loan=False: opened.append((fid, loan))  # type: ignore[method-assign]
        view = await open_budget(app, pilot)
        await select(view, pilot, "Rent")
        await pilot.press("e", "enter", "d")
        rent = flow_id(app, "Rent")
        assert opened == [(rent, False), (rent, False), (rent, True)]
        await pilot.press("home", "d")  # Paycheck is money in: it can't be a loan
        assert len(opened) == 3
        assert app.toasts[-1] == "Paycheck is money coming in, so it can't be a loan."


async def test_a_click_picks_a_row_and_a_double_click_opens_it(make_app) -> None:
    app = make_app()
    opened: list[int] = []
    async with app.run_test(size=SIZE) as pilot:
        app.edit_flow = lambda fid, *, loan=False: opened.append(fid)  # type: ignore[method-assign]
        view = await open_budget(app, pilot)
        rows = flow_rows(view)
        await pilot.click(rows, offset=(4, 2))  # Income's header, Paycheck, then Tax refund
        await pilot.pause()
        refund = flow_id(app, "Tax refund")
        assert rows.key == refund and opened == []
        assert "Tax refund" in widget_text(view.query_one("#flow-side"))
        await pilot.click(rows, offset=(4, 2), times=2)
        assert opened == [refund]


async def test_a_adds_a_flow_and_the_paycheck_first(make_app, empty_file) -> None:
    app = make_app(empty_file)
    added: list[bool] = []
    async with app.run_test(size=SIZE) as pilot:
        app.add_flow = lambda *, income=False, loan=False, starts=None: added.append(income)  # type: ignore[method-assign]
        view = await open_budget(app, pilot)
        assert view.has_class("-empty")
        text = screen_text(app)
        assert "No flows yet." in text and "a add your paycheck" in text
        await pilot.press("a")
        assert added == [True]


# ── The filter ────────────────────────────────────────────────────────────────


async def test_the_filter_keeps_groups_and_esc_clears_it(make_app, budget_file, capsys) -> None:
    car = next(t for t in run_json(budget_file, capsys, "tags")["tags"] if t["name"] == "car")
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_budget(app, pilot)
        await pilot.press("slash", *"#car")
        box = view.query_one("#flow-filter", FilterInput)
        assert box.has_focus and box.display
        rows = flow_rows(view)
        assert sorted(names(rows)) == sorted(car["flow_names"])
        header = next(i for i in rows.items if isinstance(i, Header))
        assert plain(header.title) == "Expenses  3 of 13"
        assert plain(header.right).endswith(money(-cents(car["expense_monthly"])))
        # typing never fires the view's keys
        await pilot.press("backspace", "backspace", "backspace", "backspace", *"x e")
        assert box.value == "x e" and isinstance(app.screen, MainScreen)
        await pilot.press("escape")
        assert not box.display and rows.has_focus
        assert len(names(rows)) == 15
        await pilot.press("slash", *"insur", "enter")  # names and tags, by part
        assert rows.has_focus and box.display
        assert names(rows) == ["Car insurance", "Renters insurance"]
        await pilot.press("escape")
        assert len(names(rows)) == 15 and not box.display


async def test_a_filter_matching_nothing_says_so(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_budget(app, pilot)
        await pilot.press("slash", *"zzz", "enter")
        assert names(flow_rows(view)) == []
        assert "Nothing matches “zzz”. Press esc to clear the filter." in widget_text(
            flow_rows(view)
        )
        assert "Nothing selected." in widget_text(view.query_one("#flow-side"))


# ── Everyday spending ─────────────────────────────────────────────────────────


async def test_s_changes_everyday_spending(make_app, budget_file, capsys) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_budget(app, pilot)
        await pilot.press("s")
        assert isinstance(app.screen, PromptScreen)
        assert app.screen.query_one(Input).value == "175.00"
        await pilot.press("end", *["backspace"] * 6, *"200")
        preview = screen_text(app)
        assert "$869.64 a month · +$1,512.34 left over" in preview
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
        assert "$200/week" in widget_text(view.query_one("#budget-totals"))
    assert run_json(budget_file, capsys, "summary")["weekly_spend"] == "200.00"
    monthly = run_json(budget_file, capsys, "overview")["monthly"]
    assert (monthly["everyday"], monthly["net"]) == ("869.64", "1512.34")  # what the preview said


# ── Tags ──────────────────────────────────────────────────────────────────────


async def test_tags_match_bdbd_tags_and_bdbd_spend(make_app, budget_file, capsys) -> None:
    tags = {t["name"]: t for t in run_json(budget_file, capsys, "tags")["tags"]}
    spend = {
        m: run_json(budget_file, capsys, "spend", "car", "--months", str(m))["total"]
        for m in (1, 3, 12)
    }
    earns = run_json(budget_file, capsys, "spend", "job", "--income", "--months", "3")["total"]
    by_tag = {r["tag"]: r for r in run_json(budget_file, capsys, "summary")["by_tag"]}
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_budget(app, pilot)
        await pilot.press("v")
        assert view.mode == "tags" and tag_rows(view).has_focus
        rows = [i for i in tag_rows(view).items if isinstance(i, Row)]
        assert [r.key for r in rows][:3] == ["housing", "debt", "car"]  # the biggest bills first
        assert {r.key for r in rows} == set(tags)
        for r in rows:
            t = tags[str(r.key)]
            if cents(t["expense_monthly"]):
                assert plain(r.cells[1]) == money(-cents(t["expense_monthly"]))
            elif cents(t["income_monthly"]):
                assert plain(r.cells[1]) == money(cents(t["income_monthly"]), sign=True)
            else:
                assert plain(r.cells[1]) == "one-off"
            assert plain(r.cells[4]) == ", ".join(t["flow_names"])
        await select(view, pilot, "car")
        card = widget_text(view.query_one("#tag-side"))
        assert money(-cents(tags["car"]["expense_monthly"])) in card  # per month
        assert money(-cents(by_tag["car"]["expense_annual"])) in card  # per year
        for m, label in ((1, "Next month"), (3, "Next 3 months"), (12, "Next 12 months")):
            line = next(li for li in card.splitlines() if label in li)
            assert line.strip("│ ").endswith(money(-cents(spend[m])))
        await select(view, pilot, "job")
        card = widget_text(view.query_one("#tag-side"))
        assert "What it brings in" in card
        line = next(li for li in card.splitlines() if "Next 3 months" in li)
        assert line.strip("│ ").endswith(money(cents(earns), sign=True))
        await pilot.press("v")
        assert view.mode == "flows" and flow_rows(view).has_focus


async def test_r_renames_a_tag(make_app, budget_file) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_budget(app, pilot)
        await pilot.press("v")
        await select(view, pilot, "car")
        await pilot.press("r")
        assert isinstance(app.screen, PromptScreen)
        await pilot.press("end", "backspace", "backspace", "backspace", *"debt")
        assert "There's a tag called debt already" in screen_text(app)
        await pilot.press("enter")  # refused while the preview says it won't work
        assert isinstance(app.screen, PromptScreen)
        await pilot.press("backspace", "backspace", "backspace", "backspace", *"Vehicle")
        assert "car becomes vehicle on 3 flows" in screen_text(app)
        await pilot.press("enter")
        await pilot.pause()
        assert tag_rows(view).key == "vehicle"
        assert app.toasts[-1] == "Renamed the tag car to vehicle"
    tags = stored_tags(budget_file)
    assert "vehicle" in tags and "car" not in tags


async def test_x_takes_a_tag_off_every_flow(make_app, budget_file) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_budget(app, pilot)
        await pilot.press("v")
        await select(view, pilot, "insurance")
        await pilot.press("x")
        assert isinstance(app.screen, ConfirmScreen)
        assert "Car insurance, Renters insurance" in screen_text(app)
        await pilot.press("right", "enter")
        await pilot.pause()
        assert "insurance" not in {r.key for r in tag_rows(view).items if isinstance(r, Row)}
    assert "insurance" not in stored_tags(budget_file)
    assert {"Car insurance", "Renters insurance"} <= {f.name for f in stored(budget_file)}


async def test_enter_on_a_tag_shows_its_flows(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_budget(app, pilot)
        await pilot.press("v")
        await select(view, pilot, "utilities")
        await pilot.press("enter")
        assert view.mode == "flows" and flow_rows(view).has_focus
        assert view.query_one("#flow-filter", FilterInput).value == "#utilities"
        assert names(flow_rows(view)) == ["Electric", "Internet", "Phone"]


async def test_no_tags_yet(make_app, tmp_path) -> None:
    path = build_budget(tmp_path / "untagged.sqlite")
    conn = db.connect(path)
    for t in repo.list_tags(conn):
        repo.remove_tag(conn, t.name)
    conn.commit()
    conn.close()
    app = make_app(path)
    async with app.run_test(size=SIZE) as pilot:
        view = await open_budget(app, pilot)
        await pilot.press("v")
        assert view.has_class("-no-tags")
        assert "No tags yet." in screen_text(app)
        await pilot.press("r", "x")  # nothing to rename or remove
        assert isinstance(app.screen, MainScreen)
        await pilot.press("v")  # the empty state keeps the view's keys working
        assert view.mode == "flows"


# ── Around the view ───────────────────────────────────────────────────────────


async def test_help_lists_the_views_keys(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await open_budget(app, pilot)
        await pilot.press("question_mark")
        assert isinstance(app.screen, HelpScreen)
        text = screen_text(app)
        for words in ("pause or resume", "loan terms", "everyday spending"):
            assert words in text
        assert "rename tag" not in text  # only the tags half has tags to rename
        await pilot.press("escape", "v", "question_mark")
        assert "rename tag" in screen_text(app)


async def test_with_a_what_if_the_headline_says_what_it_would_leave(make_app) -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        view = await open_budget(app, pilot)
        app.session.add_change(Change("set_amount", "Rent", "2300"))
        app.refresh_views()
        await pilot.pause()
        text = widget_text(view.query_one("#budget-totals"))
        assert "+$1,621.04 left over" in text  # the budget as it is
        assert "↳ with the what-if: +$1,471.04 left over" in text
        assert f"{MINUS}$2,150.00" in widget_text(flow_rows(view))  # the stored amount


async def test_80_by_24(make_app) -> None:
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        view = await open_budget(app, pilot)
        assert not view.query_one("#flow-side").display  # no room for the card
        text = widget_text(flow_rows(view))
        assert "Paycheck" in text and "+$5,760.07" in text
        assert "left" in widget_text(view.query_one("#budget-totals"))
