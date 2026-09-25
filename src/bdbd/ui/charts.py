"""Terminal charts: balance columns, a date strip, a payoff timeline and what's left of each
paycheck."""

from __future__ import annotations

import math
from collections import Counter
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
    """Day labels for short spans, month names for longer ones, years for very long ones.

    Labels are placed by importance (the start and every new year first, then quarters, then
    the other months), at least two cells apart, so a two-year chart keeps its years and its
    months land at a steady rhythm. Years alone need only one cell between them: they're
    digits, and rounding would otherwise drop one now and then from an even row.
    """
    n = len(days)
    if n == 0:
        return []

    def col(i: int) -> int:
        return min(cells - 1, int(i * cells / n))

    span_days = (days[-1] - days[0]).days
    ranked: list[tuple[int, Tick]] = []  # (importance, tick): lower goes first
    for i, d in enumerate(days):
        prev = days[i - 1] if i else d
        first = i == 0
        if span_days <= 45:
            if first or d.isocalendar()[:2] != prev.isocalendar()[:2]:
                ranked.append((0 if first else 1, Tick(col(i), f"{MONTH_SHORT[d.month]} {d.day}")))
        elif span_days <= 800:
            if first or (d.year, d.month) != (prev.year, prev.month):
                label = MONTH_SHORT[d.month]
                year = span_days > 300 and (first or d.month == 1)
                if year:
                    label += f" {d.year}"
                rank = 0 if year or first else 1 if d.month % 3 == 1 else 2 if d.month % 2 else 3
                ranked.append((rank, Tick(col(i), label)))
        elif first or d.year != prev.year:
            ranked.append((0 if first else 1 if d.year % 5 == 0 else 2, Tick(col(i), str(d.year))))
    if len(ranked) > 1:
        start, nxt = ranked[0][1], ranked[1][1]
        if nxt.column < start.column + len(start.label) + 2:
            # the first boundary says more than the start date; it keeps the start's year
            year = start.label.rsplit(" ", 1)[-1]
            label = nxt.label
            if span_days > 300 and year.isdigit() and not label.endswith(year):
                label = label if label.isdigit() else f"{label} {year}"
            ranked = [(0, Tick(nxt.column, label)), *ranked[2:]]
    gap = 1 if span_days > 800 else 2
    if 300 < span_days <= 800 and len(ranked) > 1:
        # 'Oct 2026' would crowd out a January close behind it: 'Oct … Jan 2027' says both
        first = ranked[0][1]
        jan = next((t for _, t in ranked[1:] if t.label.startswith("Jan ")), None)
        if jan is not None and jan.column < first.column + len(first.label) + gap:
            ranked[0] = (0, Tick(first.column, first.label.split(" ")[0]))
    placed: list[Tick] = []
    for _, t in sorted(ranked, key=lambda rt: (rt[0], rt[1].column)):
        end = t.column + len(t.label)
        if end > cells:
            continue
        if all(end + gap <= p.column or p.column + len(p.label) + gap <= t.column for p in placed):
            placed.append(t)
    return sorted(placed, key=lambda t: t.column)


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
    marker: date | None = None,
    below: str = RED,
) -> list[Text]:
    """End-of-day balances as solid columns rising from zero (or falling below it).

    Each column shows the lowest balance of the days it covers, so every dip before a payday
    stays visible. Columns under zero (or under `floor`) are drawn in `below` (red; the what-if
    difference uses amber for "behind"). A `marker` date lights up the column covering it at
    full strength and labels it on the axis ('▲ Dec 12').
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
    days = [d for d, _ in series]
    marked = days.index(marker) if marker is not None and marker in days else None
    lit: list[int] = []  # the columns covering the marker day
    columns: list[tuple[int, int, int, str]] = []  # from, to (eighths), cap row, color
    for c in range(cells):
        a = int(c * n / cells)
        b = max(a + 1, int((c + 1) * n / cells))
        v = min(values[a:b])
        top = level(v)
        start, end = (zero, top) if top >= zero else (top, zero)
        cap = (end - 1) // 8 if top >= zero else start // 8
        columns.append((start, end, cap, below if v < threshold else color))
        if marked is not None and a <= marked < b:
            lit.append(c)
    lines: list[Text] = []
    for row in range(height - 1, -1, -1):
        base = row * 8
        line = Text()
        label = labels.get(row, "")
        line.append(label.rjust(label_w), style=FAINT)
        line.append(" ┤" if label else "  ", style=FAINT)
        for c, (start, end, cap, c_color) in enumerate(columns):
            a, b = max(start, base), min(end, base + 8)
            style = c_color if row == cap or c in lit else muted(c_color)
            if b - a <= 0:
                line.append(" ")
            elif b - a >= 8:
                line.append("█", style=style)
            elif b == base + 8 and a > base:  # fills the top of the cell: draw the gap reversed
                line.append(_BLOCKS[a - base], style=f"reverse {style}")
            else:  # sits on the cell's floor (or inside it: shown from the floor up)
                line.append(_BLOCKS[b - base], style=style)
        lines.append(line)
    ticks = _x_ticks(days, cells)
    if lit and marker is not None:
        lines.append(_marked_axis(ticks, cells, label_w, lit, marker, columns[lit[0]][3]))
        return lines
    tick_line = [" "] * cells
    for t in ticks:
        for k, ch in enumerate(t.label):
            tick_line[t.column + k] = ch
    lines.append(Text(" " * (label_w + 2) + "".join(tick_line).rstrip(), style=FAINT))
    return lines


def _marked_axis(
    ticks: list[Tick], cells: int, label_w: int, lit: list[int], marker: date, color: str
) -> Text:
    """The axis row with '▲ Dec 12' under the marked column; ticks it would overlap give way."""
    col = lit[len(lit) // 2]
    text = f"{MONTH_SHORT[marker.month]} {marker.day}"
    label = f"▲ {text}"
    start = col if col + len(label) <= cells else max(0, col - len(label) + 1)
    if start != col:
        label = f"{text} ▲"
    end = start + len(label)
    cells_row: list[tuple[str, str]] = [(" ", FAINT)] * cells
    for t in ticks:
        if t.column + len(t.label) < start - 1 or t.column > end:
            for k, ch in enumerate(t.label):
                cells_row[t.column + k] = (ch, FAINT)
    for k, ch in enumerate(label[: cells - start]):
        cells_row[start + k] = (ch, f"bold {color}")
    line = Text(" " * (label_w + 2), style=FAINT)
    for ch, style in cells_row:
        line.append(ch, style=style)
    line.rstrip()
    return line


# ── What's left of each paycheck ──────────────────────────────────────────────

EVERYDAY = "#8a90a6"  # the grey of what the everyday spending allowance takes
_MAX_BAR = 8  # cells: a handful of pay cycles still get slim columns


@dataclass(frozen=True)
class PayBar:
    """One pay cycle in `pay_bars`."""

    day: date  # its payday
    cents: int  # what's left of the paycheck: up from zero, or down when it falls short
    cap: int = 0  # what the everyday allowance takes out of it: a grey cap over what's left


@dataclass(frozen=True)
class _PayLayout:
    labels: dict[int, str]  # row (0 is the bottom) -> its axis label
    label_w: int
    cells: int  # the plot's width
    columns: list[tuple[int, int, int]]  # first cell, width, the bar drawn there
    groups: list[list[int]]  # the bars each column stands for (several when they don't fit)
    zero: int  # the zero line, in eighths from the bottom (always between two rows)
    scale: float  # eighths a cent


def _pay_layout(bars: Sequence[PayBar], width: int, height: int) -> _PayLayout:
    hi = max((max(b.cents + b.cap, b.cents, 0) for b in bars), default=0)
    lo = min((min(b.cents, 0) for b in bars), default=0)
    if hi == lo:
        hi = 100  # all zero: a dollar of room
    # the rows above the zero line and below it, in proportion; each side keeps one
    above = height if lo == 0 else 0 if hi == 0 else round(height * hi / (hi - lo))
    if lo < 0 < hi:
        above = min(height - 1, max(1, above))
    below = height - above
    scale = min(above * 8 / hi if hi else math.inf, below * 8 / -lo if lo else math.inf)
    labels = {height - 1: compact(hi), 0: compact(lo)}
    if lo < 0 < hi:
        labels[below] = compact(0)  # on the row the columns rise from
    label_w = max(len(s) for s in labels.values())
    cells = max(8, width - label_w - 2)
    n = len(bars)
    if n <= cells:
        w = max(1, min(_MAX_BAR, cells // n - 1))
        groups = [[i] for i in range(n)]
        starts = [i * cells // n for i in range(n)]
    else:  # more cycles than cells: a column stands for a few, and shows the tightest
        w = 1
        firsts = [c * n // cells for c in range(cells)]
        groups = [list(range(a, max(a + 1, (c + 1) * n // cells))) for c, a in enumerate(firsts)]
        starts = list(range(cells))
    columns = [
        (start, w, min(group, key=lambda i: bars[i].cents))
        for start, group in zip(starts, groups, strict=True)
    ]
    return _PayLayout(labels, label_w, cells, columns, groups, below * 8, scale)


def _cell(eighths: list[str | None]) -> tuple[str, str]:
    """A cell's eighths (the bottom first) as a glyph and a style: one color, or the two that
    fit best, split at an eighth."""
    first = eighths[0]
    if all(e == first for e in eighths):
        return ("█", first) if first else (" ", "")
    best: tuple[int, int, str | None, str | None] | None = None
    for k in range(1, 8):
        low = Counter(eighths[:k]).most_common(1)[0][0]
        high = Counter(eighths[k:]).most_common(1)[0][0]
        misses = sum(e != low for e in eighths[:k]) + sum(e != high for e in eighths[k:])
        if best is None or misses < best[0]:
            best = (misses, k, low, high)
    assert best is not None
    _, k, low, high = best
    if low == high:
        return ("█", low) if low else (" ", "")
    if low is None:  # color at the top of the cell: draw the gap under it, reversed
        return _BLOCKS[k], f"reverse {high}"
    if high is None:
        return _BLOCKS[k], low
    return _BLOCKS[k], f"{low} on {high}"


def pay_bars(
    bars: Sequence[PayBar], width: int, height: int = 6, *, selected: date | None = None
) -> list[Text]:
    """What's left of each paycheck: a column per pay cycle around a zero line.

    Green rises for what's free to spend or save and red falls for what's short; a grey cap on
    top is what the everyday spending allowance takes (without it, that would be free too).
    Every column is muted but the `selected` cycle's, which the axis labels ('▲ Oct 2'). When
    the cycles outnumber the cells, a column stands for a few and shows the tightest.
    """
    if not bars:
        return []
    layout = _pay_layout(bars, width, height)
    zero, scale = layout.zero, layout.scale

    def level(cents: int) -> int:
        eighths = round(cents * scale)
        if cents and not eighths:  # a cent short still shows
            eighths = 1 if cents > 0 else -1
        return zero + eighths

    marked = next((i for i, b in enumerate(bars) if b.day == selected), None)
    plot: list[tuple[int, int, list[tuple[int, int, str]]]] = []
    lit: list[int] = []
    for (start, w, i), group in zip(layout.columns, layout.groups, strict=True):
        bar = bars[i]
        on = marked in group
        color = GREEN if bar.cents >= 0 else RED
        segments = []
        left, top = max(bar.cents, 0), max(bar.cents + bar.cap, 0)
        if top > left:
            segments.append((level(left), level(top), EVERYDAY if on else muted(EVERYDAY, 0.6)))
        a, b = sorted((zero, level(bar.cents)))
        segments.append((a, b, color if on else muted(color, 0.55)))
        plot.append((start, w, segments))
        if on:
            lit += range(start, start + w)
    lines: list[Text] = []
    for row in range(height - 1, -1, -1):
        base = row * 8
        label = layout.labels.get(row, "")
        line = Text(label.rjust(layout.label_w), style=FAINT)
        line.append(" ┤" if label else "  ", style=FAINT)
        at = 0
        for start, w, segments in plot:
            eighths: list[str | None] = [None] * 8
            for a, b, color in segments:
                for lv in range(max(a, base), min(b, base + 8)):
                    eighths[lv - base] = color
            glyph, style = _cell(eighths)
            line.append(" " * (start - at))
            line.append(glyph * w, style=style)
            at = start + w
        lines.append(line)
    days = [b.day for b in bars]
    ticks = _x_ticks(days, layout.cells)
    if lit and selected is not None and marked is not None:
        color = GREEN if bars[marked].cents >= 0 else RED
        lines.append(_marked_axis(ticks, layout.cells, layout.label_w, lit, selected, color))
        return lines
    tick_line = [" "] * layout.cells
    for t in ticks:
        for k, ch in enumerate(t.label):
            tick_line[t.column + k] = ch
    lines.append(Text(" " * (layout.label_w + 2) + "".join(tick_line).rstrip(), style=FAINT))
    return lines


def pay_bar_at(bars: Sequence[PayBar], width: int, height: int, x: int) -> date | None:
    """The payday of the column at cell `x` of a `pay_bars` chart as wide and tall (a gap
    counts for the column before it), or None left of the columns."""
    if not bars:
        return None
    layout = _pay_layout(bars, width, height)
    cell = x - layout.label_w - 2
    hit = None
    for start, _, i in layout.columns:
        if start > cell:
            break
        hit = i
    return bars[hit].day if hit is not None else None
