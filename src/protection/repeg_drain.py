"""The pending-repeg drain: re-applies stop repegs that were written ahead but never confirmed at the broker.

Lifted verbatim out of `ProtectionMixin` (src/pipeline_protection.py) as a
standalone class: every collaborator is an explicit keyword-only
constructor argument, so it is built and exercised without a pipeline.
The mixin keeps a thin method of the same name that builds this object
and calls it, so every existing caller and patch target is unchanged.
"""

import logging

#: Logs under `src.pipeline`, as the bodies did before the move;
#: binding the name rather than `__name__` keeps log records byte-identical.
from src.sentinel.guarded import record_guarded_pass
logger = logging.getLogger("src.pipeline")


class RepegDrain:
    """The pending-repeg drain: re-applies stop repegs that were written ahead but never confirmed at the broker."""

    def __init__(self, *,
                 broker,
                 db,
                 delete_repeg_row) -> None:
        self.broker = broker
        self.db = db
        self._delete_repeg_row = delete_repeg_row

    def _drain_pending_repegs(self) -> int:
        """Repoint trade rows the re-peg WAL says were left behind (see
        `pending_repegs`). Returns the number of rows cleared.

        Recovers the one window the bounded re-peg cannot make atomic: the
        broker accepted a replacement — minting a NEW order id and killing the
        old one — and the process died before `trades.broker_order_id` caught
        up. The stale id will report status 'replaced' forever, which is in
        neither of `_reconcile_fills`'s terminal sets, so the trade would sit
        unreconciled while a live order worked untracked.

        The broker is the authority here, not the WAL. A row whose
        `new_order_id` is still the sentinel is resolved by asking Alpaca what
        the old order became (`replaced_by`); a broker read that FAILS leaves
        the row in place for the next session rather than guessing.

        Runs at session start, before `_reconcile_fills`, alongside the other
        recovery drains.
        """
        try:
            rows = self.db.get_pending_repegs()
            record_guarded_pass(self.db, "repeg_drain.get_pending_repegs")
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(self.db, "repeg_drain.get_pending_repegs", exc, log=logger)
            return 0
        if not rows:
            return 0

        from src.pipeline_stages import _WAL_REPEG_SENTINEL

        drained = 0
        for row in rows:
            row_id = row["id"]
            symbol = row["symbol"]
            old_id = str(row["old_order_id"])
            new_id = str(row["new_order_id"] or "")

            if new_id == _WAL_REPEG_SENTINEL or not new_id:
                # Crash inside the replace window: we do not know whether the
                # PATCH landed. Ask.
                try:
                    resolved = self.broker.resolve_replacement_chain(old_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "drain_pending_repegs: chain read raised for %s (%s) "
                        "— leaving row %d for next session",
                        old_id, exc, row_id,
                    )
                    continue
                if resolved is None:
                    logger.warning(
                        "drain_pending_repegs: broker could not resolve %s — "
                        "leaving row %d for next session", old_id, row_id,
                    )
                    continue
                if resolved == old_id:
                    # The replacement never landed. The trades row was already
                    # correct the whole time; nothing to repair.
                    logger.info(
                        "drain_pending_repegs: %s order %s was never replaced "
                        "— clearing row %d", symbol, old_id, row_id,
                    )
                    self._delete_repeg_row(row_id)
                    drained += 1
                    continue
                new_id = resolved
                try:
                    self.db.resolve_pending_repeg(row_id, new_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "drain_pending_repegs: could not record %s on row %d: "
                        "%s", new_id, row_id, exc,
                    )

            trade_row_id = row.get("trade_row_id")
            if not trade_row_id:
                logger.error(
                    "drain_pending_repegs: row %d (%s, %s → %s) has no trades "
                    "row to repoint — MANUAL REVIEW: the live order id is %s",
                    row_id, symbol, old_id, new_id, new_id,
                )
                continue
            try:
                updated = self.db.repoint_trade_broker_order_id(
                    trade_row_id, old_order_id=old_id, new_order_id=new_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "drain_pending_repegs: repoint of trades row %s failed: "
                    "%s — leaving row %d", trade_row_id, exc, row_id,
                )
                continue
            if updated:
                logger.warning(
                    "drain_pending_repegs: recovered %s — trades row %s "
                    "repointed from replaced order %s to %s",
                    symbol, trade_row_id, old_id, new_id,
                )
            else:
                # Already repointed (the in-session code got there before the
                # crash, or a previous drain did). Nothing left to do.
                logger.info(
                    "drain_pending_repegs: trades row %s already off %s — "
                    "clearing row %d", trade_row_id, old_id, row_id,
                )
            self._delete_repeg_row(row_id)
            drained += 1

        if drained:
            logger.info("drain_pending_repegs: cleared %d row(s)", drained)
        return drained

    def _delete_repeg_row(self, row_id: int) -> None:
        try:
            self.db.delete_pending_repeg(row_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "drain_pending_repegs: could not delete row %d: %s", row_id, exc,
            )
