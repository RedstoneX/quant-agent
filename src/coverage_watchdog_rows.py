"""Scale-in window readers for the coverage watchdog, each catch-all loud.

Lifted out of the watchdog and its records module unchanged apart from the
optional `db` handle: a swallowed fault logs a full traceback and writes a
counted ``disagreed`` row when a ledger handle is in reach (``db=None`` logs
the traceback and skips the row). Nothing is stored here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.coverage_watchdog_records import record_watchdog_pass


def _scale_in_row_age_seconds(created_at: Any, moment: datetime, db: Any = None) -> float | None:
    """Approximate seconds since a scale-in write-ahead row was written.

    The write-ahead row's `created_at` is a DATABASE WRITE time, not the
    broker's cancel acknowledgement, so this is an approximation of how
    long protection has been down and is labelled as one everywhere it is
    used. The exact figure is the `unprotected_window_closed` event the
    session files at rearm. `None` when the stamp cannot be read — an
    unreadable stamp must never be treated as a long window.
    """
    text = str(created_at or "")
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        record_watchdog_pass("scale_in_row_age", fault=True, db=db)
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return round(max(0.0, (moment - stamp).total_seconds()), 1)


def measured_window_bound_seconds(
    db_path: str | Path | None,
    db=None,
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
        record_watchdog_pass("measured_window_bound", fault=True, db=db)
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
