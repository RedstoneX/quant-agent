"""Loud catch-alls for the scale-in path: full traceback plus one counted row.

Same contract as ``src.sentinel.guarded`` (the settled shape): the handlers
STAY, and what they record changes. Three states stay distinct:

  * no row        -- the site was never reached
  * ``agreed``    -- it ran clean (``exc`` left as ``None``)
  * ``disagreed`` -- it ran and swallowed a fault (traceback plus detail row)

`owner` is the ledger handle (a database object) or a broker that was lent one
via ``attach_reconciliation_db``. With neither, the traceback is still logged in
full and the row is skipped, as ``record_reconciliation`` documents. Nothing is
stored here and nothing is returned for a caller to branch on.
"""

from __future__ import annotations

import logging
import sys

from src.sentinel.guarded import RECON_DB_ATTR
from src.sentinel.reconciliation import record_guarded_outcome

logger = logging.getLogger("src.execution.scale_in")


def record_scale_in(owner, where: str, exc: BaseException | None = None, **context) -> None:
    """Record ONE pass through a scale-in catch-all; never raises."""
    try:
        record_guarded_outcome(
            db=getattr(owner, RECON_DB_ATTR, None) or owner,
            where=f"scale_in.{where}",
            exc=exc,
            log=logger,
            context=context,
        )
    except Exception:  # noqa: BLE001 - an observer must not break what it observes
        logger.error("record_scale_in could not record %s", where, exc_info=True)


def record_scale_in_fault(owner, where: str, **context) -> None:
    """As `record_scale_in` for a handler that binds no name: takes the live exception."""
    record_scale_in(owner, where, sys.exc_info()[1], **context)
