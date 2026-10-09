"""The route-event panel: what the desk's model fallbacks actually did.

Every time a decision seat is moved onto a different language-model route
the desk writes a durable row into `llm_route_events`. Until this panel
nothing read that table, so a seat quietly running on the free model
reached no human. This is the read: the owner's only surfaces are the
dashboard and Telegram, and Telegram is muted by his ruling.

It asks for what HAPPENED: every row, newest first, with no list of known
event types, so an event type added later appears with no change here. It
introduces no threshold, window or row limit (the precedent panel, item 228,
reads whole record too). It is read-only (`mode=ro`) and nothing in the
trading path reads it; a failure here returns an explicit "could not be
read" note, never an empty list that looks like "nothing happened".
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter
from pydantic import BaseModel

from src.api.db_reads import _connect

router = APIRouter()

_UNREADABLE = "The model-fallback record could not be read, which is not the same as no fallback having happened."
_NONE = "The model-fallback record has no entries. Nothing has been written to it yet."
_NOTE = (
    "Each line is something the desk's model routing did, newest first. "
    "'Free' means the model costs nothing per million words and its answers "
    "have not been measured. Information only."
)


class RouteEvent(BaseModel):
    when: str | None
    seat: str
    what: str
    cost: str
    detail: str


class RouteEventsResponse(BaseModel):
    note: str
    events: list[RouteEvent]


def _words(name) -> str:
    return str(name or "unknown").replace("_", " ").strip() or "unknown"


def _cost(inp, out) -> str:
    if inp is None and out is None:
        return "cost not recorded"
    if not inp and not out:
        return "free model"
    return f"paid model, ${inp} in and ${out} out per million tokens"


def describe(row: dict) -> RouteEvent:
    seat = _words(row.get("agent_name"))
    what = f"{_words(row.get('event_type'))} for the {seat} seat"
    if row.get("from_route") and row.get("route"):
        what += " (moved to a different model)"
    wait = row.get("wait_s")
    if wait:
        what += f", waited {wait} seconds"
    extra = " - ".join(str(row[k]) for k in ("error_shape", "detail") if row.get(k))
    return RouteEvent(
        when=row.get("timestamp"),
        seat=seat,
        what=what,
        cost=_cost(row.get("input_usd_per_mtok"), row.get("output_usd_per_mtok")),
        detail=extra,
    )


def build(rows: list[dict]) -> RouteEventsResponse:
    if not rows:
        return RouteEventsResponse(note=_NONE, events=[])
    return RouteEventsResponse(note=_NOTE, events=[describe(r) for r in rows])


@router.get("/route-events", response_model=RouteEventsResponse)
def get_route_events() -> RouteEventsResponse:
    try:
        conn = _connect()
    except Exception:  # noqa: BLE001
        return RouteEventsResponse(note=_UNREADABLE, events=[])
    try:
        rows = conn.execute("SELECT * FROM llm_route_events ORDER BY id DESC").fetchall()
        return build([dict(r) for r in rows])
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return RouteEventsResponse(note=_NONE, events=[])
        return RouteEventsResponse(note=_UNREADABLE, events=[])
    except Exception:  # noqa: BLE001
        return RouteEventsResponse(note=_UNREADABLE, events=[])
    finally:
        conn.close()
