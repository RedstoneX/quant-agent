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
reversal. A SECOND ratchet now stacks on top of the breakeven step and stays
below the target: once price reaches +2R (see `RANGE_SECOND_RATCHET_TRIGGER_R`)
the stop moves up to +1R (see `RANGE_SECOND_RATCHET_LOCK_R`), locking one
initial-risk-unit of gain. This REDUCES the give-back to a +1R floor once +2R
is tagged. Both steps are Type A only, and both ratchet the stop UP only —
never down. Breakouts are
untouched: they use the structural/chandelier trail from entry and never reach
either R-multiple step.

2026-09-30: the remaining give-back — a run to +5R reversing all the way back
to the +1R lock — was caused by the TARGET being the switch that enabled Type
A structural trailing at all. The target is an unsourced number and the desk
bars a made-up number from governing an exit, so it is no longer that switch.
Once a Type A position is past the ratified +2R trigger, the ordinary
structural / chandelier trail runs for it exactly as it does for Type B, and
it may never place a stop below the +1R lock or below wherever the stop
already is. The two ratchets are untouched and still take precedence; the
target survives as a signal for everything else it feeds.

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
#:
#: **2026-09-30: an attempt to re-express this as a reading off the instrument
#: FAILED, and the constant stays at 2.0 with the failure recorded.** The desk's
#: standing doctrine bars a flat picked percentage on a stop or exit, and this
#: one is squarely in scope — so the attempt was made, measured, and is written
#: down here rather than quietly abandoned.
#:
#: The complaint is real and is now MEASURED, not asserted. Expressing "2% of
#: the live stop" in each name's own ATR(14), over the six positions this gate
#: actually refused in production between 2026-09-21 and 2026-09-29:
#:   MRVL 0.31 ATR | NET 0.33 ATR | RKLB 0.34 ATR | AMD 0.47 ATR
#:   META 0.52 ATR | AAPL 0.91 ATR
#: The same nominal rule demands a ratchet nearly three times larger on AAPL
#: than on MRVL. That is exactly the incoherence the doctrine names.
#:
#: The natural repair is `k * ATR`, the unit this module already uses for
#: `NOISE_BAND_ATR_MULTIPLE` and `CHANDELIER_ATR_MULTIPLE`. It was measured
#: against the same production record — the seven refused tightens whose
#: candidate could be reconstructed from daily bars — and it does not work:
#:   * every refused tighten fell between 0.12 and 0.50 ATR;
#:   * any k >= 0.75 blocks ALL SEVEN, strictly MORE than the flat 2% blocks
#:     (which lets one through), so the change would tighten the gate, not
#:     loosen it;
#:   * only k <= 0.5 lets anything through, and choosing 0.25 to admit three
#:     of seven is fitting a constant to the outcomes the data happened to
#:     like. That is barred outright, and it is the same failure mode as the
#:     2.0 it would replace — a picked multiple re-imported through the ATR
#:     door.
#: No published work fetched fixes a minimum stop-adjustment size; the
#: literature on stop placement addresses DISTANCE from price (which is what
#: `NOISE_BAND_ATR_MULTIPLE` and the chandelier already answer), not the
#: minimum INCREMENT worth replacing a resting order for.
#:
#: Deleting the gate instead was considered and rejected on a measured cost,
#: not a preference: `AlpacaBroker.replace_stop_loss` cannot edit an Alpaca
#: OTO stop leg in place, so every replace is a cancel-then-resubmit with a
#: real window in which the position carries no protective order. Removing
#: the gate would have added seven such windows across nine evaluation runs
#: on an eleven-name book. The money cost of a replace is zero (the ledger
#: entry establishes this); the naked-window cost is not.
#:
#: What the same pass DID settle is the redundancy question the ledger left
#: open. On THIS deterministic path there are two gates, not three: the
#: ~2-4-session ratchet cooldown (`_trail_tightened_recently`) is reached
#: only from the discretionary midday `TRAIL_STOP` branch and never from
#: `_apply_deterministic_trails`. And the two that are here are NOT
#: redundant — all seven reconstructed refusals sat OUTSIDE the 1.25-ATR
#: noise band, so the noise band would have admitted every one of them and
#: this gate is doing independent work.
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
#: of gain. This REDUCES the give-back to a +1R floor once +2R is tagged; it
#: does NOT close the give-back gap — between +1R (the lock) and the target the
#: stop is pinned at +1R and no structural trail runs, so a run to +5R then a
#: reversal still gives back to +1R. The residual give-back between +1R and
#: target is unchanged. Both multiples are OWNER APPETITE, ratified by Rex on
#: 2026-09-25 for item 142 ("let's try your recommendation"): they are CHOSEN
#: appetite multiples — 2R is not computed from the +1R breakeven unit by any
#: formula, and the 1R lock equals the breakeven unit only by coincidence of
#: appetite — so both are recorded as `arbitrary`, not `derived` and not
#: sourced. Ratified is not sourced: it records WHO chose the number, not a
#: measurement behind it. R itself is `abs(entry - initial_stop)`, the same
#: denominator the breakeven ratchet uses; never the live (already ratcheted)
#: stop.
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
TRAIL_CODE_RANGE_TRAIL_BELOW_LOCK = "range_structural_trail_would_sit_below_second_ratchet_lock"
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

    **Measured 2026-09-30, live production DB: this function has never once
    produced a stop.** All nine deterministic trails ever placed came from the
    chandelier fallback (eight) or the Type A breakeven ratchet (one); the
    structural leg has contributed zero. The cause is arithmetic, not a bug:
    `window * 2 + 1` = 7 bars are needed before a single low can be confirmed,
    and this desk's positions were 4-9 sessions old when evaluated, with a
    scale-in additionally resetting the caller's bar window to zero until
    `src/pipeline.py::_apply_deterministic_trails` was changed to slice from
    the POSITION OPEN (`Database.get_position_open_timestamp`) instead of the
    latest add.

    **That change is not risk-free, and it was described as such in error.**
    A longer window can only raise `highest`, so it can only raise
    `chandelier = highest - CHANDELIER_ATR_MULTIPLE * ATR`; a candidate that
    rises through the noise floor makes `evaluate_trailing_stop` return
    `TRAIL_CODE_INSIDE_NOISE_BAND` outright, with no fallback to a lower
    level the shorter window would have accepted. Price 100, ATR 4, live stop
    90: a 106 high proposes 94 and the stop tightens; a 108 high proposes 96,
    above the 95 floor, and the stop stays at 90. The wider window can LOSE a
    tighten. The justification is that the old window disagreed by
    construction with the blended `avg_entry` price used in the same call —
    not that the change cannot cost protection.

    **Since then the candidate search was widened, which narrows that
    exposure without moving any number.** `evaluate_trailing_stop` used to
    build the chandelier only when structure produced nothing, so it fixed on
    one candidate BEFORE testing it; it now builds both legs and carries each
    through the same invariants, taking the first that survives. A structural
    pivot inside the noise band therefore no longer suppresses a chandelier
    level that clears it. This does NOT rescue the case above, where the
    chandelier is itself the offending candidate — there is no lower
    already-derived level to fall back to, and synthesising one at the band's
    own edge would trail under today's price, which is the very thing
    `_swing_lows` refuses on principle.

    **The exposure was then MEASURED, not assumed.** Re-running all 21
    recorded refusals through both windows (live DB, 2026-09-30): only 5 have
    a window start that moves at all — every one of them MRVL, the only
    position whose adds fall on different sessions; META's two adds are the
    same session, and the other four names never scaled in. In all 5 the
    verdict is unchanged and NOT ONE lands inside the noise band that did
    not before. Newly-refused-as-inside-noise-band: ZERO.

    The 7-bar floor is what binds in 20 of the 21, but not in all of them,
    and the PR first claimed otherwise. The widest new window (MRVL,
    2026-09-29) holds 8 bars and CLEARS the floor — it still produces no
    pivot, because the eight lows rise almost monotonically and neither of
    the two eligible centre bars is a strict local minimum. Confirmed pivots
    found under the new window: ZERO, same as the old. "Flips none" survives;
    "the 7-bar floor is the only reason" does not.

    So on today's holding periods the chandelier IS the trail, and the
    "trail under each successive higher low" rule in the module docstring
    describes an intent rather than observed behaviour. Shortening
    `PIVOT_WINDOW` would make structure fire, and that is precisely why it
    has not been done: the constant is documented above as unsourceable in
    the literature, and moving it to obtain a result the data would like is
    picking a number.
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


def _range_second_ratchet_state(
    *, ent: float, cur: float, initial_stop: float | None, is_short: bool,
) -> tuple[bool, float | None]:
    """`(price is past the +2R second-ratchet trigger, the +1R lock price)`.

    The SAME arithmetic `_range_second_ratchet` runs, exposed so the Type A
    branch of `evaluate_trailing_stop` can ask the two questions it needs
    without re-deriving anything: has the owner-ratified +2R trigger been
    reached, and where is the owner-ratified +1R lock. No new number is
    introduced — both multiples are the existing ratified constants.

    Fails closed exactly as the ratchet does: without a usable `initial_stop`
    R cannot be measured, so this reports `(False, None)` and the caller
    leaves Type A behaviour untouched.
    """
    init_stop = _finite(initial_stop) if initial_stop is not None else None
    if init_stop is None or init_stop <= 0:
        return (False, None)
    risk = abs(ent - init_stop)
    if risk <= 0:
        return (False, None)
    if is_short:
        trigger = ent - RANGE_SECOND_RATCHET_TRIGGER_R * risk
        lock = ent - RANGE_SECOND_RATCHET_LOCK_R * risk
        return (cur <= trigger, lock)
    trigger = ent + RANGE_SECOND_RATCHET_TRIGGER_R * risk
    lock = ent + RANGE_SECOND_RATCHET_LOCK_R * risk
    return (cur >= trigger, lock)


def _range_second_ratchet(
    *, symbol: str, ent: float, cur: float, stop: float,
    initial_stop: float | None, is_short: bool, setup_type: str | None,
) -> TrailEvaluation:
    """Type A's SECOND ratchet — item 142, owner-ratified 2026-09-25.

    Stacks ON TOP of `_range_breakeven_ratchet` and BELOW the target: once a
    range trade reaches +`RANGE_SECOND_RATCHET_TRIGGER_R`R (2R) of open profit,
    move the stop up to +`RANGE_SECOND_RATCHET_LOCK_R`R (1R), locking in one
    initial-risk-unit of gain. This REDUCES the give-back to a +1R floor once
    +2R is tagged; it does NOT close the gap — the stop is then pinned at +1R
    with no structural trail until the target is exceeded, so a run past +2R
    and a reversal still gives back down to +1R.

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

    # Set only on the Type A past-+2R path below: the +1R lock the ratified
    # second ratchet installed, which a structural trail may never undercut.
    range_trail_floor: float | None = None

    # --- Type A: structural trailing once the position is past +2R ---------
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
            breakeven = _range_breakeven_ratchet(
                symbol=symbol, ent=ent, cur=cur, stop=stop,
                initial_stop=initial_stop, is_short=is_short,
                setup_type=setup_type,
            )
            if breakeven.proposal is not None:
                return breakeven
            # The take-profit target is NOT a measured level, and the desk's
            # doctrine bars a made-up number from governing an exit. It used
            # to be the switch that enabled Type A structural trailing at
            # all, so a range trade that ran to +5R and reversed still gave
            # back to the +1R lock. It stops being that switch here: once the
            # position is past the OWNER-RATIFIED +2R second-ratchet trigger
            # — a level measured off this trade's own initial risk, not off a
            # guessed price — the ordinary structural / chandelier trail runs
            # for Type A too. The target survives untouched as a signal
            # elsewhere; it simply no longer gates protection.
            #
            # Nothing loosens: the structural path below only ever proposes a
            # candidate strictly BETWEEN the live stop and price (mirrored
            # for a short), so the stop can never retreat, and
            # `range_trail_floor` additionally refuses any candidate that
            # would sit below the +1R lock the ratified ratchet installed.
            # Both ratchets above are untouched and still take precedence.
            past_2r, lock_price = _range_second_ratchet_state(
                ent=ent, cur=cur, initial_stop=initial_stop,
                is_short=is_short,
            )
            if not (past_2r and lock_price is not None):
                # Below +2R (or R unmeasurable): unchanged — the two
                # ratchets are the whole of Type A protection here.
                return breakeven
            range_trail_floor = lock_price
        # Target exceeded: fall through to the structural/chandelier trail
        # below exactly as before fix #3 — unchanged.

    # --- Candidate SET: structure first, chandelier second -----------------
    # BOTH legs are now always built. Before this change the chandelier was
    # computed only `if candidate is None`, so the module committed to the
    # structural pivot BEFORE testing it — and a pivot that the invariants
    # below then rejected (most sharply the noise band) silently suppressed a
    # chandelier level that would have passed every one of them. Committing
    # to the first candidate before testing it is a defect in how the
    # candidate is FOUND, not a reason to drop a leg.
    #
    # Nothing about the preference order or the arithmetic changes: structure
    # is still tried first, the chandelier is still second, no new multiple or
    # threshold is introduced, and each candidate is carried through exactly
    # the SAME invariants as before. The first candidate that survives all of
    # them is used. There is deliberately NO synthesised third candidate at
    # the noise band's own edge: a level read off today's price is a pure
    # price-follower, which is a different exit rule from the ratified one and
    # needs an argued decision, not a quiet patch here.
    candidates: list[tuple[float, str]] = []
    if is_short:
        pivot = _structural_pivot(_swing_highs(bars or []), is_short=True)
        # The pivot is only usable if it is BELOW the current stop and ABOVE
        # current price: above the stop is not a ratchet, below the price is
        # not a stop.
        if pivot is not None and cur < pivot < stop:
            candidates.append((pivot, "structure"))
    else:
        pivot = _structural_pivot(_swing_lows(bars or []), is_short=False)
        # Mirror: only a pivot ABOVE the current stop and BELOW current price
        # is usable — below the stop is not a ratchet, above the price is not
        # a stop.
        if pivot is not None and stop < pivot < cur:
            candidates.append((pivot, "structure"))

    if atr_f is not None and atr_f > 0:
        if is_short:
            lows = [_finite(getattr(b, "low", None)) for b in (bars or [])]
            lows = [l for l in lows if l is not None]
            lowest = min(lows) if lows else cur
            chandelier = lowest + CHANDELIER_ATR_MULTIPLE * atr_f
            if cur < chandelier < stop:
                candidates.append((chandelier, "chandelier"))
        else:
            highs = [
                _finite(getattr(b, "high", None)) for b in (bars or [])
            ]
            highs = [h for h in highs if h is not None]
            highest = max(highs) if highs else cur
            chandelier = highest - CHANDELIER_ATR_MULTIPLE * atr_f
            if stop < chandelier < cur:
                candidates.append((chandelier, "chandelier"))

    if not candidates:
        return TrailEvaluation(None, TRAIL_CODE_NO_CANDIDATE)

    # --- Invariants --------------------------------------------------------
    # Ratchet toward less risk only, and only when the move is worth an order,
    # and never inside one ordinary day's range of current price. Applied per
    # candidate. The refusal reported when every candidate fails is the FIRST
    # candidate's, which keeps the recorded reason identical to what this
    # module produced before whenever only one leg offered anything at all —
    # which is every refusal in the production record to date.
    candidate: float | None = None
    source = ""
    first_refusal: str | None = None
    for _cand, _source in candidates:
        _refusal: str | None = None
        if is_short:
            if _cand >= stop * (1 - min_ratchet_pct / 100.0):
                _refusal = TRAIL_CODE_BELOW_MIN_RATCHET
        else:
            if _cand <= stop * (1 + min_ratchet_pct / 100.0):
                _refusal = TRAIL_CODE_BELOW_MIN_RATCHET
        if _refusal is None and atr_f is not None and atr_f > 0:
            if is_short:
                noise_ceiling = cur + NOISE_BAND_ATR_MULTIPLE * atr_f
                if _cand < noise_ceiling:
                    _refusal = TRAIL_CODE_INSIDE_NOISE_BAND
            else:
                noise_floor = cur - NOISE_BAND_ATR_MULTIPLE * atr_f
                if _cand > noise_floor:
                    _refusal = TRAIL_CODE_INSIDE_NOISE_BAND
        if _refusal is None:
            candidate, source = _cand, _source
            break
        if first_refusal is None:
            first_refusal = _refusal

    if candidate is None:
        return TrailEvaluation(None, first_refusal or TRAIL_CODE_NO_CANDIDATE)

    candidate = round(candidate, 2)
    if is_short:
        if candidate >= stop or candidate <= cur:
            return TrailEvaluation(None, TRAIL_CODE_ROUNDED_OFF_SIDE)
    else:
        if candidate <= stop or candidate >= cur:
            return TrailEvaluation(None, TRAIL_CODE_ROUNDED_OFF_SIDE)

    # Type A past +2R only: never place a stop below (above, for a short)
    # the owner-ratified +1R lock. In practice the ratchet has already put
    # the live stop at or beyond the lock and the guards above require the
    # candidate to beat that stop, so this is a belt-and-braces refusal.
    if range_trail_floor is not None:
        if is_short:
            if candidate > range_trail_floor:
                return TrailEvaluation(None, TRAIL_CODE_RANGE_TRAIL_BELOW_LOCK)
        else:
            if candidate < range_trail_floor:
                return TrailEvaluation(None, TRAIL_CODE_RANGE_TRAIL_BELOW_LOCK)

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
