"""Realised sector-weight recording at the decision stage (board item 224).

Moved out of `src/pipeline_entry_orders.py` verbatim: it needs only
`pipeline.db`, `pipeline.portfolio_constructor.last_order_sectors` and
`ctx.run_id`, so it is exercised from `SimpleNamespace` stubs without a
pipeline. A recording fault is COUNTED as a specialist-evidence row, never
only logged, and never aborts the decision stage.
"""

from __future__ import annotations

import json
import logging

from src.recording_accessors import pinned_evidence

logger = logging.getLogger(__name__)


def _record_realised_sector_weights(
    pipeline,
    ctx,
    portfolio_decision,
    total_value,
) -> None:
    """One durable row per run with the REALISED `(sector, side)` weights of
    the orders the constructor actually built this session.

    Board item 224 (2026-10-01). RECORDING ONLY: nothing may read this back
    into a sizing, ordering or refusal decision, and it may NEVER be swept
    for the sector cap that would have performed best — see the
    `realised_sector_weights` note in `src/storage/db.py::_migrate` for the
    unit, the denominator and the full bar on its use.

    Called here, immediately after `construct_orders` has returned, because
    this is the first point at which the FINISHED order list exists: the
    gross-exposure rationing inside the constructor runs last and changes
    sizes after each order is built, so anything recorded earlier would be
    what was hoped for rather than what was built. Never raises.
    """
    db = getattr(pipeline, "db", None)
    if db is None or not hasattr(db, "record_realised_sector_weights"):
        return
    try:
        # Plain attribute access with NO default: a renamed or removed
        # `last_order_sectors` raises HERE, loudly, at the accessor. It is
        # caught at this recording boundary so a recording fault can never
        # abort the decision stage, and COUNTED below, never just logged.
        sectors = pipeline.portfolio_constructor.last_order_sectors
        db.record_realised_sector_weights(
            decisions=list(getattr(portfolio_decision, "decisions", None) or []),
            sectors=sectors,
            total_value=total_value,
            run_id=pinned_evidence(ctx, "run_id"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("realised sector-weight recording failed: %s", exc)
        try:
            db.insert_specialist_evidence(
                run_id=str(getattr(ctx, "run_id", None) or ""),
                agent_name="realised_sector_weights_failure",
                kind="pipeline_event",
                scope="run",
                symbol=None,
                evidence_json=json.dumps(
                    {
                        "event": "realised_sector_weights_recording_failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                    sort_keys=True,
                    default=str,
                ),
            )
        except Exception as exc2:  # noqa: BLE001
            logger.error("sector-weight failure row not written: %s", exc2)
