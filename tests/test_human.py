"""What people see: every screen renders from the sample household and says the right things."""

from __future__ import annotations

import re

import pytest

from bdbd import cli
from bdbd.core import db

from . import sample


@pytest.fixture
def home(db_path, human, monkeypatch):
    monkeypatch.setenv("BDBD_TODAY", sample.TODAY.isoformat())
    conn = db.connect(db_path, create=True)
    sample.build(conn)
    conn.close()
    return human


def ok(result: tuple[int, str]) -> str:
    code, text = result
    assert code == 0, text
    return text


def test_dashboard(home):
    text = ok(home())
    assert "Balance now" in text and "$4,070.00" in text  # carried from Sep 22
    assert "Spare until payday" in text and "Paycheck" in text and "Fri Oct 2" in text
    assert "Lowest ahead" in text
    assert "Coming up" in text and "Rent" in text
    assert "Debts" in text and "debt-free" in text
    assert "Try bdbd upcoming" in text


def test_dashboard_without_a_balance(db_path, human, monkeypatch):
    monkeypatch.setenv("BDBD_TODAY", sample.TODAY.isoformat())
    conn = db.connect(db_path, create=True)
    sample.build(conn, with_balance=False)
    conn.close()
    text = ok(human())
    assert "unknown" in text and "bdbd balance AMOUNT" in text


def test_ls_and_show(home):
    text = ok(home("ls"))
    assert "Income" in text and "Expenses" in text
    assert "Every 2 weeks on Fri" in text and "Monthly on the 1st →Mon" in text
    assert re.search(r"Car loan ◆\s+\u2212\$412\.37", text)
    text = ok(home("show", "car loan"))
    assert "EXPENSE" in text and "DEBT" in text and "Paid off" in text
    assert "simple interest" in text


def test_unknown_flow_suggests(home):
    code, text = home("show", "Rnet")
    assert code == 1 and "no flow called 'Rnet'" in text and "Did you mean" in text


def test_upcoming_and_calendar(home):
    text = ok(home("upcoming", "--days", "10"))
    assert "Credit card" in text and "tomorrow" in text and "lowest" in text
    text = ok(home("cal", "oct"))
    assert "October 2026" in text and "Paycheck" in text and "+2,650" in text
    assert "lowest $1,595.00 on Oct 1" in text


def test_project(home):
    text = ok(home("project", "--months", "6"))
    assert "PROJECTION" in text and "Starts at" in text and "$4,070.00" in text
    assert "Month by month" in text and "Spare then" in text


def test_debts_and_schedule(home):
    text = ok(home("debts"))
    assert "Owed today" in text and "Debt-free" in text and "Credit card" in text
    text = ok(home("debt", "schedule", "Credit card", "-n", "3"))
    assert "SCHEDULE" in text and "more payments" in text


def test_spend_and_summary(home):
    text = ok(home("spend", "car", "--until", "dec", "31"))  # an unquoted date
    assert "$1,621.11 going out on car" in text
    text = ok(home("summary"))
    assert "Left over" in text and "Bills by tag" in text and "One-offs" in text


def test_compare_earliest_plan(home):
    text = ok(home("compare", "--settle", "Car loan:13000@nov 1", "--stop-tag", "car@nov 1"))
    assert "Catches up on" in text
    text = ok(home("earliest", "--floor", "1000", "--add-expense", "Flight:650@?"))
    assert "EARLIEST" in text and "Fri Oct 2" in text
    text = ok(home("plan", "--extra", "300"))
    assert "Debt-free Aug 2029" in text and "Interest saved" in text


def test_changes_round_trip(home):
    text = ok(home("add", "Netflix", "15.49", "monthly", "on", "the", "12th", "-t", "fun"))
    assert "Added Netflix" in text and "Monthly net" in text
    text = ok(home("edit", "netflix", "--amount", "17.99", "--when", "monthly on the", "15th"))
    assert "$15.49 → $17.99" in text and "Monthly on the 15th" in text
    assert "Paused" in ok(home("pause", "Netflix"))
    assert "[bold]" not in ok(home("resume", "Netflix"))
    assert "Removed" in ok(home("rm", "Netflix", "-y"))


def test_balance_screens(home):
    text = ok(home("balance"))
    assert "Recorded" in text and "$4,120.00" in text and "estimated" in text
    text = ok(home("balance", "3980"))
    assert "Recorded $3,980.00 for today" in text and "behind it" in text


def test_debt_events(home):
    text = ok(home("debt", "extra", "Car loan", "1000", "--on", "oct", "20"))
    assert "extra payment of $1,000.00" in text and "Paid off" in text
    text = ok(home("debt", "adjust", "Car loan", "-250", "--on", "oct 21"))
    assert "\u2212$250.00" in text


def test_data_commands(home, tmp_path):
    assert "weekly_spend" in ok(home("config"))
    assert "$200.00 a week" in ok(home("config", "weekly_spend", "200"))
    backup = tmp_path / "backup.json"
    assert "Wrote 15 flows" in ok(home("export", "-o", str(backup)))
    assert "Imported 15 flows" in ok(home("import", str(backup), "--replace"))
    assert "Nothing from past months" in ok(home("tidy"))
    assert "3 rows" in ok(home("sql", "select name from flow limit 3"))
    code, text = home("sql", "delete from flow")
    assert code == 1 and "read-only" in text
    assert "bdbd guide" in ok(home("guide"))


def test_help_lists_every_panel(human):
    text = ok(human("--help"))
    for panel in ("Look", "Change", "Debts", "Ask what if", "Data", "Examples"):
        assert panel in text


def test_interactive_add_asks_for_whats_missing(home, monkeypatch):
    answers = iter(["Gym bag", "45", "out", "yearly on mar 3", "fitness", "n"])
    monkeypatch.setattr(cli, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    text = ok(home("add"))
    assert "Yearly on Mar 3" in text and "Added Gym bag" in text


def test_the_guide_covers_every_command():
    from importlib import resources

    guide = (resources.files("bdbd") / "guide.md").read_text()
    names = {c.name for c in cli.app.registered_commands if c.name}
    names |= {g.name for g in cli.app.registered_groups if g.name}
    assert names, "no commands found"
    for name in names:
        assert f"bdbd {name}" in guide, f"guide.md doesn't mention `bdbd {name}`"
