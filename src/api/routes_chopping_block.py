"""Board item 228 - the chopping-block heads-up: every holding, every day.

OWNER RULING 2026-10-02 - visibility only. The desk sells anything below its
own entry bar, always; this panel tells the owner which holdings are drifting
that way before it happens. It is NOT an approval step, a delay or a veto: it
is read-only (`mode=ro` connection, one GET) and nothing in the trading path
reads it. No threshold, danger band or day-count is introduced.

What it reads: the durable `rotation`/`precheck` rows (held set examined and
the held names below the entry bar, one per session) and, where present, the
run-scoped `rotation`/`dispositions` row that stores WHY a below-bar name fails.
For a name that still clears the bar, the run-scoped `rotation`/`margins` row
(`src/rotation_margins.py`) records how far it sits from failing the two rules
that have a distance: R2 in rating steps from neutral and R5 in independent
net-evidence points above failing. Direction of travel is each rule's value on
the latest recorded day against the day before. Nothing here is a threshold:
any fall is reported as closing in, any rise as widening, and the owner judges.
R3, R6 and R7 are membership tests with no distance. Standing and its streak
come from the `precheck` rows as before.
"""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter
from pydantic import BaseModel

from src.api.db_reads import _connect

router = APIRouter()

_UNREADABLE = "The pruning record could not be read, which is not the same as every holding being healthy."
_NO_MARGIN = (
    "Margins are rating steps from neutral (rule R2) and independent "
    "net-evidence points above failing (rule R5), compared day against day. "
    "The other entry rules are yes-or-no and have no distance. The bar is made "
    "of ratings and evidence counts, not a price, so there is no price gap to "
    "scale by the stock's daily range. A name with no margin record is shown "
    "as unrecorded, never as safe."
)
_RULES = (
    ("r2_steps_from_neutral", "rating", "steps from neutral"),
    ("r5_net_evidence", "net evidence", "independent points above failing"),
)


class Margin(BaseModel):
    rule: str
    unit: str
    now: int
    previous: int | None
    first: int
    direction: str  # "closing_in" | "widening" | "steady" | "first_record"


class ChoppingBlockRow(BaseModel):
    symbol: str
    standing: str  # "below_bar" | "clears_bar"
    direction: str  # "slipped" | "recovered" | "steady" | "first_seen" | "closing_in"
    margins: list[Margin] = []
    headline: str
    reason: str
    distance_known: bool = False
    distance: str = ""


class ChoppingBlockResponse(BaseModel):
    as_of: str | None
    holdings: list[ChoppingBlockRow]
    note: str
    summary: str = ""


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
    return (
        "below the entry bar; the record does not say which rule it "
        "fails, because the pass stored reasons only for the name it cut"
    )


def _margins(sym: str, passes: list[dict]) -> list[Margin]:
    """Each rule's latest value against the previous recorded DAY's value."""
    out: list[Margin] = []
    for key, label, unit in _RULES:
        by_day: dict[str, int] = {}
        for p in passes:  # oldest first, so the last pass of a day wins
            v = (p.get("margins") or {}).get(sym, {}).get(key)
            if isinstance(v, int) and not isinstance(v, bool):
                by_day[_day(p["ts"])] = v
        if not by_day:
            continue
        vals = list(by_day.values())
        now, prev = vals[-1], (vals[-2] if len(vals) > 1 else None)
        d = "first_record" if prev is None else "closing_in" if now < prev else "widening" if now > prev else "steady"
        out.append(Margin(rule=label, unit=unit, now=now, previous=prev, first=vals[0], direction=d))
    return out


def _distance(standing: str, ms: list[Margin]) -> tuple[bool, str]:
    """What the owner reads as 'how far from the bar'. Never blank, never a guess."""
    if standing == "below_bar":
        return True, ("Already below the bar. The desk sells any name below it; being close earns no grace.")
    if not ms:
        return False, (
            "Distance to the bar is NOT recorded for this name (no margin record yet), so it cannot be called safe."
        )
    return True, "; ".join(
        f"{m.rule} {m.now} {m.unit}" + ("" if m.previous is None else f" (was {m.previous})") for m in ms
    )


def _summary(rows: list[ChoppingBlockRow]) -> str:
    below = sum(r.standing == "below_bar" for r in rows)
    closing = sum(r.direction == "closing_in" for r in rows)
    blind = sum(not r.distance_known for r in rows)
    return f"{len(rows)} holdings: {below} below the bar, {closing} closing in, {blind} with no distance recorded."


def _closing_text(ms: list[Margin]) -> str:
    return "; ".join(f"{m.rule} fell from {m.previous} to {m.now} {m.unit}" for m in ms if m.direction == "closing_in")


def build_rows(passes: list[dict]) -> ChoppingBlockResponse:
    """Pure. `passes` is oldest-first: {ts, record, disposition}."""
    if not passes:
        return ChoppingBlockResponse(
            as_of=None,
            holdings=[],
            note="No pruning pass has been recorded yet, which is not the same as every holding being healthy.",
        )
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
                head = (
                    f"Slipped below the entry bar on {_day(streak['ts'])}; "
                    f"below it on {n} pass{'es' if n != 1 else ''} since. "
                    "This is the kind of name the desk sells."
                )
            else:
                direction = "first_seen" if n == 1 else "steady"
                head = (
                    f"Below the entry bar on every recorded pass since "
                    f"{_day(streak['ts'])}. This is the kind of name the "
                    "desk sells."
                )
        else:
            standing = "clears_bar"
            why = "clears the desk's own entry bar, so the case for holding stands"
            if prior is not None:
                direction = "recovered"
                head = (
                    f"Back above the entry bar since {_day(streak['ts'])}; it had been below it on {_day(prior['ts'])}."
                )
            else:
                direction = "steady"
                head = f"Clears the entry bar on every recorded pass since {_day(streak['ts'])}."
        ms = _margins(sym, passes)
        closing = _closing_text(ms)
        if standing == "clears_bar" and closing:
            direction = "closing_in"
            head = f"Still clears the entry bar but is closing in on it: {closing}. " + head
        known, dist = _distance(standing, ms)
        rows.append(
            ChoppingBlockRow(
                symbol=sym,
                standing=standing,
                direction=direction,
                margins=ms,
                headline=head,
                reason=why,
                distance_known=known,
                distance=dist,
            )
        )
    rows.sort(key=lambda r: (r.standing != "below_bar", r.direction != "closing_in", r.distance_known, r.symbol))
    return ChoppingBlockResponse(
        as_of=latest["ts"],
        holdings=rows,
        summary=_summary(rows),
        note=_NO_MARGIN + " Holdings are those the latest pass examined; a "
        "name bought since appears after the next pass.",
    )


def read_passes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT run_id, timestamp, evidence_json FROM specialist_evidence "
        "WHERE agent_name = 'pipeline' AND kind = 'pipeline_event' "
        "AND evidence_json LIKE '%\"rotation\"%' ORDER BY id ASC"
    ).fetchall()
    dispositions: dict[str, dict] = {}
    margin_rows: dict[str, dict] = {}
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
        elif data.get("outcome") == "margins":
            try:
                margin_rows[row["run_id"]] = json.loads(data.get("held_margins") or "{}")
            except (TypeError, ValueError):
                pass
        elif data.get("outcome") == "precheck" and "held_examined" in data:
            passes.append({"run_id": row["run_id"], "ts": row["timestamp"], "record": data})
    for p in passes:
        p["disposition"] = dispositions.get(p["run_id"], {})
        p["margins"] = margin_rows.get(p["run_id"], {})
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
