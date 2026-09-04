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
    RANGE_BREAKEVEN_R_MULTIPLE,
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
    """No target means no defined point at which STRUCTURAL trailing begins.
    Silence is the correct answer, not a guess. (No `initial_stop` is passed
    here either, so the +1R breakeven ratchet below also has nothing to
    measure risk from — see `test_range_setup_with_no_target_still_gets_the_
    breakeven_ratchet` for the case where it does.)"""
    assert compute_trailing_stop(
        symbol="AAA", setup_type="range", entry=100.0, current_price=125.0,
        current_stop=95.0, reference_target=None,
        bars=_rising_with_higher_lows(), atr=2.0,
    ) is None


# ---------------------------------------------------------------------------
# Type A (range) +1R breakeven ratchet — 2026-09-04 audit fix #3.
#
# Standard practice (Van Tharp's R-multiple framework; Elder's Triple
# Screen): move the stop to breakeven once a trade has banked +1R, instead
# of leaving it fully exposed to the original risk until 100% of a possibly
# much larger target is hit. Additive to the target-exceeded structural
# trail above, which is unchanged.
# ---------------------------------------------------------------------------

def test_range_setup_reaches_breakeven_at_plus_1r():
    """Entry 100, initial stop 90 -> R = 10. +1R = price 110. Below target
    (130), so the OLD code proposed nothing at all here; the new code moves
    the stop to breakeven (100)."""
    proposal = compute_trailing_stop(
        symbol="AAA", setup_type="range", entry=100.0, current_price=110.0,
        current_stop=90.0, reference_target=130.0,
        bars=[], atr=2.0, initial_stop=90.0,
    )
    assert proposal is not None
    assert proposal.new_stop == 100.0
    assert proposal.source == "breakeven_ratchet"


def test_range_setup_below_1r_still_gets_no_protection():
    """Same trade, price only at 105 (0.5R) — not yet earned breakeven."""
    assert compute_trailing_stop(
        symbol="AAA", setup_type="range", entry=100.0, current_price=105.0,
        current_stop=90.0, reference_target=130.0,
        bars=[], atr=2.0, initial_stop=90.0,
    ) is None


def test_range_setup_does_not_re_propose_breakeven_once_already_there():
    """Once the stop is already at or beyond breakeven, no repeat order."""
    assert compute_trailing_stop(
        symbol="AAA", setup_type="range", entry=100.0, current_price=115.0,
        current_stop=100.0, reference_target=130.0,
        bars=[], atr=2.0, initial_stop=90.0,
    ) is None


def test_range_setup_retrace_after_1r_no_longer_gives_back_the_full_risk():
    """The audit's concrete failure mode: a range trade reaches +1R, then
    retraces hard. Under the OLD logic (no protection below target) it would
    ride all the way back down to the original stop, giving back the full
    initial risk (100 -> 90) for zero net gain. Under the fix, +1R already
    ratcheted the stop to breakeven, so the SAME retrace only gives back the
    gain, never the original risk."""
    initial_stop = 90.0
    # Step 1: price reaches +1R (110) -> stop ratchets to breakeven.
    first = compute_trailing_stop(
        symbol="AAA", setup_type="range", entry=100.0, current_price=110.0,
        current_stop=initial_stop, reference_target=130.0,
        bars=[], atr=2.0, initial_stop=initial_stop,
    )
    assert first is not None and first.new_stop == 100.0
    live_stop = first.new_stop

    # Step 2: price retraces hard, back toward the original stop.
    second = compute_trailing_stop(
        symbol="AAA", setup_type="range", entry=100.0, current_price=91.0,
        current_stop=live_stop, reference_target=130.0,
        bars=[], atr=2.0, initial_stop=initial_stop,
    )
    assert second is None  # no further ratchet proposed on the way down
    # The live stop is what the broker actually holds; it stayed at
    # breakeven rather than reverting to the original risk level.
    assert live_stop == 100.0 > initial_stop


def test_range_setup_short_mirror_reaches_breakeven_at_plus_1r():
    """Short mirror: entry 100, initial stop 110 -> R = 10, +1R at price 90."""
    proposal = compute_trailing_stop(
        symbol="AAA", setup_type="range", entry=100.0, current_price=90.0,
        current_stop=110.0, reference_target=70.0,
        bars=[], atr=2.0, initial_stop=110.0, qty=-10,
    )
    assert proposal is not None
    assert proposal.new_stop == 100.0
    assert proposal.source == "breakeven_ratchet"


def test_range_setup_with_no_initial_stop_gets_no_breakeven_ratchet():
    """Backward compatibility: omitting `initial_stop` (every pre-fix call
    site until updated) means R cannot be measured, so nothing is proposed —
    fails closed, exactly the old behaviour, never a guessed risk."""
    assert compute_trailing_stop(
        symbol="AAA", setup_type="range", entry=100.0, current_price=110.0,
        current_stop=90.0, reference_target=130.0,
        bars=[], atr=2.0,
    ) is None


def test_range_setup_with_no_target_still_gets_the_breakeven_ratchet():
    """The breakeven ratchet does not require a target at all — only entry,
    stop and current price — so a range trade with no recorded target still
    gets SOME protection at +1R instead of the old blanket 'no target -> no
    trailing ever' answer."""
    proposal = compute_trailing_stop(
        symbol="AAA", setup_type="range", entry=100.0, current_price=110.0,
        current_stop=90.0, reference_target=None,
        bars=[], atr=2.0, initial_stop=90.0,
    )
    assert proposal is not None
    assert proposal.new_stop == 100.0
    assert proposal.source == "breakeven_ratchet"


def test_breakeven_ratchet_uses_the_initial_stop_not_the_live_one():
    """R must be measured from the risk actually taken at entry. If the live
    stop had already been ratcheted elsewhere (here: further out, at 80, as
    if a previous move had gone the wrong way), the breakeven trigger still
    uses the original 90 -> R = 10, not the live 20."""
    proposal = compute_trailing_stop(
        symbol="AAA", setup_type="range", entry=100.0, current_price=110.0,
        current_stop=80.0, reference_target=130.0,
        bars=[], atr=2.0, initial_stop=90.0,
    )
    assert proposal is not None
    assert proposal.new_stop == 100.0


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
