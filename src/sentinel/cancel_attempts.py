"""Every broker cancel becomes one `order_attempts` row, success or failure.

Owner ruling 2026-10-02: "Everything important needs to be logged and counted."
Cancels were not counted at all. The desk reaches the broker's cancel calls from
nine places (entry re-peg, stop replacement, orphan sweep, sell finalization,
cancel-all); wrapping the client once covers all of them, and any future call
site, instead of each site remembering to record.

A recording failure never changes the cancel's outcome: the cancel's own result
or exception is returned or re-raised untouched.
"""

from __future__ import annotations

import logging
import sqlite3

from src.sentinel.order_attempts import OrderAttemptLog

logger = logging.getLogger(__name__)

CANCELLED = "cancelled"
CANCEL_FAILED = "cancel_failed"


class CancelRecordingClient:
    """Delegates everything to the real trading client; records cancel calls."""

    def __init__(self, *, inner, conn_getter):
        self._inner = inner
        self._conn_getter = conn_getter

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _record(self, *, outcome: str, broker_order_id=None, reason: str = "") -> None:
        try:
            conn = self._conn_getter()
            if not isinstance(conn, sqlite3.Connection):
                logger.warning("cancel attempt not recorded: no sqlite connection")
                return
            OrderAttemptLog(conn=conn).record(
                symbol=None,
                side=None,
                qty=None,
                outcome=outcome,
                broker_order_id=None if broker_order_id is None else str(broker_order_id),
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 - never let the record break the cancel
            logger.warning("cancel attempt not recorded: %s", exc)

    def cancel_order_by_id(self, order_id, *args, **kwargs):
        try:
            result = self._inner.cancel_order_by_id(order_id, *args, **kwargs)
        except Exception as exc:
            self._record(outcome=CANCEL_FAILED, broker_order_id=order_id, reason=f"{type(exc).__name__}: {exc}")
            raise
        self._record(outcome=CANCELLED, broker_order_id=order_id, reason="cancel_order_by_id")
        return result

    def cancel_orders(self, *args, **kwargs):
        try:
            result = self._inner.cancel_orders(*args, **kwargs)
        except Exception as exc:
            self._record(outcome=CANCEL_FAILED, reason=f"cancel_orders: {type(exc).__name__}: {exc}")
            raise
        if not result:
            self._record(outcome=CANCELLED, reason="cancel_orders: no open orders")
        for item in result or []:
            status = getattr(item, "status", None)
            ok = status is None or 200 <= int(status) < 300
            self._record(
                outcome=CANCELLED if ok else CANCEL_FAILED,
                broker_order_id=getattr(item, "id", None),
                reason=f"cancel_orders: http {status}",
            )
        return result


def install_cancel_recording(*, broker, conn_getter) -> None:
    """Wrap `broker.client` once (idempotent)."""
    if getattr(broker, "client", None) is None:
        logger.warning(
            "cancel recording NOT installed for %s: it has no trading client, so its cancels will not be counted",
            type(broker).__name__,
        )
        return  # a test fake with no trading client has no cancels to count; an observer must not break construction
    # The order desk is a COLLABORATOR built per call from the broker's client, not
    # the broker, so the ledger handle is lent to the client (observability only;
    # the recording wrapper delegates attribute reads to it). See src/sentinel/guarded.py.
    from src.sentinel.guarded import attach_reconciliation_db

    attach_reconciliation_db(broker.client, conn_getter)
    if not isinstance(broker.client, CancelRecordingClient):
        broker.client = CancelRecordingClient(inner=broker.client, conn_getter=conn_getter)
    # Same single wiring site lends the broker's broad catch-alls the ledger
    # they count their reconciliation rows through — observability only, no
    # decision reads it. See src/sentinel/guarded.py.
    from src.sentinel.guarded import attach_reconciliation_db

    attach_reconciliation_db(broker, conn_getter)
