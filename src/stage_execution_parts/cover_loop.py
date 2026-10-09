"""The COVER loop's per-name pieces, lifted out of `ExecutionStage._run_session` (src/stage_execution.py).

Bodies are unchanged apart from dedenting; where the loop body said
`continue`, the lifted body returns `SKIP` and the caller continues. The
buy-to-cover limit (`cover_price * 1.005`) stays in `_run_session`, where
config/number_ledger.yaml cites it. Live-money exit code: behaviour is
identical.
"""

from __future__ import annotations

from src.pipeline_stages import logger
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


def cover_qty_and_label(pipeline, decision, held_qty):
    """Resolve the COVER quantity and label; `SKIP` where the loop skipped."""
    if decision.allocation_pct == 0:
        logger.warning(
            "Skipping COVER %s with allocation_pct=0 (ambiguous — use 100 for full exit)",
            decision.symbol,
        )
        return SKIP
    if 0 < decision.allocation_pct < 100:
        cover_fraction = decision.allocation_pct / 100
        qty = held_qty * cover_fraction
        if float(held_qty).is_integer():
            qty = max(1.0, float(int(qty)))
        if qty <= 0:
            return SKIP
        if qty >= held_qty:
            qty = pipeline._full_sell_qty(held_qty)
            if qty is None:
                return SKIP
            action_label = "COVER"
        else:
            action_label = f"PARTIAL_COVER({decision.allocation_pct:.0f}%)"
    else:
        qty = pipeline._full_sell_qty(held_qty)
        if qty is None:
            return SKIP
        action_label = "COVER"
    return qty, action_label
