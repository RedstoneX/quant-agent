"""Board item 219 - why each below-bar holding was kept, recorded at the source.

The pruning pass's `precheck` row stores a reason only for the one name it
cut. The owner's rule is "sell anything below the bar, always", so a name
that was below the bar and SURVIVED is the line that looks like the rule did
not fire, and it must say why. Two causes are decided here, not inferred
later: the pass never reached it, or it was reached and refused (that refusal
is already a durable `rotation`/`skipped` row under the name's own reason,
which the dashboard joins). This module adds one run-scoped
`rotation`/`dispositions` row carrying, per below-bar name, the conviction
reasons it fails the bar on and, where the pass never reached it, the exact
"not reached: <why>". No threshold, limit or lookback is introduced.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _walk_order(precheck) -> list[tuple[str, tuple[str, ...]]]:
    opp = getattr(precheck, "opportunity", None)
    cands = tuple(getattr(opp, "ineligible_candidates", ()) or ())
    if cands:
        return [(str(s).upper(), tuple(r)) for s, r in cands]
    below = tuple(getattr(precheck, "held_below_entry_bar", ()) or ())
    return [(str(s).upper(), ()) for s in below]


def disposition_payload(
    precheck,
    closed: set[str],
    execution_enabled: bool,
) -> dict[str, str]:
    """Pure: the per-name reasons for one pass. Empty when nothing is below the bar."""
    order = _walk_order(precheck)
    if not order:
        return {}
    first_cut = next((s for s, _ in order if s in closed), "")
    cut_seen = False
    fail_on, not_reached = [], []
    for sym, reasons in order:
        fail_on.append(f"{sym}={'; '.join(str(r) for r in reasons)}")
        if sym in closed:
            cut_seen = True
        elif not execution_enabled:
            not_reached.append(
                f"{sym}=not reached: pruning execution is switched off, so "
                "no below-bar name was put up to be cut this session"
            )
        elif cut_seen:
            not_reached.append(
                f"{sym}=not reached: the pass closes one below-bar name per run and {first_cut} was closed first"
            )
    return {
        "below_bar_reasons": "|".join(fail_on),
        "not_reached": "|".join(not_reached),
    }


def apply_rotation_recording_dispositions(
    pipeline,
    ctx,
    portfolio_decision,
    positions,
    position_history,
) -> None:
    """Run the rotation acting path, then record why each below-bar name stayed.

    Never raises on the bookkeeping side: a failed record must not take a
    live session with it.
    """
    from src.pipeline_rotation_exec import (
        _apply_rotation_execution,
        _rotation_execution_enabled,
    )
    from src.pipeline_stages import _record_pipeline_event

    _apply_rotation_execution(
        pipeline,
        ctx,
        portfolio_decision,
        positions,
        position_history,
    )
    try:
        precheck = getattr(
            getattr(pipeline, "portfolio_manager", None),
            "last_rotation_precheck",
            None,
        )
        closed = {
            str(getattr(t, "symbol", "")).upper()
            for t in (getattr(portfolio_decision, "targets", None) or [])
            if getattr(t, "is_close", False)
        }
        payload = disposition_payload(
            precheck,
            closed,
            _rotation_execution_enabled(pipeline),
        )
        if payload:
            _record_pipeline_event(
                pipeline,
                ctx,
                None,
                "rotation",
                "dispositions",
                "below_bar_names",
                **payload,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Rotation dispositions record failed: %s", exc)
