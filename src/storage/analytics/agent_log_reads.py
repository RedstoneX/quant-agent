"""Agent-log reads for the prompt builders, with the pre-cut candidate count.

Lifted from ``src/storage/analytics/calibration.py``, which keeps a thin
same-named shim. The prompt facts that cut a candidate list with the query's
own ``LIMIT`` could never say how many candidates there WERE: the database
swallowed the count before the caller saw a row, and the ledger's recording
routes for those caps ask for exactly that number. So the predicate the
``LIMIT`` rides on is factored out here and offered twice -- once as the
limited read, byte-identical in SQL and in ``ORDER BY`` to the one it
replaced, and once as a ``COUNT`` over the same predicate.

Why a second query and not an unbounded fetch cut in code. ``agent_logs``
grows without bound, so dropping the ``LIMIT`` would pull an arbitrarily
large result set, and no bound "provably above any real candidate count" is
derivable from anything -- picking one would be the exact defect the number
ledger exists to remove. Counting separately also cannot move the surviving
set, because the read beside it is unchanged; a fetch with a different
``LIMIT`` could reorder rows that tie on ``timestamp``.
"""
from __future__ import annotations

import logging
from datetime import datetime as _dt, timezone as _tz

from src.util.time import ET

logger = logging.getLogger(__name__)

_COLUMNS = "agent_name, timestamp, full_response, output_summary"


def candidate_predicate(agent_name: str,
                        before_date: str | None = None) -> tuple[str, list]:
    """The WHERE clause and params shared by the limited read and its count.

    `before_date` is an ET trading-day key converted to the UTC instant for
    00:00 ET on that date, because SQLite's `datetime('now')` writes UTC; a
    naive `date(timestamp) < before_date` compares a UTC date against an ET
    key and drops logs written in the last hours of ET-today. Moved here
    verbatim so the count and the read can never diverge on that comparison.
    """
    conditions = ["agent_name = ?"]
    params: list = [agent_name]
    if before_date:
        try:
            et_midnight = _dt.fromisoformat(before_date).replace(tzinfo=ET)
            utc_cutoff = et_midnight.astimezone(_tz.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            conditions.append("timestamp < ?")
            params.append(utc_cutoff)
        except (ValueError, TypeError) as exc:
            # Unparseable: degrade by skipping the date filter rather than
            # applying the known-wrong UTC-vs-ET comparison. Every production
            # caller passes session_date_key(), so this is unreachable there.
            logger.warning(
                "get_recent_agent_outputs: unparseable before_date=%r (%s); "
                "skipping the date filter (returning most-recent rows "
                "unfiltered) to avoid a UTC-vs-ET mismatch",
                before_date, exc,
            )
    return "WHERE " + " AND ".join(conditions), params


def recent_agent_outputs(*, conn, lock, agent_name: str, limit: int = 5,
                         before_date: str | None = None) -> list[dict]:
    """Last `limit` agent_logs rows for `agent_name`, newest first."""
    where, params = candidate_predicate(agent_name, before_date)
    with lock:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM agent_logs {where} "
            f"ORDER BY timestamp DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def count_recent_agent_outputs(*, conn, lock, agent_name: str,
                               before_date: str | None = None) -> int:
    """How many candidates `recent_agent_outputs` had to choose from.

    The number the `LIMIT` used to hide. Returned, never stored: the caller
    records it as one row at the moment it cut, and nothing keeps a tally.
    """
    where, params = candidate_predicate(agent_name, before_date)
    with lock:
        row = conn.execute(
            f"SELECT COUNT(*) FROM agent_logs {where}", params,
        ).fetchone()
    return int(row[0])
