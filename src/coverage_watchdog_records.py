"""Counted, traceback-loud records for the coverage watchdog's broad catch-alls.

The handlers stay and behave as before; what changes is what they RECORD: the
full traceback at ERROR plus one counted row on the existing reconciliation
channel (``record_guarded_outcome``). Three states stay distinct:

  * no row      -- the site was never reached
  * ``agreed``  -- it ran clean (``exc`` left as ``None``)
  * ``disagreed`` -- it ran and swallowed a fault

Where a site has no ledger handle in scope (`db=None`) the traceback is still
logged in full and the row is skipped, as ``record_reconciliation`` documents.
Nothing is stored here and nothing is returned for a caller to branch on.
"""

from __future__ import annotations

import logging
import sys

from src.sentinel.reconciliation import record_guarded_outcome

logger = logging.getLogger("src.coverage_watchdog")


def record_watchdog_pass(where, exc=None, *, db=None, fault=False, context=None):
    """Record ONE pass through a watchdog catch-all; never raises.

    `fault=True` inside an unbound ``except Exception:`` reads the exception
    being handled, so the handler text need not change.
    """
    try:
        if fault and exc is None:
            exc = sys.exc_info()[1]
        record_guarded_outcome(db=db, where=f"coverage_watchdog.{where}", exc=exc, log=logger, context=context)
    except Exception:  # noqa: BLE001 - an observer must not break what it observes
        logger.error("record_watchdog_pass could not record %s", where, exc_info=True)
