"""Make the morning-research stage's broad catch-alls LOUD, without changing them.

Same settled pattern as ``src/sentinel/guarded.py`` (the broker's): the handler
STAYS and keeps its fallback value; what changes is only what it records. A
swallowed fault logs a full traceback at ERROR and writes one counted
``disagreed`` row on the existing reconciliation channel. ``exc=None`` is the
clean pass (``agreed`` row); a site never reached writes nothing (``not_run``).
When the stage holds no ledger handle the traceback is still logged and the row
is skipped, which ``record_reconciliation`` reports at debug. Nothing is stored
here. It never re-raises, so the observer cannot break what it observes.
"""

from __future__ import annotations

import logging

from src.sentinel.reconciliation import record_guarded_outcome

logger = logging.getLogger(__name__)


def record_morning_fault(owner, where: str, exc: BaseException | None = None, **context) -> None:
    """Record ONE pass through a morning-research catch-all (`exc` None = clean)."""
    try:
        record_guarded_outcome(
            db=getattr(owner, "db", None), where=f"morning.{where}", exc=exc, log=logger, context=context or None
        )
    except Exception:  # noqa: BLE001
        logger.error("record_morning_fault could not record %s", where, exc_info=True)
