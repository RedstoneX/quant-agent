"""Item 55: a level is a band the BARS drew, not a percentage of price.

The old rule chained a pivot into a level when its PRICE sat within a flat
1% of the group's anchor. These pin the replacement: two pivots are the same
level when the price ranges their bars actually traded overlap, the level's
zone is those bars' own combined span, and a level with no recorded zone
falls back to the old percentage rather than losing its zone entirely.
"""
import math

import pytest

from src.data.levels import (
    CLUSTER_TOLERANCE_PCT_FALLBACK,
    _cluster,
    cluster_span,
    level_zone_halfwidth,
)


def pivot(index, price, kind, low, high):
    return (index, price, kind, low, high)


def test_overlapping_bars_are_one_level_even_far_apart_in_price():
    """Two turning points 6% apart in price, but their bars overlap."""
    a = pivot(0, 100.0, "S", 99.0, 106.5)
    b = pivot(10, 106.0, "R", 105.0, 108.0)
    groups = _cluster([a, b])
    assert len(groups) == 1, "overlapping traded ranges are one level"
    assert cluster_span(groups[0]) == (99.0, 108.0)


def test_non_overlapping_bars_are_separate_levels_even_when_close():
    """Two turning points 0.4% apart — inside the old 1% — but no overlap."""
    a = pivot(0, 100.0, "S", 99.8, 100.1)
    b = pivot(10, 100.4, "R", 100.3, 100.6)
    groups = _cluster([a, b])
    assert len(groups) == 2, "bars that never traded the same prices are two levels"


def test_zone_halfwidth_is_read_off_the_span_not_a_percentage():
    hw = level_zone_halfwidth(100.0, zone_low=96.0, zone_high=103.0)
    assert hw == pytest.approx(4.0), "the furthest edge of the level's own band"
    assert hw != pytest.approx(100.0 * CLUSTER_TOLERANCE_PCT_FALLBACK / 100.0)


def test_no_zone_falls_back_to_todays_behaviour():
    """FAIL CLOSED. A bare price keeps the old bound, never a zero zone."""
    expected = 200.0 * CLUSTER_TOLERANCE_PCT_FALLBACK / 100.0
    assert level_zone_halfwidth(200.0) == pytest.approx(expected)
    assert level_zone_halfwidth(200.0, zone_low=None, zone_high=None) == pytest.approx(expected)
    # Unusable bounds are the same case, not a narrower zone.
    assert level_zone_halfwidth(200.0, zone_low=float("nan"), zone_high=210.0) == pytest.approx(expected)
    assert level_zone_halfwidth(200.0, zone_low=210.0, zone_high=190.0) == pytest.approx(expected)
    # A zero-range bar cannot bound a match either.
    assert level_zone_halfwidth(200.0, zone_low=200.0, zone_high=200.0) == pytest.approx(expected)


def test_unevaluable_range_falls_back_to_the_percentage_rule():
    """A pivot whose bar range is not a number still gets grouped, not dropped."""
    nan = float("nan")
    a = pivot(0, 100.0, "S", 99.9, 100.1)
    b = pivot(9, 100.5, "R", nan, nan)          # within the old 1% of 100.0
    c = pivot(18, 140.0, "R", nan, nan)         # far outside it
    groups = _cluster([a, b, c])
    assert len(groups) == 2
    assert b in groups[0], "unmeasurable range keeps yesterday's answer"
    assert c not in groups[0]


def test_cluster_span_is_none_when_nothing_is_measurable():
    nan = float("nan")
    assert cluster_span([pivot(0, 100.0, "S", nan, nan)]) == (None, None)


def test_levels_built_from_bars_carry_their_measured_zone():
    from src.data.levels import find_structural_levels
    from src.models import OHLCV
    from datetime import date, timedelta

    start = date(2025, 1, 2)
    bars = []
    for i in range(160):
        base = 100.0 + (3.0 if i % 20 < 10 else -3.0)
        bars.append(OHLCV(
            date=start + timedelta(days=i),
            open=base, high=base + 1.5, low=base - 1.5, close=base, volume=1_000_000,
        ))
    sup, res = find_structural_levels(bars)
    found = sup + res
    assert found, "the fixture must produce levels or it pins nothing"
    for lv in found:
        assert lv.zone_low is not None and lv.zone_high is not None
        assert lv.zone_low <= lv.price <= lv.zone_high
        assert lv.zone_halfwidth > 0
