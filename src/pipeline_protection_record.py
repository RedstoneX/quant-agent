"""Make the protection pipeline's remaining catch-alls LOUD, without changing them.

Same shape as ``src/api/broker_reads_record.py``: a swallowed fault logs a FULL
traceback at ERROR plus a counted ``disagreed`` row; a clean pass writes an
``agreed`` row; a site never reached writes nothing (``not_run``).

The ledger handle is the owner's ``db`` when it has one; otherwise ``None``,
and the traceback is still logged while the row is skipped. Nothing is stored
here and nothing here can raise into the handler that called it.
"""

from __future__ import annotations

import logging

from src.sentinel.reconciliation import record_guarded_outcome

logger = logging.getLogger("src.pipeline_protection")


def record_protection_fault(owner, where: str, exc: BaseException | None = None, **context) -> None:
    """One pass through a protection catch-all: `exc` set = swallowed fault
    (traceback + ``disagreed``); `exc` None = clean pass (``agreed``)."""
    try:
        record_guarded_outcome(
            db=getattr(owner, "db", None),
            where=f"pipeline_protection.{where}",
            exc=exc,
            log=logger,
            context=context or None,
        )
    except Exception:  # noqa: BLE001 - an observer must never break protection
        logger.error("record_protection_fault could not record %s", where, exc_info=True)
