"""Agent mode, end to end: the real command, the JSON envelope, and hand-checked numbers.

The reference budget (see conftest) is entered in plain English, so these also prove that the
English schedules store the rules the numbers were checked against.
"""

from __future__ import annotations

import contextlib
import json

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
    assert d["spare"]["next_income"] == payday
    assert d["spare"]["committed_before_next_income"] == [
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
    from bdbd import cli

    cli.main(["ls", "--select", "count", "--db", str(db_path)])  # --select implies --agent
    assert json.loads(capsys.readouterr().out)["data"] == {"count": 4}


def test_errors_are_envelopes_with_hints(reference):
    env = reference("show", "Rnet", ok=False)
    assert env["error"]["code"] == "unknown_flow"
    assert "Rent" in env["error"]["hint"] and "[" not in env["error"]["hint"]
    env = reference("add", "Gym", ok=False)
    assert env["error"]["code"] == "usage"
    env = reference("frobnicate", ok=False)
    assert env["error"]["code"] == "usage"
    env = reference("add", "Rent", "10", "monthly", ok=False)
    assert env["error"]["code"] == "duplicate_flow"


def test_missing_budget(tmp_path, capsys):
    from bdbd import cli

    try:
        cli.main(["--agent", "--db", str(tmp_path / "nope.sqlite"), "ls"])
    except SystemExit as exc:
        assert exc.code == 1
    env = json.loads(capsys.readouterr().out)
    assert env["error"]["code"] == "db_not_found" and "bdbd init" in env["error"]["hint"]


def test_agent_env_var(reference, db_path, capsys, monkeypatch):
    from bdbd import cli

    monkeypatch.setenv("BDBD_AGENT", "1")
    with contextlib.suppress(SystemExit):
        cli.main(["--db", str(db_path), "debts"])
    env = json.loads(capsys.readouterr().out)
    assert env["data"]["total_owed"] == "20000.00"


def test_tidy_is_reported(reference, monkeypatch):
    reference("add", "Flight", "550", "once on 2026-09-20")
    monkeypatch.setenv("BDBD_TODAY", "2026-10-02")
    env = reference("ls", "--select", "count")
    assert any("Flight" in t for t in env["tidied"])
    assert reference("ls", "--select", "count").get("tidied") is None


def test_guide(agent):
    env = agent("guide")
    assert env["data"]["format"] == "markdown" and "--agent" in env["data"]["guide"]
