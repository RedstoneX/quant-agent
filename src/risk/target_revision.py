"""Re-derive a held position's take-profit when the structure it was
measured against has changed — and never on an opinion or a price move.

WHY THIS EXISTS
---------------
`PortfolioConstructor._resolve_entry_and_stop` derives the take-profit ONCE,
at entry, from `src.data.levels.derive_structural_target`: the nearest
structural level in the trade's direction if one is reachable inside the
pinned horizon, otherwise an ATR measured move. That number then sits frozen
on `trades.take_profit` for the life of the position.

Frozen is wrong when the measurement it was taken from no longer exists. The
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

THE ONE RE-ANCHOR, AND WHY IT IS NOT "THE CURRENT PRICE" (item 114)
-------------------------------------------------------------------
That refusal is the EXPECTED outcome for the desk's strongest winners, not
an edge case: the further a position travels, the more certain it becomes
that every level still within one horizon's reach OF ENTRY sits behind the
price. A rule whose refusal rate rises with how right the desk was is not a
safety property.

So the entry-anchored derivation gets exactly ONE fallback, and only after
it has already refused with `REVISION_BEHIND_PRICE`: re-derive once more
with the reach anchored on the LATEST COMPLETED CLOSE over the REMAINING
horizon — `remaining_horizon_sessions` = the pinned horizon minus the
trading sessions the position has already used. Both numbers already exist
and are already used elsewhere on this desk (`trades.
expected_horizon_sessions` pinned at BUY, and the holiday-aware
`broker.trading_sessions_held` that `exit_guard`'s noise band and the
reviewer's own facts already read), so nothing here is invented.

It is the remaining horizon that makes this a measurement rather than a
chase. Re-anchoring on the current price ALONE — the thing this item
forbids — gives a target that is always reachable, because the reach is
regenerated in full every time price moves. Here the reach SHRINKS with
every session the position spends: a position five sessions into a ten
session horizon may reach half as far as it could at entry, and one whose
horizon is spent may not re-anchor at all. The target therefore cannot be
pushed indefinitely by the price move.

Three further conditions keep it honest, all of them refusals back to
`REVISION_BEHIND_PRICE` when unmet:

* the remaining horizon must be READ, not assumed. No pinned horizon or no
  sessions-held count means no re-anchor — never a default.
* the re-anchored target must land on a STRUCTURAL LEVEL still in the way.
  The measured-move fallback is explicitly not accepted here, because
  "close + k * ATR" IS the current price wearing a hat: it exists whatever
  the chart looks like. If the chart holds no level ahead within the
  remaining reach, the refusal stands and the position is on its stop.
* the re-anchored target must sit FURTHER FROM ENTRY than the stored one.
  A revision may extend a target; it may never pull it back toward entry.

WHAT LEGITIMISES ONE
--------------------
A structural event, never a price move and never a judgement. Three, and no
fourth:

1. `TRIGGER_LEVEL_BROKEN` — the level the target was measured against has
   been closed through, and the break is CONFIRMED on two consecutive
   trading-day closes. Both halves of that are existing desk convention,
   reused rather than reinvented: the margin is
   `exit_guard.BREAK_CONFIRMATION_ATR_MULTIPLE` (the desk's one answer to "is
   a close beyond a level real"), and the two-close confirmation is the same
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

3. `TRIGGER_WALL_IN_FRONT_OF_TARGET` — a structural level that is STILL IN
   THE WAY now stands between the entry and the stored target. The target
   is aiming past a wall.

   THIS IS THE MIRROR OF THE MOTIVATING CASE, and the argument for it is
   the same argument, run backwards. Trigger 1 says: the ceiling the target
   was measured against has GONE, so the measurement describes a chart
   nobody is looking at. This one says: a ceiling the measurement did not
   know about has APPEARED between the position and its target, so the
   measurement again describes a chart nobody is looking at. In both cases
   the set of overhead levels that `derive_structural_target` would
   partition today differs from the set it partitioned at entry, and in
   both cases the stored number is the answer to a question about the old
   set. Accepting the first and refusing the second would mean the desk
   revises when the news is good and freezes when it is bad, which is a
   preference, not a measurement.

   It is also the DOCTRINE VIOLATION the entry derivation exists to
   prevent, arriving by a different route: "the target is the nearest wall,
   never the level past it". A target with a standing wall in front of it
   was not wrong when it was derived — no such wall existed — and it is
   wrong now. Nothing in triggers 1 and 2 can see this: trigger 1 asks only
   about the ONE level the target itself sat on, and a new level forming
   somewhere below it leaves that level untouched; trigger 2 asks only
   whether today's ATR has moved the stored distance outside
   `horizon_reach`, and a pivot forming mid-way changes no ATR. Measured on
   the live book 2026-09-30, both AAPL and NOK aim past a level that did
   not exist on their entry dates, and both returned
   `REFUSAL_NO_STRUCTURAL_EVENT` from this function before this trigger
   existed.

   NO NEW CONSTANT AND NO NEW DEFINITION OF "WALL". `walls_between` counts
   only levels that have already survived `levels_still_in_the_way`, which
   is the module's single existing answer to "is this level still
   overhead", and a level sitting ON the stored target is excluded by
   strict inequality — that is a target on its own wall, which is the
   correct outcome rather than a finding. The reference point is the ENTRY,
   never the latest close, for the same reason everything else here is
   measured from entry: measuring the gap from today's price would make the
   trigger fire on every position that has moved, which is a statement
   about the price move and not about the target.

   WHAT IT DOES NOT DO is exit anything, and it does not assert the wall is
   new. Whether a level formed after entry or was there all along is not
   recoverable from the trade row — only the resulting price is stored —
   and it does not need to be: the re-derivation body is the same either
   way, so a row that is really the old derivation bug gets the same
   correct answer from this path as it does from `assess_bugfix_backfill`.

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

It also cannot trigger an exit. Nothing in this module exits anything; the
automatic profit trim was deleted in PR #321 and the trailing stop remains
the only automatic exit.
"""

from __future__ import annotations

import logging

from src.data.levels import (
    BREAKOUT_PROJECTION_ATR_MULTIPLE,
    COVERAGE_UNKNOWN,
    MAX_HORIZON_SESSIONS,
    MAX_REACH_ATR_MULTIPLE,
    MIN_TARGET_ATR_MULTIPLE,
    horizon_reach,
)
from src.risk.exit_guard import BREAK_CONFIRMATION_ATR_MULTIPLE
from src.risk.target_revision_backfill import TRIGGER_DERIVATION_CORRECTED, assess_bugfix_backfill
from src.risk.target_revision_basis import (
    REANCHORED_BASIS,
    REVISION_BEHIND_PRICE,
    REVISION_BREAK_PENDING_CONFIRMATION,
    REVISION_NO_CEILING_LEFT,
    REVISION_NO_CHANGE,
    REVISION_NO_PINNED_HORIZON,
    REVISION_NO_STORED_TARGET,
    REVISION_NO_TRIGGER,
    REVISION_REACH_PENDING_CONFIRMATION,
    REVISION_UNMEASURABLE_INPUTS,
    REVISION_WALL_PENDING_CONFIRMATION,
    SEAT_STRUCTURAL_SWEEP,
    STRUCTURAL_LEVEL_BASIS,
    SWEEP_EVIDENCE,
    TRIGGER_LEVEL_BROKEN,
    TRIGGER_TARGET_BEYOND_REACH,
    TRIGGER_TARGET_INSIDE_NOISE,
    TRIGGER_WALL_IN_FRONT_OF_TARGET,
    TargetRevisionOutcome,
    _finite,
    level_backing_target,
    levels_still_in_the_way,
    remaining_horizon_sessions,
    walls_between,
)
from src.risk.target_revision_rederive import _rederive_on_todays_bars

logger = logging.getLogger(__name__)

__all__ = [
    "TRIGGER_LEVEL_BROKEN",
    "TRIGGER_TARGET_BEYOND_REACH",
    "TRIGGER_TARGET_INSIDE_NOISE",
    "TRIGGER_WALL_IN_FRONT_OF_TARGET",
    "REVISION_NO_TRIGGER",
    "REVISION_BREAK_PENDING_CONFIRMATION",
    "REVISION_REACH_PENDING_CONFIRMATION",
    "REVISION_WALL_PENDING_CONFIRMATION",
    "raw_trigger_flags",
    "REVISION_NO_PINNED_HORIZON",
    "REVISION_NO_STORED_TARGET",
    "REVISION_UNMEASURABLE_INPUTS",
    "REVISION_NO_CEILING_LEFT",
    "REVISION_BEHIND_PRICE",
    "REVISION_NO_CHANGE",
    "REANCHORED_BASIS",
    "STRUCTURAL_LEVEL_BASIS",
    "TargetRevisionOutcome",
    "remaining_horizon_sessions",
    "level_backing_target",
    "levels_still_in_the_way",
    "walls_between",
    "target_level_broken",
    "stale_reach_trigger",
    "assess_target_revision",
    "TRIGGER_DERIVATION_CORRECTED",
    "assess_bugfix_backfill",
    "SEAT_STRUCTURAL_SWEEP",
    "SWEEP_EVIDENCE",
]


def target_level_broken(
    *,
    target_level: float | None,
    close_price: float | None,
    atr: float | None,
    is_short: bool,
    break_margin_atr_multiple: float = BREAK_CONFIRMATION_ATR_MULTIPLE,
) -> bool | None:
    """True when today's CLOSE has cleared the target's level decisively.

    `close_price` MUST be the latest completed DAILY CLOSE, never a live
    quote — a wick through a level that closes back inside is not a break.
    Same requirement, and same `BREAK_CONFIRMATION_ATR_MULTIPLE` margin, as
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
    margin = vol * break_margin_atr_multiple
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

    **ONE test since 2026-09-30, not two.** `derive_structural_target`
    used to accept a level only when its distance from entry was both (a)
    beyond `atr * min_target_atr_multiple` — the noise floor — and (b)
    within `horizon_reach(atr, horizon)`. The noise floor is no longer an
    acceptance test there: it was filtering the candidate set, so a wall
    inside one ATR was deleted and the target promoted to the next level
    out, past structure price had been rejected from (META, 2026-09-21).
    It now only LABELS the result (`TargetDerivation.target_inside_noise`).

    This function's whole contract is to re-apply the derivation's own
    tests, so it has to follow. Leaving the noise arm in place made the
    two modules disagree every session about what the derivation accepts:
    a sub-noise target fired `TRIGGER_TARGET_INSIDE_NOISE`, the
    re-derivation returned the identical price, and the outcome was
    `REVISION_NO_CHANGE` — no write and no harm, but a trigger whose
    stated premise ("the derivation would no longer accept this") had
    become false. A trigger that is always wrong is not a safe trigger to
    leave running.

    Reach remains, and it is still a function of ATR, so a large enough
    change in ATR still moves the stored target outside it. No constant is
    introduced; one was retired.
    """
    entry = _finite(entry_price)
    target = _finite(stored_target)
    vol = _finite(atr)
    if entry is None or target is None or vol is None or vol <= 0:
        return ""
    reach = horizon_reach(
        vol,
        horizon_sessions,
        max_reach_atr_multiple=max_reach_atr_multiple,
        max_horizon_sessions=max_horizon_sessions,
    )
    distance = abs(target - entry)
    if reach is not None and distance > reach:
        return TRIGGER_TARGET_BEYOND_REACH
    # No noise-floor arm — see this function's docstring. The derivation
    # no longer refuses a sub-noise level, so a sub-noise stored target is
    # not evidence that the measurement went stale. `min_target_atr_multiple`
    # is kept in the signature because callers pass it and the constant
    # still governs the LABEL; it no longer gates anything here.
    del min_target_atr_multiple
    return ""


def raw_trigger_flags(
    *,
    entry_price: float | None,
    stored_target: float | None,
    target_level: float | None,
    atr: float | None,
    close_price: float | None,
    horizon_sessions: int | None,
    levels: list[float] | tuple[float, ...] | None,
    is_short: bool,
    min_target_atr_multiple: float = MIN_TARGET_ATR_MULTIPLE,
    max_reach_atr_multiple: float = MAX_REACH_ATR_MULTIPLE,
    max_horizon_sessions: int = MAX_HORIZON_SESSIONS,
    break_margin_atr_multiple: float = BREAK_CONFIRMATION_ATR_MULTIPLE,
) -> dict[str, bool | None]:
    """TODAY'S RAW state of all three triggers, before any confirmation.

    ONE DEFINITION, THREE TRIGGERS. The level-broken trigger has always
    required the same condition on two consecutive completed daily closes:
    today's raw state is persisted, and the next trading day's read asks
    whether the PRIOR close agreed. That is the only confirmation mechanism
    the desk has, and this function exists so the reach and wall triggers
    can be put through the identical one rather than growing a second and
    third idea of "confirmed". No count of closes and no margin is chosen
    here — the count is the existing two, and the only margin involved is
    `BREAK_CONFIRMATION_ATR_MULTIPLE`, which the break test already owned.

    Each value is True, False, or None when the question cannot be asked
    at all on today's inputs — a missing entry, target, ATR or close for
    any of them, and additionally an EMPTY OR MISSING LEVEL SET for the
    wall, because a chart with no levels read off it cannot answer whether
    a level stands in the way. A None is never persisted, and the
    prior-close read skips a row that does not answer the question rather
    than reading the silence as False, so a missing input can never become
    half of a confirmation nor erase an answer already given.
    """
    entry = _finite(entry_price)
    target = _finite(stored_target)
    vol = _finite(atr)
    close = _finite(close_price)
    broken = target_level_broken(
        target_level=target_level,
        close_price=close,
        atr=vol,
        is_short=is_short,
        break_margin_atr_multiple=break_margin_atr_multiple,
    )
    reach: bool | None = None
    wall: bool | None = None
    if entry is not None and target is not None and vol is not None and vol > 0:
        horizon = None
        try:
            horizon = int(horizon_sessions) if horizon_sessions else None
        except (TypeError, ValueError):
            horizon = None
        if horizon and horizon > 0:
            reach = bool(
                stale_reach_trigger(
                    entry_price=entry,
                    stored_target=target,
                    atr=vol,
                    horizon_sessions=horizon,
                    min_target_atr_multiple=min_target_atr_multiple,
                    max_reach_atr_multiple=max_reach_atr_multiple,
                    max_horizon_sessions=max_horizon_sessions,
                )
            )
        # UNASKABLE IS None, AND AN EMPTY LEVEL SET IS UNASKABLE. A
        # degraded bar fetch hands this function `levels=[]`, and
        # `levels_still_in_the_way([])` is `[]`, and `walls_between([])` is
        # a definite "no wall" — so one degraded cycle used to persist
        # "no wall" as a FACT and erase a genuine wall recorded earlier the
        # same day. An absent reading must never produce an action. A
        # structureless chart is indistinguishable from a failed fetch at
        # this layer, and the safe reading of both is "not measured".
        if close is not None and levels:
            wall = bool(
                walls_between(
                    stored_target=target,
                    reference_price=entry,
                    surviving_levels=levels_still_in_the_way(
                        computed_levels=levels,
                        close_price=close,
                        atr=vol,
                        is_short=is_short,
                        break_margin_atr_multiple=break_margin_atr_multiple,
                    ),
                    is_short=is_short,
                )
            )
    return {"raw_broken": broken, "raw_reach": reach, "raw_wall": wall}


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
    reach_seen_prior_close: bool = False,
    wall_seen_prior_close: bool = False,
    sessions_held: int | None = None,
    min_target_atr_multiple: float = MIN_TARGET_ATR_MULTIPLE,
    breakout_projection_atr_multiple: float = BREAKOUT_PROJECTION_ATR_MULTIPLE,
    max_reach_atr_multiple: float = MAX_REACH_ATR_MULTIPLE,
    max_horizon_sessions: int = MAX_HORIZON_SESSIONS,
    break_margin_atr_multiple: float = BREAK_CONFIRMATION_ATR_MULTIPLE,
) -> TargetRevisionOutcome:
    """Decide whether one flagged symbol's target may be re-derived, and if
    so re-derive it. Pure — no DB, no broker, no market-data, no LLM.

    Trigger first, re-derivation second, deliberately in that order: the
    re-derivation is only ever consulted once a structural event has already
    legitimised asking. A seat's evidence is never itself a trigger.
    """
    sym = str(symbol or "").strip().upper()
    is_short = str(direction or "").strip().lower() == "short"

    entry = _finite(entry_price)
    target = _finite(stored_target)
    if target is None or target <= 0:
        return TargetRevisionOutcome(
            symbol=sym,
            code=REVISION_NO_STORED_TARGET,
            refusal=REVISION_NO_STORED_TARGET,
            detail=(
                "no usable take-profit is stored on this position's opening row, so there is no derivation to revise"
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
            symbol=sym,
            code=REVISION_NO_PINNED_HORIZON,
            refusal=REVISION_NO_PINNED_HORIZON,
            prior_price=target,
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
            symbol=sym,
            code=REVISION_UNMEASURABLE_INPUTS,
            fault=REVISION_UNMEASURABLE_INPUTS,
            prior_price=target,
            detail=(
                "DATA FAULT: no usable entry price, ATR reading or completed "
                "daily close could be obtained, so whether the target's "
                "structure has changed cannot be measured at all"
            ),
        )

    # --- THE THREE TRIGGERS ARE EVALUATED TOGETHER, NOT IN SEQUENCE.
    #
    # They used to be asked one after another with an early return on the
    # first pending confirmation, which meant an UNCONFIRMED trigger could
    # suppress a CONFIRMED one: a name with a wall standing on two closes
    # and a one-day ATR blip got no revision at all, and when it finally
    # fired it fired under the reach trigger, so the owner was handed the
    # reach reason and the reach basis for a change the wall had caused.
    # Every trigger is now asked, each is paired with ITS OWN prior-close
    # agreement, and a confirmed one is always preferred over a pending
    # one. The order below is only a tie-break between two CONFIRMED
    # triggers, and it is the old order: narrowest premise first.
    broken = target_level_broken(
        target_level=target_level,
        close_price=close,
        atr=vol,
        is_short=is_short,
        break_margin_atr_multiple=break_margin_atr_multiple,
    )
    reach_trigger = stale_reach_trigger(
        entry_price=entry,
        stored_target=target,
        atr=vol,
        horizon_sessions=horizon,
        min_target_atr_multiple=min_target_atr_multiple,
        max_reach_atr_multiple=max_reach_atr_multiple,
        max_horizon_sessions=max_horizon_sessions,
    )
    walls = (
        walls_between(
            stored_target=target,
            reference_price=entry,
            surviving_levels=levels_still_in_the_way(
                computed_levels=levels,
                close_price=close,
                atr=vol,
                is_short=is_short,
                break_margin_atr_multiple=break_margin_atr_multiple,
            ),
            is_short=is_short,
        )
        if levels
        else []
    )

    level_txt = (
        f"${_finite(target_level):,.2f}" if _finite(target_level) is not None else "the level it was measured against"
    )
    # (fired today, confirmed by the prior close, trigger code, refusal
    # code, refusal words). Narrowest premise first.
    candidates = [
        (
            bool(broken),
            break_seen_prior_close,
            TRIGGER_LEVEL_BROKEN,
            REVISION_BREAK_PENDING_CONFIRMATION,
            f"the level this target was measured against ({level_txt}) was "
            f"closed through on today's close but not on the prior trading "
            f"day's — the break is not yet confirmed, so the target stands",
        ),
        (
            bool(reach_trigger),
            reach_seen_prior_close,
            reach_trigger or TRIGGER_TARGET_BEYOND_REACH,
            REVISION_REACH_PENDING_CONFIRMATION,
            "today's ATR puts the stored target outside the reach the "
            "derivation accepted it under, but the prior trading day's "
            "close did not — one session's ATR reading is not a structural "
            "change, so the target stands",
        ),
        (
            bool(walls),
            wall_seen_prior_close,
            TRIGGER_WALL_IN_FRONT_OF_TARGET,
            REVISION_WALL_PENDING_CONFIRMATION,
            (
                f"a structural level (${walls[0]:,.2f}) stands between the "
                f"entry and the target on today's close but did not on the "
                f"prior trading day's — the wall is not yet confirmed, so "
                f"the target stands"
            )
            if walls
            else "",
        ),
    ]

    trigger = ""
    for fired, confirmed, code, _refusal, _words in candidates:
        if fired and confirmed:
            trigger = code
            break

    if not trigger:
        # Nothing confirmed. A trigger that fired TODAY but has no prior
        # day's agreement is reported as its own pending refusal — it is a
        # hold on a number the desk has stopped believing, not a clean
        # bill of health — and only a chart with no trigger at all gets
        # REVISION_NO_TRIGGER.
        for fired, _confirmed, _code, refusal, words in candidates:
            if fired:
                return TargetRevisionOutcome(
                    symbol=sym,
                    code=refusal,
                    refusal=refusal,
                    prior_price=target,
                    level_used=_finite(target_level),
                    detail=words,
                )

    if not trigger:
        return TargetRevisionOutcome(
            symbol=sym,
            code=REVISION_NO_TRIGGER,
            refusal=REVISION_NO_TRIGGER,
            prior_price=target,
            level_used=_finite(target_level),
            detail=(
                "no structural event backs this flag: the level the target "
                "was measured against is intact on the latest close, today's "
                "ATR still puts the target inside the same reach the "
                "derivation accepted it under, and no structural level "
                "stands between the entry and the target. A view that there "
                "is further upside is not a trigger"
            ),
        )

    return _rederive_on_todays_bars(
        sym=sym,
        direction=direction,
        is_short=is_short,
        entry=entry,
        target=target,
        target_level=target_level,
        horizon=horizon,
        setup_type=setup_type,
        levels=levels,
        vol=vol,
        close=close,
        levels_coverage=levels_coverage or COVERAGE_UNKNOWN,
        trigger=trigger,
        sessions_held=sessions_held,
        allow_reanchor=True,
        min_target_atr_multiple=min_target_atr_multiple,
        breakout_projection_atr_multiple=breakout_projection_atr_multiple,
        max_reach_atr_multiple=max_reach_atr_multiple,
        max_horizon_sessions=max_horizon_sessions,
        break_margin_atr_multiple=break_margin_atr_multiple,
    )
