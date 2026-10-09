"""Pins the item-55 level sweep's two load-bearing mechanics.

The sweep's conclusion rests on (a) the bounce classifier calling a hold a
hold and a break a break, and (b) the threshold-free clustering introducing
no percentage. Both are checked here so a silent change cannot rewrite the
measurement's meaning. docs/board_notes/item-055.md.
"""

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "item55_level_sweep",
    Path(__file__).resolve().parents[1] / "ops/research/item55_level_sweep.py",
)
sweep = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sweep)


def _bar(close, high=None, low=None):
    return {"close": close, "high": high if high is not None else close, "low": low if low is not None else close}


def test_bounce_classifier_separates_hold_from_break():
    zone = [(99.0, 101.0, 2)]
    held = sweep.bounce_events([_bar(105), _bar(100, 101, 99), _bar(106)], zone)
    broke = sweep.bounce_events([_bar(105), _bar(100, 101, 99), _bar(95)], zone)
    assert held == (1, 0), held
    assert broke == (0, 1), broke


def test_overlap_clustering_reads_width_off_the_bars():
    # Two pivots whose BAR RANGES overlap are one level; the zone is their
    # union, so the width is stated by the instrument, not by a constant.
    pvs = [(0, 100.0, 99.0, 101.0), (10, 100.8, 100.5, 102.0)]
    ((lo, hi, touches),) = sweep.cluster_overlap(pvs)
    assert (lo, hi, touches) == (99.0, 102.0, 2)
    # Non-overlapping bars stay separate however close the prices are.
    apart = [(0, 100.0, 99.0, 100.2), (10, 100.5, 100.4, 101.0)]
    assert sweep.cluster_overlap(apart) == []


def test_min_touches_is_the_sourced_two_and_is_not_swept():
    assert sweep.MIN_TOUCHES == 2
    assert "overlap" not in sweep.TOLERANCES
