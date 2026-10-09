"""Indexes the prune queries scan on, moved out of `schema/manager.py`.

Takes a bare sqlite connection, so an in-memory database is enough to
construct and exercise it without the schema manager.
"""

from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger(__name__)


def ensure_prune_indexes(conn: sqlite3.Connection) -> None:
    # Indexes for prune queries. Both prune_trades and prune_agent_logs
    # scan WHERE timestamp < ?. 5-year retention on trades (~10-20k rows
    # before pruning) and 2-year retention on agent_logs (~15-25k rows
    # with full_response 20-40KB each) make these scans slow without
    # an index — write lock is held for the full delete duration.
    # IDX_IF_NOT_EXISTS is idempotent so existing DBs gain the index
    # on the next initialize().
    for table, col in (
        ("trades", "timestamp"),
        ("trades", "position_id"),
        ("agent_logs", "timestamp"),
        ("pending_protection_restores", "created_at"),
        ("pending_repegs", "created_at"),
        ("specialist_evidence", "run_id"),
        ("specialist_evidence", "symbol"),
        ("specialist_evidence", "decision_id"),
    ):
        try:
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_{col} ON {table}({col})")
        except Exception as e:
            _log.warning("Index creation failed for %s.%s: %s", table, col, e)
