"""Automatic removal of data that only concerns months before the current one.

Runs before every command that opens the database (skip it with --no-tidy). Rules:
- a one-off flow dated before the current month is removed;
- a flow whose last occurrence (until, or COUNT) is before the current month is removed;
- a debt that is paid off before the current month is removed (with its payment flow);
- a debt whose balance date is before the current month is rolled forward: its balance becomes
  the simulated balance at the end of the previous month, and events before that are dropped;
- tags no longer used by any flow are removed.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from bdbd.core import engine, repo
from bdbd.core.dates import iso, month_start
from bdbd.core.models import EffectiveModel
from bdbd.core.money import cents_to_str, dec_to_str, q
from bdbd.core.recurrence import last_occurrence
from bdbd.core.scenario import build_effective_model


def run_cleanup(conn: sqlite3.Connection, today: date) -> list[str]:
    start = month_start(today)
    prev_end = start - timedelta(days=1)
    actions: list[str] = []
    for f in repo.list_flows(conn, include_inactive=True):
        last = last_occurrence(f.rrule, f.dtstart, f.until)
        if last is not None and last < start:
            what = "one-off" if f.rrule is None else "ended"
            repo.remove_flow(conn, int(f.id))
            actions.append(
                f"removed {what} {f.kind} {f.name!r} "
                f"(last occurrence {iso(last)}, before {iso(start)})"
            )
            continue
        d = f.debt
        if d is None or d.balance_as_of >= prev_end:
            continue
        # roll the debt forward to the end of the previous month
        model = build_effective_model([f], None, prev_end, include_inactive=True)
        model = EffectiveModel(flows=[x for x in model.flows if x.key == f.id])
        res = engine.run(model, as_of=prev_end, until=prev_end)
        summ = res.debts[f.id]
        if summ.paid_off_on is not None and summ.paid_off_on <= prev_end:
            repo.remove_flow(conn, int(f.id))
            actions.append(
                f"removed debt {f.name!r}: paid off on {iso(summ.paid_off_on)}, before {iso(start)}"
            )
            continue
        new_balance = int(q(summ.balance_at_until) * 100)
        conn.execute("BEGIN")
        try:
            conn.execute(
                "UPDATE debt SET balance_cents = ?, balance_as_of = ? WHERE flow_id = ?",
                (new_balance, iso(prev_end), f.id),
            )
            cur = conn.execute(
                "DELETE FROM debt_event WHERE flow_id = ? AND date <= ?", (f.id, iso(prev_end))
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        actions.append(
            f"rolled debt {f.name!r} forward: balance {cents_to_str(d.balance_cents)} as of "
            f"{iso(d.balance_as_of)} -> {dec_to_str(summ.balance_at_until)} as of {iso(prev_end)}"
            + (f", dropped {cur.rowcount} past event(s)" if cur.rowcount else "")
        )
    before = {t.name for t in repo.list_tags(conn)}
    repo.prune_unused_tags(conn)
    gone = before - {t.name for t in repo.list_tags(conn)}
    if gone:
        actions.append("removed unused tag(s): " + ", ".join(sorted(gone)))
    return actions
