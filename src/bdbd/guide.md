# bdbd guide

bdbd keeps a budget (incomes, expenses and debts) in one sqlite file and answers cash-flow
questions: what you'll have on a date, what's spare until payday, what something costs, when a
debt is gone, and what-ifs. **Every command prints exactly one JSON object on stdout**, made
for LLM agents and scripts. (`bdbd` alone opens a full-screen app for people; this guide is
about the commands. `--help` and `--version` print plain text.)

```
bdbd [--select PATHS] [--db PATH] [--no-tidy] COMMAND [ARGS]
```

Global flags work anywhere on the line. Never do budget arithmetic yourself: ask bdbd and
report its numbers.

## The envelope

- Success: `{"ok": true, "command": "project", "data": {...}, "warnings": [...]}`, plus
  `"tidied": [...]` when the automatic tidy-up changed something (see below).
- Failure: `{"ok": false, "command": "...", "error": {"code": "...", "message": "...",
  "hint": "..."}}`, plus `tidied` when the tidy-up ran before the command failed. Exit codes:
  0 ok, 1 a problem with the request (unknown flow, bad date), 2 a usage mistake (missing
  argument, unknown option, or `bdbd` with no command outside a terminal: `no_terminal`).
- **Always relay `warnings` and `tidied` to the person.** Warnings flag things like a stale
  balance, negative amortization, or a what-if tag that matched nothing.
- Money is a string with two decimals (`"1234.50"`), negative when money leaves. Dates are ISO
  (`YYYY-MM-DD`). Rates are decimal fractions (`"0.0649"` is 6.49%).
- `--select a,b.c,rows[0].date,series[-1].balance` keeps only those paths of `data`. When a path
  misses, the error lists the keys that are there: fix the path from it. On a command that
  changes the budget, a miss is not a failure (the change is already saved): the envelope is
  `ok` with all of the data and a warning saying so. Don't repeat the change.
- bdbd never prompts and never asks for confirmation: pass everything as arguments.
- `--verbose` on queries adds bulky sections (per-flow usage, full scenario spec). `project
  --ledger` adds every transaction; `project --daily` gives a series row per day.
- `BDBD_TODAY=YYYY-MM-DD` pins "today" for reproducible runs. `BDBD_DB` names the budget file
  (default `~/.config/bdbd/budget.sqlite`, or under `$XDG_CONFIG_HOME`).

## Writing things down

**Money**: `386.66`, `1,234`, `$1,234.56`. Amounts of flows are always positive; whether money
comes in or goes out is the flow's kind (`--income`, or `--kind income|expense`).
**Names**: a flow's name needs a letter in it (a number alone reads as an id); tags can't
contain commas.
**Rates**: `6.49%` or `0.0649`.

**Dates**: `2026-10-15`, `oct 15`, `15 oct`, `oct 15 2027`, `10/15` (month first), `today`,
`tomorrow`, `yesterday`, `fri`, `next fri`, `last fri`, `in 3 weeks`, `2 weeks ago`, `eom` (end
of month), `eoy`, `+10d`, `+2w`, `+3m`, `+1y`, `-1m`. A date without a year means the next one
(for balances, the last one). `eom`/`eoy`/`±N` count from the query's start date. Dates after an
option may be written without quotes (`--until dec 31`); quote them inside what-if specs.

**Schedules** (`bdbd add NAME AMOUNT WHEN…` or `--when "…"`):

| want | say |
|---|---|
| monthly on the 1st | `monthly on the 1st` |
| 15th and last day | `monthly on the 15th and last day` |
| first Friday | `monthly on the first friday` (or `last friday`, `2nd tue`) |
| every two weeks | `every 2 weeks on fri from sep 18` (also `biweekly`, `every other week`) |
| weekly on days | `weekly on mon and thu`, `every weekday` |
| every N months | `every 3 months on the 10th`, `quarterly on the 1st` |
| twice a month | `semimonthly` (1st and 15th) |
| yearly | `yearly on nov 20`, `yearly on the 3rd tuesday of november` |
| one-off | `once on oct 15`, or just `oct 15` |
| limits | `… from oct 1`, `… until 2027-06-30`, `… 12 times` |
| raw rule | `FREQ=MONTHLY;BYMONTHDAY=1` (RFC 5545 body; DTSTART/UNTIL go in from/until) |

A lone 29th or 30th means "that day, or the month's last day when it's shorter"; `31st` is the
last day. Without `from`, a schedule starts at its next occurrence from today. Weekend rule:
`--ach` (or `--weekend next`) moves Saturday/Sunday dates to Monday, `--weekend previous` to
Friday. Holidays are not modelled.

## The balance

bdbd remembers the balance you give it: `bdbd balance 3200` (optionally `--on DATE`; an
overdrawn account is `bdbd balance -120.50`). It is
cash on hand **before that day's scheduled items**; if the day's items have already posted, say
`--posted` (otherwise bdbd assumes `--pending` and warns when that day has items). Every
projection starts from the latest balance, carried forward day by day through the budget, so a
balance from Tuesday still works on Friday. Outputs report where the starting balance came from:

`opening: {balance, as_of, source: given|recorded|carried|none, recorded: {as_of, balance,
recorded_at} | null}`

`--balance A` on a query overrides it for that run. With no balance at all, projections start
from 0 and warn.

**Everyday spending**: `bdbd config weekly_spend 175` is the weekly amount for groceries and
incidentals, charged a little each day after the start date in every projection (exact to the
cent over any span). `--weekly-spend A` overrides it for one run. Ask the person for it rather
than assume 0.

**Spare money**: the balance on a date minus everything due after it and before the next income
(everyday spending included). This is the answer to "how much will I have on DATE": report
`spare_balance` and name the bills it deducted; give the raw `ending_balance` when asked.

## Commands and their data

### Where things stand
- `bdbd overview` → `today`, `balance` (the opening block for today), `spare_balance`, `spare
  {date, balance, next_income {date,name,amount}, committed_total, committed_lifestyle,
  committed_before_next_income[{date,name,amount}], spare_balance}`, `low_point {date, balance,
  horizon_days}` (next 90 days), `monthly {income, bills, everyday, net}`, `upcoming[]` (ledger
  rows until the next income after today, at least 10 days, at most 12 rows), `debts {…}` (as
  `bdbd debts`). The same numbers the app's overview shows.
- `bdbd balance [AMOUNT] [--on DATE] [--posted|--pending] [--forget]` → without AMOUNT: `today`
  (opening block), `recorded`, `since_recorded[]`, `history[]`. With AMOUNT: `recorded {as_of,
  balance}`, `typed`, `already_posted[]`, `previous`, `expected` (what the budget predicted from
  the previous balance), `difference_from_expected`. `--forget` → `{forgot: N}`.
- `bdbd upcoming [--days N | --until DATE] [--balance A] [--weekly-spend A]` → `from`, `until`,
  `opening`, `weekly_spend`, `items[{date, name, kind, amount, balance_after,
  end_of_day_balance, debt?}]`, `lifestyle_total`, `ending_balance`. Ledger `kind`: income,
  expense, debt_payment, extra_payment, payoff, settle. `balance_after` is right after the item;
  `end_of_day_balance` is at the close of its day, after that day's everyday spending: quote it
  for "what's left after Rent" (it's what the app shows, and what low points use). Overview
  `upcoming[]` and balance `since_recorded[]` rows carry it too.
- `bdbd cal [MONTH] [--balance A]` (oct, 2026-11, next, +2) → `month`, `opening`, `days[{date, items[{name,
  amount, kind}], net, balance}]` (end-of-day balances from today on; past days have no balance).

### Flows
- `bdbd ls [--income|--expenses] [--tag T] [--all]` → `flows[]`, `count`. A flow: `{id, name,
  kind, amount, schedule (English), rrule, dtstart, until, next, active, tags[], weekend, notes,
  debt, monthly}`. `--all` includes paused flows.
- `bdbd show FLOW` → the flow plus `upcoming[]` (next dates), `monthly`, and for debts
  `debt_outlook {balance_at_as_of, payoff_date, payments_remaining, total_paid_remaining,
  total_interest_remaining}`. FLOW is a name (any case) or id. `bdbd debt show FLOW` is the
  same (its envelope says `"command": "show"`).
- `bdbd add NAME AMOUNT WHEN… [--income | --kind K] [--when TEXT] [--from D] [--until D]
  [--tag T,…] [--ach | --weekend W] [--notes S] [--paused]` → the new flow.
- `bdbd edit FLOW [--name] [--amount] [--when TEXT] [--from D] [--until D | --no-until]
  [--income | --expense] [--tag T] [--untag T] [--tags A,B] [--ach | --weekend W] [--notes S]`
  → the updated flow.
- `bdbd rm FLOW` → `{removed: flow}` (its debt record and events go too; nothing asks first).
- `bdbd pause FLOW` / `bdbd resume FLOW` → the flow. Paused flows leave every projection.
- `bdbd tags` → `tags[{name, flows, flow_names[], expense_monthly, income_monthly}]`;
  `bdbd tags rename OLD NEW` → `{renamed: {from, to}}`; `bdbd tags rm TAG` → `{removed}`.

### Debts
A debt is an expense flow (the payment) plus its terms.
- `bdbd debts` (or `bdbd debt`) → `debts[{flow, name, balance (today), annual_rate, payment,
  monthly, paid_off_on, interest_remaining, tags}]`, `total_owed`, `monthly_payments`,
  `interest_remaining`, `debt_free_on`. `payment` is each payment and `monthly` the payments
  as a steady monthly amount (0 once paid off); `monthly_payments` adds those up.
  `interest_remaining` is the interest still to pay **from today** until payoff, so balance +
  interest_remaining = everything still to pay. A debt that is never paid off at its current
  payment has `paid_off_on` and `interest_remaining` null, is left out of the total, and a
  warning names it.
- `bdbd debt set FLOW --balance A --rate R --compounding simple|daily|monthly|continuous
  [--as-of D] [--day-count actual/365|actual/360|30/360] [--capitalize | --no-capitalize]
  [--payment-mode fixed|interest_only|percent_of_balance] [--payment-pct P]
  [--original-principal A] [--posting-day 1-31]` → the flow with its `debt`. `--balance` is
  what's owed right after the payment due on `--as-of` (default today). Simple interest accrues
  daily and never compounds (most car and student loans); `daily` suits cards; `monthly` posts
  `balance × rate / 12` on the posting day (mortgages); unpaid interest capitalizes unless
  `--no-capitalize`. `percent_of_balance` pays the larger of the amount and `pct × balance`.
- `bdbd debt show FLOW`, `bdbd debt unset FLOW`.
- Recorded events (saved, unlike what-ifs): `bdbd debt extra FLOW AMOUNT [--on D]`,
  `debt rate FLOW RATE [--on D]`, `debt payment FLOW AMOUNT [--on D]` (new regular payment),
  `debt adjust FLOW ±AMOUNT [--on D]`, `debt payoff FLOW [--on D]`, `debt drop EVENT_ID` → the
  event `{id, flow_id, date, type, rate, amount, notes}`.
- `bdbd debt schedule FLOW [--rows N | --all] [--as-of D] [--until D | --months N (600)]
  [--solve-payment MONTHS] [what-ifs]` → `terms`, `balance_at_as_of`, `payoff_date`,
  `payments_remaining`, `total_paid_remaining`, `total_interest_remaining`, `rows[{n, date,
  payment, interest, principal, balance, kind}]`, `rows_truncated`, `solved_payment`. Here
  `total_interest_remaining` is the interest inside the remaining payments, which includes
  interest already accrued since the balance date (so it can be a little more than `bdbd debts`
  interest_remaining); each row's interest is rounded to the cent, so the column can differ
  from the total by a few cents.

### Questions
Every question accepts the what-if flags below. Horizons: `--until DATE` or `--months N`.
- `bdbd project [--until D | --months N (12)] [--as-of D] [--balance A] [--weekly-spend A]
  [--daily] [--ledger] [--verbose]` → `as_of`, `until`, `opening`, `starting_balance`,
  `weekly_spend`, `lifestyle_total`, **`spare_balance`**, `spare {…}`, `ending_balance`,
  `min_balance {date, balance}`, `max_balance`, `totals {income, expense, net, interest_paid,
  principal_paid}`, `series[{date, balance, income, expense, net, spare}]` (as-of, each month
  end, until), `debts[{flow, name, balance_at_until, paid_off_on}]`, `total_debt_at_until`,
  `scenario`, `scenario_applied[]`, `ledger[]` with `--ledger`.
- `bdbd spend [TAG_OR_FLOW…] [--exclude X] [--income] [--from D] [--until D | --months N (1)]`
  → **`total`**, `matched[{term, as: tag|flow}]`, `excluded`, `by_flow[{flow, name, tags,
  count, total}]` (largest first), `items[{date, name, amount}]`, `lifestyle_total`. No terms =
  everything going out, everyday spending included (tag `lifestyle`).
- `bdbd summary [--tag T] [--actual --months N] [--by tag|flow|both] [--all]` → `net
  {income_monthly, expense_monthly, net_monthly, income_annual, expense_annual, net_annual}`
  (bills only: everyday spending is `weekly_spend`), `by_flow[{name, amount, monthly, annual,
  occurrences_per_year (occurrences_in_window with --actual), is_debt}]`,
  `by_tag[{tag, expense_monthly, …}]`, `untagged`,
  `one_offs[]`. Steady state counts `amount × occurrences per year / 12`; `--actual` averages
  the real dates in the window. A flow with several tags counts under each tag.
- `bdbd compare [what-ifs] [--baseline FILE] [--balance A] [--months N (24)]` → `a` (as it is)
  and `b` (the what-if), each `{label, ending_balance, min_balance, max_balance, total_income,
  total_expense, interest_paid, series}`, `difference {ending, series[{date, a, b, diff}]}`,
  `breakeven`.
- `bdbd breakeven [what-ifs] [--months N (120)]` → `breakeven {status: reached | immediate |
  never_in_horizon | identical, date, first_divergence, diff_at_end, max_shortfall {date,
  amount}, caveat, trend_per_month_since_worst, extrapolated_date}`. It doesn't depend on the
  balance.
- `bdbd earliest --floor A [--measure balance|spare] [--from D] [--before D] [--step N]
  [--weekdays] [--balance A] [--months N (12)] <what-ifs dated ?>` → `status: found |
  none_in_range`, **`date`**, `feasible_through`, `result {balance_on_date, spare_on_date,
  min_after {date, balance, spare}, headroom, ending_balance, spare_balance,
  placeholder_entries[]}`, `last_infeasible {date, min_after, shortfall}`, `best_infeasible`,
  `candidates {from, before, step_days, weekdays_only, checked}`. A date works when the measure
  stays at or above the floor from that day to the end of the horizon.
- `bdbd plan --extra A [--strategy avalanche|snowball] [--order A,B] [--tag T] [--exclude D]
  [--from D] [--no-rollover] [--months N (600)]` → `status`, **`debt_free_on`**, `months`,
  `interest_paid`, `interest_saved`, `baseline {debt_free_on, months, interest_paid}`,
  `monthly_outlay {scheduled_payments, with_extra}` (steady monthly amounts), `steps[{order,
  name, annual_rate,
  balance_at_start, scheduled_payment, payment_during, from, carry_in, paid_off_on, months,
  interest_paid, freed_to_pool, leftover_to_next, paid_off_before_turn}]`, `plan_scenario
  {debt_events[]}` (feed it to `--scenario-json` to see the cash side), `cash_check {…,
  affordable}` when a balance is known, `start` (the plan's first day), `opening`.

### Data
- `bdbd init [--force]` → `{db, schema_version}`.
- `bdbd config [KEY [VALUE]] [--unset]` → `{config, keys}` (or `{key, value}`). The only key:
  `weekly_spend`.
- `bdbd export` → the backup `{bdbd_export: 1, schema_version, config, flows[], balances[],
  exported_at}`; `bdbd export -o FILE` writes that backup to FILE (never over the budget file
  itself) and returns `{written, flows}`. `bdbd import FILE [--replace]` (a file from `-o`, or a
  saved `bdbd export` envelope) → `{imported_flows}`. The whole backup is checked first and
  restored at once (flows, settings and balances), so a damaged one changes nothing. Without
  `--replace` it only restores into a budget with nothing in it (`db_not_empty` otherwise).
- `bdbd tidy` → `{month, actions[]}`. `bdbd sql "SELECT …" [--limit N]` → `{columns, rows,
  row_count, truncated}` (read-only). `bdbd guide` → this document.

## What-ifs

Flags on every question (repeat and combine them; nothing is saved):

```
--disable FLOW                --disable-tag TAG              --enable FLOW
--stop "FLOW@DATE"            --stop-tag "TAG@DATE"          (nothing after DATE)
--payoff "FLOW@DATE"          --extra-payment "FLOW:AMOUNT@DATE"
--settle "FLOW:PROCEEDS@DATE" (sell and clear a debt: proceeds go to it, you pay any shortfall)
--set-payment "FLOW:AMOUNT@DATE"                             --rate-change "FLOW:RATE@DATE"
--add-income "NAME:AMOUNT@DATE"                              --add-expense "NAME:AMOUNT@DATE"
--set-amount "FLOW:AMOUNT[@FROM]"
```

The amount is after the last `:` and the date after the last `@`, so names may contain spaces
and colons. Any DATE may be `?` (or `?+N`/`?-N` days): `bdbd earliest` searches for it, and every
other question needs `--on DATE` to pin it, so "find the date, then show me" is one flag.
"Sell the car" = `--settle "Car loan:29500@?" --stop-tag "car@?"`.

For anything else, `--scenario FILE` (or `-` for stdin) or `--scenario-json '{…}'`:

```json
{
  "name": "sell the car",
  "weekly_spend": "150",
  "disable": {"flows": ["Car insurance"], "tags": ["car"]},
  "enable": {"flows": ["Bus pass"]},
  "end": [{"flow": "Car insurance", "after": "2026-10-15"}, {"tag": "car", "after": "?"}],
  "amount_changes": [{"flow": "Rent", "amount": "1650", "from": "2027-01-01", "until": null}],
  "add_flows": [
    {"name": "Car sale", "kind": "income", "amount": "15000", "on": "2026-10-15"},
    {"name": "Bus pass", "kind": "expense", "amount": "90", "rrule": "FREQ=MONTHLY;BYMONTHDAY=1",
     "dtstart": "2026-11-01", "tags": ["transport"], "weekend": "next"},
    {"name": "New loan", "kind": "expense", "amount": "386.66", "rrule": "FREQ=MONTHLY",
     "dtstart": "2026-11-01", "debt": {"balance": "20000", "balance_as_of": "2026-10-01",
     "annual_rate": "6%", "compounding": "monthly"}}
  ],
  "debt_events": [
    {"flow": "Car loan", "type": "settle", "date": "?", "amount": "29500"},
    {"flow": "Car loan", "type": "extra_payment", "date": "2026-10-15", "amount": "5000"},
    {"flow": "s:3", "type": "extra_payment", "date": "2027-06-01", "amount": "1000"}
  ]
}
```

Scenario JSON takes ISO dates (or `?`). `enable` runs before `disable`, and disable wins.
Disabling a debt's payment removes the debt and its payments (with a warning) unless a
`payoff`/`settle` event closes it. Ending a debt's payments without closing it warns: the
balance stays and keeps accruing. Added flows are keyed `s:1`, `s:2`… Outputs list what was
applied under `scenario_applied`.

## Automatic tidy-up

Before every command, anything that only concerns months before the current one goes: past
one-offs, flows whose last date has passed, debts paid off before this month (with their
payment), balances recorded before this month (the newest is kept), and unused tags. A debt
whose balance date is in an earlier month is rolled forward to the end of last month (payments
and interest applied, older events dropped). What happened is reported under `tidied`.
`--no-tidy` skips it.

## Recipes

**"Where do I stand?"** `bdbd overview`: report `balance.balance`, `spare_balance` (naming
`spare.next_income`), `low_point` and the warnings.

**"How much will I have on Dec 12?"**
```
bdbd project --until 2026-12-12 --select spare_balance,ending_balance,spare,opening
```
Report `spare_balance`, naming `spare.committed_before_next_income`. If `opening.source` is
`carried` from an old date or `none`, ask for the current balance and record it with
`bdbd balance AMOUNT`.

**"What will I spend on the car by Dec 12?"** `bdbd tags` (find the tag), then
`bdbd spend car --until 2026-12-12 --select total,by_flow`.

**"What does the car cost a month?"** `bdbd summary --tag car --select by_tag,by_flow`.

**"When can I book a $550 flight without dropping below $1,000?"**
```
bdbd earliest --floor 1000 --add-expense "Flight:550@?" --select status,date,result
bdbd project --on DATE --add-expense "Flight:550@?" --until +2m      # the detail
```

**"How fast can I clear my debts with $300 a month more?"**
`bdbd plan --extra 300 --select debt_free_on,months,interest_saved,steps,cash_check`;
run it with `--strategy snowball` too when asked to compare.

**"If I sell the car for $15,000 on Oct 15, when am I better off?"**
`bdbd breakeven --settle "Car loan:15000@2026-10-15" --stop-tag "car@2026-10-15"`.

**Keeping it current**: `add`, `edit`, `rm`, `pause`, `debt set`, `debt extra` and friends. After a
change, read it back with `bdbd show NAME` and confirm it to the person.

## Database

One sqlite file. Money columns are integer cents, rates are decimal strings, dates ISO text.

```sql
CREATE TABLE flow (id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
  kind TEXT NOT NULL,                 -- income | expense
  amount_cents INTEGER NOT NULL, rrule TEXT, dtstart TEXT NOT NULL, until TEXT,
  active INTEGER NOT NULL DEFAULT 1, notes TEXT, created_at TEXT, updated_at TEXT,
  weekend TEXT NOT NULL DEFAULT 'none');  -- none | next | previous
CREATE TABLE tag (id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE);
CREATE TABLE flow_tag (flow_id INTEGER, tag_id INTEGER, PRIMARY KEY (flow_id, tag_id));
CREATE TABLE debt (flow_id INTEGER PRIMARY KEY, original_principal_cents INTEGER,
  balance_cents INTEGER NOT NULL, balance_as_of TEXT NOT NULL, annual_rate TEXT NOT NULL,
  compounding TEXT NOT NULL, day_count TEXT NOT NULL, capitalize_interest INTEGER NOT NULL,
  payment_mode TEXT NOT NULL, payment_pct TEXT, posting_day INTEGER);
CREATE TABLE debt_event (id INTEGER PRIMARY KEY, flow_id INTEGER NOT NULL, date TEXT NOT NULL,
  type TEXT NOT NULL,  -- rate_change | balance_adjustment | extra_payment | payment_change | payoff
  rate TEXT, amount_cents INTEGER, notes TEXT);
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE bdbd_balance (id INTEGER PRIMARY KEY, as_of TEXT NOT NULL UNIQUE,
  amount_cents INTEGER NOT NULL, recorded_at TEXT NOT NULL);  -- made on first use
```

## Error codes

`usage`, `db_not_found`, `db_exists`, `db_not_empty`, `db_newer_than_cli`, `unknown_flow`,
`duplicate_flow`, `invalid_name`, `invalid_amount`, `negative_amount`, `invalid_rate`,
`invalid_date`, `invalid_schedule`, `invalid_rrule`, `invalid_tag`, `unknown_tag`,
`duplicate_tag`, `unknown_term`, `unknown_config`, `debt_requires_expense`, `no_debt`,
`invalid_debt`, `invalid_event`, `unknown_event`, `invalid_scenario`, `scenario_unknown_flow`,
`scenario_no_debt`, `scenario_not_found`, `select_not_found`, `readonly_sql`, `sql_error`,
`invalid_import`, `file_not_found`, `file_error`, `integrity`, `db_busy` (another program has
the file locked: try again), `db_readonly`, `db_unreadable`, `internal` (a bug: please report
it), `no_terminal` (`bdbd` alone, outside a terminal: run `bdbd overview` instead).
