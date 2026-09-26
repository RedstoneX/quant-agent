"""Re-derive a held position's take-profit when the structure it was
measured against has changed — and never on an opinion or a price move.

WHY THIS EXISTS
---------------
`PortfolioConstructor._resolve_entry_and_stop` derives the take-profit ONCE,
at entry, from `src.data.levels.derive_structural_target`: the nearest
structural level in the trade's direction if one is reachable inside the
pinned horizon, otherwise an ATR measured move. That number was then frozen
on `trades.take_profit` for the life of the position.

Frozen is wrong. Owner ruling 2026-09-25: the desk re-derives the target off
today's bars on EVERY review (the `require_trigger=False` path below), so a
two-week-old target can never sit ignored. The seat-flag path
(`require_trigger=True`) is unchanged and kept for the case a seat wants to
flag one with evidence; both call the SAME derivation and both hold the entry,
horizon and setup fixed. What follows describes that shared machinery.

Frozen is most obviously wrong when the measurement it was taken from no
longer exists. The
motivating case: a long's target sits on an overhead resistance level, the
name gaps clean through that level on earnings, and the ceiling the target
was measured against is simply gone. The position is then managed against a
target that describes a chart nobody is looking at any more.

WHAT A REVISION IS, AND IS NOT
------------------------------
A revision is a RE-DERIVATION. A seat raises a flag carrying EVIDENCE; this
module re-runs `derive_structural_target` on today's bars and the code
supplies the number. `src.models.TargetRevisionFlag` deliberately has no
price field at all, so a model-typed target price cannot enter the system
even by accident — that is docs/WORK.md item 80 (stop provenance) on a field
that feeds `thesis_progress_pct`, `pace` and therefore the exit guard.

Three things are held fixed across the re-derivation, so that only MEASURED
inputs move:

* the ENTRY PRICE. The question a target answers is "how far does this
  instrument travel FROM THIS ENTRY over this horizon". Re-deriving from
  today's price would make the target a function of the price move, which is
  exactly the thing a revision must not be legitimised by.
* the PINNED HORIZON (`trades.expected_horizon_sessions`, pinned at BUY).
  `derive_structural_target` returns `REFUSAL_NO_HORIZON` without one, and
  recomputing it would reintroduce the moving yardstick that Phase 3.1
  removed from `pace`. Two owner decisions are open on the horizon itself;
  reusing the pinned value keeps this module clear of both.
* the SETUP TYPE, pinned at entry beside the horizon, because it selects the
  measured-move branch inside the derivation.

Only `levels`, `atr` and `levels_coverage` are re-read from today's bars —
with the levels put through `levels_still_in_the_way` first, because
`find_structural_levels` keeps returning a ceiling price has gapped through
and the derivation partitions against entry, so without that filter a
re-derivation hands back the very level that just broke.

Holding the entry and the pinned horizon fixed has one honest cost, recorded
at the check that enforces it: the reach stays measured from entry, so after
a big run the only derivable target can be one the price has already passed.
That is refused by name (`REVISION_BEHIND_PRICE`) and the old target stands,
rather than storing a target behind the price. In practice the structural
break that legitimises a revision arrives with an ATR expansion, which
widens the reach enough to reach the next level.

WHAT LEGITIMISES ONE
--------------------
A structural event, never a price move and never a judgement. Two, and no
third:

1. `TRIGGER_LEVEL_BROKEN` — the level the target was measured against has
   been closed through, and the break is CONFIRMED on two consecutive
   trading-day closes. Both halves of that are existing desk convention,
   reused rather than reinvented: the margin is
   `exit_guard.NOISE_BAND_ATR_MULTIPLE` (the desk's one answer to "is a move
   beyond a level real"), and the two-close confirmation is the same
   CONFIRMATION GATE `exit_guard.check_structural_protection` applies, for
   the same reason — a one-day spring or an intrabar wick is not a break.

2. `TRIGGER_TARGET_BEYOND_REACH` / `TRIGGER_TARGET_INSIDE_NOISE` — today's
   ATR has moved enough that the stored target no longer passes the
   derivation's OWN two tests. `derive_structural_target` accepts a level
   only when it is farther than `atr * min_target_atr_multiple` (the noise
   floor) and no farther than `horizon_reach(atr, horizon)`. Re-apply those
   same two tests to the stored target against today's ATR: if the target it
   would no longer accept, the reach measurement is measuring something
   different and the derivation is stale.

   NO NEW THRESHOLD IS INTRODUCED for "ATR changed enough". The condition is
   read off the constants the derivation already uses, because inventing a
   percentage here would be an arbitrary number on the live risk path.

WHAT A REVISION CANNOT DO
-------------------------
It cannot move `thesis_progress_pct` or `pace`. Those are re-based on the
PINNED entry target (`trades.initial_take_profit`) in
`TradingPipeline._build_position_facts`, precisely because the target is the
DENOMINATOR of both: raising a target lowers progress and lowers pace, which
would register in `exit_guard.MetricDeltas.worsened`, which would clear
`net_improved`, which would switch OFF `veto_contradicted_exit` — turning a
revision on good news into a licence for a "this position is stalling" SELL.
See `tests/test_target_revision.py`.

It also cannot trigger an exit. Nothing in THIS MODULE exits anything: it
adjusts a measurement and returns, and `tests/test_target_revision.py` pins
that it names no order, no broker and no exit action.

What happens AT the number it produces is decided elsewhere and is no longer
nothing. Owner ruling 2026-09-25: reaching a structural target is a decision
point whose default is to close the position in full, taken by
`src.risk.exit_guard.decide_at_target` and executed by the pipeline; the desk
holds instead only while the chart is clearly still trending. The FIXED-GAIN
trim deleted in PR #321 stays deleted — that sold a fixed fraction at a fixed
gain with no reference to the instrument — and the trailing stop still runs
underneath everything. Calling it "the only automatic exit" was true when this
module was written and stopped being true the day the target became a decision.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from src.data.levels import (
    BREAKOUT_PROJECTION_ATR_MULTIPLE,
    COVERAGE_UNKNOWN,
    MAX_HORIZON_SESSIONS,
    MAX_REACH_ATR_MULTIPLE,
    MIN_TARGET_ATR_MULTIPLE,
    TargetDerivation,
    derive_structural_target,
    horizon_reach,
)
from src.risk.exit_guard import NOISE_BAND_ATR_MULTIPLE

logger = logging.getLogger(__name__)

__all__ = [
    "TRIGGER_LEVEL_BROKEN",
    "TRIGGER_TARGET_BEYOND_REACH",
    "TRIGGER_TARGET_INSIDE_NOISE",
    "TRIGGER_EACH_REVIEW_REREAD",
    "REVISION_NO_TRIGGER",
    "REVISION_BREAK_PENDING_CONFIRMATION",
    "REVISION_NO_PINNED_HORIZON",
    "REVISION_NO_STORED_TARGET",
    "REVISION_UNMEASURABLE_INPUTS",
    "REVISION_NO_CEILING_LEFT",
    "REVISION_BEHIND_PRICE",
    "REVISION_WOULD_WEAKEN",
    "REVISION_NO_CHANGE",
    "TargetRevisionOutcome",
    "level_backing_target",
    "levels_still_in_the_way",
    "target_level_broken",
    "stale_reach_trigger",
    "assess_target_revision",
]

# --- Triggers: what legitimises re-deriving -------------------------------

#: The level the target was measured against has been closed through by at
#: least one noise band, on two consecutive trading-day closes.
TRIGGER_LEVEL_BROKEN = "TARGET_LEVEL_BROKEN_CONFIRMED"

#: Today's ATR puts the stored target past `horizon_reach` for the pinned
#: horizon — the derivation would no longer accept it as reachable.
TRIGGER_TARGET_BEYOND_REACH = "TARGET_BEYOND_TODAYS_REACH"

#: Today's ATR puts the stored target inside the derivation's own noise
#: floor — it no longer clears the instrument's own daily range.
TRIGGER_TARGET_INSIDE_NOISE = "TARGET_INSIDE_TODAYS_NOISE_FLOOR"

#: The desk re-reads the structural target off TODAY's bars on EVERY review
#: (owner ruling 2026-09-25), not only when a structural event or a seat flag
#: legitimises asking. This is the trigger recorded when `require_trigger` is
#: False and none of the three structural triggers above fired: the target is
#: still re-derived from the instrument's current structure, so a two-week-old
#: target can never sit ignored. It introduces NO new number — the re-derivation
#: uses the same pinned entry/horizon/setup and the same derivation constants as
#: every other path; "each review" is a schedule, not a threshold.
TRIGGER_EACH_REVIEW_REREAD = "EACH_REVIEW_STRUCTURAL_REREAD"

# --- Outcomes that are NOT a revision, each recorded by name --------------

#: The seat's flag was real but no structural event backs it. This is the
#: expected outcome for "the seat thinks there is more upside", and it is a
#: recorded refusal rather than a silent no-op.
REVISION_NO_TRIGGER = "REFUSAL_NO_STRUCTURAL_EVENT"

#: The target's level was closed through TODAY but not on the prior trading
#: day's close. Same one-day-spring guard as `check_structural_protection`.
REVISION_BREAK_PENDING_CONFIRMATION = "REFUSAL_BREAK_PENDING_CONFIRMATION"

#: No `expected_horizon_sessions` on the trade row. Legacy rows predate the
#: pin; the horizon is never recomputed, so these can never be revised.
REVISION_NO_PINNED_HORIZON = "REFUSAL_NO_PINNED_HORIZON"

#: No usable take-profit on the row to revise in the first place.
REVISION_NO_STORED_TARGET = "REFUSAL_NO_STORED_TARGET"

#: Bars/indicators for the symbol could not be read, so the trigger tests
#: themselves cannot run. A DATA FAULT, not a judgement.
REVISION_UNMEASURABLE_INPUTS = "FAULT_NO_BARS_FOR_REVISION"

#: A trigger fired, the chart HAS structure, and the price has closed
#: decisively beyond all of it — nothing in the trade's direction is left to
#: measure against. Distinct from the derivation's REFUSAL_NO_STRUCTURE,
#: which asserts the history holds no level at all.
REVISION_NO_CEILING_LEFT = "REFUSAL_NO_STRUCTURE_LEFT_IN_DIRECTION"

#: A trigger fired and today's bars yield a target the price has already
#: passed. The old target stands — see the note at the check itself.
REVISION_BEHIND_PRICE = "REFUSAL_DERIVED_TARGET_BEHIND_PRICE"

#: A re-derivation landed on a target CLOSER to entry than the one already
#: stored — it would step the take-profit DOWN and weaken it. Owner ruling
#: 2026-09-25: a re-derivation may only EXTEND the target further from entry
#: (upward for a long, downward for a short), for a clear runner; it may never
#: pull the take-profit back toward entry. The stored target stands.
REVISION_WOULD_WEAKEN = "REFUSAL_WOULD_STEP_TARGET_TOWARD_ENTRY"

#: A trigger fired and the re-derivation succeeded, but it landed on the
#: same price. Recorded so the flag is never a blank.
REVISION_NO_CHANGE = "NO_CHANGE_ON_REDERIVATION"


@dataclass(frozen=True)
class TargetRevisionOutcome:
    """What happened to one flag, on one symbol.

    Exactly one of `new_price` / `refusal` / `fault` is meaningful, mirroring
    `src.data.levels.TargetDerivation`'s own split between a trade judgement
    and a data fault. `code` is always set — a flag never produces a blank.
    """

    symbol: str
    code: str
    detail: str = ""
    trigger: str = ""
    new_price: float | None = None
    prior_price: float | None = None
    basis: str = ""
    level_used: float | None = None
    refusal: str = ""
    fault: str = ""

    @property
    def revised(self) -> bool:
        return self.new_price is not None and self.code == self.trigger


def _finite(value: object) -> float | None:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def level_backing_target(
    *,
    stored_target: float | None,
    computed_levels: list | tuple | None,
    level_cluster_tolerance_pct: float,
) -> float | None:
    """Which computed structural level the stored target sits on, or None.

    The entry derivation recorded `TargetDerivation.level_used`, but that
    field is not persisted on the trade row — only the resulting price is.
    So the level is RECOVERED here by the same IDENTITY test the stop side
    already uses (`exit_guard._structural_level_backing_stop`): the closest
    computed level whose own zone, `level_cluster_tolerance_pct` of its
    price, contains the stored target. That constant is
    `src.data.levels.CLUSTER_TOLERANCE_PCT` — the one
    `find_structural_levels` clustered the pivots with — and is passed in
    rather than imported so there is only ever one set of numbers.

    Returning None is the honest answer for a `measured_move` target: it was
    never measured against a level, so no level of its can break. Such a
    position is still revisable on the ATR/reach trigger, which needs no
    level at all.
    """
    target = _finite(stored_target)
    if target is None or target <= 0:
        return None
    try:
        tol = float(level_cluster_tolerance_pct)
    except (TypeError, ValueError):
        return None
    best: float | None = None
    best_gap = float("inf")
    for raw in computed_levels or []:
        price = _finite(raw)
        if price is None or price <= 0:
            continue
        zone = abs(price) * tol / 100.0
        gap = abs(price - target)
        if gap <= zone and gap < best_gap:
            best, best_gap = price, gap
    return best


def levels_still_in_the_way(
    *,
    computed_levels: list | tuple | None,
    close_price: float | None,
    atr: float | None,
    is_short: bool,
    noise_band_atr_multiple: float = NOISE_BAND_ATR_MULTIPLE,
) -> list[float]:
    """Drop the levels TODAY'S CLOSE has already decisively cleared.

    WHY THIS IS NECESSARY, not a refinement. `find_structural_levels` reads
    pivots out of the whole bar history, so a ceiling price has gapped
    through is STILL in its output — reclassified as support, but present in
    the union of levels. And `derive_structural_target` partitions that
    union against the ENTRY price, so a broken overhead level is still
    "above entry" and would simply be picked again. Re-deriving without this
    filter returns the same number in exactly the motivating case: the
    ceiling is gone, and the derivation hands it back.

    The test applied is the SAME break test as `target_level_broken` — one
    `NOISE_BAND_ATR_MULTIPLE` beyond the level on a completed daily close —
    applied to every level rather than only the one the target sat on. No
    new constant, and no second definition of "broken": a resistance the
    price has closed decisively above is not overhead any more, whichever
    level it happens to be.

    Degrades to the levels unchanged when the close or ATR is missing, so a
    missing input can never silently empty the level set and turn a
    structural read into a measured move.
    """
    levels = [p for p in (_finite(lv) for lv in computed_levels or ()) if p is not None]
    close = _finite(close_price)
    vol = _finite(atr)
    if close is None or vol is None or vol <= 0:
        return levels
    margin = vol * noise_band_atr_multiple
    if is_short:
        # A short's targets sit below; a level the close has fallen a noise
        # band beneath is no longer a floor in the way.
        return [p for p in levels if close > p - margin]
    return [p for p in levels if close < p + margin]


def target_level_broken(
    *,
    target_level: float | None,
    close_price: float | None,
    atr: float | None,
    is_short: bool,
    noise_band_atr_multiple: float = NOISE_BAND_ATR_MULTIPLE,
) -> bool | None:
    """True when today's CLOSE has cleared the target's level decisively.

    `close_price` MUST be the latest completed DAILY CLOSE, never a live
    quote — a wick through a level that closes back inside is not a break.
    Same requirement, and same `NOISE_BAND_ATR_MULTIPLE` margin, as
    `exit_guard.check_structural_protection`.

    Direction is the mirror of the stop's case. A long's target sits ABOVE
    entry on overhead resistance, so ITS level breaks when price closes
    ABOVE it — a favourable break, which is the whole motivating case. A
    short's target sits below entry, and breaks on a close below.

    Returns None when the question cannot be asked (no level on record for
    this target, no close, no ATR) so the caller can file a data fault
    rather than read a missing input as "not broken".
    """
    level = _finite(target_level)
    close = _finite(close_price)
    vol = _finite(atr)
    if level is None or level <= 0 or close is None or vol is None or vol <= 0:
        return None
    margin = vol * noise_band_atr_multiple
    if is_short:
        return close <= level - margin
    return close >= level + margin


def stale_reach_trigger(
    *,
    entry_price: float | None,
    stored_target: float | None,
    atr: float | None,
    horizon_sessions: int | None,
    min_target_atr_multiple: float = MIN_TARGET_ATR_MULTIPLE,
    max_reach_atr_multiple: float = MAX_REACH_ATR_MULTIPLE,
    max_horizon_sessions: int = MAX_HORIZON_SESSIONS,
) -> str:
    """Re-apply the derivation's OWN two acceptance tests to the stored
    target using TODAY's ATR; return the trigger code, or "".

    `derive_structural_target` accepts a level only when its distance from
    entry is both (a) beyond `atr * min_target_atr_multiple` — the noise
    floor — and (b) within `horizon_reach(atr, horizon)`. Those two bounds
    are functions of ATR, so a large enough change in ATR moves the stored
    target outside them. That, and nothing else, is this desk's definition
    of "ATR has changed enough that the reach measurement is measuring
    something different": no new constant is introduced, because the
    derivation already owns both bounds.
    """
    entry = _finite(entry_price)
    target = _finite(stored_target)
    vol = _finite(atr)
    if entry is None or target is None or vol is None or vol <= 0:
        return ""
    reach = horizon_reach(
        vol, horizon_sessions,
        max_reach_atr_multiple=max_reach_atr_multiple,
        max_horizon_sessions=max_horizon_sessions,
    )
    distance = abs(target - entry)
    if reach is not None and distance > reach:
        return TRIGGER_TARGET_BEYOND_REACH
    if distance <= vol * min_target_atr_multiple:
        return TRIGGER_TARGET_INSIDE_NOISE
    return ""


def assess_target_revision(
    *,
    symbol: str,
    direction: str,
    entry_price: float | None,
    stored_target: float | None,
    target_level: float | None,
    pinned_horizon_sessions: int | None,
    setup_type: str | None,
    levels: list[float] | tuple[float, ...] | None,
    atr: float | None,
    close_price: float | None,
    levels_coverage: str = COVERAGE_UNKNOWN,
    break_seen_prior_close: bool = False,
    min_target_atr_multiple: float = MIN_TARGET_ATR_MULTIPLE,
    breakout_projection_atr_multiple: float = BREAKOUT_PROJECTION_ATR_MULTIPLE,
    max_reach_atr_multiple: float = MAX_REACH_ATR_MULTIPLE,
    max_horizon_sessions: int = MAX_HORIZON_SESSIONS,
    noise_band_atr_multiple: float = NOISE_BAND_ATR_MULTIPLE,
    require_trigger: bool = True,
) -> TargetRevisionOutcome:
    """Decide whether one symbol's target may be re-derived, and if so
    re-derive it. Pure — no DB, no broker, no market-data, no LLM.

    `require_trigger` is the whole difference between the two ways this desk
    reaches this function (owner ruling 2026-09-25):

    * True (the SEAT-FLAG path, unchanged) — trigger first, re-derivation
      second: the re-derivation is only consulted once a STRUCTURAL EVENT has
      legitimised asking (a confirmed level break, or today's ATR putting the
      stored target outside the derivation's own reach/noise bounds). A seat's
      evidence is never itself a trigger, and an opinion with no structural
      event behind it is refused by name (`REVISION_NO_TRIGGER`).

    * False (the EVERY-REVIEW path) — the desk re-derives the structural target
      off today's bars on EVERY review, so a stale target can never sit
      ignored. When none of the three structural triggers fired the re-read
      still runs, recorded under `TRIGGER_EACH_REVIEW_REREAD`. This is NOT a
      new number or a looser rule: it is the SAME derivation, with the SAME
      pinned entry/horizon/setup and the SAME constants; only its SCHEDULE
      changed from "on a structural event" to "every review". Every protective
      refusal below still applies unchanged — the two-consecutive-close spring
      guard (`REVISION_BREAK_PENDING_CONFIRMATION`), a target the price has
      already passed (`REVISION_BEHIND_PRICE`), a chart with no structure left
      in the trade's direction (`REVISION_NO_CEILING_LEFT`), unmeasurable
      inputs (`FAULT_NO_BARS_FOR_REVISION`) and no pinned horizon
      (`REVISION_NO_PINNED_HORIZON`) all still leave the last good target
      standing rather than blanking it.
    """
    sym = str(symbol or "").strip().upper()
    is_short = str(direction or "").strip().lower() == "short"

    entry = _finite(entry_price)
    target = _finite(stored_target)
    if target is None or target <= 0:
        return TargetRevisionOutcome(
            symbol=sym, code=REVISION_NO_STORED_TARGET,
            refusal=REVISION_NO_STORED_TARGET,
            detail=(
                "no usable take-profit is stored on this position's opening "
                "row, so there is no derivation to revise"
            ),
        )

    horizon = None
    try:
        horizon = int(pinned_horizon_sessions) if pinned_horizon_sessions else None
    except (TypeError, ValueError):
        horizon = None
    if not horizon or horizon <= 0:
        # The horizon is pinned at BUY and never recomputed (README /
        # Phase 3.1). Without one, the derivation returns
        # REFUSAL_NO_HORIZON anyway; refusing here names the real cause
        # instead of reporting a derivation failure.
        return TargetRevisionOutcome(
            symbol=sym, code=REVISION_NO_PINNED_HORIZON,
            refusal=REVISION_NO_PINNED_HORIZON, prior_price=target,
            detail=(
                "no expected_horizon_sessions was pinned at entry for this "
                "position, and the horizon is never recomputed — there is no "
                "period over which to re-ask how far this symbol travels"
            ),
        )

    vol = _finite(atr)
    close = _finite(close_price)
    if entry is None or vol is None or vol <= 0 or close is None:
        return TargetRevisionOutcome(
            symbol=sym, code=REVISION_UNMEASURABLE_INPUTS,
            fault=REVISION_UNMEASURABLE_INPUTS, prior_price=target,
            detail=(
                "DATA FAULT: no usable entry price, ATR reading or completed "
                "daily close could be obtained, so whether the target's "
                "structure has changed cannot be measured at all"
            ),
        )

    # --- Trigger 1: the level the target sat on, closed through and
    # confirmed. Only asked when the entry derivation recorded a level; a
    # measured-move target never sat on one.
    trigger = ""
    broken = target_level_broken(
        target_level=target_level, close_price=close, atr=vol,
        is_short=is_short, noise_band_atr_multiple=noise_band_atr_multiple,
    )
    if broken:
        if not break_seen_prior_close:
            # Same CONFIRMATION GATE as check_structural_protection: one
            # close beyond a level can be a spring, and a target re-derived
            # off a one-day break would have to be re-derived back again.
            return TargetRevisionOutcome(
                symbol=sym, code=REVISION_BREAK_PENDING_CONFIRMATION,
                refusal=REVISION_BREAK_PENDING_CONFIRMATION,
                prior_price=target, level_used=_finite(target_level),
                detail=(
                    f"the level this target was measured against "
                    f"(${_finite(target_level):,.2f}) was closed through on "
                    f"today's close but not on the prior trading day's — the "
                    f"break is not yet confirmed, so the target stands"
                ),
            )
        trigger = TRIGGER_LEVEL_BROKEN

    # --- Trigger 2: today's ATR puts the stored target outside the
    # derivation's own acceptance bounds.
    if not trigger:
        trigger = stale_reach_trigger(
            entry_price=entry, stored_target=target, atr=vol,
            horizon_sessions=horizon,
            min_target_atr_multiple=min_target_atr_multiple,
            max_reach_atr_multiple=max_reach_atr_multiple,
            max_horizon_sessions=max_horizon_sessions,
        )

    if not trigger:
        if require_trigger:
            return TargetRevisionOutcome(
                symbol=sym, code=REVISION_NO_TRIGGER, refusal=REVISION_NO_TRIGGER,
                prior_price=target, level_used=_finite(target_level),
                detail=(
                    "no structural event backs this flag: the level the target "
                    "was measured against is intact on the latest close, and "
                    "today's ATR still puts the target inside the same reach and "
                    "noise bounds the derivation accepted it under. A view that "
                    "there is further upside is not a trigger"
                ),
            )
        # EVERY-REVIEW path (owner ruling 2026-09-25): no structural event, but
        # the desk still re-reads the target off today's bars so it cannot sit
        # frozen. The re-derivation below runs on exactly the pinned inputs; if
        # today's structure is unchanged it will land on the same price and be
        # recorded as REVISION_NO_CHANGE, never as a silent no-op.
        trigger = TRIGGER_EACH_REVIEW_REREAD

    # --- Re-derive. Entry, horizon and setup_type are the pinned values;
    # only levels, ATR and coverage come from today's bars.
    raw_levels = [p for p in (_finite(lv) for lv in levels or ()) if p is not None]
    surviving = levels_still_in_the_way(
        computed_levels=raw_levels, close_price=close, atr=vol,
        is_short=is_short, noise_band_atr_multiple=noise_band_atr_multiple,
    )
    if raw_levels and not surviving:
        # The chart HAS structure; the price has closed decisively beyond all
        # of it. Refused under its own name rather than by handing the
        # derivation an empty list — that would come back
        # REFUSAL_NO_STRUCTURE, which asserts the price history holds no
        # level, and the record must not say something untrue about the
        # chart. The measured-move projection is not substituted here
        # either: measured from the pinned entry it lands, by construction,
        # at most one horizon's travel from a price that has already run
        # past every level, which is the REVISION_BEHIND_PRICE case below.
        return TargetRevisionOutcome(
            symbol=sym, code=REVISION_NO_CEILING_LEFT,
            refusal=REVISION_NO_CEILING_LEFT, trigger=trigger,
            prior_price=target, level_used=_finite(target_level),
            detail=(
                f"{trigger} fired, but the latest close of ${close:,.2f} has "
                f"closed decisively beyond every one of the "
                f"{len(raw_levels)} level(s) on this chart — today's bars "
                f"hold no structure left in this trade's direction to "
                f"measure a target against, so the stored ${target:,.2f} "
                f"stands"
            ),
        )
    derivation: TargetDerivation = derive_structural_target(
        entry_price=entry,
        direction=direction,
        levels=surviving,
        atr=vol,
        horizon_sessions=horizon,
        setup_type=setup_type,
        model_target=None,
        min_target_atr_multiple=min_target_atr_multiple,
        breakout_projection_atr_multiple=breakout_projection_atr_multiple,
        max_reach_atr_multiple=max_reach_atr_multiple,
        max_horizon_sessions=max_horizon_sessions,
        levels_coverage=levels_coverage or COVERAGE_UNKNOWN,
    )
    if derivation.price is None:
        # A refusal or fault from the re-derivation leaves the old target
        # exactly where it is, and is filed under its own machine code.
        return TargetRevisionOutcome(
            symbol=sym,
            code=derivation.fault or derivation.refusal or REVISION_NO_TRIGGER,
            trigger=trigger, prior_price=target,
            refusal=derivation.refusal, fault=derivation.fault,
            detail=(
                f"{trigger} fired, but today's bars yield no derivable "
                f"target — the stored ${target:,.2f} stands: "
                f"{derivation.detail}"
            ),
        )

    new_price = float(derivation.price)
    # A target must be somewhere the position has not already been. The
    # constructor applies exactly this check at entry (`derivation.price <=
    # entry_price` is a rejection in `_resolve_entry_and_stop`); here the
    # reference is the latest completed close, because that is where the
    # position actually is by the time a revision is being considered.
    #
    # This is the honest failure mode of holding the ENTRY and the PINNED
    # HORIZON fixed across the re-derivation. Reach is measured from entry
    # over the whole pinned horizon, so once price has run, the only levels
    # the derivation will accept are ones still within that distance OF
    # ENTRY — and if none survive the break filter, the measured-move
    # fallback projects from entry too and can land behind the current
    # price. In practice the structural break that legitimises a revision
    # comes with an ATR expansion, which widens the reach enough to pick up
    # the next level; when it does not, refusing and leaving the old target
    # standing is the truthful answer. Re-anchoring the reach on the current
    # price would need the REMAINING horizon, and the horizon has two open
    # owner decisions on it — this module deliberately does not touch them.
    ahead = new_price < close if is_short else new_price > close
    if not ahead:
        return TargetRevisionOutcome(
            symbol=sym, code=REVISION_BEHIND_PRICE,
            refusal=REVISION_BEHIND_PRICE, trigger=trigger,
            prior_price=target, basis=derivation.basis,
            level_used=derivation.level_used,
            detail=(
                f"{trigger} fired, but the only target derivable from the "
                f"pinned ${entry:,.2f} entry over the pinned {horizon}-session "
                f"horizon is ${new_price:,.2f}, which the latest close of "
                f"${close:,.2f} has already passed — the stored "
                f"${target:,.2f} stands"
            ),
        )
    if round(new_price, 2) == round(target, 2):
        return TargetRevisionOutcome(
            symbol=sym, code=REVISION_NO_CHANGE, trigger=trigger,
            prior_price=target, basis=derivation.basis,
            level_used=derivation.level_used,
            detail=(
                f"{trigger} fired and the re-derivation ran, but it landed on "
                f"the same ${target:,.2f} ({derivation.basis})"
            ),
        )

    # MONOTONIC EXTENSION ONLY (owner ruling 2026-09-25). A re-derivation may
    # only push the target FURTHER from entry — up for a long, down for a short
    # — for a clear runner. A closer target would weaken the take-profit and
    # would let the "reached the target" test fire early on a lower number, so
    # it is refused by name and the stored target stands. The take-profit is a
    # floor that only ratchets away from entry, never a number that drifts back.
    further = new_price < target if is_short else new_price > target
    if not further:
        return TargetRevisionOutcome(
            symbol=sym, code=REVISION_WOULD_WEAKEN,
            refusal=REVISION_WOULD_WEAKEN, trigger=trigger,
            prior_price=target, basis=derivation.basis,
            level_used=derivation.level_used,
            detail=(
                f"{trigger} fired and today's bars derive ${new_price:,.2f}, "
                f"which sits CLOSER to the ${entry:,.2f} entry than the stored "
                f"${target:,.2f} — a re-derivation may only extend the target "
                f"further out, never step it back toward entry, so the stored "
                f"${target:,.2f} stands"
            ),
        )

    return TargetRevisionOutcome(
        symbol=sym, code=trigger, trigger=trigger,
        new_price=round(new_price, 2), prior_price=target,
        basis=derivation.basis, level_used=derivation.level_used,
        detail=(
            f"{trigger}: re-derived on today's bars from the pinned "
            f"${entry:,.2f} entry and {horizon}-session horizon — "
            f"${target:,.2f} -> ${new_price:,.2f} ({derivation.basis}). "
            f"{derivation.detail}"
        ),
    )
