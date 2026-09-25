"""Command-line shortcuts for the most common what-ifs, merged into a scenario dict.

    --disable FLOW            --disable-tag TAG          --enable FLOW
    --stop FLOW@DATE          --stop-tag TAG@DATE
    --payoff FLOW@DATE        --extra-payment FLOW:AMOUNT@DATE
    --settle FLOW:PROCEEDS@DATE
    --set-payment FLOW:AMOUNT@DATE                      --rate-change FLOW:RATE@DATE
    --add-income NAME:AMOUNT@DATE                       --add-expense NAME:AMOUNT@DATE
    --set-amount FLOW:AMOUNT[@FROM]

`FLOW`/`NAME` may contain spaces and colons (quote them); the amount is taken after the LAST ':'
and the date after the LAST '@'. Any DATE may be the placeholder `?` (or `?+N` / `?-N` days),
resolved by `bdbd earliest` or `--on DATE`.
"""

from __future__ import annotations

from typing import Any

from bdbd.core.errors import CashError


def _split_date(spec: str, flag: str, required: bool = True) -> tuple[str, str | None]:
    if "@" in spec:
        head, _, d = spec.rpartition("@")
        return head.strip(), d.strip()
    if required:
        raise CashError(f"{flag} expects ...@DATE, got {spec!r}", "usage")
    return spec.strip(), None


def _split_amount(spec: str, flag: str) -> tuple[str, str]:
    if ":" not in spec:
        raise CashError(f"{flag} expects NAME:AMOUNT..., got {spec!r}", "usage")
    head, _, amount = spec.rpartition(":")
    if not head.strip() or not amount.strip():
        raise CashError(f"{flag} expects NAME:AMOUNT..., got {spec!r}", "usage")
    return head.strip(), amount.strip()


def scenario_from_flags(args) -> dict | None:
    """Build a scenario dict from shortcut flags; None when no shortcut was used."""
    sc: dict = {}

    def add(key, item):
        sc.setdefault(key, []).append(item)

    for flow in getattr(args, "disable", None) or []:
        sc.setdefault("disable", {}).setdefault("flows", []).append(flow)
    for tag in getattr(args, "disable_tag", None) or []:
        sc.setdefault("disable", {}).setdefault("tags", []).append(tag)
    for flow in getattr(args, "enable", None) or []:
        sc.setdefault("enable", {}).setdefault("flows", []).append(flow)
    for spec in getattr(args, "stop", None) or []:
        flow, d = _split_date(spec, "--stop")
        add("end", {"flow": flow, "after": d})
    for spec in getattr(args, "stop_tag", None) or []:
        tag, d = _split_date(spec, "--stop-tag")
        add("end", {"tag": tag, "after": d})
    for spec in getattr(args, "payoff", None) or []:
        flow, d = _split_date(spec, "--payoff")
        add("debt_events", {"flow": flow, "type": "payoff", "date": d})
    for spec in getattr(args, "settle", None) or []:
        head, d = _split_date(spec, "--settle")
        flow, amount = _split_amount(head, "--settle")
        add("debt_events", {"flow": flow, "type": "settle", "date": d, "amount": amount})
    for spec in getattr(args, "extra_payment", None) or []:
        head, d = _split_date(spec, "--extra-payment")
        flow, amount = _split_amount(head, "--extra-payment")
        add("debt_events", {"flow": flow, "type": "extra_payment", "date": d, "amount": amount})
    for spec in getattr(args, "set_payment", None) or []:
        head, d = _split_date(spec, "--set-payment")
        flow, amount = _split_amount(head, "--set-payment")
        add("debt_events", {"flow": flow, "type": "payment_change", "date": d, "amount": amount})
    for spec in getattr(args, "rate_change", None) or []:
        head, d = _split_date(spec, "--rate-change")
        flow, rate = _split_amount(head, "--rate-change")
        add("debt_events", {"flow": flow, "type": "rate_change", "date": d, "rate": rate})
    for kind, flag in (("income", "add_income"), ("expense", "add_expense")):
        for spec in getattr(args, flag, None) or []:
            head, d = _split_date(spec, "--" + flag.replace("_", "-"))
            name, amount = _split_amount(head, "--" + flag.replace("_", "-"))
            add("add_flows", {"name": name, "kind": kind, "amount": amount, "on": d})
    for spec in getattr(args, "set_amount", None) or []:
        head, d = _split_date(spec, "--set-amount", required=False)
        flow, amount = _split_amount(head, "--set-amount")
        item = {"flow": flow, "amount": amount}
        if d:
            item["from"] = d
        add("amount_changes", item)
    return sc or None


def merge_scenarios(base: dict | None, extra: dict | None) -> dict | None:
    """Combine a loaded scenario with shortcut flags (lists concatenate, extra wins on scalars)."""
    if base is None:
        return extra
    if extra is None:
        return base
    out: dict[str, Any] = {
        k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
        for k, v in base.items()
    }
    for k, v in extra.items():
        if isinstance(v, list):
            out.setdefault(k, []).extend(v)
        elif isinstance(v, dict):
            tgt: dict[str, Any] = out.setdefault(k, {})
            for kk, vv in v.items():
                tgt.setdefault(kk, []).extend(vv)
        else:
            out[k] = v
    return out


def add_shortcut_flags(p) -> None:
    g = p.add_argument_group("what-if shortcuts (combine freely; merged into --scenario)")
    g.add_argument("--disable", action="append", metavar="FLOW", help="drop a flow (repeatable)")
    g.add_argument(
        "--disable-tag", action="append", metavar="TAG", help="drop every flow with this tag"
    )
    g.add_argument("--enable", action="append", metavar="FLOW", help="re-enable an inactive flow")
    g.add_argument(
        "--stop", action="append", metavar="FLOW@DATE", help="no occurrences of FLOW after DATE"
    )
    g.add_argument(
        "--stop-tag",
        action="append",
        metavar="TAG@DATE",
        help="no occurrences of any flow tagged TAG after DATE",
    )
    g.add_argument(
        "--payoff", action="append", metavar="FLOW@DATE", help="pay a debt off in full on DATE"
    )
    g.add_argument(
        "--settle",
        action="append",
        metavar="FLOW:PROCEEDS@DATE",
        help="clear a debt on DATE using PROCEEDS (e.g. a sale price) toward it: the shortfall "
        "is paid from cash, a surplus is received, and the payments stop",
    )
    g.add_argument(
        "--extra-payment",
        action="append",
        metavar="FLOW:AMOUNT@DATE",
        help="one-off extra debt payment",
    )
    g.add_argument(
        "--set-payment",
        action="append",
        metavar="FLOW:AMOUNT@DATE",
        help="new scheduled payment from DATE",
    )
    g.add_argument(
        "--rate-change", action="append", metavar="FLOW:RATE@DATE", help="new annual rate from DATE"
    )
    g.add_argument(
        "--add-income", action="append", metavar="NAME:AMOUNT@DATE", help="one-off income on DATE"
    )
    g.add_argument(
        "--add-expense", action="append", metavar="NAME:AMOUNT@DATE", help="one-off expense on DATE"
    )
    g.add_argument(
        "--set-amount",
        action="append",
        metavar="FLOW:AMOUNT[@FROM]",
        help="change a flow's amount (from DATE)",
    )
