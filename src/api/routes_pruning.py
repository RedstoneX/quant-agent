"""Board item 219 - the pruning pass on its own dashboard panel.

The pass decides which held names are sold for falling below the desk's own
entry bar. Until now the owner could only see it by opening one run's detail.
This route lists every pass recorded on the most recent day that has one, so a
pass that kept the whole book is visibly a pass that ran. Read-only: it reads
the durable `rotation`/`precheck` rows through the API's `mode=ro` connection
and registers only a GET, so it cannot write anything. No threshold, limit or
lookback is introduced - the window is "the newest day that has a record".
"""
from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter
from pydantic import BaseModel

from src.api.db_reads import _connect
from src.rotation import (
    ROTATION_TELEMETRY_UNAVAILABLE,
    owner_precheck_lines,
    pruning_pass_lines,
)

router = APIRouter()

_UNREADABLE = (
    "The pruning record could not be read, which is not the same as no "
    "pass having run."
)


class PruningVerdict(BaseModel):
    symbol: str
    verdict: str  # "cut" | "below_bar_not_cut" | "kept"
    reason: str


class PruningPass(BaseModel):
    run_id: str
    recorded_at: str | None
    examined_count: int
    verdicts: list[PruningVerdict]
    lines: list[str]


class PruningPassesResponse(BaseModel):
    day: str | None
    passes: list[PruningPass]
    note: str


def _split(value: object) -> list[str]:
    return [s for s in str(value or "").split(",") if s]


def verdicts_for(record: dict) -> list[PruningVerdict]:
    """One verdict and one reason per examined name, off the durable row."""
    examined = _split(record.get("held_examined"))
    below = set(_split(record.get("held_below_entry_bar")))
    reasons = "; ".join(_split(record.get("held_reasons")))
    cut = str(record.get("held_symbol") or "").upper()
    cut_tier = str(record.get("tier") or "") == "ineligible_hold"
    out: list[PruningVerdict] = []
    for sym in examined:
        if cut_tier and sym == cut:
            out.append(PruningVerdict(
                symbol=sym, verdict="cut",
                reason=reasons or "it no longer clears the desk's own entry bar",
            ))
        elif sym in below:
            out.append(PruningVerdict(
                symbol=sym, verdict="below_bar_not_cut",
                reason="below the desk's own entry bar today; not cut this "
                       "pass, and the record does not say which rule held it back",
            ))
        else:
            out.append(PruningVerdict(
                symbol=sym, verdict="kept",
                reason="still clears the bar it was bought on, so the case "
                       "for holding it stands",
            ))
    return out


def read_passes(conn: sqlite3.Connection) -> PruningPassesResponse:
    day_row = conn.execute(
        "SELECT date(MAX(timestamp)) AS d FROM specialist_evidence "
        "WHERE agent_name = 'pipeline' AND kind = 'pipeline_event' "
        "AND evidence_json LIKE '%\"rotation\"%' "
        "AND evidence_json LIKE '%\"precheck\"%'"
    ).fetchone()
    day = day_row["d"] if day_row else None
    if not day:
        return PruningPassesResponse(
            day=None, passes=[],
            note="No pruning pass has been recorded yet, which is not the "
                 "same as a pass having run and kept everything.",
        )
    rows = conn.execute(
        "SELECT run_id, timestamp, evidence_json FROM specialist_evidence "
        "WHERE agent_name = 'pipeline' AND kind = 'pipeline_event' "
        "AND date(timestamp) = ? ORDER BY id DESC", (day,),
    ).fetchall()
    passes: list[PruningPass] = []
    seen: set[str] = set()
    for row in rows:
        try:
            data = json.loads(row["evidence_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if data.get("stage") != "rotation" or data.get("outcome") != "precheck":
            continue
        if row["run_id"] in seen:
            continue
        record = {**data, "outcome": data.get("reason")}
        if record["outcome"] == ROTATION_TELEMETRY_UNAVAILABLE:
            continue
        seen.add(row["run_id"])
        passes.append(PruningPass(
            run_id=row["run_id"], recorded_at=row["timestamp"],
            examined_count=int(record.get("held_examined_count") or 0),
            verdicts=verdicts_for(record),
            lines=list(owner_precheck_lines(record))
            + list(pruning_pass_lines(record)),
        ))
    return PruningPassesResponse(
        day=day, passes=passes,
        note="Every pass recorded on the newest day that has one, newest first.",
    )


@router.get("/pruning-passes", response_model=PruningPassesResponse)
def get_pruning_passes() -> PruningPassesResponse:
    try:
        conn = _connect()
    except Exception:  # noqa: BLE001
        return PruningPassesResponse(day=None, passes=[], note=_UNREADABLE)
    try:
        return read_passes(conn)
    except sqlite3.DatabaseError:
        return PruningPassesResponse(day=None, passes=[], note=_UNREADABLE)
    finally:
        conn.close()
