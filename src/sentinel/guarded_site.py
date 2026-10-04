"""Loud catch-alls for the pipeline's own sites (intraday scan and monitor).

Same contract as ``src.sentinel.guarded.record_guarded_pass``, which is bound
to the broker; this one reads the ledger handle from the pipeline's own
``db`` attribute instead. Where the owner has no sqlite handle (an isolated
unit test) the traceback is still logged in full and the row is skipped, as
``record_reconciliation`` documents. Nothing is stored here and nothing is
returned for a caller to branch on, so a handler's behaviour is unchanged.

  * no row      -- the site was never reached
  * ``agreed``  -- it ran clean (``exc`` left as ``None``)
  * ``disagreed`` -- it ran and swallowed a fault (traceback plus detail row)
"""
from __future__ import annotations

import logging

from src.sentinel.reconciliation import record_guarded_outcome

logger = logging.getLogger(__name__)


def record_site(owner, where: str, exc: BaseException | None = None, *,
                context: dict | None = None, log=None, scope: str = "intraday") -> None:
    """Record ONE pass through a broad catch-all; never raises."""
    try:
        record_guarded_outcome(db=getattr(owner, "db", None), where=f"{scope}.{where}",
                               exc=exc, log=log or logger, context=context)
    except Exception:  # noqa: BLE001 - an observer must not break what it observes
        logger.error("record_site could not record %s", where, exc_info=True)
