"""Make the dashboard's broker-read catch-alls LOUD, without changing them.

Same shape as ``src/sentinel/guarded.py``: a swallowed fault logs a FULL
traceback at ERROR plus a counted ``disagreed`` row; a clean pass writes an
``agreed`` row; a site never reached writes nothing (``not_run``).

The dashboard side holds no ledger handle and must not write to a database,
so ``db`` is ``None`` here: the traceback is always logged and the row is
skipped, which ``record_reconciliation`` already reports at debug. Nothing is
stored, and nothing here can raise into the read that called it.
"""

from __future__ import annotations

import logging

from src.sentinel.reconciliation import record_guarded_outcome

logger = logging.getLogger("src.api.broker_reads")


def record_read_fault(where: str, exc: BaseException | None = None, **context) -> None:
    """One pass through a broker-read catch-all: `exc` set = swallowed fault
    (traceback + ``disagreed``); `exc` None = clean pass (``agreed``)."""
    try:
        record_guarded_outcome(db=None, where=f"api.broker_reads.{where}", exc=exc, log=logger, context=context or None)
    except Exception:  # noqa: BLE001 - an observer must never break the read
        logger.error("record_read_fault could not record %s", where, exc_info=True)


def record_read_pass(where: str, **context) -> None:
    """The clean pass: its own ``agreed`` row, distinct from never-reached."""
    record_read_fault(where, None, **context)
