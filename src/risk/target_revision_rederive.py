"""Re-derivation on today's bars, lifted verbatim from
`src.risk.target_revision`."""

from __future__ import annotations

from src.data.levels import COVERAGE_UNKNOWN, TargetDerivation, derive_structural_target
from src.risk.target_revision_basis import (
    REANCHORED_BASIS,
    REVISION_BEHIND_PRICE,
    REVISION_NO_CEILING_LEFT,
    REVISION_NO_CHANGE,
    REVISION_NO_TRIGGER,
    STRUCTURAL_LEVEL_BASIS,
    TargetRevisionOutcome,
    _finite,
    levels_still_in_the_way,
    remaining_horizon_sessions,
)


def _rederive_on_todays_bars(
    *,
    sym: str,
    direction: str,
    is_short: bool,
    entry: float,
    target: float,
    target_level: float | None,
    horizon: int,
    setup_type: str | None,
    levels: list[float] | tuple[float, ...] | None,
    vol: float,
    close: float,
    levels_coverage: str,
    trigger: str,
    sessions_held: int | None,
    allow_reanchor: bool,
    min_target_atr_multiple: float,
    breakout_projection_atr_multiple: float,
    max_reach_atr_multiple: float,
    max_horizon_sessions: int,
    break_margin_atr_multiple: float,
) -> TargetRevisionOutcome:
    """The re-derivation itself, shared by every caller that is allowed to
    ask for one. Pure — no DB, no broker, no market data, no LLM.

    THIS IS ONE BODY ON PURPOSE. It was lifted out of
    `assess_target_revision` unchanged when `assess_bugfix_backfill` was
    added, rather than copied: two derivations of the same number drift,
    and a target that disagrees with itself depending on which caller asked
    is the exact failure this desk keeps hitting. Everything above this
    point — WHETHER the question may be asked — differs between callers;
    nothing below it does.

    `allow_reanchor` is the single behavioural difference, and it is a
    difference in what the CALLER is entitled to, not in the derivation. A
    revision may extend a target once the entry-anchored reach has been
    outrun (item 114). A correction of a number the derivation itself got
    wrong may not: the re-anchor is defined to only ever move a target
    further from entry, and a correction whose content is "the stored
    number is too far out" would be silently undone by it.
    """
    # --- Re-derive. Entry, horizon and setup_type are the pinned values;
    # only levels, ATR and coverage come from today's bars.
    raw_levels = [p for p in (_finite(lv) for lv in levels or ()) if p is not None]
    surviving = levels_still_in_the_way(
        computed_levels=raw_levels,
        close_price=close,
        atr=vol,
        is_short=is_short,
        break_margin_atr_multiple=break_margin_atr_multiple,
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
            symbol=sym,
            code=REVISION_NO_CEILING_LEFT,
            refusal=REVISION_NO_CEILING_LEFT,
            trigger=trigger,
            prior_price=target,
            level_used=_finite(target_level),
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
            trigger=trigger,
            prior_price=target,
            refusal=derivation.refusal,
            fault=derivation.fault,
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
        # THE ONE RE-ANCHOR (item 114). See the module docstring: the
        # entry-anchored reach has just produced a target the price has
        # passed, which is the EXPECTED outcome for a strong winner. Try
        # once more with the reach anchored on the latest completed close
        # over the REMAINING horizon — never on the close alone, and never
        # on a measured move.
        remaining = (
            remaining_horizon_sessions(
                pinned_horizon_sessions=horizon,
                sessions_held=sessions_held,
            )
            if allow_reanchor
            else None
        )
        reanchor_note = ""
        if not allow_reanchor:
            reanchor_note = (
                "; no re-anchor was attempted, because the caller is "
                "correcting a derivation this desk now knows was wrong "
                "rather than revising one that has gone stale — the "
                "re-anchor may only ever move a target FURTHER from entry, "
                "and the whole content of such a correction is that the "
                "stored number already sits too far out"
            )
        elif remaining is None:
            reanchor_note = (
                "; the remaining horizon could not be read (no holiday-aware "
                "sessions-held count for this position), and it is never "
                "assumed, so no re-anchored derivation was attempted"
            )
        elif remaining <= 0:
            reanchor_note = (
                f"; this position has used all {horizon} of its pinned "
                f"horizon's sessions, so there is no remaining horizon to "
                f"re-anchor the reach on and it is managed by its stop"
            )
        else:
            redo: TargetDerivation = derive_structural_target(
                entry_price=close,
                direction=direction,
                levels=surviving,
                atr=vol,
                horizon_sessions=remaining,
                setup_type=setup_type,
                model_target=None,
                min_target_atr_multiple=min_target_atr_multiple,
                breakout_projection_atr_multiple=breakout_projection_atr_multiple,
                max_reach_atr_multiple=max_reach_atr_multiple,
                max_horizon_sessions=max_horizon_sessions,
                levels_coverage=levels_coverage or COVERAGE_UNKNOWN,
            )
            re_price = _finite(redo.price)
            if re_price is None:
                reanchor_note = (
                    f"; re-anchored on the ${close:,.2f} close over the "
                    f"{remaining} session(s) of horizon this position has "
                    f"left, today's bars still yield no target: "
                    f"{redo.detail}"
                )
            elif redo.basis != STRUCTURAL_LEVEL_BASIS:
                # A measured move from the current close is the current
                # price wearing a hat — it exists whatever the chart looks
                # like, so accepting it here would be the disguised
                # price-chase this item forbids.
                reanchor_note = (
                    f"; the only thing re-anchoring on the ${close:,.2f} "
                    f"close over the remaining {remaining} session(s) "
                    f"produces is a {redo.basis} projection, not a level "
                    f"still in the way — a target measured off the current "
                    f"price with no structure behind it is not a target"
                )
            else:
                re_ahead = re_price < close if is_short else re_price > close
                further = re_price < target if is_short else re_price > target
                if re_ahead and further:
                    return TargetRevisionOutcome(
                        symbol=sym,
                        code=trigger,
                        trigger=trigger,
                        new_price=round(re_price, 2),
                        prior_price=target,
                        basis=REANCHORED_BASIS,
                        level_used=redo.level_used,
                        detail=(
                            f"{trigger}: nothing within one pinned "
                            f"{horizon}-session reach of the ${entry:,.2f} "
                            f"entry is still ahead of the ${close:,.2f} "
                            f"close, so the reach was re-anchored on that "
                            f"close over the {remaining} session(s) of the "
                            f"pinned horizon this position has left — "
                            f"${target:,.2f} -> ${re_price:,.2f} on a "
                            f"structural level still in the way. "
                            f"{redo.detail}"
                        ),
                    )
                reanchor_note = (
                    f"; re-anchored on the ${close:,.2f} close over the "
                    f"remaining {remaining} session(s) the nearest level "
                    f"still in the way is ${re_price:,.2f}, which is not "
                    f"both ahead of that close and further from entry than "
                    f"the stored target — a revision may extend a target, "
                    f"never pull it back toward entry"
                )
        return TargetRevisionOutcome(
            symbol=sym,
            code=REVISION_BEHIND_PRICE,
            refusal=REVISION_BEHIND_PRICE,
            trigger=trigger,
            prior_price=target,
            basis=derivation.basis,
            level_used=derivation.level_used,
            detail=(
                f"{trigger} fired, but the only target derivable from the "
                f"pinned ${entry:,.2f} entry over the pinned {horizon}-session "
                f"horizon is ${new_price:,.2f}, which the latest close of "
                f"${close:,.2f} has already passed — the stored "
                f"${target:,.2f} stands{reanchor_note}"
            ),
        )
    if round(new_price, 2) == round(target, 2):
        return TargetRevisionOutcome(
            symbol=sym,
            code=REVISION_NO_CHANGE,
            trigger=trigger,
            prior_price=target,
            basis=derivation.basis,
            level_used=derivation.level_used,
            detail=(
                f"{trigger} fired and the re-derivation ran, but it landed on "
                f"the same ${target:,.2f} ({derivation.basis})"
            ),
        )

    return TargetRevisionOutcome(
        symbol=sym,
        code=trigger,
        trigger=trigger,
        new_price=round(new_price, 2),
        prior_price=target,
        basis=derivation.basis,
        level_used=derivation.level_used,
        detail=(
            f"{trigger}: re-derived on today's bars from the pinned "
            f"${entry:,.2f} entry and {horizon}-session horizon — "
            f"${target:,.2f} -> ${new_price:,.2f} ({derivation.basis}). "
            f"{derivation.detail}"
        ),
    )
