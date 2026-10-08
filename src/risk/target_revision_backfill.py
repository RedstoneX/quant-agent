"""Bug-fix backfill assessment, lifted verbatim from
`src.risk.target_revision`."""

from __future__ import annotations

from src.data.levels import (
    BREAKOUT_PROJECTION_ATR_MULTIPLE,
    COVERAGE_UNKNOWN,
    MAX_HORIZON_SESSIONS,
    MAX_REACH_ATR_MULTIPLE,
    MIN_TARGET_ATR_MULTIPLE,
)
from src.risk.exit_guard import BREAK_CONFIRMATION_ATR_MULTIPLE
from src.risk.target_revision_basis import (
    REVISION_NO_PINNED_HORIZON,
    REVISION_NO_STORED_TARGET,
    REVISION_UNMEASURABLE_INPUTS,
    TargetRevisionOutcome,
    _finite,
)
from src.risk.target_revision_rederive import _rederive_on_todays_bars

#: The one thing that legitimises re-deriving a target with no market event
#: behind it at all: the code that produced the stored number has been
#: FIXED, so that number is not stale, it is wrong. Deliberately a distinct
#: string from every `TRIGGER_*` above, so a reader of
#: `specialist_evidence` can separate a bug-fix backfill from an
#: evidence-driven revision by the code alone and never has to infer it
#: from a timestamp.
TRIGGER_DERIVATION_CORRECTED = "TARGET_DERIVATION_BUG_CORRECTED"


def assess_bugfix_backfill(
    *,
    symbol: str,
    direction: str,
    entry_price: float | None,
    stored_target: float | None,
    pinned_horizon_sessions: int | None,
    setup_type: str | None,
    levels: list[float] | tuple[float, ...] | None,
    atr: float | None,
    close_price: float | None,
    levels_coverage: str = COVERAGE_UNKNOWN,
    # DELIBERATELY `None`-defaulted rather than repeating the constants as
    # defaults here. Written the other way these five would be five NEW
    # numeric definition sites on the trade-governing path
    # (`config/number_ledger.yaml`, `tests/test_number_sources.py`) — five
    # more places a ratified bar could be changed in one and not the other.
    # A caller that has read them off `risk_engine.config` passes them; a
    # caller that has not gets `src.data.levels`' own module constants,
    # which is where these values live and the only place they are stated.
    min_target_atr_multiple: float | None = None,
    breakout_projection_atr_multiple: float | None = None,
    max_reach_atr_multiple: float | None = None,
    max_horizon_sessions: int | None = None,
    break_margin_atr_multiple: float | None = None,
) -> TargetRevisionOutcome:
    """Re-derive a held position's target because the DERIVATION was wrong,
    not because the chart changed.

    WHY THIS IS A SEPARATE ENTRY POINT AND NOT A THIRD TRIGGER
    ----------------------------------------------------------
    Every trigger `assess_target_revision` accepts is a statement about the
    MARKET: a level broke, or today's ATR moved the stored target outside
    the reach the derivation accepted it under. This one is a statement
    about the DESK: `derive_structural_target` used the noise floor to
    filter its candidate levels, so a wall inside one ATR of entry left the
    candidate set and the target was promoted to the next level out. Any
    target derived before that was fixed may be a number the desk would
    never compute today.

    Putting that through the trigger list would have been wrong twice over.
    It would have made a code deploy look like a market event in the
    record, and — because a corrected target is usually NEARER than the one
    it replaces — a market trigger would have had to be invented for
    symbols whose charts did nothing at all.

    WHAT IS HELD FIXED is exactly what a revision holds fixed, for exactly
    the reasons in this module's docstring: the ENTRY PRICE, the PINNED
    HORIZON and the SETUP TYPE. Only levels, ATR and coverage come from
    today's bars. A correction re-asks the ORIGINAL question with working
    code; it does not ask a new question from today's price.

    WHAT IS NOT ALLOWED, and is the one difference from a revision: the
    re-anchor on the latest close over the remaining horizon (item 114).
    That path exists so a revision can EXTEND a target the price has
    outrun, and it refuses anything not further from entry than the stored
    number. A correction is very often nearer, so the re-anchor could only
    ever suppress it or replace it with a longer reach — neither of which
    is the corrected derivation. `REFUSAL_DERIVED_TARGET_BEHIND_PRICE` is
    therefore the EXPECTED outcome on a position that has already run, and
    it is a correct answer: no number is substituted and the stored target
    stands, flagged rather than quietly replaced.

    Nothing here exits anything and nothing here moves `thesis_progress_pct`
    or `pace`, which stay measured against the pinned
    `trades.initial_take_profit` — the column this path must never write.
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
                "row, so there is no derivation to correct"
            ),
        )

    try:
        horizon = int(pinned_horizon_sessions) if pinned_horizon_sessions else None
    except (TypeError, ValueError):
        horizon = None
    if not horizon or horizon <= 0:
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
                "daily close could be obtained, so the target cannot be "
                "re-derived at all"
            ),
        )

    return _rederive_on_todays_bars(
        sym=sym, direction=direction, is_short=is_short, entry=entry,
        target=target, target_level=None, horizon=horizon,
        setup_type=setup_type, levels=levels, vol=vol, close=close,
        levels_coverage=levels_coverage or COVERAGE_UNKNOWN,
        trigger=TRIGGER_DERIVATION_CORRECTED, sessions_held=None,
        allow_reanchor=False,
        min_target_atr_multiple=(
            MIN_TARGET_ATR_MULTIPLE if min_target_atr_multiple is None
            else min_target_atr_multiple
        ),
        breakout_projection_atr_multiple=(
            BREAKOUT_PROJECTION_ATR_MULTIPLE
            if breakout_projection_atr_multiple is None
            else breakout_projection_atr_multiple
        ),
        max_reach_atr_multiple=(
            MAX_REACH_ATR_MULTIPLE if max_reach_atr_multiple is None
            else max_reach_atr_multiple
        ),
        max_horizon_sessions=(
            MAX_HORIZON_SESSIONS if max_horizon_sessions is None
            else max_horizon_sessions
        ),
        break_margin_atr_multiple=(
            BREAK_CONFIRMATION_ATR_MULTIPLE
            if break_margin_atr_multiple is None
            else break_margin_atr_multiple
        ),
    )
