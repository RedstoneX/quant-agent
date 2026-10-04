"""Migration step: make `realised_sector_weights.weights_json` NOT NULL on databases created before it was.

SQLite cannot ALTER a column's nullability, so old tables are backfilled and rebuilt. Idempotent.
"""
from __future__ import annotations

import sqlite3


def ensure_not_null(conn: sqlite3.Connection) -> None:
    # Databases created before `weights_json` became NOT NULL may hold
    # contentless rows. Backfill them to `[]` — a run with
    # `entry_orders_built` 0 built nothing, which is exactly what `[]`
    # says — and rebuild the table so the constraint actually holds
    # going forward. SQLite cannot ALTER a column's nullability.
    try:
        cols = conn.execute(
            "PRAGMA table_info(realised_sector_weights)"
        ).fetchall()
        nullable = any(
            c[1] == "weights_json" and not c[3] for c in cols
        )
    except sqlite3.DatabaseError:
        nullable = False
    if nullable:
        conn.execute(
            "UPDATE realised_sector_weights SET weights_json = '[]' "
            "WHERE weights_json IS NULL"
        )
        conn.execute(
            """
            CREATE TABLE realised_sector_weights__new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                run_id TEXT,
                session_date TEXT,
                weights_json TEXT NOT NULL,
                denominator TEXT NOT NULL,
                total_value REAL,
                entry_orders_built INTEGER,
                reducing_orders_built INTEGER,
                unknown_sector_orders INTEGER,
                UNIQUE (run_id)
            )
            """
        )
        conn.execute(
            "INSERT INTO realised_sector_weights__new "
            "SELECT id, timestamp, run_id, session_date, weights_json, "
            "denominator, total_value, entry_orders_built, "
            "reducing_orders_built, unknown_sector_orders "
            "FROM realised_sector_weights"
        )
        conn.execute("DROP TABLE realised_sector_weights")
        conn.execute(
            "ALTER TABLE realised_sector_weights__new "
            "RENAME TO realised_sector_weights"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_realised_sector_weights_date "
            "ON realised_sector_weights (session_date)"
        )
