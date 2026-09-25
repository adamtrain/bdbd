"""Colors, glyphs and number formatting shared by every view."""

from __future__ import annotations

import os
from decimal import ROUND_HALF_UP, Decimal

from rich import box
from rich.console import Console
from rich.rule import Rule
from rich.text import Text

MAX_WIDTH = 110

ACCENT = "#7c83f7"
GREEN = "#1fbf8f"
AMBER = "#eb9a12"
RED = "#f2506e"
PURPLE = "#a871f7"
CYAN = "#56b6c2"
FAINT = "grey42"
DARK = "#111111"

MINUS = "\u2212"  # a real minus sign, as wide as "+"
DOT = "·"
ARROW = "→"
PANEL_BOX = box.ROUNDED

out = Console(highlight=False)
err = Console(stderr=True, highlight=False)


BACKGROUND = (16, 18, 25)  # what muted colors fade toward (the screenshots' background too)


def muted(color: str, amount: float = 0.5) -> str:
    """A hex color faded toward the background, for the body of chart columns."""
    if not color.startswith("#"):
        return color
    rgb = [int(color[i : i + 2], 16) for i in (1, 3, 5)]
    mixed = [round(bg + (c - bg) * amount) for c, bg in zip(rgb, BACKGROUND, strict=True)]
    return "#" + "".join(f"{v:02x}" for v in mixed)


def width(console: Console) -> int:
    return min(console.width, MAX_WIDTH)


def currency() -> str:
    return os.environ.get("BDBD_CURRENCY", "$")


# ── Numbers ───────────────────────────────────────────────────────────────────


def dec(cents: int) -> Decimal:
    return Decimal(cents).scaleb(-2)


def money(cents: int, *, sign: bool = False, symbol: bool = True) -> str:
    """1234567 -> '$12,345.67'; sign=True adds '+' to positives. Negatives use a real minus."""
    body = f"{currency() if symbol else ''}{dec(abs(cents)):,.2f}"
    if cents < 0:
        return MINUS + body
    return ("+" + body) if sign and cents > 0 else body


def money_dec(value: Decimal | str, *, sign: bool = False) -> str:
    d = Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return money(int(d.scaleb(2)), sign=sign)


def cents_of(value: Decimal | str) -> int:
    """'1234.56' (as the core returns money) -> 123456."""
    return int(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP).scaleb(2))


def compact(cents: int) -> str:
    """Axis labels: '$850', '$1.2k', '$12k', '$1.5M'."""
    v = abs(Decimal(cents).scaleb(-2))
    sym = currency()
    if v < 1000:
        body = f"{sym}{v:,.0f}"
    elif v < 10_000:
        body = f"{sym}{(v / 1000).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}k"
    elif v < 1_000_000:
        body = f"{sym}{(v / 1000).quantize(Decimal('1'), rounding=ROUND_HALF_UP)}k"
    else:
        body = f"{sym}{(v / 1_000_000).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}M"
    body = body.replace(".0k", "k").replace(".0M", "M")
    return (MINUS + body) if cents < 0 else body


def pct(rate: Decimal | str) -> str:
    """Decimal('0.0525') -> '5.25%', Decimal('0.06') -> '6%'."""
    value = (Decimal(rate) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    text = f"{value:f}".rstrip("0").rstrip(".")
    return f"{text}%"


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n:,} {word if n == 1 else (many or word + 's')}"


# ── Styled pieces ─────────────────────────────────────────────────────────────


def balance_color(cents: int, low: int = 0) -> str:
    """Red below zero, amber below `low`, green otherwise."""
    if cents < 0:
        return RED
    if cents < low:
        return AMBER
    return GREEN


def amount(cents: int, *, kind: str = "expense", style: str = "") -> Text:
    """A signed flow amount: '+$2,500.00' in green for money in, '-$120.00' for money out."""
    if kind == "income" or (cents > 0 and kind == "in"):
        return Text(money(abs(cents), sign=True), style=f"{GREEN} {style}".strip())
    return Text(money(-abs(cents)), style=style)


def badge(label: str, color: str = ACCENT) -> Text:
    return Text(f" {label} ", style=f"bold {DARK} on {color}")


def chip(label: str, color: str) -> Text:
    return Text(label, style=color)


def section(title: str) -> Rule:
    return Rule(Text(f" {title} ", style=FAINT), align="left", style=FAINT, characters="─")


def bar(value: int, total: int, cells: int, color: str) -> Text:
    """A share bar: '━━━━━───────'."""
    filled = 0 if total <= 0 else round(max(0, min(value, total)) * cells / total)
    if value > 0 and filled == 0:
        filled = 1
    return Text.assemble(("━" * filled, color), ("─" * (cells - filled), FAINT))


def hint(*commands: str) -> Text:
    """'Try bdbd add · bdbd cal' in the accent color."""
    text = Text("Try ", style=FAINT)
    for i, cmd in enumerate(commands):
        if i:
            text.append(f" {DOT} ", style=FAINT)
        text.append(cmd, style=ACCENT)
    return text


def error(console: Console, message: str, *, hint: str | None = None) -> None:
    console.print(Text.assemble(("✗ ", f"bold {RED}"), (message, "bold")))
    if hint:
        console.print(Text.from_markup(f"  {hint}"))


def success(console: Console, message: Text | str) -> None:
    text = message if isinstance(message, Text) else Text.from_markup(message)
    console.print(Text.assemble(("✓ ", f"bold {GREEN}"), text))


def note(console: Console, message: Text | str, glyph: str = "·") -> None:
    text = message if isinstance(message, Text) else Text.from_markup(message)
    console.print(Text.assemble((f"{glyph} ", FAINT), text))
