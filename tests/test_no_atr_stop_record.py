"""Every no-ATR structural stop placement is counted, and nothing else moves.

Nobody knows how often the no-ATR structural-stop path runs, because nothing
has ever counted it. Protection on that path cannot be changed until somebody
does -- so this records it, and asserts the recording changes no stop.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.portfolio_constructor import PortfolioConstructor
from src.portfolio_constructor.no_atr_stop_record import signal_bar_range

LOGGER = "src.portfolio_constructor.no_atr_stop_record"


def _placements(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


def _no_atr_analysis(**overrides):
    base = dict(
        atr_14=None, setup_type="breakout", signal_bar_low=None,
        signal_bar_high=None, computed_levels=[], computed_level_touches={},
        expected_horizon_sessions=20, reference_target=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_a_structural_placement_is_logged_with_rule_and_denominator(caplog):
    caplog.set_level("INFO", logger=LOGGER)
    constructor = PortfolioConstructor()
    analysis = _no_atr_analysis(
        computed_levels=[95.0], computed_level_touches={95.0: 5},
        signal_bar_low=99.0, signal_bar_high=101.0,
    )
    stop = constructor._widen_stop_past_noise(
        "ACME", analysis, 100.0, None, direction="long", target_price=None,
    )
    lines = _placements(caplog)
    assert stop is not None
    assert len(lines) == 1
    assert "rule=stop_read_from_structure_no_atr" in lines[0]
    assert "signal_bar_range=2.0000" in lines[0]


def test_the_prior_bar_tier_is_logged_separately_and_its_denominator_is_absent(caplog):
    caplog.set_level("INFO", logger=LOGGER)
    """The tier-2 fallback with only one bar edge: counted, and recorded as
    having NO per-name denominator -- the fact the buffer question needs."""
    constructor = PortfolioConstructor()
    analysis = _no_atr_analysis(
        computed_levels=[95.0], computed_level_touches={95.0: 2},
        signal_bar_low=97.0,
    )
    stop = constructor._widen_stop_past_noise(
        "ACME", analysis, 100.0, None, direction="long", target_price=None,
    )
    lines = _placements(caplog)
    assert stop is not None
    assert len(lines) == 1
    assert "rule=stop_read_from_prior_bar_no_atr" in lines[0]
    assert "signal_bar_range=none" in lines[0]


def test_a_refusal_logs_nothing(caplog):
    caplog.set_level("INFO", logger=LOGGER)
    """No structure at all is a refusal, not a placement."""
    constructor = PortfolioConstructor()
    stop = constructor._widen_stop_past_noise(
        "ACME", _no_atr_analysis(), 100.0, None, direction="long",
        target_price=None,
    )
    assert stop is None
    assert _placements(caplog) == []


def test_recording_changes_no_stop():
    """The counted placement is byte-for-byte the stop the flat buffer gave:
    this branch observes the path, it does not re-price it."""
    constructor = PortfolioConstructor()
    analysis = _no_atr_analysis(
        computed_levels=[95.0], computed_level_touches={95.0: 5},
        signal_bar_low=90.0, signal_bar_high=110.0,   # a huge bar range
    )
    stop = constructor._widen_stop_past_noise(
        "ACME", analysis, 100.0, None, direction="long", target_price=None,
    )
    expected = 95.0 * (1.0 - constructor.cfg.structural_stop_buffer_pct)
    assert stop is not None and abs(stop - expected) < 1e-9


def test_the_denominator_reader_refuses_a_degenerate_bar():
    assert signal_bar_range(_no_atr_analysis(signal_bar_low=10.0, signal_bar_high=10.0)) is None
    assert signal_bar_range(_no_atr_analysis(signal_bar_low=11.0, signal_bar_high=10.0)) is None
    assert signal_bar_range(_no_atr_analysis(signal_bar_low=9.0, signal_bar_high=10.5)) == 1.5
