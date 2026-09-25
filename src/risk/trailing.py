"""Deterministic trailing stops — spec Phase 3.7.

Trailing is arithmetic. It belongs in Python, not in an LLM's discretion.

Until this landed, every stop movement came from the Position Reviewer
emitting a `TRAIL_STOP` action with a price it chose, clamped after the fact
by a ratchet cooldown and a 1.25x ATR noise band. Those clamps exist because
the discretionary version marched stops into the daily-noise band in three or
four sessions — GE was ratcheted 325 -> 350 in eight sessions on one flag.
Clamping a bad generator is not the same as having a good one.

The rule depends on how the position is MANAGED, which the Technical Analyst
decides at entry and which is pinned to the trade row alongside the horizon:

**Type A — `range`.** There is structure on both sides and the target is a
level someone is defending. Do not trail STRUCTURALLY until price EXCEEDS
that target; the stop stays where it was placed at entry until then, so a
position gets stopped out inside the very range it was bought to traverse
if it trails on every wiggle. (Short mirror: the target is a level BELOW
the short, and the stop does not move until price falls PAST it.)

2026-09-04 audit fix #3: "no trailing until the target is exceeded" used to
mean a Type A trade got ZERO profit protection for its entire life until it
had captured 100% of the planned move — the single largest un-backtested,
asymmetric-downside rule found in the exit-management audit, and Type A is
this desk's most common setup by real observed frequency. A trade could
travel 90%+ of the way to its target and give back all of it with nothing
in place. Standard, widely cited practice (Van Tharp's R-multiple framework;
Elder's "Triple Screen"; the same R-multiple convention this codebase's own
docs already use elsewhere) is to move the stop to breakeven once a trade
has banked a defensible fraction of its planned risk — commonly +1R (one
initial-risk-unit of profit). `compute_trailing_stop` now does exactly that
for Type A specifically, ADDITIVE to the existing "no structural trail below
target" rule above, which is unchanged: once price reaches entry +/- 1R (see
`RANGE_BREAKEVEN_R_MULTIPLE`), the stop ratchets to breakeven if it hasn't
already reached breakeven or better; once price then goes on to exceed the
full target, the pre-existing structural/chandelier trail below takes back
over exactly as before. Type B's trail-from-entry behaviour is untouched —
it already rides the position from day one and has no equivalent gap.

2026-09-25, item 142 (owner-ratified): breakeven alone still gave back
everything a range trade earned BETWEEN breakeven and the target on a
reversal — the largest asymmetric-downside gap left after fix #3. A SECOND
ratchet now stacks on top of the breakeven step and stays below the target:
once price reaches +2R (see `RANGE_SECOND_RATCHET_TRIGGER_R`) the stop moves
up to +1R (see `RANGE_SECOND_RATCHET_LOCK_R`), locking one initial-risk-unit
of gain. Both steps are Type A only, both fire only below the target, and
both ratchet the stop UP only — never down. Breakouts are untouched: they use
the structural/chandelier trail from entry and never reach either R-multiple
step.

**Type B — `breakout`.** There is no overhead structure and the target is a
measured-move reference, not a level. Progress and pace are meaningless here
(see `pipeline._build_position_facts`), so trailing IS the management: ride it
and let structure decide when it is over. Trail from entry, under each
successive higher low, with a chandelier stop where structure is unclear.
(Short mirror: trail from entry, above each successive lower high, chandelier
above the lowest low since entry where structure is unclear.)

Invariants, all of them enforced below:
  - **Ratchet toward less risk only.** Up for a long, down for a short. A
    stop never moves the wrong way. Ever.
  - A move must clear the existing stop by `MIN_RATCHET_PCT` to be worth an
    order at all — otherwise every session nudges the stop a few cents and the
    cooldown is doing all the work.
  - A new stop is never placed inside `NOISE_BAND_ATR_MULTIPLE` ATRs of
    current price. That is the same floor the discretionary path already
    clamps to, applied at the source instead of after the fact.
  - Missing data yields no proposal, never a guess.

**Stage 2 of short selling (shorts-safe).** Every rule above was written and
tested against a long-only book, where "trail" only ever means "raise the
stop". A short's protective stop is a BUY stop ABOVE the market, and every
one of these rules mirrors through a price-axis flip: ratchet DOWN instead of
UP, trail under successive LOWER highs instead of higher lows, chandelier off
the LOWEST low instead of the highest high, noise band and ratchet-minimum
measured on the other side of the stop. `qty` supplies only the SIDE (same
convention as `risk.metrics.r_multiple`): negative is a short. No order path
in this repo can open a short yet, so `qty` defaults to +1.0 and every
existing call site — which only ever knows about longs — is unchanged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "TrailProposal",
    "TrailEvaluation",
    "compute_trailing_stop",
    "evaluate_trailing_stop",
    "MIN_RATCHET_PCT",
    "CHANDELIER_ATR_MULTIPLE",
    "NOISE_BAND_ATR_MULTIPLE",
    "PIVOT_WINDOW",
    "RANGE_BREAKEVEN_R_MULTIPLE",
    "RANGE_SECOND_RATCHET_TRIGGER_R",
    "RANGE_SECOND_RATCHET_LOCK_R",
]

#: A proposed stop must sit at least this far above the live stop. Mirrors the
#: reviewer's historical ">= 1.02x old stop" min-bump rule so the deterministic
#: path does not churn orders the discretionary one would have skipped.
MIN_RATCHET_PCT = 2.0

#: Chandelier distance below the highest high since entry, used only where
#: structure is unclear. 3x ATR is the conventional setting and is deliberately
#: loose: a fallback that strangles the trade defeats the purpose of Type B.
CHANDELIER_ATR_MULTIPLE = 3.0

#: A stop closer than this many ATRs to current price sits inside one ordinary
#: day's range. Same value the TRAIL_STOP clamp in `pipeline.py` uses.
NOISE_BAND_ATR_MULTIPLE = 1.25

#: Bars either side of a candidate swing low, for THIS module only.
#:
#: **This is a convention with no derivation, and it is deliberately not
#: reconciled with `src/data/levels.py`.** Until 2026-09-13 the comment here
#: claimed it "matches `src/data/levels.py`'s pivot detection so 'a higher
#: low' means the same thing in both places". That was false the day it was
#: written: `src/data/levels.py::PIVOT_WINDOW` is 5, this is 3, and they have
#: never been equal.
#:
#: What the desk adopted is the ARCHETYPE, not anyone's constant: a swing
#: point is a bar that strictly dominates N bars on BOTH sides, and the
#: right-hand arm means the verdict arrives N bars late. That is the Williams
#: fractal / pivot-high-low construction as implemented by TA-Lib's FRACTAL
#: (`optInLeftBars` / `optInRightBars`, https://ta-lib.org/functions/fractal.html)
#: and by Pine's `ta.pivothigh` / `ta.pivotlow`.
#:
#: The NUMBER is not adopted, because no source derives one:
#:   * TA-Lib's FRACTAL defaults both arms to 2 and states no rationale; the
#:     page only notes "Bill Williams' original is the symmetric five-candle
#:     case" — i.e. 2 either side, which is neither 3 nor 5.
#:   * MetaTrader 5's own fractal documentation defines the pattern as "at
#:     least five successive bars ... and two lower HIGHs on both sides" and
#:     gives no reason for the count
#:     (https://www.metatrader5.com/en/terminal/help/indicators/bw_indicators/fractals).
#:   * fxssi records that Williams did NOT require five, and that five became
#:     standard "due to its inclusion in the list of standard indicators of
#:     MetaTrader 4 trading terminal" (https://fxssi.com/bill-williams-fractals)
#:     — i.e. the popular number is a default that shipped, not a measurement.
#:   * LuxAlgo's swing high/low reference states outright: "There is no
#:     universally best setting ... different settings produce genuinely
#:     different structure from the same chart"
#:     (https://www.luxalgo.com/library/concept/swing-high-low/).
#: Reviewed 2026-09-13; ~all of this literature is assertion rather than
#: measurement, and no source fetched offered a derivation for any window.
#:
#: Second pass the same day (docs/WORK.md item 55) added the ACADEMIC source
#: the vendor docs above are not: Tsinaslanidis, "Technical Trading
#: Strategies, Pattern Recognition and Financial Risk Management" (PhD
#: thesis, University of Macedonia, 2012, §4.3). It defines the identical
#: symmetric construction and, uniquely, MEASURES its sensitivity — but at
#: rolling windows of 50/100/150 days total, i.e. 25/50/75 bars either side,
#: reporting the results "robust to any different parameterization" over
#: that range. That range does not contain 3, so it neither supports nor
#: refutes this constant. It is evidence that the object is insensitive at
#: swing scale, and silence at the scale a stop is actually placed on.
#:
#: So 3 stays, labelled honestly, rather than being changed to a number with
#: no better claim on being right. Changing it would be picking a number.
#:
#: Sole consumers: `_swing_lows` / `_swing_highs` in this file, reached only
#: through `compute_trailing_stop`. Callers of that are
#: `src/pipeline.py::_trail_open_positions` and
#: `src/backtest/engine.py`. Nothing downstream compares a pivot found here
#: against a level from `src/data/levels.py`, so the two windows disagreeing
#: is currently harmless — see `src/data/levels.py::PIVOT_WINDOW` for the
#: other half of this note and `tests/test_pivot_window_independence.py` for
#: the test that pins it.
PIVOT_WINDOW = 3

#: How many initial-risk-units (R) of profit a Type A / range trade must
#: bank before its stop ratchets to breakeven. 1.0 is the standard,
#: widely-cited default (Van Tharp's R-multiple framework; Elder's Triple
#: Screen) — not a backtested or desk-specific tuning, a conventional
#: starting point for "this trade has proven itself enough to stop risking
#: the full original bet." R itself is `abs(entry - initial_stop)`, i.e. the
#: risk actually taken at entry, never the (possibly already-ratcheted)
#: live stop.
RANGE_BREAKEVEN_R_MULTIPLE = 1.0

#: Item 142 — the SECOND range ratchet, stacked ON TOP of the +1R breakeven
#: step above and BELOW the target. Once a range / Type A trade reaches +2R of
#: open profit, its stop is moved up to +1R, locking in one initial-risk-unit
#: of gain instead of letting a move between breakeven and target be given back
#: in full on a reversal (the largest asymmetric-downside gap the exit audit
#: left open after fix #3 closed the "no protection until target" gap). Both
#: multiples are OWNER APPETITE, ratified by Rex on 2026-09-25 for item 142
#: ("let's try your recommendation"): the desk chose to express the second
#: ratchet in the SAME initial-risk unit (R) the first ratchet already uses —
#: trigger at 2R, lock at 1R — so both are recorded as `derived` from
#: `RANGE_BREAKEVEN_R_MULTIPLE`, not as freshly-invented arbitrary numbers and
#: not as sourced measurements. R itself is `abs(entry - initial_stop)`, the
#: same denominator the breakeven ratchet uses; never the live (already
#: ratcheted) stop.
RANGE_SECOND_RATCHET_TRIGGER_R = 2.0
RANGE_SECOND_RATCHET_LOCK_R = 1.0


def _finite(value: object) -> float | None:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


@dataclass(frozen=True)
class TrailProposal:
    """A deterministic proposal to raise one position's stop."""

    symbol: str
    new_stop: float
    previous_stop: float
    source: str          # "structure" | "chandelier"
    reason: str


#: Why an evaluation ended the way it did. One code per exit of
#: `evaluate_trailing_stop`, so "why has this stop never trailed?" has an
#: answer on file (`src/execution/exit_path_records.py`). Names only — no
#: code path branches on them.
TRAIL_CODE_TRAILED = "trailed"
TRAIL_CODE_BAD_PRICE_INPUT = "no_usable_entry_or_price"
TRAIL_CODE_NO_LIVE_STOP = "no_live_stop"
TRAIL_CODE_RANGE_NO_INITIAL_STOP = "range_below_target_no_entry_stop_on_record"
TRAIL_CODE_RANGE_ZERO_RISK = "range_below_target_entry_equals_entry_stop"
TRAIL_CODE_RANGE_BELOW_1R = "range_below_target_not_yet_1r"
TRAIL_CODE_RANGE_ALREADY_BREAKEVEN = "range_below_target_stop_already_at_breakeven"
TRAIL_CODE_RANGE_BREAKEVEN_OFF_SIDE = "range_breakeven_not_between_stop_and_price"
TRAIL_CODE_RANGE_BELOW_2R = "range_below_target_not_yet_2r"
TRAIL_CODE_RANGE_SECOND_OFF_SIDE = "range_second_ratchet_lock_not_between_stop_and_price"
TRAIL_CODE_NO_CANDIDATE = "no_structure_and_no_usable_chandelier"
TRAIL_CODE_BELOW_MIN_RATCHET = "move_smaller_than_min_ratchet"
TRAIL_CODE_INSIDE_NOISE_BAND = "inside_noise_band"
TRAIL_CODE_ROUNDED_OFF_SIDE = "rounded_candidate_not_between_stop_and_price"


@dataclass(frozen=True)
class TrailEvaluation:
    """The proposal (or None) AND the code naming why — so a caller can
    record a no-trail instead of discarding it."""

    proposal: TrailProposal | None
    code: str


def _swing_lows(bars, window: int = PIVOT_WINDOW) -> list[float]:
    """Confirmed swing lows, oldest first.

    A low is confirmed only when `window` bars on BOTH sides are higher, so the
    most recent `window` bars can never produce one. That lag is the point: an
    unconfirmed low is just today's price, and trailing under today's price is
    how a stop ends up inside the noise band.
    """
    lows: list[float] = []
    n = len(bars)
    if n < window * 2 + 1:
        return lows
    values = [_finite(getattr(b, "low", None)) for b in bars]
    for i in range(window, n - window):
        centre = values[i]
        if centre is None:
            continue
        neighbourhood = [v for v in values[i - window:i + window + 1] if v is not None]
        if len(neighbourhood) < window + 1:
            continue
        if centre <= min(neighbourhood):
            lows.append(centre)
    return lows


def _swing_highs(bars, window: int = PIVOT_WINDOW) -> list[float]:
    """Confirmed swing highs, oldest first — the short's mirror of `_swing_lows`.

    A high is confirmed only when `window` bars on BOTH sides are lower, so
    the most recent `window` bars can never produce one, for the same lag
    reason as the long side: an unconfirmed high is just today's price, and
    trailing above today's price is how a short's stop ends up inside the
    noise band.
    """
    highs: list[float] = []
    n = len(bars)
    if n < window * 2 + 1:
        return highs
    values = [_finite(getattr(b, "high", None)) for b in bars]
    for i in range(window, n - window):
        centre = values[i]
        if centre is None:
            continue
        neighbourhood = [v for v in values[i - window:i + window + 1] if v is not None]
        if len(neighbourhood) < window + 1:
            continue
        if centre >= max(neighbourhood):
            highs.append(centre)
    return highs


def _structural_pivot(pivots: list[float], *, is_short: bool) -> float | None:
    """The one pivot this module is entitled to trail against, or None.

    The rule this module states is "trail under each successive HIGHER low"
    (mirror: above each successive LOWER high). What the code did for a long
    was take the highest confirmed low sitting between the stop and price —
    which is a different rule, and on a stock making LOWER lows it is the
    wrong one. Example, all real shapes this desk holds: entry 100, stop 90,
    confirmed lows 95 then 92 then 91, price back at 98. The old code trailed
    to 95 — a level price had since traded straight through down to 91 and
    only recovered above afterwards. A support level that has been broken is
    not support; the sequence is making lower lows, so structure has not
    offered a trail at all and the chandelier fallback below is the honest
    answer.

    So: the pivot is the MOST RECENT confirmed one, and it counts only when
    it is genuinely higher than the pivot before it (a real higher low).
    Where only ONE pivot is confirmed there is no sequence to judge and it is
    accepted on its own — that is the pre-existing behaviour, it is what a
    freshly-broken-out position looks like, and tightening it would remove
    protection rather than add it.

    Returns None when structure gives no answer. The caller falls through to
    the chandelier, which is exactly what "where structure is unclear" in the
    module docstring means.
    """
    if not pivots:
        return None
    latest = pivots[-1]
    if len(pivots) == 1:
        return latest
    previous = pivots[-2]
    if is_short:
        # A short trails above successive LOWER highs.
        return latest if latest < previous else None
    return latest if latest > previous else None


def _range_breakeven_ratchet(
    *, symbol: str, ent: float, cur: float, stop: float,
    initial_stop: float | None, is_short: bool, setup_type: str | None,
) -> TrailEvaluation:
    """Type A's +1R breakeven ratchet — see the module docstring's 2026-09-04
    fix #3 note.

    Fails closed: with no `initial_stop` (the ENTRY stop, never the live one
    a prior trail may have already moved), R cannot be measured, so this
    proposes nothing rather than guessing at the risk that was taken.
    Deliberately skips the ordinary `min_ratchet_pct` / noise-band invariants
    below — this move is not a structural ratchet being tuned to avoid
    churn, it is a one-time, always-worthwhile transition from "full initial
    risk" to "no risk", regardless of how small the percentage move to
    breakeven happens to be.
    """
    init_stop = _finite(initial_stop) if initial_stop is not None else None
    if init_stop is None or init_stop <= 0:
        return TrailEvaluation(None, TRAIL_CODE_RANGE_NO_INITIAL_STOP)
    risk = abs(ent - init_stop)
    if risk <= 0:
        return TrailEvaluation(None, TRAIL_CODE_RANGE_ZERO_RISK)

    if is_short:
        trigger = ent - RANGE_BREAKEVEN_R_MULTIPLE * risk
        reached_1r = cur <= trigger
        already_protected = stop <= ent  # breakeven or better already
    else:
        trigger = ent + RANGE_BREAKEVEN_R_MULTIPLE * risk
        reached_1r = cur >= trigger
        already_protected = stop >= ent

    if not reached_1r:
        return TrailEvaluation(None, TRAIL_CODE_RANGE_BELOW_1R)
    if already_protected:
        return TrailEvaluation(None, TRAIL_CODE_RANGE_ALREADY_BREAKEVEN)

    candidate = round(ent, 2)
    if is_short:
        if not (cur < candidate < stop):
            return TrailEvaluation(None, TRAIL_CODE_RANGE_BREAKEVEN_OFF_SIDE)
    else:
        if not (stop < candidate < cur):
            return TrailEvaluation(None, TRAIL_CODE_RANGE_BREAKEVEN_OFF_SIDE)

    return TrailEvaluation(TrailProposal(
        symbol=symbol.upper(), new_stop=candidate, previous_stop=stop,
        source="breakeven_ratchet",
        reason=(
            f"deterministic trail (breakeven_ratchet): {setup_type or 'unknown'} "
            f"setup reached +{RANGE_BREAKEVEN_R_MULTIPLE:.0f}R (price ${cur:.2f}, "
            f"entry ${ent:.2f}, initial risk ${risk:.2f}); stop ${stop:.2f} -> "
            f"${candidate:.2f} (breakeven) per standard R-multiple practice "
            f"(Van Tharp / Elder) rather than staying fully unprotected until "
            f"the whole target is hit"
        ),
    ), TRAIL_CODE_TRAILED)


def _range_second_ratchet(
    *, symbol: str, ent: float, cur: float, stop: float,
    initial_stop: float | None, is_short: bool, setup_type: str | None,
) -> TrailEvaluation:
    """Type A's SECOND ratchet — item 142, owner-ratified 2026-09-25.

    Stacks ON TOP of `_range_breakeven_ratchet` and BELOW the target: once a
    range trade reaches +`RANGE_SECOND_RATCHET_TRIGGER_R`R (2R) of open profit,
    move the stop up to +`RANGE_SECOND_RATCHET_LOCK_R`R (1R), locking in one
    initial-risk-unit of gain instead of giving back everything between
    breakeven and target on a reversal.

    Mirrors `_range_breakeven_ratchet` exactly: fails closed with no
    `initial_stop` (the ENTRY stop, never the live one a prior trail moved) so
    R cannot be guessed; measures R the same way (`abs(entry - initial_stop)`);
    and skips the ordinary `min_ratchet_pct` / noise-band invariants because
    this is a one-time step-up in protection, not a structural trail being
    tuned against churn. The `stop < candidate < cur` guard (mirrored for a
    short) is what enforces NEVER LOOSEN A STOP: the +1R lock is proposed only
    when it beats the current stop and still sits below price.
    """
    init_stop = _finite(initial_stop) if initial_stop is not None else None
    if init_stop is None or init_stop <= 0:
        return TrailEvaluation(None, TRAIL_CODE_RANGE_NO_INITIAL_STOP)
    risk = abs(ent - init_stop)
    if risk <= 0:
        return TrailEvaluation(None, TRAIL_CODE_RANGE_ZERO_RISK)

    if is_short:
        trigger = ent - RANGE_SECOND_RATCHET_TRIGGER_R * risk
        lock = ent - RANGE_SECOND_RATCHET_LOCK_R * risk
        reached_2r = cur <= trigger
    else:
        trigger = ent + RANGE_SECOND_RATCHET_TRIGGER_R * risk
        lock = ent + RANGE_SECOND_RATCHET_LOCK_R * risk
        reached_2r = cur >= trigger

    if not reached_2r:
        return TrailEvaluation(None, TRAIL_CODE_RANGE_BELOW_2R)

    candidate = round(lock, 2)
    if is_short:
        # Never loosen: the lock must sit BELOW the live stop (a real tighten)
        # and ABOVE price (still a stop). If the stop is already at or past the
        # lock, this returns without moving it.
        if not (cur < candidate < stop):
            return TrailEvaluation(None, TRAIL_CODE_RANGE_SECOND_OFF_SIDE)
    else:
        if not (stop < candidate < cur):
            return TrailEvaluation(None, TRAIL_CODE_RANGE_SECOND_OFF_SIDE)

    return TrailEvaluation(TrailProposal(
        symbol=symbol.upper(), new_stop=candidate, previous_stop=stop,
        source="second_ratchet",
        reason=(
            f"deterministic trail (second_ratchet): {setup_type or 'unknown'} "
            f"setup reached +{RANGE_SECOND_RATCHET_TRIGGER_R:.0f}R (price "
            f"${cur:.2f}, entry ${ent:.2f}, initial risk ${risk:.2f}); stop "
            f"${stop:.2f} -> ${candidate:.2f}, locking in "
            f"+{RANGE_SECOND_RATCHET_LOCK_R:.0f}R of gain (owner appetite "
            f"ruling 2026-09-25, item 142) rather than giving it all back "
            f"between breakeven and target on a reversal"
        ),
    ), TRAIL_CODE_TRAILED)


def compute_trailing_stop(
    *,
    symbol: str,
    setup_type: str | None,
    entry: float,
    current_price: float,
    current_stop: float | None,
    reference_target: float | None,
    bars=None,
    atr: float | None = None,
    min_ratchet_pct: float = MIN_RATCHET_PCT,
    qty: float = 1.0,
    initial_stop: float | None = None,
    structural_ceiling: bool | None = None,
) -> TrailProposal | None:
    """Propose a tightened stop, or None when no move is warranted.

    `bars` are the daily bars SINCE ENTRY (the caller slices them); only those
    matter, because a swing low/high from before the position existed is not
    a level this trade ever defended.

    `qty` supplies only the SIDE — same convention as `risk.metrics.r_multiple`.
    A negative qty is a short: everything below mirrors across the entry
    price. A long's stop lives BELOW price and ratchets UP toward it as the
    trade works; a short's stop lives ABOVE price and ratchets DOWN toward it.
    Defaults to +1.0 so every existing (long-only) call site is unchanged.

    `initial_stop` is the stop AT ENTRY (before any trail ever moved it) —
    used only by the Type A breakeven ratchet (fix #3, see module docstring)
    to measure the risk actually taken. Optional and additive: omitting it
    (every pre-fix call site, until updated) simply means that ratchet never
    fires, reproducing the exact old behaviour.

    `structural_ceiling` is the constructor's MEASURED breakout verdict,
    pinned at entry (item 82, #649/#652). Passing it routes a measured
    breakout the analyst mislabelled "range" to Type B trailing instead of
    Type A — see `src.risk.constants.is_trend_trade`. `None` (the default,
    and every legacy row) falls back to the analyst's label alone, which is
    the exact behaviour this argument replaces.
    """
    return evaluate_trailing_stop(
        symbol=symbol, setup_type=setup_type, entry=entry,
        current_price=current_price, current_stop=current_stop,
        reference_target=reference_target, bars=bars, atr=atr,
        min_ratchet_pct=min_ratchet_pct, qty=qty, initial_stop=initial_stop,
        structural_ceiling=structural_ceiling,
    ).proposal


def evaluate_trailing_stop(
    *,
    symbol: str,
    setup_type: str | None,
    entry: float,
    current_price: float,
    current_stop: float | None,
    reference_target: float | None,
    bars=None,
    atr: float | None = None,
    min_ratchet_pct: float = MIN_RATCHET_PCT,
    qty: float | None = None,  # None reads as a long — see `is_short` below
    initial_stop: float | None = None,
    structural_ceiling: bool | None = None,
) -> TrailEvaluation:
    """`compute_trailing_stop`, plus the code naming why it ended where it
    did. Same arguments, same arithmetic, same proposal — this IS the body;
    the other is its one-field view. See `compute_trailing_stop` for the
    argument contract."""

    ent = _finite(entry)
    cur = _finite(current_price)
    stop = _finite(current_stop) if current_stop is not None else None
    atr_f = _finite(atr) if atr is not None else None

    if ent is None or cur is None or ent <= 0 or cur <= 0:
        return TrailEvaluation(None, TRAIL_CODE_BAD_PRICE_INPUT)
    if stop is None or stop <= 0:
        # No live stop means the position is unprotected, which is a repair
        # problem, not a trailing problem. Inventing a trailing stop here
        # would paper over a missing protective order.
        return TrailEvaluation(None, TRAIL_CODE_NO_LIVE_STOP)

    is_short = (_finite(qty) or 1.0) < 0

    # Type A vs Type B is the SAME breakout verdict every other money-path
    # reader now uses: the analyst's label OR the constructor's MEASURED
    # `structural_ceiling` — either sufficient (item 82, #649/#652). A
    # measured breakout the analyst mislabelled "range" (setup_type="range"
    # with structural_ceiling=False → is_trend_trade True) is trailed as
    # Type B, not left in Type A. `structural_ceiling=None` (legacy row, or
    # a caller that cannot measure it) falls back to the label alone —
    # identical to the pre-item-82 `!= "breakout"` compare this replaces.
    from src.risk.constants import is_trend_trade

    # --- Type A: no STRUCTURAL trailing until the target is exceeded -------
    if not is_trend_trade(setup_type, structural_ceiling=structural_ceiling):
        target = _finite(reference_target) if reference_target is not None else None
        exceeded = False
        if target is not None:
            # Short mirror: the target is a level BELOW entry someone is
            # defending. "Exceeded" means price fell PAST it.
            exceeded = (cur < target) if is_short else (cur > target)
        if not exceeded:
            # Fix #3 + item 142: not yet past the target, so no STRUCTURAL
            # trail — but the two R-multiple ratchets still apply here, which
            # is exactly the gap these fixes close (previously: fully
            # unprotected until 100% of target, target-missing data included).
            # Try the higher-protection +2R -> lock-+1R step first (item 142);
            # if price has not reached +2R it returns no proposal and the +1R
            # -> breakeven step (fix #3) decides. Both fail closed without an
            # initial stop, and neither ever loosens a stop.
            second = _range_second_ratchet(
                symbol=symbol, ent=ent, cur=cur, stop=stop,
                initial_stop=initial_stop, is_short=is_short,
                setup_type=setup_type,
            )
            if second.proposal is not None:
                return second
            return _range_breakeven_ratchet(
                symbol=symbol, ent=ent, cur=cur, stop=stop,
                initial_stop=initial_stop, is_short=is_short,
                setup_type=setup_type,
            )
        # Target exceeded: fall through to the structural/chandelier trail
        # below exactly as before fix #3 — unchanged.

    # --- Candidate: structure first ---------------------------------------
    candidate: float | None = None
    source = ""
    if is_short:
        pivot = _structural_pivot(_swing_highs(bars or []), is_short=True)
        # The pivot is only usable if it is BELOW the current stop and ABOVE
        # current price: above the stop is not a ratchet, below the price is
        # not a stop.
        if pivot is not None and cur < pivot < stop:
            candidate = pivot
            source = "structure"
    else:
        pivot = _structural_pivot(_swing_lows(bars or []), is_short=False)
        # Mirror: only a pivot ABOVE the current stop and BELOW current price
        # is usable — below the stop is not a ratchet, above the price is not
        # a stop.
        if pivot is not None and stop < pivot < cur:
            candidate = pivot
            source = "structure"

    # --- Fallback: chandelier, where structure is unclear ------------------
    if candidate is None and atr_f is not None and atr_f > 0:
        if is_short:
            lows = [_finite(getattr(b, "low", None)) for b in (bars or [])]
            lows = [l for l in lows if l is not None]
            lowest = min(lows) if lows else cur
            chandelier = lowest + CHANDELIER_ATR_MULTIPLE * atr_f
            if cur < chandelier < stop:
                candidate = chandelier
                source = "chandelier"
        else:
            highs = [
                _finite(getattr(b, "high", None)) for b in (bars or [])
            ]
            highs = [h for h in highs if h is not None]
            highest = max(highs) if highs else cur
            chandelier = highest - CHANDELIER_ATR_MULTIPLE * atr_f
            if stop < chandelier < cur:
                candidate = chandelier
                source = "chandelier"

    if candidate is None:
        return TrailEvaluation(None, TRAIL_CODE_NO_CANDIDATE)

    # --- Invariants --------------------------------------------------------
    # Ratchet toward less risk only, and only when the move is worth an order.
    if is_short:
        if candidate >= stop * (1 - min_ratchet_pct / 100.0):
            return TrailEvaluation(None, TRAIL_CODE_BELOW_MIN_RATCHET)
    else:
        if candidate <= stop * (1 + min_ratchet_pct / 100.0):
            return TrailEvaluation(None, TRAIL_CODE_BELOW_MIN_RATCHET)

    # Never inside one ordinary day's range of current price.
    if atr_f is not None and atr_f > 0:
        if is_short:
            noise_ceiling = cur + NOISE_BAND_ATR_MULTIPLE * atr_f
            if candidate < noise_ceiling:
                return TrailEvaluation(None, TRAIL_CODE_INSIDE_NOISE_BAND)
        else:
            noise_floor = cur - NOISE_BAND_ATR_MULTIPLE * atr_f
            if candidate > noise_floor:
                return TrailEvaluation(None, TRAIL_CODE_INSIDE_NOISE_BAND)

    candidate = round(candidate, 2)
    if is_short:
        if candidate >= stop or candidate <= cur:
            return TrailEvaluation(None, TRAIL_CODE_ROUNDED_OFF_SIDE)
    else:
        if candidate <= stop or candidate >= cur:
            return TrailEvaluation(None, TRAIL_CODE_ROUNDED_OFF_SIDE)

    locked = ""
    if is_short:
        if candidate <= ent:
            locked = " — at or below entry, so this position stops consuming risk budget"
    else:
        if candidate >= ent:
            locked = " — at or above entry, so this position stops consuming risk budget"
    return TrailEvaluation(TrailProposal(
        symbol=symbol.upper(), new_stop=candidate, previous_stop=stop,
        source=source,
        reason=(
            f"deterministic trail ({source}): {setup_type or 'unknown'} setup, "
            f"stop ${stop:.2f} -> ${candidate:.2f} with price ${cur:.2f}"
            f"{locked}"
        ),
    ), TRAIL_CODE_TRAILED)
