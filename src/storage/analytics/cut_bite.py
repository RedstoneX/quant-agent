"""src.storage.analytics.cut_bite -- assemble a prompt cut's bite, joined by run id.

Every cut-bite row in the number ledger asks the same four things of a prompt
cut: how many candidates existed BEFORE the cut, how many SURVIVED it, the age
of the oldest surviving row, and the SEAT'S VERDICT for that session. The first
three are known where the cut happens. The fourth is formed later in the same
session, by the seat that reads the cut prompt, so it is not available at the
cut site and must never be guessed, defaulted or approximated there -- a
fabricated verdict would settle a money-governing number on a number nobody
measured.

`run_id` already names one session uniquely and is carried by BOTH halves:
`reconciliation_runs.run_id` on the cut-site record written here, and
`agent_logs.run_id` on the seat's reply. Joining on it is how the observation
is assembled from the two points in time at which its halves actually exist.

A run whose seat never replied yields NO observation. `read_cut_bite` returns
those runs flagged `complete=False` so the gap is visible, and
`complete_cut_bite_observations` drops them: a three-of-four record is not an
observation and must never be counted as one.

The row goes through the existing `ReconciliationLog` writer. The handle-free
`counted` recorder is deliberately NOT used: it discards its context, so it can
say a site ran but cannot carry a measured value.
"""

import json
import logging
import sqlite3

from src.sentinel.reconciliation import ReconciliationLog

logger = logging.getLogger(__name__)

#: `reconciliation_runs.kind` for a cut-site record, suffixed with the site.
KIND_PREFIX = "cut_bite:"

#: The seat whose verdict closes the observation. The decision stage logs the
#: portfolio manager's reply under this `agent_logs.agent_name`.
DEFAULT_SEAT = "portfolio_manager"


def _conn(db) -> sqlite3.Connection | None:
    conn = getattr(db, "conn", None)
    return conn if isinstance(conn, sqlite3.Connection) else None


def record_cut_bite(
    *,
    db,
    run_id: str | None,
    site: str,
    cuts: dict[str, dict[str, int]],
    oldest_surviving_age_days: float | None,
) -> bool:
    """Write one cut-site record for one session. Returns whether it landed.

    `cuts` maps the name of each cut parameter to `{"before": n, "survived": n}`
    -- the candidate count the cut chose FROM and the count it left behind.
    Both are counted at the cut itself, never reconstructed afterwards.

    Without a `run_id` the record could never be joined to the verdict, so it
    is refused rather than written as an orphan that looks like evidence.

    An observer must never break the thing it observes: every failure here is
    logged with its traceback and swallowed, and the caller carries on.
    """
    if not run_id:
        logger.warning("cut_bite %s not recorded: no run_id to join the verdict on", site)
        return False
    conn = _conn(db)
    if conn is None:
        logger.debug("cut_bite %s not recorded: db has no sqlite connection", site)
        return False
    payload = {
        "site": site,
        "cuts": {
            name: {"before": int(edge["before"]), "survived": int(edge["survived"])}
            for name, edge in cuts.items()
        },
        "oldest_surviving_age_days": oldest_surviving_age_days,
    }
    try:
        ReconciliationLog(conn=conn).record(
            # `agreed` carries no agreement here -- there are no two sides to
            # agree. It is 1 to mean the cut site ran and recorded.
            kind=f"{KIND_PREFIX}{site}", agreed=True,
            detail=json.dumps(payload, default=str), run_id=run_id,
        )
    except Exception:  # noqa: BLE001
        logger.error("cut_bite %s could not be recorded", site, exc_info=True)
        return False
    return True


def read_cut_bite(*, db, site: str, seat: str = DEFAULT_SEAT) -> list[dict]:
    """Every cut-site record for `site`, newest first, joined to the seat's verdict.

    Each entry carries `run_id`, `ran_at`, `cuts`, `oldest_surviving_age_days`,
    `verdict` and `complete`. `complete` is False -- and `verdict` None -- when
    the session recorded the cut but the seat left no reply to join to.
    """
    conn = _conn(db)
    if conn is None:
        logger.debug("cut_bite %s not readable: db has no sqlite connection", site)
        return []
    rows = conn.execute(
        "SELECT ran_at, detail, run_id FROM reconciliation_runs "
        "WHERE kind = ? AND run_id IS NOT NULL ORDER BY id DESC",
        (f"{KIND_PREFIX}{site}",),
    ).fetchall()
    out: list[dict] = []
    for ran_at, detail, run_id in rows:
        try:
            payload = json.loads(detail or "{}")
        except (TypeError, ValueError):
            logger.warning("cut_bite %s: unreadable record for run %s", site, run_id)
            continue
        if not isinstance(payload, dict):
            continue
        verdict_row = conn.execute(
            "SELECT output_summary, full_response FROM agent_logs "
            "WHERE run_id = ? AND agent_name = ? ORDER BY id DESC LIMIT 1",
            (run_id, seat),
        ).fetchone()
        verdict = None
        if verdict_row is not None:
            verdict = verdict_row[0] or verdict_row[1] or None
        out.append({
            "run_id": run_id,
            "ran_at": ran_at,
            "cuts": payload.get("cuts") or {},
            "oldest_surviving_age_days": payload.get("oldest_surviving_age_days"),
            "verdict": verdict,
            "complete": verdict is not None,
        })
    return out


def complete_cut_bite_observations(*, db, site: str, seat: str = DEFAULT_SEAT) -> list[dict]:
    """Only the runs where all four fields exist -- the observation series itself."""
    return [o for o in read_cut_bite(db=db, site=site, seat=seat) if o["complete"]]
