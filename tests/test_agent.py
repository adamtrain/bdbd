"""The JSON commands, end to end: the real command, the envelope, and hand-checked numbers.

The reference budget (see conftest) is entered in plain English, so these also prove that the
English schedules store the rules the numbers were checked against.
"""

from __future__ import annotations

import json
import sys
import types
from decimal import Decimal
from importlib import resources
from pathlib import Path

from bdbd import __version__, cli

BASE = ["project", "--balance", "3000", "--as-of", "2026-09-16", "--months", "6"]


def test_reference_budget_stores_the_expected_rules(reference):
    flows = {f["name"]: f for f in reference("ls")["data"]["flows"]}
    assert flows["Salary"]["rrule"] == "FREQ=WEEKLY;INTERVAL=2"
    assert flows["Salary"]["dtstart"] == "2026-09-18"
    assert flows["Rent"]["rrule"] == "FREQ=MONTHLY;BYMONTHDAY=1"
    assert flows["Car loan"]["schedule"] == "Monthly on the 1st"
    assert flows["Car loan"]["tags"] == ["car", "loan"]
    assert flows["Car loan"]["debt"]["annual_rate"] == "0.06"
    assert flows["Salary"]["next"] == "2026-09-18"


def test_envelope_shape(reference):
    env = reference("project", "--months", "1")
    assert set(env) == {"ok", "command", "data", "warnings"}
    assert env["command"] == "project"
    assert env["warnings"] == ["no balance recorded: this projection starts from 0"]


def test_project_six_months(reference):
    d = reference(*BASE, "--verbose")["data"]
    assert d["until"] == "2027-03-16"
    assert d["ending_balance"] == "14846.70"
    assert d["min_balance"] == {"date": "2026-10-01", "balance": "2500.00"}
    assert d["totals"]["income"] == "32500.00"
    assert d["totals"]["expense"] == "20653.30"
    assert [r["date"] for r in d["series"]] == [
        "2026-09-16", "2026-09-30", "2026-10-31", "2026-11-30", "2026-12-31", "2027-01-31",
        "2027-02-28", "2027-03-16",
    ]  # fmt: skip
    salary = next(u for u in d["flows_used"] if u["name"] == "Salary")
    assert salary["occurrences"] == 13 and salary["total"] == "32500.00"
    loan = d["debts"][0]
    assert loan["balance_at_as_of"] == "20000.00" and loan["balance_at_until"] == "18552.30"
    assert d["opening"]["source"] == "given" and d["opening"]["balance"] == "3000.00"


def test_spare_balance_on_payday(reference):
    d = reference("project", "--balance", "3000", "--until", "2026-11-13")["data"]
    assert d["ending_balance"] == "8993.34"
    payday = {"date": "2026-11-27", "name": "Salary", "amount": "2500.00"}
    assert d["spare"]["next_payday"] == payday
    assert d["spare"]["committed_before_payday"] == [
        {"date": "2026-11-15", "name": "Car insurance", "amount": "120.00"}
    ]
    assert d["spare_balance"] == "8873.34"


def test_weekly_spend_in_project_and_spare(reference):
    d = reference("project", "--balance", "3000", "--until", "2026-11-13", "--weekly-spend", "70")
    d = d["data"]
    assert d["weekly_spend"] == "70.00" and d["lifestyle_total"] == "580.00"
    assert d["ending_balance"] == "8413.34"
    assert d["spare"]["committed_total"] == "250.00"
    assert d["spare_balance"] == "8163.34"


def test_stored_weekly_spend_is_the_default(reference):
    reference("config", "weekly_spend", "70")
    d = reference("project", "--balance", "3000", "--until", "2026-11-13")["data"]
    assert d["weekly_spend"] == "70.00" and d["ending_balance"] == "8413.34"


def test_settle_pays_the_shortfall_from_cash(reference):
    d = reference(*BASE, "--settle", "Car loan:15000@2026-11-15", "--ledger")["data"]
    row = next(e for e in d["ledger"] if e["kind"] == "settle")
    assert row["date"] == "2026-11-15" and row["delta"] == "-4713.34"
    assert d["ending_balance"] == "11680.00"
    assert d["total_debt_at_until"] == "0.00"


def test_settle_surplus_and_stop_tag(reference):
    d = reference(*BASE, "--settle", "Car loan:25000@2026-11-15")["data"]
    assert d["ending_balance"] == "21680.00"
    env = reference(*BASE, "--stop-tag", "car@2026-11-15", "--settle", "Car loan:15000@2026-11-15")
    assert env["warnings"] == [] and env["data"]["ending_balance"] == "12160.00"
    env = reference(*BASE, "--stop-tag", "car@2026-11-15")
    assert env["data"]["ending_balance"] == "16873.34"
    assert any("ends debt flow 'Car loan'" in w for w in env["warnings"])


def test_what_if_dates_can_be_written_in_words(reference):
    iso = reference(*BASE, "--settle", "Car loan:15000@2026-11-15")["data"]["ending_balance"]
    words = reference(*BASE, "--settle", "Car loan:15000@nov 15")["data"]["ending_balance"]
    assert iso == words == "11680.00"


def test_placeholder_needs_on_or_earliest(reference):
    env = reference(*BASE, "--add-expense", "Flight:550@?", ok=False)
    assert env["error"]["code"] == "usage" and "earliest" in env["error"]["hint"]
    pinned = reference(*BASE, "--add-expense", "Flight:550@?", "--settle",
                       "Car loan:15000@?+3", "--on", "2026-11-12")["data"]  # fmt: skip
    assert pinned["ending_balance"] == "11130.00"


EARLIEST = [
    "earliest", "--balance", "3000", "--as-of", "2026-09-16", "--months", "6",
    "--settle", "Car loan:15000@?", "--add-expense", "Flight:550@?", "--stop",
    "Car insurance@?",
]  # fmt: skip


def test_earliest_finds_first_feasible_date(reference):
    env = reference(*EARLIEST, "--floor", "2000")
    d = env["data"]
    assert d["status"] == "found" and d["date"] == "2026-11-13"
    assert d["candidates"]["checked"] == 182
    r = d["result"]
    assert r["balance_on_date"] == "3730.00"
    assert r["min_after"] == {"date": "2026-12-01", "balance": "3230.00", "spare": "3230.00"}
    assert r["headroom"] == "1230.00" and r["ending_balance"] == "11730.00"
    assert d["last_infeasible"]["shortfall"] == "770.00"
    assert env["warnings"] == []


def test_earliest_none_in_range(reference):
    env = reference(*EARLIEST, "--floor", "1000000")
    assert env["data"]["status"] == "none_in_range"
    assert any("no date between" in w for w in env["warnings"])


def test_summary(reference):
    d = reference("summary", "--tag", "car")["data"]
    car = next(t for t in d["by_tag"] if t["tag"] == "car")
    assert car["expense_monthly"] == "506.66" and car["expense_annual"] == "6079.92"
    d = reference("summary")["data"]
    assert d["net"]["income_monthly"] == "5436.51"
    assert d["net"]["expense_monthly"] == "3506.66"
    d = reference("summary", "--actual", "--months", "6", "--by", "flow")["data"]
    salary = next(f for f in d["by_flow"] if f["name"] == "Salary")
    assert salary["occurrences_in_window"] == "13" and salary["monthly"] == "5416.67"
    assert "by_tag" not in d


def test_spend(reference):
    d = reference("spend", "car", "--until", "2026-12-12")["data"]
    assert d["total"] == "1013.32"
    assert [(f["name"], f["count"], f["total"]) for f in d["by_flow"]] == [
        ("Car loan", 2, "773.32"),
        ("Car insurance", 2, "240.00"),
    ]
    d = reference("spend", "car insurance", "rent", "--until", "2026-12-12")["data"]
    assert d["total"] == "9240.00"
    d = reference("spend", "car", "--exclude", "loan", "--until", "2026-12-12")["data"]
    assert d["total"] == "240.00"
    d = reference("spend", "--income", "--until", "2026-10-31")["data"]
    assert d["total"] == "10000.00" and d["direction"] == "in"
    env = reference("spend", "cars", ok=False)
    assert env["error"]["code"] == "unknown_term"


def test_breakeven_and_compare(reference, tmp_path):
    spec = {
        "name": "sell car",
        "disable": {"tags": ["car"]},
        "add_flows": [
            {"name": "Car sale", "kind": "income", "amount": "15000", "on": "2026-10-15"}
        ],
        "debt_events": [{"flow": "Car loan", "type": "payoff", "date": "2026-10-15"}],
    }
    f = tmp_path / "sell.json"
    f.write_text(json.dumps(spec))
    d = reference("breakeven", "--scenario", str(f))["data"]
    assert d["breakeven"]["date"] == "2027-08-01" and d["breakeven"]["status"] == "reached"
    assert d["breakeven"]["max_shortfall"]["amount"] == "-4880.00"
    d = reference("compare", "--disable-tag", "car", "--balance", "3000", "--months", "6")["data"]
    assert d["a"]["ending_balance"] == "14846.70" and d["b"]["ending_balance"] == "17500.00"
    assert d["difference"]["ending"] == "2653.30"
    env = reference("compare", ok=False)
    assert env["error"]["code"] == "usage"


def test_debt_schedule_reference_table(reference):
    d = reference("debt", "schedule", "Car loan", "--as-of", "2026-10-01")["data"]
    assert d["payoff_date"] == "2031-10-01" and d["payments_remaining"] == 60
    first = d["rows"][0]
    assert (first["interest"], first["principal"], first["balance"]) == ("100.00", "286.66",
                                                                        "19713.34")  # fmt: skip
    assert d["rows"][-1]["payment"] == "386.38"
    assert d["total_interest_remaining"] == "3199.32"


def _plan_debts(run):
    for name, amount, bal, rate in (("Card", "50", "1000", "24%"), ("Store", "25", "500", "3%")):
        run("add", name, amount, "monthly on the 1st", "--from", "2026-10-01", "--tag", "loan")
        run("debt", "set", name, "--balance", bal, "--as-of", "2026-09-16", "--rate", rate,
            "--compounding", "simple")  # fmt: skip


def test_plan_avalanche(reference):
    _plan_debts(reference)
    env = reference("plan", "--extra", "300", "--balance", "3000")
    d = env["data"]
    card = d["steps"][0]
    assert card["name"] == "Card" and card["payment_during"] == "350.00"
    assert card["paid_off_on"] == "2026-12-01" and card["leftover_to_next"] == "20.31"
    loan = next(s for s in d["steps"] if s["name"] == "Car loan")
    assert loan["payment_during"] == "736.66" and loan["paid_off_on"] == "2029-05-01"
    assert d["debt_free_on"] == "2029-05-01" and d["months"] == 31
    assert d["interest_paid"] == "1696.06" and d["interest_saved"] == "1789.73"
    assert d["cash_check"]["affordable"] is True
    assert d["start"] == "2026-09-16" and d["opening"]["source"] == "given"


def test_plan_snowball(reference):
    _plan_debts(reference)
    d = reference("plan", "--extra", "300", "--strategy", "snowball")["data"]
    assert [s["name"] for s in d["steps"]] == ["Store", "Card", "Car loan"]
    assert d["debt_free_on"] == "2029-04-01" and d["interest_paid"] == "1723.31"


# ── The envelope, errors and --select ─────────────────────────────────────────


def test_select_trims_and_explains_misses(reference):
    env = reference(*BASE, "--select", "ending_balance,totals.income,series[-1].date")
    assert env["data"] == {
        "ending_balance": "14846.70",
        "totals.income": "32500.00",
        "series[-1].date": "2027-03-16",
    }
    env = reference(*BASE, "--select", "nope", ok=False)
    assert env["error"]["code"] == "select_not_found"
    assert "ending_balance" in env["error"]["message"]


def test_global_flags_work_anywhere(reference, db_path, capsys):
    cli.main(["ls", "--select", "count", "--db", str(db_path)])
    assert json.loads(capsys.readouterr().out)["data"] == {"count": 4}
    cli.main(["--no-tidy", "ls", "--db", str(db_path), "--select", "count"])
    assert json.loads(capsys.readouterr().out)["data"] == {"count": 4}


def test_errors_are_envelopes_with_hints(reference):
    env = reference("show", "Rnet", ok=False)
    assert env["error"]["code"] == "unknown_flow"
    assert env["error"]["message"] == "there's no flow called 'Rnet'"
    assert "Rent" in env["error"]["hint"] and "[" not in env["error"]["hint"]
    env = reference("add", "Gym", ok=False)
    assert env["error"]["code"] == "usage"
    env = reference("frobnicate", ok=False)
    assert env["error"]["code"] == "usage"
    env = reference("add", "Rent", "10", "monthly", ok=False)
    assert env["error"]["code"] == "duplicate_flow"


def test_missing_budget(tmp_path, capsys):
    code, out = call(capsys, "--db", str(tmp_path / "nope.sqlite"), "ls")
    env = json.loads(out)
    assert code == 1
    assert env["error"]["code"] == "db_not_found" and "bdbd init" in env["error"]["hint"]


def test_tidy_is_reported(reference, monkeypatch):
    reference("add", "Flight", "550", "once on 2026-09-20")
    monkeypatch.setenv("BDBD_TODAY", "2026-10-02")
    env = reference("ls", "--select", "count")
    assert any("Flight" in t for t in env["tidied"])
    assert reference("ls", "--select", "count").get("tidied") is None


def test_guide(agent):
    env = agent("guide")
    guide = env["data"]["guide"]
    assert env["data"]["format"] == "markdown" and "bdbd overview" in guide
    assert "--agent" not in guide and "BDBD_AGENT" not in guide


def test_the_guide_covers_every_command():
    guide = (resources.files("bdbd") / "guide.md").read_text()
    names = {c.name for c in cli.app.registered_commands if c.name}
    names |= {g.name for g in cli.app.registered_groups if g.name}
    assert "overview" in names
    for name in names:
        assert f"bdbd {name}" in guide, f"guide.md doesn't mention `bdbd {name}`"


# ── No command, --agent, --version, --help ────────────────────────────────────


def call(capsys, *argv: str) -> tuple[int, str]:
    """Run bdbd as it is, with no checks: (exit code, stdout)."""
    try:
        cli.main(list(argv))
    except SystemExit as exc:
        return int(exc.code or 0), capsys.readouterr().out
    return 0, capsys.readouterr().out


def test_overview_is_the_old_bare_agent_call(reference):
    reference("balance", "3000")
    env = reference("overview")
    d = env["data"]
    assert env["command"] == "overview" and env["warnings"] == []
    assert set(d) == {"today", "balance", "spare", "spare_balance", "low_point", "monthly",
                      "upcoming", "debts"}  # fmt: skip
    assert d["today"] == "2026-09-16"
    assert d["balance"]["balance"] == "3000.00" and d["balance"]["source"] == "recorded"
    assert d["spare_balance"] == "3000.00"
    salary = {"date": "2026-09-18", "name": "Salary", "amount": "2500.00"}
    assert d["spare"]["next_payday"] == salary
    assert d["low_point"] == {"date": "2026-10-01", "balance": "2500.00", "horizon_days": 90}
    assert d["monthly"] == {"income": "5436.51", "bills": "3506.66", "everyday": "0.00",
                            "net": "1929.85"}  # fmt: skip
    assert [(u["date"], u["name"], u["balance_after"]) for u in d["upcoming"]] == [
        ("2026-09-18", "Salary", "5500.00")
    ]
    assert d["debts"]["total_owed"] == "20000.00"
    assert d["debts"]["debt_free_on"] == "2031-10-01"
    assert d["debts"]["interest_remaining"] == "3199.32"
    trimmed = reference("overview", "--select", "spare_balance,low_point.balance")["data"]
    assert trimmed == {"spare_balance": "3000.00", "low_point.balance": "2500.00"}


def test_overview_without_a_balance(reference):
    d = reference("overview")["data"]
    assert d["balance"]["source"] == "none"
    assert d["spare"] is None and d["spare_balance"] is None and d["low_point"] is None


def test_agent_flag_is_gone(reference, db_path, capsys):
    for argv in (["--agent", "ls"], ["ls", "--agent"], ["--agent"]):
        code, out = call(capsys, "--db", str(db_path), *argv)
        env = json.loads(out)
        assert code == 2 and env["ok"] is False
        assert env["error"]["code"] == "usage" and "--agent" in env["error"]["message"]


def test_bdbd_agent_env_var_does_nothing(reference, db_path, capsys, monkeypatch):
    plain = reference("debts")
    monkeypatch.setenv("BDBD_AGENT", "1")
    assert reference("debts") == plain
    code, out = call(capsys, "--db", str(db_path))
    assert code == 2 and json.loads(out)["error"]["code"] == "no_terminal"


def test_no_command_needs_a_terminal(reference, db_path, capsys):
    code, out = call(capsys, "--db", str(db_path))
    assert code == 2
    assert json.loads(out) == {
        "ok": False,
        "command": "app",
        "error": {
            "code": "no_terminal",
            "message": "bdbd opens an app, which needs a terminal",
            "hint": "For the overview as JSON, run bdbd overview.",
        },
    }


def test_no_command_at_a_terminal_opens_the_app(reference, db_path, capsys, monkeypatch):
    opened = []
    fake = types.ModuleType("bdbd.tui")
    monkeypatch.setattr(
        fake, "run", lambda path, *, tidy=True: opened.append((path, tidy)), raising=False
    )
    monkeypatch.setitem(sys.modules, "bdbd.tui", fake)
    monkeypatch.setattr(cli, "_at_terminal", lambda: True)
    assert call(capsys, "--db", str(db_path)) == (0, "")
    assert call(capsys, "--no-tidy", "--db", str(db_path)) == (0, "")
    assert opened == [(db_path, True), (db_path, False)]


def test_select_needs_a_command(reference, db_path, capsys):
    code, out = call(capsys, "--db", str(db_path), "--select", "spare_balance")
    env = json.loads(out)
    assert code == 2 and env["command"] == "app"
    assert env["error"]["code"] == "usage" and "bdbd overview --select" in env["error"]["hint"]


def test_version_is_plain_text(capsys):
    assert call(capsys, "--version") == (0, f"bdbd {__version__}\n")
    assert call(capsys, "-V") == (0, f"bdbd {__version__}\n")


def test_help_says_bdbd_alone_opens_the_app(capsys):
    code, out = call(capsys, "--help")
    assert code == 0 and "opens the app" in out and "bdbd guide" in out
    for panel in ("Look", "Change", "Debts", "Ask what if", "Data", "Examples"):
        assert panel in out
    assert "overview" in out and "--agent" not in out


# ── The made-up household (tests/sample.py): the numbers the app shows ────────


def test_household_overview(home):
    d = home("overview")["data"]
    assert d["balance"]["balance"] == "4070.00" and d["balance"]["source"] == "carried"
    assert d["spare"]["next_payday"] == {"date": "2026-10-02", "name": "Paycheck",
                                         "amount": "2650.00"}  # fmt: skip
    assert d["spare_balance"] == "1595.00"
    assert d["low_point"] == {"date": "2026-10-01", "balance": "1595.00", "horizon_days": 90}
    assert d["monthly"] == {"income": "5760.07", "bills": "3378.09", "everyday": "760.94",
                            "net": "1621.04"}  # fmt: skip
    assert [u["name"] for u in d["upcoming"]] == ["Credit card", "Rent", "Paycheck", "Gym"]
    assert d["debts"]["debt_free_on"] == "2034-08-21"


def test_household_flows(home):
    flows = {f["name"]: f for f in home("ls")["data"]["flows"]}
    assert flows["Paycheck"]["schedule"] == "Every 2 weeks on Fri"
    assert flows["Rent"]["schedule"] == "Monthly on the 1st" and flows["Rent"]["weekend"] == "next"
    assert flows["Car loan"]["amount"] == "412.37" and flows["Car loan"]["debt"] is not None
    d = home("show", "car loan")["data"]
    assert d["kind"] == "expense" and d["debt"]["compounding"] == "simple"
    assert d["debt_outlook"]["payoff_date"] == "2030-02-05"


def test_household_upcoming_and_calendar(home):
    items = home("upcoming", "--days", "10")["data"]["items"]
    assert (items[0]["date"], items[0]["name"]) == ("2026-09-25", "Credit card")  # tomorrow
    days = {x["date"]: x for x in home("cal", "oct")["data"]["days"]}
    paycheck = {"name": "Paycheck", "amount": "2650.00", "kind": "income"}
    assert paycheck in days["2026-10-02"]["items"]
    lowest = min((x["balance"], x["date"]) for x in days.values() if x["balance"])
    assert lowest == ("1595.00", "2026-10-01")


def test_household_project_and_debts(home):
    assert home("project", "--months", "6")["data"]["opening"]["balance"] == "4070.00"
    d = home("debts")["data"]
    assert [x["name"] for x in d["debts"]] == ["Credit card", "Car loan", "Student loan"]
    d = home("debt", "schedule", "Credit card", "-n", "3")["data"]
    assert len(d["rows"]) == 3 and d["rows_truncated"]


def test_household_questions(home):
    assert home("spend", "car", "--until", "dec", "31")["data"]["total"] == "1621.11"  # unquoted
    one_offs = home("summary")["data"]["one_offs"]
    assert [o["name"] for o in one_offs] == ["Tax refund", "Dentist"]
    d = home("compare", "--settle", "Car loan:13000@nov 1", "--stop-tag", "car@nov 1")["data"]
    assert d["breakeven"]["status"] == "reached" and d["breakeven"]["date"] == "2027-01-15"
    d = home("earliest", "--floor", "1000", "--add-expense", "Flight:650@?", "--months", "3")
    assert d["data"]["status"] == "found" and d["data"]["date"] == "2026-10-02"
    assert home("plan", "--extra", "300")["data"]["debt_free_on"] == "2029-08-21"


def test_household_changes_round_trip(home):
    d = home("add", "Netflix", "15.49", "monthly", "on", "the", "12th", "-t", "fun")["data"]
    assert d["schedule"] == "Monthly on the 12th" and d["next"] == "2026-10-12"
    d = home("edit", "netflix", "--amount", "17.99", "--when", "monthly on the", "15th")["data"]
    assert d["amount"] == "17.99" and d["schedule"] == "Monthly on the 15th"
    assert home("pause", "Netflix")["data"]["active"] is False
    assert home("resume", "Netflix")["data"]["active"] is True
    assert home("rm", "Netflix")["data"]["removed"]["name"] == "Netflix"  # never asks
    assert home("show", "Netflix", ok=False)["error"]["code"] == "unknown_flow"


def test_household_balance(home):
    d = home("balance")["data"]
    assert d["recorded"]["balance"] == "4120.00" and d["recorded"]["as_of"] == "2026-09-22"
    assert d["today"]["balance"] == "4070.00" and d["today"]["source"] == "carried"
    d = home("balance", "3980")["data"]
    assert d["recorded"]["balance"] == "3980.00"
    assert d["expected"] == "4070.00" and d["difference_from_expected"] == "-90.00"


def test_balance_on_a_day_with_items_assumes_pending(home):
    env = home("balance", "3980", "--on", "sep", "18")
    assert env["data"]["recorded"]["balance"] == "3980.00"
    assert "Pass --posted" in env["warnings"][0]
    d = home("balance", "3980", "--on", "sep", "18", "--posted")["data"]
    assert d["recorded"]["balance"] == "1330.00"
    assert [e["name"] for e in d["already_posted"]] == ["Paycheck"]


def test_household_debt_events(home):
    d = home("debt", "extra", "Car loan", "1000", "--on", "oct", "20")["data"]
    assert (d["type"], d["date"], d["amount"]) == ("extra_payment", "2026-10-20", "1000.00")
    d = home("debt", "adjust", "Car loan", "-250", "--on", "oct 21")["data"]
    assert (d["type"], d["amount"]) == ("balance_adjustment", "-250.00")


def test_household_data_commands(home, tmp_path):
    assert "weekly_spend" in home("config")["data"]["keys"]
    assert home("config", "weekly_spend", "200")["data"]["config"] == {"weekly_spend": "200"}
    backup = tmp_path / "backup.json"
    assert home("export", "-o", str(backup))["data"]["flows"] == 15
    assert home("import", str(backup), "--replace")["data"] == {"imported_flows": 15}
    assert home("tidy")["data"]["actions"] == []
    assert home("sql", "select name from flow limit 3")["data"]["row_count"] == 3
    assert home("sql", "delete from flow", ok=False)["error"]["code"] == "readonly_sql"


# ── Robustness: every command answers with one envelope ────────────────────────


def test_bad_debt_event_amounts_are_envelopes(home):
    for argv in (
        ("debt", "extra", "Car loan", "abc"),
        ("debt", "payment", "Car loan", "abc"),
        ("debt", "adjust", "Credit card", "abc"),
        ("debt", "rate", "Credit card", "abc"),
        ("debt", "extra", "Nope", "abc"),
    ):
        env = home(*argv, ok=False)
        assert env["error"]["code"] in ("invalid_amount", "invalid_rate", "unknown_flow"), argv


def test_unreadable_budgets_are_envelopes(tmp_path, capsys):
    junk = tmp_path / "junk.sqlite"
    junk.write_text("not a database")
    code, out = call(capsys, "--db", str(junk), "ls")
    env = json.loads(out)
    assert code == 1 and env["error"]["code"] == "db_unreadable"
    code, out = call(capsys, "--db", str(tmp_path), "ls")
    assert code == 1 and not json.loads(out)["ok"]


def test_a_bad_today_is_an_envelope(agent, db_path, capsys, monkeypatch):
    monkeypatch.setenv("BDBD_TODAY", "garbage")
    code, out = call(capsys, "--db", str(db_path), "ls")
    env = json.loads(out)
    assert code == 1 and env["error"]["code"] == "invalid_date"


def test_select_and_db_need_values(agent, db_path, capsys):
    code, out = call(capsys, "--db", str(db_path), "ls", "--select")
    env = json.loads(out)
    assert code == 2 and "--select needs a value" in env["error"]["message"]


def test_usage_errors_name_the_whole_command(agent, db_path, capsys):
    code, out = call(capsys, "--db", str(db_path), "debt", "set")
    assert code == 2 and json.loads(out)["command"] == "debt set"


def test_a_select_miss_after_a_change_saves_once(home):
    env = home("debt", "extra", "Car loan", "500", "--select", "amount_paid")
    assert env["ok"] and env["data"]["amount"] == "500.00"
    assert "The change was saved" in env["warnings"][-1]
    rows = home("sql", "select count(*) as n from debt_event")["data"]["rows"]
    assert rows == [{"n": 1}]
    assert home("ls", "--select", "nope", ok=False)["error"]["code"] == "select_not_found"


def test_guide_and_init_apply_select(agent):
    assert set(agent("guide", "--select", "format")["data"]) == {"format"}


def test_negative_balances(home):
    d = home("balance", "-120.50", "--on", "yesterday")["data"]
    assert d["typed"] == "-120.50" and d["recorded"]["as_of"] == "2026-09-23"
    assert home("balance", "-120", "--select", "typed")["data"] == {"typed": "-120.00"}


def test_tidied_is_reported_when_a_command_fails(home, monkeypatch):
    monkeypatch.setenv("BDBD_TODAY", "2026-11-03")
    env = home("show", "Nope", ok=False)
    assert env["error"]["code"] == "unknown_flow"
    assert any("Tax refund" in t for t in env["tidied"])


def test_the_stale_warning_names_the_command(home, monkeypatch):
    monkeypatch.setenv("BDBD_TODAY", "2026-10-20")
    warnings = home("overview")["warnings"]
    assert any("update it with `bdbd balance AMOUNT`" in w for w in warnings)


def test_names_that_read_as_ids_and_tags_with_commas_are_refused(home):
    assert home("add", "3", "20", "monthly on the 2nd", ok=False)["error"]["code"] == (
        "invalid_name"
    )
    env = home("tags", "rename", "insurance", "home, auto", ok=False)
    assert env["error"]["code"] == "invalid_tag"


# ── Backups ───────────────────────────────────────────────────────────────────


def _backup(home, tmp_path, name="b.json") -> tuple[Path, dict]:
    path = tmp_path / name
    home("export", "-o", str(path))
    return path, json.loads(path.read_text())


def test_export_never_writes_over_the_budget(home, db_path, tmp_path):
    env = home("export", "-o", str(db_path), ok=False)
    assert env["error"]["code"] == "file_error"
    assert home("ls", "--select", "count")["data"]["count"] == 15
    env = home("export", "-o", str(tmp_path / "missing" / "b.json"), ok=False)
    assert env["error"]["code"] == "file_not_found"


def test_a_damaged_backup_changes_nothing(home, tmp_path):
    path, data = _backup(home, tmp_path)
    home("add", "Only here", "5", "monthly on the 3rd")
    cases = {
        "bad balance date": {**data, "balances": [{"as_of": "2026-13-01", "balance": "10"}]},
        "balance without amount": {**data, "balances": [{"as_of": "2026-09-20"}]},
        "flow without kind": {
            **data,
            "flows": [{k: v for k, v in data["flows"][0].items() if k != "kind"}],
        },
        "flows not a list": {**data, "flows": "nope"},
    }
    for label, bad in cases.items():
        path.write_text(json.dumps(bad))
        env = home("import", str(path), "--replace", ok=False)
        assert env["error"]["code"] == "invalid_import", label
        assert home("ls", "--select", "count")["data"]["count"] == 16, label
    path.write_text(json.dumps(cases["flow without kind"]))
    assert "has no 'kind'" in home("import", str(path), "--replace", ok=False)["error"]["message"]


def test_import_takes_a_saved_envelope_and_replaces_balances(home, tmp_path, capsys):
    env = home("export")
    path = tmp_path / "envelope.json"
    path.write_text(json.dumps(env))
    home("balance", "999")
    assert home("import", str(path), "--replace")["data"] == {"imported_flows": 15}
    history = home("balance")["data"]["history"]
    assert [h["balance"] for h in history] == ["4120.00"]  # the backup's, not today's 999
    none = {**env["data"], "balances": []}
    path.write_text(json.dumps(none))
    home("import", str(path), "--replace")
    assert home("balance")["data"]["history"] == []


def test_import_without_replace_needs_an_empty_budget(agent, tmp_path, db_path):
    agent("balance", "50")
    backup = {"bdbd_export": 1, "schema_version": 4, "config": {}, "flows": [], "balances": []}
    path = tmp_path / "b.json"
    path.write_text(json.dumps(backup))
    env = agent("import", str(path), ok=False)
    assert env["error"]["code"] == "db_not_empty" and "your balances" in env["error"]["message"]


# ── Numbers the review caught ─────────────────────────────────────────────────


def test_breakeven_trend_is_money_a_month(home):
    d = home("compare", "--extra-payment", "Credit card:1000@2026-10-15")["data"]
    assert d["breakeven"]["trend_per_month_since_worst"] == "42.31"
    assert "~42.31/month" in d["breakeven"]["caveat"]


def test_a_debt_that_is_never_paid_off(home):
    home("edit", "Credit card", "--amount", "50")
    env = home("debts")
    card = next(r for r in env["data"]["debts"] if r["name"] == "Credit card")
    assert card["paid_off_on"] is None and card["interest_remaining"] is None
    assert any("Credit card is never paid off" in w for w in env["warnings"])
    others = [r for r in env["data"]["debts"] if r["name"] != "Credit card"]
    total = sum(Decimal(r["interest_remaining"]) for r in others)
    assert Decimal(env["data"]["interest_remaining"]) == total


def test_monthly_payments_are_steady_and_skip_paid_off_debts(home):
    assert home("debts")["data"]["monthly_payments"] == "798.37"
    home("debt", "payoff", "Credit card", "--on", "2026-09-20")
    d = home("debts")["data"]
    assert d["monthly_payments"] == "648.37"
    home("add", "Boat loan", "200", "every 2 weeks on fri")
    home("debt", "set", "Boat loan", "--balance", "5000", "--rate", "7%", "--compounding", "simple")
    d = home("debts")["data"]
    boat = next(r for r in d["debts"] if r["name"] == "Boat loan")
    assert boat["payment"] == "200.00" and boat["monthly"] == "434.92"
    assert d["monthly_payments"] == "1083.29"


def test_items_carry_the_end_of_day_balance(home):
    items = home("upcoming", "--days", "8")["data"]["items"]
    rent = next(i for i in items if i["name"] == "Rent")
    assert (rent["balance_after"], rent["end_of_day_balance"]) == ("1620.00", "1595.00")
    upcoming = home("overview")["data"]["upcoming"]
    assert next(i for i in upcoming if i["name"] == "Rent")["end_of_day_balance"] == "1595.00"


def test_coming_up_on_a_payday_runs_to_the_next_one(home, monkeypatch):
    monkeypatch.setenv("BDBD_TODAY", "2026-10-02")
    d = home("overview")["data"]
    assert d["spare"]["next_payday"]["date"] == "2026-10-16"
    assert "Car insurance" in [i["name"] for i in d["upcoming"]]


# ── Ordering and the next 12 months ───────────────────────────────────────────


def test_a_days_items_list_money_in_first_then_the_largest(reference):
    reference("add", "Bonus", "500", "once on 2026-11-01", "--income")
    items = reference("upcoming", "--balance", "3000", "--until", "2026-11-01")["data"]["items"]
    day = [i for i in items if i["date"] == "2026-11-01"]
    assert [i["name"] for i in day] == [
        "Bonus",
        "Rent",
        "Car loan",
    ]  # the loan paid first, listed after
    after = [Decimal(i["balance_after"]) for i in day]
    assert after[1] == after[0] - Decimal("3000.00")  # running balances follow the listing
    assert after[2] == after[1] - Decimal("386.66")
    assert day[-1]["end_of_day_balance"] == day[-1]["balance_after"]


def test_a_flows_next_12_months_stop_at_its_end_date(home):
    home("add", "Bay Ridge rent", "2000", "monthly on the 1st", "--until", "2027-08-31")
    d = home("show", "Bay Ridge rent")["data"]
    assert d["monthly"] == "2000.00"
    assert d["next_12_months"] == "22000.00"  # Oct 1 to Aug 1: eleven months, not twelve
    assert home("spend", "Bay Ridge rent", "--months", "12")["data"]["total"] == "22000.00"
    loan = home("show", "Credit card")["data"]
    assert loan["next_12_months"] == "1800.00"  # twelve payments of 150.00


def test_spend_months_are_whole_months(home):
    # a yearly bill due on the first day is in the next 12 months once, not twice
    d = home("spend", "Car registration", "--months", "12", "--from", "2027-03-14")["data"]
    assert d["total"] == "220.00"
