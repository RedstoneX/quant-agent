"""Make money-path catch-alls LOUD where no ledger handle is in reach.

A swallowed fault logs a FULL traceback at ERROR; ``db`` is ``None`` so the
counted row is skipped (reported at debug by ``record_reconciliation``). A
site never reached writes nothing. Nothing is stored here and nothing in here
can raise into the code it observes.
"""
from __future__ import annotations

import logging

from src.sentinel.reconciliation import record_guarded_outcome

logger = logging.getLogger("src.sentinel.swallow_record")


def record_swallow(where: str, exc: BaseException | None = None, **context) -> None:
    """One pass through a swallowing handler: `exc` set = fault (traceback +
    ``disagreed``); `exc` None = clean pass (``agreed``)."""
    try:
        record_guarded_outcome(db=None, where=f"swallow.{where}", exc=exc,
                               log=logger, context=context or None)
    except Exception:  # noqa: BLE001 - an observer must never break the site
        logger.error("record_swallow could not record %s", where, exc_info=True)
