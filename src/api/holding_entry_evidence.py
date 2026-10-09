"""Load the evidence the holding view needs, including the OPENING run's.

Split out of `src/api/db_reads.py` because that module is at its size
ceiling and because this selection rule is the whole fix for a measured
owner-facing defect: the holding page could not say why six of the
eleven open positions were bought.

**The defect, measured against production on 2026-10-04.** The view took
the most recent `BUY`/`SHORT` row as the entry and loaded only that row's
run. For a position that was opened once and added to later — which is
most of them — the most recent BUY is an intraday ADD, and an add's run
contains no discovery event. The run that opened the position does, and
it was never read. The answer was recorded and then discarded by the
query.

**The rule.** Keep the latest entry row as `entry`: it carries the live
stop, the current take-profit and the most recent thesis, and changing
that would make the page show a stale stop. Then also load the run that
opened the position, and APPEND its rows after the latest run's. Append,
never prepend — the assembler takes the first row of each
`(agent, kind)` pair, so appending can only fill a field that was empty
and can never overwrite a current one with a three-week-old one.
"""

from __future__ import annotations

import sqlite3

#: Evidence from the entry's own run, plus any later position review or
#: re-derived target. The entry run's evidence is written BEFORE the fill
#: (a Form 4 admission precedes the order by minutes), so the
#: review-metrics cutoff must not be applied to it.
_RUN_EVIDENCE_SQL = (
    "SELECT * FROM specialist_evidence WHERE symbol = ? AND "
    "(run_id = ? OR (kind IN ('review_metrics', 'target_revision') "
    "AND timestamp >= ?)) ORDER BY id"
)

#: The opening run's rows only. No review cutoff here: the later-review
#: rows are already carried by the query above, and repeating them would
#: duplicate every review line on the page.
_OPENING_EVIDENCE_SQL = "SELECT * FROM specialist_evidence WHERE symbol = ? AND run_id = ? ORDER BY id"

#: The earliest entry row written against the same position. `position_id`
#: is what ties an add back to the open; without one there is nothing to
#: tie and the opening run is simply the entry's own.
_OPENING_TRADE_SQL = (
    "SELECT run_id FROM trades WHERE position_id = ? AND action IN ('BUY', 'SHORT') ORDER BY timestamp, id LIMIT 1"
)


def opening_run_id(conn: sqlite3.Connection, entry: dict) -> str | None:
    """The run that OPENED this position, or None when it is the entry's own."""
    position_id = entry.get("position_id")
    if not position_id:
        return None
    row = conn.execute(_OPENING_TRADE_SQL, (position_id,)).fetchone()
    if row is None:
        return None
    run_id = str(dict(row).get("run_id") or "")
    if not run_id or run_id == str(entry.get("run_id") or ""):
        return None
    return run_id


def entry_evidence(conn: sqlite3.Connection, symbol: str, entry: dict) -> list[dict]:
    """The entry run's evidence, then the opening run's, de-duplicated by id."""
    rows = [
        dict(row)
        for row in conn.execute(
            _RUN_EVIDENCE_SQL,
            (symbol, entry.get("run_id") or "", entry.get("timestamp") or ""),
        ).fetchall()
    ]
    opening = opening_run_id(conn, entry)
    if not opening:
        return rows
    seen = {row.get("id") for row in rows}
    rows.extend(
        dict(row) for row in conn.execute(_OPENING_EVIDENCE_SQL, (symbol, opening)) if dict(row).get("id") not in seen
    )
    return rows
