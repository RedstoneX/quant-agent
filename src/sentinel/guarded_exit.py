"""Make the exit path's broad catch-alls LOUD without changing what they do.

Same shape as ``src/sentinel/guarded.py`` (the broker's), for the pipeline's
exit engine: a swallowed fault logs its full traceback at ERROR plus one
counted row on the existing reconciliation channel, and a clean pass writes
its own ``agreed`` row, so never-reached / ran-clean / ran-and-swallowed stay
three distinguishable states. Nothing is stored here and nothing is re-raised:
an observer must never break the exit it observes.
"""

from __future__ import annotations

import logging

from src.sentinel.reconciliation import record_guarded_outcome

logger = logging.getLogger(__name__)


def record_exit_guard(owner, where: str, exc: BaseException | None = None, log=None, **context) -> None:
    """Record ONE pass through an exit-path catch-all (`exc` None means clean)."""
    try:
        record_guarded_outcome(
            db=getattr(owner, "db", None),
            where=f"exits.{where}",
            exc=exc,
            log=log or logger,
            context=context,
        )
    except Exception:  # noqa: BLE001
        logger.error("record_exit_guard could not record %s", where, exc_info=True)
