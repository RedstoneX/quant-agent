"""The most recent reconciliation result, per reconciler kind.

Why: docs/FUTURE.md's heartbeat carries "last reconciliation", and until now
the reconcilers wrote back fixes but kept no record of WHEN they last ran or
WHETHER broker and ledger agreed. One row per run is appended; `status()`
reports 'agreed', 'disagreed' or 'not_run' -- the third is never collapsed
into either of the others. Nothing in the desk reads this yet.
"""
from __future__ import annotations

import json
import logging
import sqlite3

logger = logging.getLogger(__name__)

AGREED = "agreed"
DISAGREED = "disagreed"
NOT_RUN = "not_run"


class ReconciliationLog:
    """Writer and reader for the `reconciliation_runs` table on one sqlite connection."""

    def __init__(self, *, conn: sqlite3.Connection):
        self.conn = conn

    def record(self, *, kind: str, agreed: bool, detail: str = "",
               run_id: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO reconciliation_runs (kind, agreed, detail, run_id)"
            " VALUES (?, ?, ?, ?)",
            (kind, 1 if agreed else 0, detail, run_id),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def latest(self, *, kind: str) -> dict | None:
        """The newest row for `kind`, or None when that reconciler has never run."""
        cur = self.conn.execute(
            "SELECT id, ran_at, kind, agreed, detail, run_id FROM reconciliation_runs"
            " WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [c[0] for c in cur.description]
        out = dict(zip(cols, row))
        out["agreed"] = bool(out["agreed"])
        return out

    def status(self, *, kind: str) -> str:
        """'agreed' | 'disagreed' | 'not_run' -- three distinct answers, by design."""
        row = self.latest(kind=kind)
        if row is None:
            return NOT_RUN
        return AGREED if row["agreed"] else DISAGREED


def record_reconciliation(*, db, kind: str, result, run_id: str | None = None):
    """Hook for a reconciler's return site: record the outcome, hand `result` back unchanged.

    `result` is what the reconciler already returns -- a list of mismatches/
    gaps or a count of corrections. Empty or zero means broker and ledger
    agreed. `db` is whatever the caller holds; only a real sqlite connection
    on it is written to (the skip is logged, never hidden).
    """
    conn = getattr(db, "conn", None)
    if not isinstance(conn, sqlite3.Connection):
        logger.debug("reconciliation %s not recorded: db has no sqlite connection", kind)
        return result
    agreed = not result
    detail = json.dumps(result, default=str) if not agreed else ""
    ReconciliationLog(conn=conn).record(kind=kind, agreed=agreed, detail=detail, run_id=run_id)
    return result
