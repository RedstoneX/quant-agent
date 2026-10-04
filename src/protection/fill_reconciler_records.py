"""Counted, traceback-bearing rows for the fill reconciler's broad catch-alls.

Lives beside `fill_reconciler` (not inside it) so that file does not grow.
Same shape as `src/sentinel/guarded.py`: a swallowed fault logs its full
traceback at ERROR plus one ``disagreed`` row; a clean pass writes its own
``agreed`` row; a site never reached writes nothing (``not_run``). Never
re-raises, so an observer fault cannot alter reconciliation. `db` is the
ledger handle the reconciler already holds; None means log-only, no row.
"""
import logging

from src.sentinel.reconciliation import record_guarded_outcome

logger = logging.getLogger("src.pipeline")


def record_fill_pass(db, where: str, exc: BaseException | None = None, *, context: dict | None = None) -> None:
    try:
        record_guarded_outcome(db=db, where=f"fill_reconciler.{where}", exc=exc, log=logger, context=context)
    except Exception:  # noqa: BLE001
        logger.error("record_fill_pass could not record %s", where, exc_info=True)
