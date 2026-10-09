"""Keep legacy unknown sector weights while rejecting new NULL recordings.

Old databases have a nullable ``weights_json`` column. A legacy NULL means the
recording is unknown: the order counts cannot reconstruct sector, side or size.
SQLite cannot add a NOT NULL constraint without rewriting the table, so the
legacy table uses triggers for future writes. New tables have NOT NULL.
"""

from __future__ import annotations

import sqlite3


def ensure_not_null(conn: sqlite3.Connection) -> None:
    """Reject future NULL payloads without changing historical NULL rows."""
    cols = conn.execute("PRAGMA table_info(realised_sector_weights)").fetchall()
    nullable = any(c[1] == "weights_json" and not c[3] for c in cols)
    if not nullable:
        return

    # A NULL already on disk is evidence that the payload was not recorded.
    # In particular, zero entry orders does not mean zero reducing orders.
    # Do not replace it with [] or invent reducer sectors and weights.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS realised_sector_weights_nonnull_insert
        BEFORE INSERT ON realised_sector_weights
        WHEN NEW.weights_json IS NULL
        BEGIN
            SELECT RAISE(ABORT, 'weights_json must be recorded');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS realised_sector_weights_nonnull_update
        BEFORE UPDATE OF weights_json ON realised_sector_weights
        WHEN NEW.weights_json IS NULL
        BEGIN
            SELECT RAISE(ABORT, 'weights_json must be recorded');
        END
        """
    )
    # The previous initializer backfilled NULL to [] before rebuilding the
    # table. If that code runs again after a rollback, stop initialization
    # rather than let it erase the missing-payload evidence.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS realised_sector_weights_preserve_unknown
        BEFORE UPDATE OF weights_json ON realised_sector_weights
        WHEN OLD.weights_json IS NULL
        BEGIN
            SELECT RAISE(ABORT, 'historical weights_json is unknown');
        END
        """
    )
