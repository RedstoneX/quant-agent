"""Read-only lookups of the live scale-in write-ahead rows, by database path.

Split out of ``scale_in`` (re-exported there). No ledger handle exists on these
paths, so a swallowed fault logs its full traceback and writes no row.
"""

from __future__ import annotations

import os
import sqlite3

from src.execution.scale_in_loud import record_scale_in_fault

#: Distinct from `_WAL_SELL_SENTINEL` (`__WAL_PENDING__`). Drain must NOT
#: run the SELL-finalize math on a scale-in row: a grown position would
#: restore the OLD stop size and leave the add naked — the partial-fill
#: size bug this path exists to close.
WAL_SCALE_IN_SENTINEL = "__WAL_SCALE_IN__"


def _read_rows(db_path, sql: str) -> list[sqlite3.Row] | None:
    """Rows of `sql` against the WAL table, or None when the read faulted."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, (WAL_SCALE_IN_SENTINEL,)).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        record_scale_in_fault(None, "pending_rows_from_path")
        return None


def pending_scale_in_rows_from_path(db_path: str | os.PathLike | None) -> list[dict]:
    """Read-only lookup of the LIVE scale-in WAL rows, with the row's own
    `created_at` and the quantity the cancel exposed (board item 193).

    `created_at` is the row's write time, not the broker's cancel
    acknowledgement, so a duration derived from it approximates the window; the
    exact figure is the `unprotected_window_closed` event. Empty on any failure:
    observability must never break the sweep.
    """
    if not db_path:
        return []
    rows = _read_rows(
        db_path,
        "SELECT id, symbol, created_at, position_qty_before_sell "
        "FROM pending_protection_restores WHERE sell_order_id = ? ORDER BY created_at ASC",
    )
    return [dict(r) for r in rows or [] if r["symbol"]]


def pending_scale_in_symbols_from_path(db_path: str | os.PathLike | None) -> set[str]:
    """Read-only lookup for the coverage watchdog. Empty on any failure."""
    if not db_path:
        return set()
    rows = _read_rows(db_path, "SELECT symbol FROM pending_protection_restores WHERE sell_order_id = ?")
    return {str(r["symbol"]) for r in rows or [] if r["symbol"]}
