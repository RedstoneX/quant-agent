"""The COVER loop's per-name pieces, lifted out of `ExecutionStage._run_session` (src/stage_execution.py).

Bodies are unchanged apart from dedenting; where the loop body said
`continue`, the lifted body returns `SKIP` and the caller continues. The
buy-to-cover limit (`cover_price * 1.005`) stays in `_run_session`, where
config/number_ledger.yaml cites it. Live-money exit code: behaviour is
identical.
"""

from __future__ import annotations

from src.pipeline_stages import _record_pipeline_event, logger
from src.stage_execution_parts.state import SKIP


def await_cover_and_finalize(pipeline, prot) -> None:
    """Wait for this cover's order, then rebuild this short's stop coverage."""
    # Same per-name discipline as the SELL loop above: this short's
    # BUY-stop coverage is rebuilt before the next short's is touched.
    order_id = prot["order_id"]
    try:
        status = pipeline.broker.wait_for_order_terminal(order_id)
    except Exception as e:
        logger.warning(
            "ExecutionStage: wait_for_order_terminal failed for %s: %s "
            "— treating as unknown status so finalize still runs",
            order_id,
            e,
        )
        status = None
    if status != "filled":
        logger.warning(
            "Cover order %s did not fill before buy phase (status=%s)",
            order_id,
            status or "unknown",
        )
    pipeline._finalize_pending_protections(
        [prot],
        context="ExecutionStage-Cover",
        wait=False,
    )


#: Refusal reason for a partial COVER. Owner ruling 2026-10-09: a held
#: position is kept whole or closed whole; short-side twin of
#: `PARTIAL_SELL_REFUSED` in sell_loop.py.
PARTIAL_COVER_REFUSED = "partial_cover_refused_whole_exits_only"


def cover_qty_and_label(pipeline, decision, held_qty, ctx):
    """Resolve the COVER quantity and label; `SKIP` where the loop skipped."""
    if decision.allocation_pct == 0:
        logger.warning(
            "Skipping COVER %s with allocation_pct=0 (ambiguous — use 100 for full exit)",
            decision.symbol,
        )
        return SKIP
    if 0 < decision.allocation_pct < 100:
        logger.error(
            "Refusing PARTIAL_COVER %s (%.0f%%): a held position is kept whole or closed whole",
            decision.symbol,
            decision.allocation_pct,
        )
        try:
            _record_pipeline_event(
                pipeline,
                ctx,
                decision.symbol,
                "order",
                "refused",
                PARTIAL_COVER_REFUSED,
                allocation_pct=decision.allocation_pct,
            )
        except Exception as exc:  # noqa: BLE001 — a record write never changes the refusal
            logger.error("partial-cover refusal record failed for %s: %s", decision.symbol, exc)
        return SKIP
    qty = pipeline._full_sell_qty(held_qty)
    if qty is None:
        return SKIP
    return qty, "COVER"
