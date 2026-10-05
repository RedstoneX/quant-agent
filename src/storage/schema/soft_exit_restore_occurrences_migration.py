"""Add and backfill `soft_exit_heal_restores.occurrences`.

WHY THE COLUMN EXISTS: the mechanical soft-exit heal runs inside a
Pydantic validator that re-runs per name per pass, so it used to append
an indistinguishable row every time and burn the in-memory cap. One live
session generated about 20,122 observations and discarded 15,122 of
them, and 5,136 of the 5,174 survivors carried the identical payload.
Observations are now deduplicated where they are parked and a repeat
increments this count, so nothing is lost and the cap did not have to be
replaced by a bigger invented number. `dropped_before` is untouched and
still reports anything the cap refuses.

SAFE ON THE ROWS ALREADY THERE: every row written before the fix is one
appended observation, so it is exactly one occurrence — known, not
invented. The column stays nullable on purpose: a count of 1 is a
recorded single observation and must never be confusable with an
observation that was never recorded at all.

Idempotent: safe to run on every startup, on a fresh database and on one
that has already been migrated.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def ensure_soft_exit_restore_occurrences(conn) -> None:
    """Add the column if absent, then backfill pre-existing rows to 1."""
    try:
        cursor = conn.execute("PRAGMA table_info(soft_exit_heal_restores)")
        if "occurrences" not in {row[1] for row in cursor.fetchall()}:
            conn.execute(
                "ALTER TABLE soft_exit_heal_restores ADD COLUMN occurrences INTEGER"
            )
        conn.execute(
            "UPDATE soft_exit_heal_restores SET occurrences = 1 "
            "WHERE occurrences IS NULL"
        )
        conn.commit()
    except Exception as e:  # noqa: BLE001 — a migration hiccup is not fatal
        logger.error(
            "Schema migration failed for soft_exit_heal_restores.occurrences: %s", e
        )
