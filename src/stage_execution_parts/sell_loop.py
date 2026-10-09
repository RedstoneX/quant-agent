"""The SELL loop's per-name pieces, lifted out of `ExecutionStage._run_session` (src/stage_execution.py).

Bodies are unchanged apart from dedenting and a one-line unpack of the
per-name facts at the top of `record_rotation_close`; where the loop body
said `continue`, the lifted body returns `SKIP` and the caller continues. The
sell limit (`sell_price * 0.995`) and the rotation refusal gate
(`if rotation_final_reason is _ROTATION_SELL_REFUSED: continue`) stay in
`_run_session`, where config/number_ledger.yaml and
tests/test_rotation_sequencing.py read them. Live-money exit code: behaviour
is identical.
"""

from __future__ import annotations

from src.pipeline_stages import (
    _alert_rotation_executed,
    _record_pipeline_event,
    logger,
)
from src.stage_execution_parts.state import SKIP


def await_sell_and_finalize(pipeline, prot, sell_status_by_id) -> None:
    """Wait for this sell's order, then rebuild this name's stop coverage."""
    # Wait for THIS sell and rebuild THIS name's stop coverage on its
    # actual fill before the loop cancels the next name's stops —
    # the per-name discipline the de-lever loops got (docs/WORK.md
    # item 111). Submitting every SELL first and waiting/finalizing
    # the batch afterwards left every earlier name with no
    # protective stop while later names were cancelled, submitted
    # and waited on. Runs even when the ledger write above raised:
    # the stops are off and the order is live. Which names are sold,
    # how much and at what limit are unchanged.
    order_id = prot["order_id"]
    # ExecutionStage was the lone SELL path missing this guard
    # — every other SELL path (force_delever / midday_emergency /
    # midday_llm / intra_check / take_profit) wraps the wait in
    # try/except. An uncaught exception here (broker 5xx, DNS
    # blip mid-poll) would propagate past the finalize loop
    # below. The audit F1 write-ahead row already covers a hard
    # process kill; this try/except additionally keeps the
    # in-process finalize path alive so coverage is rebuilt now
    # rather than waiting for the next session's drain.
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
    sell_status_by_id[order_id] = status
    if status != "filled":
        logger.warning(
            "Sell order %s did not fill before buy phase (status=%s); buys will use current cash only",
            order_id,
            status or "unknown",
        )
    # The wait above returned, so the broker's fill_info is final.
    # Reprotect on actual residual (filled) or restore originals
    # (no-fill terminal). wait=False: this order was just waited on.
    pipeline._finalize_pending_protections(
        [prot],
        context="ExecutionStage",
        wait=False,
    )


def record_rotation_close(pipeline, ctx, leg, order) -> None:
    """Record and page the desk's own rotation close, when this SELL is one."""
    decision, qty, sell_limit, rotation_final_reason = (
        leg.decision,
        leg.qty,
        leg.sell_limit,
        leg.rotation_final_reason,
    )
    # Phase 14b — this SELL is the desk's own rotation close.
    # Record it durably and page the owner NOW: broker
    # acceptance is the irreversible act, and a position sold
    # without a human or a model deciding to must never be
    # silent (see `_alert_rotation_executed`).
    rotation = ctx.rotation
    if isinstance(rotation, dict) and decision.symbol.upper() == rotation.get("held_symbol"):
        rotation["sell_order_id"] = order.get("id")
        rotation["sell_qty"] = float(qty)
        if isinstance(rotation_final_reason, str):
            # A SECOND durable fact, not an edit of the first.
            # The proposal the Risk Manager reviewed and the
            # clearance the sale executed under are two
            # different things that happened at two different
            # times; overwriting one with the other leaves the
            # ledger disagreeing with the alert about what was
            # said when. Written once, never edited.
            rotation["cleared_reason"] = rotation_final_reason
            _record_pipeline_event(
                pipeline,
                ctx,
                decision.symbol,
                "rotation",
                "sell_cleared_reason",
                rotation_final_reason,
                broker_order_id=order.get("id"),
                new_symbol=rotation.get("new_symbol"),
            )
        _record_pipeline_event(
            pipeline,
            ctx,
            decision.symbol,
            "rotation",
            "sell_submitted",
            rotation.get("reason", ""),
            broker_order_id=order.get("id"),
            qty=qty,
            limit_price=sell_limit,
            new_symbol=rotation.get("new_symbol"),
        )
        _alert_rotation_executed(
            rotation=rotation,
            qty=float(qty),
            limit_price=float(sell_limit),
            order_id=order.get("id"),
        )


def sell_qty_and_label(pipeline, decision, existing):
    """Resolve the SELL quantity and label; `SKIP` where the loop skipped."""
    if decision.allocation_pct == 0:
        logger.warning(
            "Skipping SELL %s with allocation_pct=0 (ambiguous — use 100 for full exit)",
            decision.symbol,
        )
        return SKIP
    if 0 < decision.allocation_pct < 100:
        sell_fraction = decision.allocation_pct / 100
        qty = existing[0].qty * sell_fraction
        if float(existing[0].qty).is_integer():
            qty = max(1.0, float(int(qty)))
        if qty <= 0:
            return SKIP
        if qty >= existing[0].qty:
            qty = pipeline._full_sell_qty(existing[0].qty)
            if qty is None:
                return SKIP
            action_label = "SELL"
        else:
            action_label = f"PARTIAL_SELL({decision.allocation_pct:.0f}%)"
    else:
        qty = pipeline._full_sell_qty(existing[0].qty)
        if qty is None:
            return SKIP
        action_label = "SELL"
    return qty, action_label
