"""Deterministic trailing stops — spec Phase 3.7.

Trailing is arithmetic and belongs in Python. These tests pin the two
management modes and every invariant that keeps a trail from strangling the
trade it is protecting.
"""

from dataclasses import dataclass

import pytest

from src.risk.trailing import (
    CHANDELIER_ATR_MULTIPLE,
    MIN_RATCHET_PCT,
    compute_trailing_stop,
)


@dataclass
class _Bar:
    high: float
    low: float


def _bars(pattern):
    """`pattern` is a list of (high, low) tuples, oldest first."""
    return [_Bar(high=h, low=lo) for h, lo in pattern]


def _rising_with_higher_lows():
    """A clean uptrend with two CONFIRMED swing lows, at 100 and 110.

    Confirmed means three bars on each side are higher — the same definition
    `src/data/levels.py` uses. The lows series is deliberately V-shaped around
    each pivot; a monotonic ramp contains no swing lows at all, which is
    exactly what the chandelier fallback is for.
    """
    lows = [110, 108, 106, 100, 106, 108, 110,
            118, 116, 114, 110, 114, 116, 118, 125]
    return _bars([(lo + 2, lo) for lo in lows])


# ---------------------------------------------------------------------------
# Type A (range) — no trailing until the target is exceeded
# ---------------------------------------------------------------------------

def test_range_setup_does_not_trail_below_its_target():
    """Trailing a range trade early is how it gets stopped out inside the very
    range it was bought to traverse."""
    assert compute_trailing_stop(
        symbol="AAA", setup_type="range", entry=100.0, current_price=118.0,
        current_stop=95.0, reference_target=130.0,
        bars=_rising_with_higher_lows(), atr=2.0,
    ) is None


def test_range_setup_trails_once_the_target_is_exceeded():
    proposal = compute_trailing_stop(
        symbol="AAA", setup_type="range", entry=100.0, current_price=125.0,
        current_stop=95.0, reference_target=120.0,
        bars=_rising_with_higher_lows(), atr=2.0,
    )
    assert proposal is not None
    assert proposal.new_stop == 110.0        # the higher low
    assert proposal.source == "structure"


def test_range_setup_with_no_target_never_trails():
    """No target means no defined point at which trailing begins. Silence is
    the correct answer, not a guess."""
    assert compute_trailing_stop(
        symbol="AAA", setup_type="range", entry=100.0, current_price=125.0,
        current_stop=95.0, reference_target=None,
        bars=_rising_with_higher_lows(), atr=2.0,
    ) is None


# ---------------------------------------------------------------------------
# Type B (breakout) — trail from entry
# ---------------------------------------------------------------------------

def test_breakout_trails_from_entry_without_needing_a_target():
    """A breakout's target is a measured-move reference, not a level anyone
    defends, so trailing IS the management."""
    proposal = compute_trailing_stop(
        symbol="AAA", setup_type="breakout", entry=100.0, current_price=125.0,
        current_stop=95.0, reference_target=None,
        bars=_rising_with_higher_lows(), atr=2.0,
    )
    assert proposal is not None
    assert proposal.new_stop == 110.0
    assert proposal.source == "structure"


def test_breakout_uses_the_highest_usable_swing_low():
    """Each successive higher low, not the first one found."""
    proposal = compute_trailing_stop(
        symbol="AAA", setup_type="breakout", entry=100.0, current_price=125.0,
        current_stop=99.0, reference_target=None,
        bars=_rising_with_higher_lows(), atr=2.0,
    )
    assert proposal.new_stop == 110.0        # not 100.0


def test_chandelier_is_the_fallback_when_structure_is_unclear():
    """A vertical move with no confirmed swing low still gets a trail."""
    straight_up = _bars([(100 + i, 99 + i) for i in range(14)])
    proposal = compute_trailing_stop(
        symbol="AAA", setup_type="breakout", entry=100.0, current_price=113.0,
        current_stop=95.0, reference_target=None,
        bars=straight_up, atr=1.0,
    )
    assert proposal is not None
    assert proposal.source == "chandelier"
    # highest high 113 - 3 x ATR(1.0) = 110
    assert proposal.new_stop == pytest.approx(113 - CHANDELIER_ATR_MULTIPLE * 1.0)


def test_structure_is_preferred_over_chandelier():
    proposal = compute_trailing_stop(
        symbol="AAA", setup_type="breakout", entry=100.0, current_price=125.0,
        current_stop=95.0, reference_target=None,
        bars=_rising_with_higher_lows(), atr=2.0,
    )
    assert proposal.source == "structure"


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------

def test_a_stop_never_ratchets_down():
    """The single most important property. A trail that can lower a stop is
    not protection, it is a slow-motion loss.

    Swept across a wide range of existing stops: every proposal that comes
    back must sit strictly above the stop it replaces, and a stop already
    above everything the chart offers must produce no proposal at all.
    """
    bars = _rising_with_higher_lows()
    produced_at_least_one = False
    for existing in [80.0, 95.0, 100.0, 105.0, 110.0, 115.0, 118.0, 121.0, 124.0]:
        proposal = compute_trailing_stop(
            symbol="AAA", setup_type="breakout", entry=100.0,
            current_price=125.0, current_stop=existing, reference_target=None,
            bars=bars, atr=2.0,
        )
        if proposal is not None:
            produced_at_least_one = True
            assert proposal.new_stop > existing, (
                f"stop moved DOWN from {existing} to {proposal.new_stop}"
            )
            assert proposal.previous_stop == existing
    assert produced_at_least_one, "the sweep proved nothing if nothing fired"


def test_a_stop_already_above_every_available_level_produces_nothing():
    """Structure exhausted and the chandelier already cleared — hold, don't
    invent a tighter level to justify an order."""
    assert compute_trailing_stop(
        symbol="AAA", setup_type="breakout", entry=100.0, current_price=125.0,
        current_stop=122.0,   # above the 121 chandelier and every swing low
        reference_target=None, bars=_rising_with_higher_lows(), atr=2.0,
    ) is None


def test_a_move_smaller_than_the_ratchet_threshold_is_not_worth_an_order():
    """Otherwise every session nudges the stop a few cents."""
    proposal = compute_trailing_stop(
        symbol="AAA", setup_type="breakout", entry=100.0, current_price=125.0,
        current_stop=110.0 / (1 + MIN_RATCHET_PCT / 100.0) + 0.01,
        reference_target=None, bars=_rising_with_higher_lows(), atr=2.0,
    )
    assert proposal is None


def test_a_stop_is_never_placed_inside_the_atr_noise_band():
    """Same floor the discretionary TRAIL_STOP path clamps to, applied at the
    source instead of after the fact."""
    # ATR 13 => noise floor 125 - 1.25*13 = 108.75; the 110 swing low sits
    # inside it, so no order is worth placing.
    assert compute_trailing_stop(
        symbol="AAA", setup_type="breakout", entry=100.0, current_price=125.0,
        current_stop=95.0, reference_target=None,
        bars=_rising_with_higher_lows(), atr=13.0,
    ) is None


def test_no_live_stop_yields_no_proposal():
    """An unprotected position is a repair problem, not a trailing problem —
    inventing a trailing stop here would paper over a missing protective
    order."""
    assert compute_trailing_stop(
        symbol="AAA", setup_type="breakout", entry=100.0, current_price=125.0,
        current_stop=None, reference_target=None,
        bars=_rising_with_higher_lows(), atr=2.0,
    ) is None
    assert compute_trailing_stop(
        symbol="AAA", setup_type="breakout", entry=100.0, current_price=125.0,
        current_stop=0.0, reference_target=None,
        bars=_rising_with_higher_lows(), atr=2.0,
    ) is None


def test_missing_bars_and_atr_yield_no_proposal_not_a_guess():
    assert compute_trailing_stop(
        symbol="AAA", setup_type="breakout", entry=100.0, current_price=125.0,
        current_stop=95.0, reference_target=None, bars=[], atr=None,
    ) is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -5.0])
def test_non_finite_or_impossible_prices_yield_no_proposal(bad):
    assert compute_trailing_stop(
        symbol="AAA", setup_type="breakout", entry=bad, current_price=125.0,
        current_stop=95.0, reference_target=None,
        bars=_rising_with_higher_lows(), atr=2.0,
    ) is None
    assert compute_trailing_stop(
        symbol="AAA", setup_type="breakout", entry=100.0, current_price=bad,
        current_stop=95.0, reference_target=None,
        bars=_rising_with_higher_lows(), atr=2.0,
    ) is None


def test_a_stop_is_never_placed_at_or_above_current_price():
    for proposal in (
        compute_trailing_stop(
            symbol="AAA", setup_type="breakout", entry=100.0,
            current_price=111.0, current_stop=95.0, reference_target=None,
            bars=_rising_with_higher_lows(), atr=0.5,
        ),
    ):
        if proposal is not None:
            assert proposal.new_stop < 111.0


def test_reason_names_when_risk_is_released():
    """Spec 2.3 — once the stop reaches entry the position stops consuming the
    book's risk budget, and the audit trail should say so."""
    proposal = compute_trailing_stop(
        symbol="AAA", setup_type="breakout", entry=100.0, current_price=125.0,
        current_stop=95.0, reference_target=None,
        bars=_rising_with_higher_lows(), atr=2.0,
    )
    assert proposal.new_stop >= 100.0
    assert "stops consuming risk budget" in proposal.reason


def test_an_unconfirmed_recent_low_is_not_used():
    """A low needs bars on BOTH sides to be confirmed. Trailing under today's
    price is how a stop lands inside the noise band."""
    from src.risk.trailing import _swing_lows

    bars = _bars([(104, 102), (103, 101), (102, 100)])   # too short to confirm
    assert _swing_lows(bars) == []
    # And a monotonic ramp has no local minimum anywhere.
    assert _swing_lows(_bars([(100 + i, 99 + i) for i in range(14)])) == []
