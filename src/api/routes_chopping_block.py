"""Board item 228 - the chopping-block heads-up: every holding, every day.

OWNER RULING 2026-10-02 - visibility only. The desk sells anything below its
own entry bar, always; this panel tells the owner which holdings are drifting
that way before it happens. It is NOT an approval step, a delay or a veto: it
is read-only (`mode=ro` connection, one GET) and nothing in the trading path
reads it. No threshold, danger band or day-count is introduced.

What it reads: the durable `rotation`/`precheck` rows (held set examined and
the held names below the entry bar, one per session) and, where present, the
run-scoped `rotation`/`dispositions` row that stores WHY a below-bar name fails.
Distance to the bar is therefore categorical (clears it / below it) - the
margin by which a healthy name clears it is not recorded anywhere, and the
panel says so. Direction is derived from the same rows: how long the name has
held its current standing and when it last changed.
"""
from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter
from pydantic import BaseModel

from src.api.db_reads import _connect

router = APIRouter()

_UNREADABLE = (
    "The pruning record could not be read, which is not the same as every "
    "holding being healthy."
)
_NO_MARGIN = (
    "Standing is whether a holding clears the desk's own entry bar. How far "
    "above the bar a healthy name sits is not recorded, so a healthy name "
    "cannot be shown drifting until it actually crosses."
)


class ChoppingBlockRow(BaseModel):
    symbol: str
    standing: str  # "below_bar" | "clears_bar"
    direction: str  # "slipped" | "recovered" | "steady" | "first_seen"
    headline: str
    reason: str


class ChoppingBlockResponse(BaseModel):
    as_of: str | None
    holdings: list[ChoppingBlockRow]
    note: str


def _split(value: object) -> list[str]:
    return [s for s in str(value or "").split(",") if s]


def _pairs(value: object) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in str(value or "").split("|"):
        sym, sep, text = part.partition("=")
        if sep and sym:
            out[sym.strip().upper()] = text.strip()
    return out


def _day(ts: object) -> str:
    return str(ts or "")[:10] or "an unrecorded date"


def _reason(sym: str, record: dict, disposition: dict) -> str:
    cut = str(record.get("held_symbol") or "").upper() == sym
    cut_tier = str(record.get("tier") or "") == "ineligible_hold"
    why = "; ".join(_split(record.get("held_reasons"))) if cut and cut_tier else ""
    why = why or _pairs(disposition.get("below_bar_reasons")).get(sym, "")
    held_back = _pairs(disposition.get("not_reached")).get(sym, "")
    if why and held_back:
        return f"fails the entry bar on: {why}. Not sold yet - {held_back}"
    if why and cut and cut_tier:
        return f"fails the entry bar on: {why}. Put up to be sold this pass."
    if why:
        return f"fails the entry bar on: {why}"
    return ("below the entry bar; the record does not say which rule it "
            "fails, because the pass stored reasons only for the name it cut")


def build_rows(passes: list[dict]) -> ChoppingBlockResponse:
    """Pure. `passes` is oldest-first: {ts, record, disposition}."""
    if not passes:
        return ChoppingBlockResponse(
            as_of=None, holdings=[],
            note="No pruning pass has been recorded yet, which is not the "
                 "same as every holding being healthy.")
    latest = passes[-1]
    rec = latest["record"]
    below_now = set(_split(rec.get("held_below_entry_bar")))
    rows: list[ChoppingBlockRow] = []
    for sym in _split(rec.get("held_examined")):
        def below(p: dict) -> bool | None:
            if sym not in _split(p["record"].get("held_examined")):
                return None
            return sym in _split(p["record"].get("held_below_entry_bar"))
        now = below(latest)
        i = len(passes) - 1
        while i >= 0 and below(passes[i]) == now:
            i -= 1
        streak = passes[i + 1]
        prior = passes[i] if i >= 0 and below(passes[i]) is not None else None
        n = len(passes) - 1 - i
        if sym in below_now:
            standing, why = "below_bar", _reason(sym, rec, latest["disposition"])
            if prior is not None:
                direction = "slipped"
                head = (f"Slipped below the entry bar on {_day(streak['ts'])}; "
                        f"below it on {n} pass{'es' if n != 1 else ''} since. "
                        "This is the kind of name the desk sells.")
            else:
                direction = "first_seen" if n == 1 else "steady"
                head = (f"Below the entry bar on every recorded pass since "
                        f"{_day(streak['ts'])}. This is the kind of name the "
                        "desk sells.")
        else:
            standing = "clears_bar"
            why = "clears the desk's own entry bar, so the case for holding stands"
            if prior is not None:
                direction = "recovered"
                head = (f"Back above the entry bar since {_day(streak['ts'])}; "
                        f"it had been below it on {_day(prior['ts'])}.")
            else:
                direction = "steady"
                head = (f"Clears the entry bar on every recorded pass since "
                        f"{_day(streak['ts'])}.")
        rows.append(ChoppingBlockRow(
            symbol=sym, standing=standing, direction=direction,
            headline=head, reason=why))
    rows.sort(key=lambda r: (r.standing != "below_bar", r.symbol))
    return ChoppingBlockResponse(
        as_of=latest["ts"], holdings=rows,
        note=_NO_MARGIN + " Holdings are those the latest pass examined; a "
             "name bought since appears after the next pass.")


def read_passes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT run_id, timestamp, evidence_json FROM specialist_evidence "
        "WHERE agent_name = 'pipeline' AND kind = 'pipeline_event' "
        "AND evidence_json LIKE '%\"rotation\"%' ORDER BY id ASC"
    ).fetchall()
    dispositions: dict[str, dict] = {}
    passes: list[dict] = []
    for row in rows:
        try:
            data = json.loads(row["evidence_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if data.get("stage") != "rotation":
            continue
        if data.get("outcome") == "dispositions":
            dispositions[row["run_id"]] = data
        elif data.get("outcome") == "precheck" and "held_examined" in data:
            passes.append({"run_id": row["run_id"], "ts": row["timestamp"],
                           "record": data})
    for p in passes:
        p["disposition"] = dispositions.get(p["run_id"], {})
    return passes


@router.get("/chopping-block", response_model=ChoppingBlockResponse)
def get_chopping_block() -> ChoppingBlockResponse:
    try:
        conn = _connect()
    except Exception:  # noqa: BLE001
        return ChoppingBlockResponse(as_of=None, holdings=[], note=_UNREADABLE)
    try:
        return build_rows(read_passes(conn))
    except sqlite3.DatabaseError:
        return ChoppingBlockResponse(as_of=None, holdings=[], note=_UNREADABLE)
    finally:
        conn.close()
