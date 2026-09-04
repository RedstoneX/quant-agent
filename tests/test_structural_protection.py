"""`check_structural_protection` / `structural_protection_broken` — spec item
25's data-driven replacement for the flat "days_held < 5" holding-discipline
window (owner decisions, 2026-09-03/04; see `src/risk/exit_guard.py`'s
"Structural (data-driven) holding protection" module note for the full
rationale, corrected same day against real technical-analysis practice).

A position is protected from a plain no-real-trigger SELL/REDUCE/COVER
UNLESS the level backing its thesis has actually been broken by price —
never by elapsed time. Priority order tested below:

  1. `thesis_invalid_if`, checked for real via `check_thesis_invalid_if`.
  2. Absent/unparseable (1): the verified structural level backing the
     stop — level IDENTIFICATION uses the same touch-count/ATR-tolerance
     rule as `PortfolioConstructor._level_backing_stop`
     (`min_level_touches`, `level_match_atr_tolerance * atr`), no new
     constant introduced.
  3. Neither resolves: the existing noise band (`adverse_move_is_noise` /
     `NOISE_BAND_ATR_MULTIPLE`) — NOT an automatic unprotect (owner
     refinement 2026-09-04), so breakout/momentum trades without classic
     multi-touch structure are not systematically stripped of protection.

`current_price` for (1) and (2) MUST be a CLOSING price, never a live
intraday quote — a wick that pierces a level and closes back inside is
noise, not a break (real technical-analysis practice, and the reason this
module's confirmation gate exists at all). Deciding WHETHER a close counts
as "beyond" the level reuses `NOISE_BAND_ATR_MULTIPLE` (the same margin
already ratified for "is an adverse move real"), not the tighter
`level_match_atr_tolerance` used only to identify which level a stop sits
on.

A break under (1) or (2) must additionally hold on the close of TWO
CONSECUTIVE TRADING DAYS before it lifts protection (owner refinement,
corrected 2026-09-04) — a "spring" (a level breaking then reclaiming the
very next day, often itself a bullish signal) is a real, well-documented
pattern and must not read as an invalidated thesis off one day's close.
(3) is NOT gated by this — it lifts immediately, as do the two independent
regime-flip / bearish-state-change triggers tested in
`test_holding_discipline_guard.py`.

Every number below is hand-computed against the real formulas in
`src/risk/exit_guard.py`, never guessed.
"""

from src.risk.exit_guard import (
    NOISE_BAND_ATR_MULTIPLE,
    check_structural_protection,
    structural_protection_broken,
)

# Shared level/touch bars used throughout — the already-ratified values
# (docs/RESEARCH_FINDINGS.md §7), never invented for this test file.
MIN_TOUCHES = 5
TOLERANCE_MULT = 0.25   # level_match_atr_tolerance — level IDENTIFICATION only


# ---------------------------------------------------------------------------
# 1. thesis_invalid_if drives the decision when stated and parseable.
#    Long, entry 100, atr 2, ma_20 98 -> "closes below MA20" triggers at any
#    close below 98.
# ---------------------------------------------------------------------------

def test_thesis_invalid_if_triggered_two_consecutive_closes_lifts_protection():
    """The key behaviour change: a broken thesis CONFIRMED on two
    consecutive trading-day closes lifts protection regardless of how young
    the position is (days_held is not even a parameter any more)."""
    result = check_structural_protection(
        thesis_invalid_if="closes below MA20",
        current_price=95.0,   # today's CLOSE: 95 < 98 -> TRIGGERED
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        min_level_touches=MIN_TOUCHES,
        level_match_atr_tolerance=TOLERANCE_MULT,
        ma_20=98.0,
        break_seen_prior_close=True,   # yesterday's close was ALSO below MA20
    )
    assert result.raw_broken is True
    assert result.protected is False
    assert result.basis == "thesis_invalid_if_triggered"
    assert structural_protection_broken(
        thesis_invalid_if="closes below MA20",
        current_price=95.0, entry_price=100.0, stop_loss=90.0, atr=2.0,
        min_level_touches=MIN_TOUCHES, level_match_atr_tolerance=TOLERANCE_MULT,
        ma_20=98.0, break_seen_prior_close=True,
    ) is True


def test_thesis_invalid_if_triggered_single_close_stays_protected():
    """A break seen for the FIRST time on today's close does not lift
    protection yet — it must still be broken on TOMORROW's close too."""
    result = check_structural_protection(
        thesis_invalid_if="closes below MA20",
        current_price=95.0,
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        min_level_touches=MIN_TOUCHES,
        level_match_atr_tolerance=TOLERANCE_MULT,
        ma_20=98.0,
        break_seen_prior_close=False,   # no prior confirming close on record
    )
    assert result.raw_broken is True
    assert result.protected is True
    assert result.basis == "thesis_invalid_if_pending_confirmation"


def test_spring_reversal_the_next_day_does_not_lift_protection():
    """THE most important case: day 1 closes below the level, day 2
    reclaims (a textbook 'spring' false-breakdown) -> protection must NEVER
    have lifted, and day 3 breaking again starts the confirmation over from
    scratch rather than instantly firing off the stale day-1 break."""
    day1 = check_structural_protection(
        thesis_invalid_if="closes below MA20",
        current_price=95.0,        # day 1 close: below MA20 -> broken
        entry_price=100.0, stop_loss=90.0, atr=2.0,
        min_level_touches=MIN_TOUCHES, level_match_atr_tolerance=TOLERANCE_MULT,
        ma_20=98.0, break_seen_prior_close=False,
    )
    assert day1.raw_broken is True
    assert day1.protected is True   # single close never lifts protection

    day2 = check_structural_protection(
        thesis_invalid_if="closes below MA20",
        current_price=99.0,        # day 2 close: reclaimed above MA20 (98.0)
        entry_price=100.0, stop_loss=90.0, atr=2.0,
        min_level_touches=MIN_TOUCHES, level_match_atr_tolerance=TOLERANCE_MULT,
        ma_20=98.0, break_seen_prior_close=day1.raw_broken,
    )
    assert day2.raw_broken is False
    assert day2.protected is True
    assert day2.basis == "thesis_invalid_if_intact"

    day3 = check_structural_protection(
        thesis_invalid_if="closes below MA20",
        current_price=95.0,        # breaks again on day 3
        entry_price=100.0, stop_loss=90.0, atr=2.0,
        min_level_touches=MIN_TOUCHES, level_match_atr_tolerance=TOLERANCE_MULT,
        ma_20=98.0, break_seen_prior_close=day2.raw_broken,   # False -> resets
    )
    assert day3.protected is True
    assert day3.basis == "thesis_invalid_if_pending_confirmation"


def test_thesis_invalid_if_not_triggered_stays_protected_indefinitely():
    """The other half of the behaviour change: an intact thesis stays
    protected no matter how long the position has been held — there is no
    days_held parameter to time it out with any more."""
    result = check_structural_protection(
        thesis_invalid_if="closes below MA20",
        current_price=105.0,   # close above MA20 -> NOT_TRIGGERED
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        min_level_touches=MIN_TOUCHES,
        level_match_atr_tolerance=TOLERANCE_MULT,
        ma_20=98.0,
        break_seen_prior_close=True,   # even a stale confirmed break can't matter here
    )
    assert result.protected is True
    assert result.basis == "thesis_invalid_if_intact"
    assert result.raw_broken is False


# ---------------------------------------------------------------------------
# 2. Structural level backing the stop, when thesis_invalid_if is absent or
#    unparseable — level IDENTIFICATION as `_level_backing_stop`, break
#    MARGIN as `NOISE_BAND_ATR_MULTIPLE` (1.0), not `level_match_atr_tolerance`.
# ---------------------------------------------------------------------------

def test_structural_level_broken_two_consecutive_closes_lifts_protection():
    # Long: entry 100, stop 90, atr 2. Level IDENTIFICATION tolerance =
    # 0.25 * 2 = 0.5 -> a verified level at 90.3 (gap 0.3 <= 0.5) with 6
    # touches (>= 5) backs the stop. Break MARGIN = NOISE_BAND_ATR_MULTIPLE
    # (1.0) * atr (2) = 2.0 -> broken when close <= level - margin = 88.3.
    assert NOISE_BAND_ATR_MULTIPLE == 1.0
    result = check_structural_protection(
        thesis_invalid_if=None,
        current_price=88.0,   # <= 88.3 -> decisively broken
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        is_short=False,
        computed_levels=[90.3],
        computed_level_touches={90.3: 6},
        min_level_touches=MIN_TOUCHES,
        level_match_atr_tolerance=TOLERANCE_MULT,
        break_seen_prior_close=True,
    )
    assert result.raw_broken is True
    assert result.protected is False
    assert result.basis == "structural_level_broken"


def test_structural_level_broken_single_close_stays_protected():
    result = check_structural_protection(
        thesis_invalid_if=None,
        current_price=88.0,
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        is_short=False,
        computed_levels=[90.3],
        computed_level_touches={90.3: 6},
        min_level_touches=MIN_TOUCHES,
        level_match_atr_tolerance=TOLERANCE_MULT,
        break_seen_prior_close=False,
    )
    assert result.raw_broken is True
    assert result.protected is True
    assert result.basis == "structural_level_pending_confirmation"


def test_small_close_below_level_within_break_margin_is_not_a_break():
    """A close that dips modestly below the level but stays inside the
    NOISE_BAND_ATR_MULTIPLE margin is exactly the same shape as an
    intrabar wick that closes back inside it — real trading practice does
    not call this a break, and neither does this function. This also
    covers the "intraday wick closing back inside on the same day never
    counts" case: the function only ever sees a close, and a close this
    close to the level is indistinguishable from — and treated the same
    as — a wick that recovered."""
    result = check_structural_protection(
        thesis_invalid_if=None,
        current_price=90.0,   # AT the level, well inside the 2.0 margin -> not broken
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        is_short=False,
        computed_levels=[90.3],
        computed_level_touches={90.3: 6},
        min_level_touches=MIN_TOUCHES,
        level_match_atr_tolerance=TOLERANCE_MULT,
        break_seen_prior_close=True,   # even a stale prior break can't matter — not broken now
    )
    assert result.raw_broken is False
    assert result.protected is True
    assert result.basis == "structural_level_intact"


def test_structural_level_intact_stays_protected_at_30_days_equivalent():
    """The key behaviour change, on the level path this time: intact
    structure protects a position with no time limit at all."""
    result = check_structural_protection(
        thesis_invalid_if=None,
        current_price=95.0,   # well above the level -> intact
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        is_short=False,
        computed_levels=[90.3],
        computed_level_touches={90.3: 6},
        min_level_touches=MIN_TOUCHES,
        level_match_atr_tolerance=TOLERANCE_MULT,
        break_seen_prior_close=False,
    )
    assert result.protected is True
    assert result.basis == "structural_level_intact"


def test_structural_level_broken_short_side_mirrors_long():
    # Short: entry 100, stop 110, atr 2. Identification tolerance 0.5 ->
    # verified resistance at 109.8 (gap 0.2 <= 0.5, 6 touches). Break
    # margin 2.0 -> broken when close >= level + margin = 111.8.
    result = check_structural_protection(
        thesis_invalid_if=None,
        current_price=112.0,   # >= 111.8 -> broken
        entry_price=100.0,
        stop_loss=110.0,
        atr=2.0,
        is_short=True,
        computed_levels=[109.8],
        computed_level_touches={109.8: 6},
        min_level_touches=MIN_TOUCHES,
        level_match_atr_tolerance=TOLERANCE_MULT,
        break_seen_prior_close=True,
    )
    assert result.protected is False
    assert result.basis == "structural_level_broken"


def test_unparseable_thesis_falls_back_to_structural_level():
    """An UNPARSEABLE thesis_invalid_if must not be treated as protected or
    broken by default — it falls through to the level check exactly as if
    no thesis had been stated at all."""
    result = check_structural_protection(
        thesis_invalid_if="RSI drops below 30 and MACD crosses down",  # compound -> UNPARSEABLE
        current_price=88.0,
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        computed_levels=[90.3],
        computed_level_touches={90.3: 6},
        min_level_touches=MIN_TOUCHES,
        level_match_atr_tolerance=TOLERANCE_MULT,
        break_seen_prior_close=True,
    )
    assert result.basis == "structural_level_broken"
    assert result.protected is False


# ---------------------------------------------------------------------------
# 3. No thesis_invalid_if and no qualifying level -> noise-band fallback
#    (owner refinement 2026-09-04), immediate (no confirmation gate).
# ---------------------------------------------------------------------------

def test_no_basis_within_noise_band_stays_protected():
    # entry 100, atr 2, NOISE_BAND_ATR_MULTIPLE == 1.0 -> band is 2.0.
    # Adverse move of 1.0 is inside the band.
    result = check_structural_protection(
        thesis_invalid_if=None,
        current_price=99.0,
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        computed_levels=[],
        computed_level_touches={},
        min_level_touches=MIN_TOUCHES,
        level_match_atr_tolerance=TOLERANCE_MULT,
    )
    assert result.protected is True
    assert result.basis == "noise_band_intact"
    assert result.raw_broken is False  # noise-band basis is never gated by confirmation


def test_no_basis_beyond_noise_band_loses_protection_immediately():
    # Adverse move of 3.0 exceeds the 2.0 band -> not protected, and NOT
    # gated by the two-day confirmation rule (that applies only to the
    # thesis/level basis).
    result = check_structural_protection(
        thesis_invalid_if=None,
        current_price=97.0,
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        computed_levels=[],
        computed_level_touches={},
        min_level_touches=MIN_TOUCHES,
        level_match_atr_tolerance=TOLERANCE_MULT,
        break_seen_prior_close=False,   # irrelevant to this basis
    )
    assert result.protected is False
    assert result.basis == "noise_band_broken"


def test_no_basis_flat_or_winning_stays_protected():
    result = check_structural_protection(
        thesis_invalid_if=None,
        current_price=105.0,   # in profit
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        computed_levels=[],
        computed_level_touches={},
        min_level_touches=MIN_TOUCHES,
        level_match_atr_tolerance=TOLERANCE_MULT,
    )
    assert result.protected is True
    assert result.basis == "noise_band_intact"


def test_no_basis_no_price_or_atr_data_fails_toward_protection():
    """When there isn't even enough data to evaluate the noise band, the
    position stays protected (never manufacture a block out of missing
    data) — and the case is visible via `basis`/`detail`, not a silent
    no-op."""
    result = check_structural_protection(
        thesis_invalid_if=None,
        current_price=None,
        entry_price=None,
        stop_loss=None,
        atr=None,
        computed_levels=[],
        computed_level_touches={},
        min_level_touches=MIN_TOUCHES,
        level_match_atr_tolerance=TOLERANCE_MULT,
    )
    assert result.protected is True
    assert result.basis == "noise_band_intact"
    assert "insufficient price/ATR data" in result.detail


def test_low_touch_level_does_not_qualify_falls_back_to_noise_band():
    """A level with fewer than `min_level_touches` prior touches does not
    back the stop (same fail-closed rule as `_level_backing_stop`), so this
    is a no-basis case even though `computed_levels` is non-empty."""
    result = check_structural_protection(
        thesis_invalid_if=None,
        current_price=99.0,
        entry_price=100.0,
        stop_loss=90.0,
        atr=2.0,
        computed_levels=[90.3],
        computed_level_touches={90.3: 2},   # below the 5-touch bar
        min_level_touches=MIN_TOUCHES,
        level_match_atr_tolerance=TOLERANCE_MULT,
    )
    assert result.basis == "noise_band_intact"
    assert result.protected is True
