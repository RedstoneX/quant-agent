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

import json
import logging
import sys
from pathlib import Path

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
        record_guarded_outcome(db=db, where=f"coverage_watchdog.{where}", exc=exc,
                               log=logger, context=context)
    except Exception:  # noqa: BLE001 - an observer must not break what it observes
        logger.error("record_watchdog_pass could not record %s", where, exc_info=True)


def measured_window_bound_seconds(
    db_path: str | Path | None,
) -> tuple[float | None, int]:
    """The longest scale-in unprotected window the desk has MEASURED, and how
    many measurements that is drawn from.

    Board item 193. Nothing here is a chosen threshold. Every closed window
    files an `unprotected_window_closed` event carrying its own
    `window_seconds`, both ends read from the broker's acknowledgements; the
    bound is the maximum of those. `(None, 0)` when no window has ever been
    measured, and the caller must then decline to call anything overdue
    rather than invent a figure to compare against.
    """
    if not db_path:
        return None, 0
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT evidence_json FROM specialist_evidence "
                "WHERE kind = 'pipeline_event' "
                "AND evidence_json LIKE '%unprotected_window_closed%' "
                "ORDER BY id DESC LIMIT 2000",
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        record_watchdog_pass("measured_window_bound", fault=True)
        return None, 0
    best: float | None = None
    seen = 0
    for (raw,) in rows:
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        if payload.get("outcome") != "unprotected_window_closed":
            continue
        value = payload.get("window_seconds")
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        seen += 1
        best = value if best is None else max(best, value)
    return best, seen
