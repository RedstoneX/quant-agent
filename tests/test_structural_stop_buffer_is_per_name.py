"""Owner ruling 2026-10-04: the no-ATR structural stop's buffer is a multiple
of the NAME'S OWN movement, not a flat fraction of its price.

Before this change both names below -- a quiet one and a volatile one -- got
an identical stop at the same level, because the buffer was a flat 0.5% of
the level price. That is the made-up-number failure the ruling names: the
same percentage is most of a quiet utility's day and a rounding error on a
volatile semiconductor.
"""
from __future__ import annotations

from types import SimpleNamespace

from src.portfolio_constructor import PortfolioConstructor
from src.portfolio_constructor.structural_buffer import (
    STRUCTURAL_BUFFER_BAR_RANGE_MULTIPLE,
    signal_bar_range,
    structural_buffer_beyond,
)


def _no_atr_analysis(**overrides):
    base = dict(
        atr_14=None, setup_type="breakout", signal_bar_low=None,
        signal_bar_high=None, computed_levels=[], computed_level_touches={},
        expected_horizon_sessions=20, reference_target=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_the_buffer_is_the_names_own_bar_range_not_a_flat_percentage():
    """Same level, same entry, two different names: the volatile one's stop
    must sit strictly further past the level."""
    constructor = PortfolioConstructor()
    mult = STRUCTURAL_BUFFER_BAR_RANGE_MULTIPLE

    quiet = _no_atr_analysis(
        computed_levels=[95.0], computed_level_touches={95.0: 5},
        signal_bar_low=99.5, signal_bar_high=100.5,       # 1.0 wide
    )
    loud = _no_atr_analysis(
        computed_levels=[95.0], computed_level_touches={95.0: 5},
        signal_bar_low=97.0, signal_bar_high=103.0,       # 6.0 wide
    )
    quiet_stop = constructor._widen_stop_past_noise(
        "QUIET", quiet, 100.0, None, direction="long", target_price=None,
    )
    loud_stop = constructor._widen_stop_past_noise(
        "LOUD", loud, 100.0, None, direction="long", target_price=None,
    )
    assert quiet_stop is not None and loud_stop is not None
    assert abs(quiet_stop - (95.0 - mult * 1.0)) < 1e-9
    assert abs(loud_stop - (95.0 - mult * 6.0)) < 1e-9
    # Under the flat 0.5% these two were the SAME price.
    assert loud_stop < quiet_stop - 1.0


def test_a_short_is_mirrored_in_the_same_units():
    constructor = PortfolioConstructor()
    mult = STRUCTURAL_BUFFER_BAR_RANGE_MULTIPLE
    loud = _no_atr_analysis(
        computed_levels=[105.0], computed_level_touches={105.0: 6},
        signal_bar_low=97.0, signal_bar_high=103.0,       # 6.0 wide
    )
    stop = constructor._widen_stop_past_noise(
        "LOUD", loud, 100.0, None, direction="short", target_price=None,
    )
    assert stop is not None and abs(stop - (105.0 + mult * 6.0)) < 1e-9


def test_an_unreadable_bar_range_keeps_the_flat_fraction():
    """A missing denominator never costs a name its protection."""
    constructor = PortfolioConstructor()
    analysis = _no_atr_analysis(
        computed_levels=[95.0], computed_level_touches={95.0: 5},
    )
    stop = constructor._widen_stop_past_noise(
        "ACME", analysis, 100.0, None, direction="long", target_price=None,
    )
    expected = 95.0 * (1.0 - constructor.cfg.structural_stop_buffer_pct)
    assert stop is not None and abs(stop - expected) < 1e-9


def test_a_degenerate_bar_is_not_a_zero_buffer():
    """A flat or inverted bar must fall back, never place the stop AT the
    level with no slack at all."""
    assert signal_bar_range(_no_atr_analysis(signal_bar_low=10.0, signal_bar_high=10.0)) is None
    assert signal_bar_range(_no_atr_analysis(signal_bar_low=11.0, signal_bar_high=10.0)) is None
    flat = _no_atr_analysis(signal_bar_low=10.0, signal_bar_high=10.0)
    assert structural_buffer_beyond(100.0, flat, 0.005, is_short=False) == 99.5
