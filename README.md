<h1 align="center">bdbd</h1>

<p align="center">
  <b>A beautiful budget in your terminal.</b><br>
  Your incomes, bills and debts, projected day by day, so you always know what's spare until payday.
</p>

<p align="center">
  <a href="https://github.com/adamtrain/bdbd/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/adamtrain/bdbd/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.13+" src="https://img.shields.io/badge/python-3.13%2B-3776ab?logo=python&logoColor=white">
  <a href="https://github.com/astral-sh/uv"><img alt="uv" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json"></a>
  <a href="https://github.com/astral-sh/ruff"><img alt="Ruff" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json"></a>
  <a href="LICENSE"><img alt="License: CC0-1.0" src="https://img.shields.io/badge/license-CC0--1.0-lightgrey"></a>
</p>

<p align="center">
  <img src="docs/overview.svg" width="860" alt="The bdbd app on its Overview. Tabs across the top for Overview, Paydays, Calendar, Forecast, Budget, Debts and What if. A 'Where you stand' panel shows a balance of $4,070.00 estimated from $4,120.00 recorded on Sep 22, $1,595.00 spare until the $2,650.00 paycheck on Fri Oct 2, and the lowest balance ahead, $1,595.00 on Thu Oct 1, with +$5,760.07 a month coming in against $3,378.09 of bills and $760.94 of everyday spending. Below it, a column chart of the balance over the next 90 days stepping up at each payday, then what's coming up with the balance after each item, and three debts with their balances, rates, progress bars and payoff dates.">
</p>

## Why bdbd

- **Where you stand, at a glance.** Run `bdbd` and see your balance, the money that's spare until
  payday, the lowest your balance will get, what's coming up and when each debt is gone.
- **What each paycheck leaves you.** On payday, Paydays says what's free to spend or save once
  the bills due before the next paycheck (and your everyday spending) are covered, and charts
  every paycheck a year ahead, so the tight ones stand out.
- **Seven views, a keypress apart.** Overview, Paydays, Calendar, Forecast, Budget, Debts and
  What if sit on `1` to `7`. Enter on anything opens it, and `?` lists the keys that work where
  you are.
- **Say it the way you'd say it.** Type "every 2 weeks on fri" or "yearly on the 3rd tuesday of
  november" and the form shows the next few dates as you type. Dates can be `fri`, `oct 15`,
  `+3w` or `eom`.
- **Your balance, carried forward.** Press `b` whenever you check your account. bdbd carries that
  balance through your budget day by day, and when you update it, it tells you how far reality
  has drifted from the plan.
- **Real debt math.** Simple, daily, monthly and continuous interest, rate changes, extra
  payments and payoffs, with amortization tables checked to the cent. No floats anywhere.
- **What-ifs that change nothing.** Sell the car, book a trip, pay a card off early: the What if
  view says whether it catches up, finds the earliest date that works, and every other view can
  show your budget with it. Nothing is saved until you make the change for real.
- **Built for agents too.** Every `bdbd COMMAND` prints one JSON object, with `--select` to keep
  it small and `bdbd guide` documenting every field.
- **One file, all yours.** Everything lives in a single sqlite file on your machine. No
  account, no server, no sync. The app reloads when something else changes the file.

## Install

You'll need [uv](https://docs.astral.sh/uv/).

```sh
uv tool install git+https://github.com/adamtrain/bdbd
bdbd
```

That puts `bdbd` on your `PATH`, and the first time you run it, it offers to create an empty
budget at `~/.config/bdbd/budget.sqlite`. To use a budget somewhere else, point `BDBD_DB` at it
(or pass `--db PATH`):

```sh
export BDBD_DB=~/Documents/budget.sqlite
```

To hack on bdbd, clone the repo and install it in editable mode, so your changes take effect
right away:

```sh
git clone https://github.com/adamtrain/bdbd && cd bdbd
uv tool install --editable .
```

## Getting started

1. Press `a` to add your paycheck: its amount and when it comes ("every 2 weeks on fri"). The
   first thing you add is money in; after that, `a` adds money out. Your paycheck is your
   payday: each one starts a pay cycle.
2. Press `a` for each bill: rent, insurance, subscriptions. For a loan or a card, switch on
   **Loan or card** and give what's owed and the rate.
3. Press `,` for settings and set your everyday spending: groceries and the like, spread over
   each day.
4. Press `b` and type what's in your account right now.

The Overview then shows where that leaves you.

<p align="center">
  <img src="docs/add.svg" width="760" alt="The Add an expense form over the Overview. Netflix at 15.49, which the form reads as −$15.49 each time, 'monthly on the 12th', which it reads back as 'Monthly on the 12th, next Mon Oct 12, Thu Nov 12, Sat Dec 12'. Optional Starts, Ends, Weekends, Tags and Notes fields follow, a Loan or card switch set to No, and at the bottom the monthly net going from +$1,621.04 to +$1,605.55, with Cancel and Add buttons.">
</p>

## The views

**Paydays** (`2`) takes one pay cycle at a time, from a payday to the day before the next: the
paycheck, the bills that land in the cycle, and what's free to spend or save once they and your
everyday spending are paid. A chart shows what's left of every paycheck for the next 12 months:
green for what's free, red for what's short, and a grey cap for what everyday spending takes.
Click a column to see its cycle. `v` leaves everyday spending out, and `p` picks which incomes
are paydays (your paycheck is; a refund isn't, though its money counts in the cycle it lands in).

<p align="center">
  <img src="docs/paydays.svg" width="860" alt="The Paydays view on the pay cycle from Fri Oct 2 to Thu Oct 15, in 8 days: $1,606.24 free to spend or save after bills and everyday spending, from the +$2,650.00 Paycheck, less −$693.76 of bills (5 bills, the Car loan the biggest) and −$350.00 of everyday spending for 14 days at $175/week, or $1,956.24 not counting everyday spending. Under it, a chart of what's left of each paycheck for the next 12 months, a column per pay cycle: green columns rising from zero with a grey cap for everyday spending, alternating with short red ones hanging below zero in the cycles that pay the rent, the Oct 2 column lit and marked on the axis. Below, every pay cycle with its bills and what's left, green where the paycheck covers them and red where it falls short; beside it, the cycle's paycheck and bills with what's left of the paycheck after each.">
</p>

**Calendar** (`3`) lays a month out day by day: what lands on each day (paychecks in green, debt
payments in purple) and the balance at the end of every day from today on. Beside it, the
selected day in full, and the month's money in and out.

<p align="center">
  <img src="docs/calendar.svg" width="860" alt="October 2026 as a Monday-first grid. Each day shows its net change and what lands on it, like Rent −2,150 on the 1st and Paycheck +2,650 on the 2nd, 16th and 30th, with the balance at the end of the day at the bottom of each cell. Thu Oct 1 is selected: at the end of the day $1,595.00, all of it spare before the Paycheck on Fri Oct 2. Below, October starts at $3,770.00, brings in +$9,190.00 against −$3,584.76 out and −$775.00 of everyday spending, ends at $8,600.24 and is lowest on Thu Oct 1 at $1,595.00.">
</p>

**Forecast** (`4`) runs your budget forward, from a month to five years or to any day you name:
where you end up, what's spare then, the lowest point on the way, what the debts cost, and
every transaction or a month-by-month table.

<p align="center">
  <img src="docs/forecast.svg" width="860" alt="A one-year forecast from Thu Sep 24 to Fri Sep 24, 2027: it starts at $4,070.00, ends at $24,307.88 with $24,007.88 spare until the Paycheck on Fri Oct 1, 2027, and is lowest at $1,595.00 on Thu Oct 1. Money in is +$70,140.00 and money out −$49,902.12, with $2,302.02 of interest paid and $29,260.57 of debt left. A horizon picker offers 1m, 3m, 6m, 1y, 2y and 5y. Below, a column chart of the balance at the end of each day climbing over the year, and a list of transactions with the balance after each.">
</p>

**Budget** (`5`) is every flow with its schedule, its next date and what it costs a month, or
the same money by tag. The card beside the list says everything about the one you're on, and
`e`, `space` and `x` edit, pause and delete it.

<p align="center">
  <img src="docs/budget.svg" width="860" alt="Each month +$5,760.07 comes in and −$3,378.09 goes to bills and −$760.94 to everyday spending, leaving +$1,621.04. Below, the two incomes and thirteen expenses with their amounts, schedules, next dates and monthly cost; debts carry a purple diamond and flows whose weekend dates move to Monday say so. The Car loan is selected, and its card shows −$412.37 monthly on the 5th, −$4,948.44 over the next 12 months, the next three dates, and its loan: $14,907.56 owed at 6.49% simple interest, paid off Feb 2030 after 41 more payments, $1,667.92 of interest to go and $16,575.48 in all.">
</p>

**Debts** (`6`) is what you owe and when it's gone: each debt's balance, rate and payoff date,
its terms and recorded events, and every payment to come. `r` records an extra payment, a new
rate or a payoff.

<p align="center">
  <img src="docs/debts.svg" width="860" alt="What you owe across 3 debts: $36,538.89 today, $798.37 a month in payments, $6,441.09 of interest to go if you keep paying as scheduled, and debt-free in Aug 2034. The debts, soonest paid off first: the Credit card, $3,226.30 at 22.99%, paid off Dec 2028; the Car loan, $14,907.56 at 6.49%, Feb 2030; the Student loan, $18,405.03 at 4.99%, Aug 2034. The Credit card is selected: $150.00 monthly on the 25th, paid off Dec 25, 2028 after 28 payments, with $888.07 of interest to go of $914.37 over its whole schedule, with a chart of its balance falling to zero, and below, every payment to come with its interest, principal and balance.">
</p>

`p` works out a payoff plan: an extra amount each month, one debt at a time, each freed payment
rolling into the next.

<p align="center">
  <img src="docs/plan.svg" width="860" alt="The Payoff plan dialog over the Debts view. With $200 extra a month, highest rate first (Credit card, then Car loan, then Student loan), starting today: debt-free in Jan 2030, 4 years 7 months sooner than Aug 2034, paying $998.37 a month instead of $798.37 and saving $2,840.57 of interest, with the balance never lower than $1,395.00, so the plan fits the budget. A timeline shows each debt paid off well before a dotted line marking its scheduled end, above a table of the payoff order, and a button to try it in What if.">
</p>

**What if** (`7`) tries changes without saving them: sell something and clear its loan, add a
one-off, stop a bill, change a payment or a rate. It compares the result with your budget as
it is, or, when a date is `?`, finds the earliest one that keeps your balance above a floor.
While it's on, every other view shows your budget with it, and `w` turns it off.

<p align="center">
  <img src="docs/whatif.svg" width="860" alt="What if, with two changes on: sell the Car loan for $13,000 on Nov 1, and stop everything tagged car after Nov 1. The Car loan's card says it is paid off in Nov 2026 instead of Feb 2030, saving $1,566.47 of interest. A green CATCHES UP badge says the what-if catches up with the budget as it is on Fri Jan 15, and over the next 10 years ends +$31,998.47 ahead, after being furthest behind by −$1,596.64 on Sun Nov 1. A chart of the difference each day dips below zero in amber at first, then climbs in green, above a month-by-month table of both balances and the difference.">
</p>

## Keys

| | |
| --- | --- |
| `1`–`7` | Overview, Paydays, Calendar, Forecast, Budget, Debts, What if |
| `a` | Add: a flow, or whatever the view adds (a debt, a what-if change) |
| `b` | Record your balance |
| `enter` | Open what's selected: a flow's card, a day's items, a month |
| `w` | The what-if on or off |
| `ctrl+p` or `:` | Commands, and any flow by name |
| `,` | Settings: everyday spending, where the budget lives, backups |
| `?` | Help: the keys that work where you are |
| `q` | Quit (bdbd asks first if a what-if would be lost) |

Lists move with the arrow keys, and the mouse works too: click to select, double-click to open,
and click a choice in a panel's border (like a forecast's `1y`) to pick it.

## Reading the numbers

| | |
| --- | --- |
| **Balance** | Cash on hand before today's scheduled items. `b` records it; everything starts from the latest one, carried forward day by day. |
| **Spare** | Your balance plus everything that comes and goes before your next payday: bills and everyday spending out, any other money (a refund) in on its day. It's what you'll have left the day before your paycheck. |
| **Everyday spending** | Groceries and incidentals, charged a little each day in every projection. Set it in settings (`,`). |
| **Lowest** | The lowest end-of-day balance ahead: the day to watch. |
| **Free to spend or save** | What's left of a paycheck once the bills in its pay cycle (payday to the day before the next) and its everyday spending are paid. |
| **Next 12 months** | What a flow really brings in or costs over the coming year, on its dates, so an end date or a loan's payoff cuts it short. |
| **Interest to go** | What a debt will still cost from today, so what's owed plus the interest to go is everything left to pay. |
| **◆** | A debt payment. Its interest and principal split comes from the loan's own terms. |
| **→Mon** | This flow's weekend dates move to Monday, the way an ACH pull does. |
| **↳** | Only in the what-if: it isn't in your budget. |

Balances turn amber below one week of everyday spending, and red below zero.

## For agents and scripts

Every command prints one JSON envelope on stdout. Nothing ever prompts, errors come back as
JSON too, and `--select` keeps only the fields you ask for:

```sh
$ bdbd project --until dec 12 --select spare_balance,spare.next_payday,opening.source
{"ok":true,"command":"project","data":{"spare_balance":"8480.72","spare.next_payday":{"date":"2026-12-25","name":"Paycheck","amount":"2650.00"},"opening.source":"carried"},"warnings":[]}
```

`bdbd overview` is the Overview's numbers, and the other commands cover everything the app does:
`add`, `edit`, `rm`, `pause`, `balance`, `debt …`, `upcoming`, `paydays`, `cal`, `project`,
`compare`, `earliest`, `plan`, `export`, `import` and more. Every question takes the same what-if
flags (`--settle "Car loan:13000@nov 1"`, `--add-expense "Flight:650@?"`), and nothing they
describe is saved. Money is a string with two decimals, dates are ISO, and `warnings` should always be
passed on to the person. `bdbd guide` returns the full reference, with every command's fields
and recipes for common questions, which is the one thing an agent should read first.

## How it works

bdbd simulates your budget one day at a time. Each flow has an amount and an
[RFC 5545](https://datatracker.ietf.org/doc/html/rfc5545) recurrence rule (the English you type
is translated into one and back), and debts carry their balance, rate, compounding method and
day count. Every day, interest accrues on each debt, then the day's incomes, loan payments,
extra payments and bills land in a fixed order, then a day's share of everyday spending. Money
is integer cents and interest is an exact decimal, so a 30-year schedule doesn't drift by a
cent. Only real payments are rounded.

What-ifs are layered on top of the stored budget and never written down. When bdbd opens (and
before each command), it tidies away what only concerns past months: one-offs that have
happened, flows that have ended, debts paid off, and loan balances rolled forward to last
month's end.

## Development

```sh
uv sync                         # set up the environment
uv run pytest                   # run the tests (the app's own are in tests/tui)
uv run ruff check . && uv run ruff format .
uv run ty check                 # type-check
uv run scripts/screenshots.py   # regenerate docs/*.svg
```

The app is built on [Textual](https://textual.textualize.io), and its tests drive it the way a
person would, then check the result against the JSON commands. `scripts/tui_shot.py` saves any
screen as an SVG (and a PNG on macOS) after pressing the keys you give it. The screenshots come
from a made-up household in `tests/sample.py`; none of those numbers are anyone's real budget.

```
src/bdbd/
├── cli.py      # every command, as one JSON envelope; bare `bdbd` opens the app
├── tui/        # the app: views, forms, dialogs, and the session they share
├── budget.py   # opening a budget, the remembered balance, what-if flags
├── ask.py      # the questions the app and the commands share
├── backup.py   # export and import, for the app and the commands alike
├── words.py    # English schedules and dates, both ways
├── agent.py    # JSON shapes and --select
├── guide.md    # the full reference (bdbd guide)
├── core/       # storage, the daily engine, debt math, scenarios and queries
└── ui/         # the theme, charts and wording the app draws with
```

## License

[CC0 1.0](LICENSE). bdbd is dedicated to the public domain.
