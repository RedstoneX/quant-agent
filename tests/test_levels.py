"""Structural level detection — deterministic support/resistance from bars.

These tests pin the behaviour the Tech Analyst now depends on. Levels feed the
stop, the target, and therefore thesis_progress and pace; a silent regression
here would put stops at prices nobody derived from the chart, which is the
failure this module was written to end.
"""

from datetime import date, timedelta

import pytest

from src.data.levels import (
    Level,
    find_structural_levels,
    format_levels_block,
)
from src.models import OHLCV


def _bars(prices: list[float], *, spread: float = 0.5) -> list[OHLCV]:
    """Build a bar series from a close path, with a small symmetric range."""
    start = date(2024, 1, 1)
    out = []
    for i, close in enumerate(prices):
        out.append(
            OHLCV(
                date=start + timedelta(days=i),
                open=close,
                high=close + spread,
                low=close - spread,
                close=close,
                volume=1_000_000,
            )
        )
    return out


def _oscillation(low: float, high: float, cycles: int, period: int = 12) -> list[float]:
    """A price path that repeatedly turns at `low` and `high`."""
    path: list[float] = []
    half = period // 2
    for _ in range(cycles):
        path += [low + (high - low) * i / half for i in range(half)]
        path += [high - (high - low) * i / half for i in range(half)]
    return path


class TestLevelDetection:
    def test_repeated_turning_points_become_levels(self):
        bars = _bars(_oscillation(100.0, 120.0, cycles=6))
        supports, resistances = find_structural_levels(bars)
        found = [lv.price for lv in supports + resistances]
        assert any(98 <= p <= 102 for p in found), f"missing ~100 level in {found}"
        assert any(118 <= p <= 122 for p in found), f"missing ~120 level in {found}"

    def test_levels_are_classified_by_position_relative_to_last_close(self):
        # Ends near the top of the range, so ~100 must read as support.
        bars = _bars(_oscillation(100.0, 120.0, cycles=6) + [119.0, 119.5])
        supports, resistances = find_structural_levels(bars)
        last = bars[-1].close
        assert all(lv.price < last for lv in supports)
        assert all(lv.price > last for lv in resistances)

    def test_a_level_touched_once_is_not_structure(self):
        # A single spike should not be reported as a level.
        path = [100.0] * 40 + [140.0] + [100.0] * 40
        _, resistances = find_structural_levels(_bars(path), max_distance_pct=100.0)
        assert not any(135 <= lv.price <= 145 for lv in resistances)

    def test_insufficient_history_returns_nothing(self):
        supports, resistances = find_structural_levels(_bars([100.0] * 4))
        assert supports == [] and resistances == []

    def test_empty_input_is_safe(self):
        assert find_structural_levels([]) == ([], [])


class TestBadData:
    def test_single_bad_print_does_not_create_a_level(self):
        """A 10x spike in one bar is a vendor error, not resistance."""
        path = _oscillation(100.0, 110.0, cycles=6)
        bars = _bars(path)
        # Corrupt one bar the way a bad feed does.
        i = len(bars) // 2
        bars[i] = OHLCV(
            date=bars[i].date, open=bars[i].open, high=1000.0,
            low=bars[i].low, close=bars[i].close, volume=bars[i].volume,
        )
        supports, resistances = find_structural_levels(bars, max_distance_pct=10_000.0)
        assert not any(lv.price > 500 for lv in supports + resistances)

    def test_impossible_bars_are_discarded(self):
        bars = _bars(_oscillation(100.0, 110.0, cycles=6))
        bars[10] = OHLCV(date=bars[10].date, open=0.0, high=0.0, low=0.0,
                         close=0.0, volume=0)
        # Must not raise, and must still find the real structure.
        supports, resistances = find_structural_levels(bars)
        assert supports or resistances

    def test_a_stock_that_multiplied_keeps_its_recent_history(self):
        """Regression: a global-median outlier filter deleted the real range.

        OKLO ran from ~$10 to ~$114. Comparing every bar to the five-year
        median rejected its entire recent range as 'outliers' and reported no
        levels at all. Outlier detection must be local to each bar's
        neighbourhood, so a trend survives and only print errors are removed.
        """
        ramp = [10.0 + i * 0.5 for i in range(200)]          # 10 -> 110
        settle = _oscillation(104.0, 110.0, cycles=6)         # consolidates high
        supports, resistances = find_structural_levels(_bars(ramp + settle))
        assert supports or resistances, "trending stock produced no levels"
        assert all(lv.price > 50 for lv in supports + resistances), (
            "levels from the pre-ramp era should be out of range, not the recent ones"
        )


class TestRelevanceFiltering:
    def test_distant_history_is_excluded(self):
        """A long-dead price zone is trivia, not actionable structure."""
        old = _oscillation(10.0, 11.0, cycles=8)      # ancient $10 era
        ramp = [10.0 + i * 0.4 for i in range(100)]
        now = _oscillation(48.0, 52.0, cycles=8)      # current range
        supports, resistances = find_structural_levels(_bars(old + ramp + now))
        assert not any(lv.price < 20 for lv in supports + resistances)

    def test_recent_touches_outrank_ancient_ones(self):
        """Strength must reward recency, not raw touch count."""
        ancient = _oscillation(95.0, 97.0, cycles=15)   # many old touches
        middle = [96.0 + i * 0.02 for i in range(150)]
        recent = _oscillation(98.0, 100.0, cycles=3)    # few, but current
        supports, _ = find_structural_levels(
            _bars(ancient + middle + recent), max_distance_pct=100.0
        )
        assert supports, "expected support levels"
        newest = min(supports, key=lambda lv: lv.last_touch_sessions_ago)
        assert newest.strength == max(lv.strength for lv in supports), (
            "a currently-defended level must outrank a long-abandoned one"
        )

    def test_levels_are_returned_strongest_first(self):
        bars = _bars(_oscillation(100.0, 120.0, cycles=8))
        supports, resistances = find_structural_levels(bars)
        for side in (supports, resistances):
            strengths = [lv.strength for lv in side]
            assert strengths == sorted(strengths, reverse=True)

    def test_result_count_is_bounded(self):
        bars = _bars(_oscillation(100.0, 120.0, cycles=20))
        supports, resistances = find_structural_levels(bars, max_per_side=3)
        assert len(supports) <= 3 and len(resistances) <= 3


class TestPromptBlock:
    def test_block_marks_absent_structure_explicitly(self):
        text = format_levels_block([], [], 42.0)
        assert "NONE IDENTIFIED" in text
        # The analyst must be told not to fabricate what wasn't found.
        assert "invent" in text.lower()

    def test_block_orders_like_a_chart_axis(self):
        supports = [Level(90.0, "support", 3, 5, 2.0)]
        resistances = [Level(110.0, "resistance", 2, 9, 1.0)]
        text = format_levels_block(supports, resistances, 100.0)
        assert text.index("110") < text.index("last close") < text.index("90")

    def test_block_reports_distance_and_recency(self):
        text = format_levels_block([Level(95.0, "support", 4, 7, 3.0)], [], 100.0)
        assert "-5.0%" in text
        assert "4 touches" in text
        assert "7d ago" in text


@pytest.mark.parametrize("cycles", [3, 6, 12])
def test_detection_is_deterministic(cycles):
    """Same bars in, same levels out — every time.

    This is the property that makes the desk back-testable and that an LLM
    reading raw bars could never guarantee.
    """
    bars = _bars(_oscillation(100.0, 115.0, cycles=cycles))
    first = find_structural_levels(bars)
    for _ in range(3):
        assert find_structural_levels(bars) == first
