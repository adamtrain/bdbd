"""Terminal charts: balance columns, a date strip and a payoff timeline."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from rich.text import Text

from bdbd.ui.theme import ACCENT, AMBER, FAINT, GREEN, RED, compact, muted
from bdbd.words import MONTH_SHORT


@dataclass(frozen=True)
class Tick:
    column: int  # character cell, 0-based within the plot area
    label: str


def _x_ticks(days: Sequence[date], cells: int) -> list[Tick]:
    """Day labels for short spans, month names for longer ones, years for very long ones."""
    n = len(days)
    if n == 0:
        return []

    def col(i: int) -> int:
        return min(cells - 1, int(i * cells / n))

    span_days = (days[-1] - days[0]).days
    ticks: list[Tick] = []
    for i, d in enumerate(days):
        prev = days[i - 1] if i else d
        first = i == 0
        if span_days <= 45:
            if first or d.isocalendar()[:2] != prev.isocalendar()[:2]:
                ticks.append(Tick(col(i), f"{MONTH_SHORT[d.month]} {d.day}"))
        elif span_days <= 800:
            if first or (d.year, d.month) != (prev.year, prev.month):
                label = MONTH_SHORT[d.month]
                if span_days > 300 and (first or d.month == 1):
                    label += f" {d.year}"
                ticks.append(Tick(col(i), label))
        elif first or d.year != prev.year:
            ticks.append(Tick(col(i), str(d.year)))
    if len(ticks) > 1 and ticks[1].column <= ticks[0].column + len(ticks[0].label):
        ticks = ticks[1:]  # the first boundary says more than the start date
    placed: list[Tick] = []
    end = -1
    for t in ticks:
        if t.column > end and t.column + len(t.label) <= cells:
            placed.append(t)
            end = t.column + len(t.label)
    return placed


def strip(
    cells_in: Sequence[tuple[date, bool]], width: int, marker: date | None = None
) -> list[Text]:
    """One cell per candidate date (or group of dates): green = feasible, red = not."""
    if not cells_in:
        return []
    n = len(cells_in)
    cells = min(width, n)
    line = Text()
    marker_col = None
    for c in range(cells):
        a = int(c * n / cells)
        b = max(a + 1, int((c + 1) * n / cells))
        group = cells_in[a:b]
        ok = [x for _, x in group]
        color = GREEN if all(ok) else RED if not any(ok) else AMBER
        line.append("█", style=color)
        if marker is not None and any(d == marker for d, _ in group):
            marker_col = c
    lines = [line]
    if marker_col is not None:
        lines.append(Text(" " * marker_col + "▲", style=GREEN))
    ticks = _x_ticks([d for d, _ in cells_in], cells)
    tick_line = [" "] * cells
    for t in ticks:
        for k, ch in enumerate(t.label):
            tick_line[t.column + k] = ch
    lines.append(Text("".join(tick_line).rstrip(), style=FAINT))
    return lines


@dataclass(frozen=True)
class Span:
    label: str
    start: date
    end: date | None  # None = not within the horizon
    baseline_end: date | None = None
    color: str = ACCENT


def timeline(spans: Sequence[Span], start: date, end: date, width: int) -> list[Text]:
    """Gantt-style bars on a shared date axis.

    A faint tail shows how much later the baseline would have ended.
    """
    label_w = min(22, max(len(s.label) for s in spans)) if spans else 0
    cells = max(10, width - label_w - 2)
    total = max(1, (end - start).days)

    def col(d: date) -> int:
        return max(0, min(cells, round((d - start).days * cells / total)))

    lines: list[Text] = []
    for s in spans:
        line = Text()
        name = s.label if len(s.label) <= label_w else s.label[: label_w - 1] + "…"
        line.append(name.ljust(label_w) + "  ")
        a = col(s.start)
        b = col(s.end) if s.end else cells
        b = max(b, a + 1)
        c = col(s.baseline_end) if s.baseline_end else b
        line.append(" " * a)
        line.append("━" * (b - a), style=s.color if s.end else AMBER)
        if c > b:
            line.append("┄" * (c - b), style=FAINT)
        lines.append(line)
    days = [date.fromordinal(start.toordinal() + i) for i in range(total + 1)]
    tick_line = [" "] * cells
    for t in _x_ticks(days, cells):
        for k, ch in enumerate(t.label):
            if t.column + k < cells:
                tick_line[t.column + k] = ch
    lines.append(Text(" " * (label_w + 2) + "".join(tick_line).rstrip(), style=FAINT))
    return lines


_BLOCKS = " ▁▂▃▄▅▆▇█"


def balance_chart(
    series: Sequence[tuple[date, int]],
    width: int,
    height: int = 7,
    *,
    color: str = ACCENT,
    floor: int | None = None,
) -> list[Text]:
    """End-of-day balances as solid columns rising from zero (or falling below it).

    Each column shows the lowest balance of the days it covers, so every dip before a payday
    stays visible. Columns under zero (or under `floor`) are red.
    """
    if not series:
        return []
    values = [v for _, v in series]
    lo, hi = min(*values, 0), max(*values, 0)
    if floor is not None:
        lo, hi = min(lo, floor), max(hi, floor)
    if hi == lo:
        hi = lo + 100
    levels = height * 8

    def level(v: int) -> int:
        return round((v - lo) * levels / (hi - lo))

    zero = level(0)
    labels = {height - 1: compact(hi), 0: compact(lo)}
    if lo < 0 < hi:
        labels[min(height - 1, zero // 8)] = compact(0)  # zero says more than the extreme
    label_w = max(len(s) for s in labels.values())
    cells = max(8, width - label_w - 2)
    n = len(values)
    threshold = floor if floor is not None else 0
    columns: list[tuple[int, int, int, str]] = []  # from, to (eighths), cap row, color
    for c in range(cells):
        a = int(c * n / cells)
        b = max(a + 1, int((c + 1) * n / cells))
        v = min(values[a:b])
        top = level(v)
        start, end = (zero, top) if top >= zero else (top, zero)
        cap = (end - 1) // 8 if top >= zero else start // 8
        columns.append((start, end, cap, RED if v < threshold else color))
    lines: list[Text] = []
    for row in range(height - 1, -1, -1):
        base = row * 8
        line = Text()
        label = labels.get(row, "")
        line.append(label.rjust(label_w), style=FAINT)
        line.append(" ┤" if label else "  ", style=FAINT)
        for start, end, cap, c_color in columns:
            a, b = max(start, base), min(end, base + 8)
            style = c_color if row == cap else muted(c_color)
            if b - a <= 0:
                line.append(" ")
            elif b - a >= 8:
                line.append("█", style=style)
            elif b == base + 8 and a > base:  # fills the top of the cell: draw the gap reversed
                line.append(_BLOCKS[a - base], style=f"reverse {style}")
            else:  # sits on the cell's floor (or inside it: shown from the floor up)
                line.append(_BLOCKS[b - base], style=style)
        lines.append(line)
    days = [d for d, _ in series]
    tick_line = [" "] * cells
    for t in _x_ticks(days, cells):
        for k, ch in enumerate(t.label):
            tick_line[t.column + k] = ch
    lines.append(Text(" " * (label_w + 2) + "".join(tick_line).rstrip(), style=FAINT))
    return lines
