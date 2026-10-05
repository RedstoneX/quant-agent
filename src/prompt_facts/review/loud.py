"""Loud catch-all for the review prompt-fact builders: traceback plus a counted row.

The handlers that call this keep their old fallback value; this only adds what
they RECORD. The row lands on the reconciliation channel as
``guarded:review_facts.<where>`` through the builder's own ``db`` handle.
"""
from __future__ import annotations

import logging

from src.sentinel.reconciliation import record_guarded_outcome

logger = logging.getLogger("src.prompt_facts.review")


def record_swallowed_review(owner, where: str, exc: BaseException, **context) -> None:
    try:
        record_guarded_outcome(db=getattr(owner, "db", None), where=f"review_facts.{where}",
                               exc=exc, log=logger, context=context)
    except Exception:  # noqa: BLE001 - an observer must never break the prompt build
        logger.error("review facts could not record %s", where, exc_info=True)
