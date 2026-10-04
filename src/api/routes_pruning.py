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
from src.rotation_unrecorded import examined_count_of

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
    examined_count: int | None  # None = the record never held a count
    verdicts: list[PruningVerdict]
    lines: list[str]


class PruningPassesResponse(BaseModel):
    day: str | None
    passes: list[PruningPass]
    note: str


def _split(value: object) -> list[str]:
    return [s for s in str(value or "").split(",") if s]


def _pairs(value: object) -> dict[str, str]:
    """Parse the dispositions row's "SYM=text|SYM2=text" fields."""
    out: dict[str, str] = {}
    for part in str(value or "").split("|"):
        sym, sep, text = part.partition("=")
        if sep and sym:
            out[sym.strip().upper()] = text.strip()
    return out


def verdicts_for(
    record: dict,
    disposition: dict | None = None,
    skips: dict[str, str] | None = None,
) -> list[PruningVerdict]:
    """One verdict and one reason per examined name, off the durable rows.

    `disposition` is the run's `rotation`/`dispositions` row and `skips` the
    run's `rotation`/`skipped` reasons by symbol; together they say why a
    below-bar name was kept. A run recorded before the disposition row
    existed says so, which is a true state, never a blank.
    """
    disposition = disposition or {}
    skips = skips or {}
    fail_on = _pairs(disposition.get("below_bar_reasons"))
    not_reached = _pairs(disposition.get("not_reached"))
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
            held_back = (
                f"refused: {skips[sym]}" if sym in skips
                else not_reached.get(sym)
                or "not reached: this run was recorded before the pass "
                   "stored why a below-bar name was kept"
            )
            fails = fail_on.get(sym)
            out.append(PruningVerdict(
                symbol=sym, verdict="below_bar_not_cut",
                reason="below the desk's own entry bar"
                + (f" ({fails})" if fails else "")
                + f"; kept because {held_back}",
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
        "SELECT run_id, symbol, timestamp, evidence_json FROM specialist_evidence "
        "WHERE agent_name = 'pipeline' AND kind = 'pipeline_event' "
        "AND date(timestamp) = ? ORDER BY id DESC", (day,),
    ).fetchall()
    passes: list[PruningPass] = []
    seen: set[str] = set()
    dispositions: dict[str, dict] = {}
    skips: dict[str, dict[str, str]] = {}
    for row in rows:
        try:
            data = json.loads(row["evidence_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if data.get("stage") == "rotation" and data.get("outcome") == "dispositions":
            dispositions.setdefault(row["run_id"], data)
        elif data.get("stage") == "rotation" and data.get("outcome") == "skipped":
            sym = str(row["symbol"] or "").upper()
            if sym:
                skips.setdefault(row["run_id"], {}).setdefault(
                    sym, str(data.get("reason") or "no reason recorded")
                )
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
            examined_count=examined_count_of(record),
            verdicts=verdicts_for(
                record, dispositions.get(row["run_id"]), skips.get(row["run_id"]),
            ),
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
