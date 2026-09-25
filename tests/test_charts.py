"""The charts' date axis: which labels show, and that they never run into each other."""

from __future__ import annotations

from datetime import date, timedelta
from itertools import pairwise

from bdbd.ui.charts import _x_ticks


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
