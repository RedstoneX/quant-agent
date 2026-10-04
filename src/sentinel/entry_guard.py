"""Loud catch-alls for the entry-order path: traceback at ERROR plus a counted row.

Same shape as ``src.sentinel.guarded`` (the broker's); the ledger handle is the
pipeline's own ``db``, already in scope at every entry-order site. Handlers
stay exactly as they were; these calls only add what they RECORD. A site
never reached writes no row, a clean pass writes ``agreed``, a swallowed fault
writes ``disagreed``. Nothing is stored here and nothing is re-raised.
"""
from __future__ import annotations

import logging
import sys

from src.sentinel.reconciliation import record_guarded_outcome

logger = logging.getLogger("src.pipeline_entry_orders")


def _record(pipeline, where, exc, context):
    try:
        record_guarded_outcome(db=getattr(pipeline, "db", None), where=f"entry_orders.{where}",
                               exc=exc, log=logger, context=context)
    except Exception:  # noqa: BLE001 - an observer must never break the order path
        logger.error("entry guard could not record %s", where, exc_info=True)


def record_swallowed(pipeline, where: str, exc: BaseException, **context) -> None:
    _record(pipeline, where, exc, context)


def record_clean_pass(pipeline, where: str, **context) -> None:
    _record(pipeline, where, None, context)


def record_swallowed_here(pipeline, where: str, **context) -> None:
    """For a handler that binds no name: reads the exception being handled."""
    exc = sys.exc_info()[1]
    if exc is not None:
        _record(pipeline, where, exc, context)
