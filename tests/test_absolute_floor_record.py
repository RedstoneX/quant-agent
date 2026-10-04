"""The hard-ATR-floor recorder counts; it must never decide.

Why this exists: the production DB (read-only, 2026-10-04) holds 9
`stop_honoured_at_computed_level` placements and ZERO
`stop_widened_to_absolute_atr_floor` ones, so the floor under every stop has
never once bound and its multiple of 1 cannot be re-derived from anything.
These tests pin the counting, not a number.
"""
from __future__ import annotations

import pytest

from src.portfolio_constructor import absolute_floor_record as rec
from src.portfolio_constructor.config import (
    STOP_RULE_ABSOLUTE_FLOOR,
    STOP_RULE_LEVEL_HONOURED,
)


@pytest.fixture(autouse=True)
def _clean():
    rec.reset()
    yield
    rec.reset()


def _call(inside, stop=95.0, atr=10.0):
    return rec.noted(
        inside_hard_floor=inside,
        symbol="AAA", side_label="LONG", side_word="below",
        entry_price=100.0, stop_loss=stop, atr=atr, level=stop,
        hard_floor=90.0, floor_multiple=1.0, band_multiple=2.5,
        band_edge=75.0,
    )


def test_a_clear_stop_is_returned_untouched():
    assert _call(False, stop=88.0) == (88.0, STOP_RULE_LEVEL_HONOURED)


def test_a_bound_stop_is_returned_at_the_floor():
    assert _call(True, stop=95.0) == (95.0 and 90.0, STOP_RULE_ABSOLUTE_FLOOR)


def test_the_module_never_recomputes_the_verdict():
    """A stop nowhere near the floor still binds if the caller says it does."""
    honoured, rule = _call(True, stop=10.0)
    assert (honoured, rule) == (90.0, STOP_RULE_ABSOLUTE_FLOOR)


def test_binds_and_clears_are_counted_separately():
    _call(False, stop=88.0)
    _call(False, stop=85.0)
    _call(True, stop=95.0)
    snap = rec.snapshot()
    assert snap["level_backed_total"] == 3
    assert snap["floor_binds"] == 1
    assert snap["level_backed_by_outcome"] == {"floor_clear": 2, "floor_bound": 1}


def test_the_spread_of_distances_is_kept_for_the_next_session():
    _call(False, stop=88.0)   # 1.2 ATRs
    _call(False, stop=70.0)   # 3.0 ATRs
    snap = rec.snapshot()
    assert snap["distance_readings"] == 2
    assert snap["tightest_atr_seen"] == pytest.approx(1.2)
    assert snap["widest_atr_seen"] == pytest.approx(3.0)
    assert snap["mean_atr_seen"] == pytest.approx(2.1)


def test_nothing_seen_reports_none_not_a_stand_in_number():
    assert rec.snapshot()["tightest_atr_seen"] is None
    assert rec.snapshot()["floor_binds"] == 0


@pytest.mark.parametrize("atr", [0.0, -1.0, float("nan"), float("inf"), None, "x"])
def test_an_unreadable_atr_yields_no_reading_rather_than_zero(atr):
    assert rec.distance_in_atrs(100.0, 95.0, atr) is None


def test_an_unreadable_atr_still_counts_the_placement():
    honoured, rule = _call(False, stop=88.0, atr=0.0)
    assert (honoured, rule) == (88.0, STOP_RULE_LEVEL_HONOURED)
    snap = rec.snapshot()
    assert snap["level_backed_total"] == 1
    assert snap["distance_readings"] == 0
    assert snap["tightest_atr_seen"] is None
    assert snap["mean_atr_seen"] is None


def test_the_summary_needs_no_cap_and_stays_flat_in_size():
    for _ in range(1000):
        _call(False, stop=88.0)
    snap = rec.snapshot()
    assert snap["distance_readings"] == 1000
    assert snap["level_backed_total"] == 1000
    assert snap["tightest_atr_seen"] == pytest.approx(snap["widest_atr_seen"])


def test_the_resolver_routes_its_level_backed_branch_through_the_recorder():
    import inspect

    from src.portfolio_constructor.entry_stop import resolver

    source = inspect.getsource(resolver)
    assert "absolute_floor_record.noted(" in source
