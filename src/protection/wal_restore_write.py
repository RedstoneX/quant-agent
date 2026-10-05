"""The write-ahead protection-restore row insert (lifted from sell_finalization), loud and counted."""
from __future__ import annotations

import json as _json

from src.sentinel.guarded import record_guarded_pass



def write_ahead_restore_row(db, logger, sentinel, symbol, position_qty_before_sell, specs, side):
    """Row id, or None when there was nothing to protect or the write failed."""
    if not specs:
        return None
    try:
        row_id = db.insert_pending_protection_restore(
            symbol=symbol, sell_order_id=sentinel,
            position_qty_before_sell=position_qty_before_sell,
            specs_json=_json.dumps(specs), side=side,
        )
        logger.info("WAL: wrote protection-restore intent for %s (row %d, %d stop(s)) before cancel/submit",
                    symbol, row_id, len(specs))
        record_guarded_pass(db, "sell_finalization.write_ahead_restore")
        return row_id
    except Exception as exc:
        record_guarded_pass(db, "sell_finalization.write_ahead_restore", exc, log=logger,
                       context={"effect": "no crash-safety for this SELL"})
        return None
