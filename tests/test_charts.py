"""The charts: the date axis (which labels show, and that they never run into each other), and
what's left of each paycheck."""

from __future__ import annotations

from datetime import date, timedelta
from itertools import pairwise

from rich.text import Text

from bdbd.ui.charts import EVERYDAY, PayBar, _x_ticks, pay_bar_at, pay_bars
from bdbd.ui.theme import GREEN, MINUS, RED, muted


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _apart(ticks, gap: int) -> bool:
    return all(a.column + len(a.label) + gap <= b.column for a, b in pairwise(ticks))


def test_ten_years_label_every_year_that_fits() -> None:
    days = _days(date(2026, 9, 24), date(2036, 9, 24))
    ticks = _x_ticks(days, 64)  # the What if chart at 120 columns
    assert [t.label for t in ticks] == [str(y) for y in range(2027, 2037)]  # none skipped
    assert _apart(ticks, 1) and ticks[-1].column + len(ticks[-1].label) <= 64


def test_months_keep_two_cells_apart_and_the_years_show() -> None:
    days = _days(date(2026, 9, 24), date(2028, 9, 24))
    ticks = _x_ticks(days, 60)
    labels = [t.label for t in ticks]
    assert labels[0] == "Oct"  # January, close behind, says the year
    assert "Jan 2027" in labels and "Jan 2028" in labels
    assert _apart(ticks, 2)


def test_a_short_span_labels_weeks() -> None:
    days = _days(date(2026, 9, 24), date(2026, 10, 24))
    ticks = _x_ticks(days, 60)
    # the first Monday, close behind the start, says more than 'Sep 24' would
    assert [t.label for t in ticks] == ["Sep 28", "Oct 5", "Oct 12", "Oct 19"]
    assert _apart(ticks, 2)


# ── What's left of each paycheck ──────────────────────────────────────────────


def _cycles(*lefts: int, cap: int = 0) -> list[PayBar]:
    return [PayBar(date(2026, 9, 18) + timedelta(days=14 * i), v, cap) for i, v in enumerate(lefts)]


def _colors(line: Text) -> set[str]:
    """Every color a row's cells use ('#aaa on #bbb' is both)."""
    return {c for span in line.spans for c in str(span.style).split() if c.startswith("#")}


def _label(line: Text) -> str:
    """A row's axis label ('' when it has none)."""
    return line.plain.split(" ┤")[0].strip() if " ┤" in line.plain else ""


def test_pay_bars_rise_for_whats_free_and_fall_for_whats_short() -> None:
    bars = _cycles(160_624, -41_576, 279_900, cap=35_000)
    lines = pay_bars(bars, 60, 6, selected=date(2026, 9, 18))
    assert len(lines) == 7  # six rows and the axis
    labels = [_label(line) for line in lines[:-1]]
    assert labels[0] == "$3.1k" and labels[-1] == f"{MINUS}$416"
    zero = labels.index("$0")
    assert zero == len(labels) - 2  # the short cycle gets the row under the zero line
    above, below = lines[: zero + 1], lines[zero + 1 : -1]
    assert any(GREEN in _colors(line) for line in above)  # the selected cycle, lit
    assert all(GREEN not in _colors(line) for line in below)
    assert any(muted(RED, 0.55) in _colors(line) for line in below)
    assert any(muted(EVERYDAY, 0.6) in _colors(line) for line in above)  # the everyday caps
    assert lines[-1].plain.strip().startswith("▲ Sep 18")
    assert all(line.cell_len <= 60 for line in lines)
    short = pay_bars(bars, 60, 6, selected=date(2026, 10, 2))  # the short one, lit in red
    assert any(RED in _colors(line) for line in short[zero + 1 : -1])
    assert "▲ Oct 2" in short[-1].plain


def test_pay_bars_with_nothing_short_have_no_row_under_zero() -> None:
    lines = pay_bars(_cycles(100_000, 50_000), 40, 4)
    assert [_label(line) for line in lines[:-1]] == ["$1k", "", "", "$0"]
    assert lines[-2].plain.count("█") > 0  # both columns reach the bottom row
    assert "▲" not in lines[-1].plain  # nothing selected


def test_pay_bars_squeeze_more_cycles_than_cells_and_show_the_tightest() -> None:
    lefts = [(-1) ** i * 1000 * (i + 1) for i in range(365)]  # a daily payday, alternating
    bars = [PayBar(date(2026, 9, 18) + timedelta(days=i), v) for i, v in enumerate(lefts)]
    lines = pay_bars(bars, 60, 4)
    assert all(line.cell_len <= 60 for line in lines)
    plot = [line.plain[8:] for line in lines[:2]]  # the rows above zero
    assert all(not row.strip() for row in plot)  # each column shows its short cycle


def test_a_handful_of_cycles_keep_slim_columns() -> None:
    lines = pay_bars(_cycles(100_000), 80, 3)
    assert lines[0].plain.count("█") == 8


def test_a_click_finds_the_cycle_under_it() -> None:
    bars = _cycles(160_624, -41_576, 279_900)
    width, height = 60, 6
    first_plot_cell = len(pay_bars(bars, width, height)[0].plain.split("┤")[0]) + 2
    assert pay_bar_at(bars, width, height, 0) is None  # the axis labels
    assert pay_bar_at(bars, width, height, first_plot_cell) == bars[0].day
    assert pay_bar_at(bars, width, height, width - 1) == bars[-1].day
    days = {pay_bar_at(bars, width, height, x) for x in range(first_plot_cell, width)}
    assert days == {b.day for b in bars}
