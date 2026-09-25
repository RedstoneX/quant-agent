"""Durable records for three stop-side decisions that used to leave none.

Owner ruling: every mechanical decision leaves a record a person can find
later — a log line rotates away and is not a record. A 2026-09-18 audit found
three places on the exit/stop side that decided something and threw the
reason away:

  * **the trailing stop** — every "no trail this time" returned None and the
    caller moved on, so a position whose stop never trailed was invisible;
  * **the stop-repair refusal** — a log line and a one-shot Telegram only,
    which is how ~$223 of shares sat unprotected on 2026-09-18 with nothing
    left afterwards to count;
  * **the kill switch blocking a protective stop** — no record, and the
    owner-facing text said the broker had refused it.

Each is now one row in `specialist_evidence`, the desk's existing evidence
table, under its own `kind`. Deliberately NOT `kind='pipeline_event'`: the
jam detector (`src/refusal_signature.py`) reads every symbol-scoped
`pipeline_event` row as "this session considered that stock as a new idea",
and none of these is one. Filing them there would let a stop-side row join
or break a refusal streak it has nothing to do with — the same reason board
item 164 kept approved exits out of that stream (docs/INCIDENT_HISTORY.md).

OBSERVABILITY ONLY. Nothing in the trading path reads these rows. Every
writer here swallows its own failure: a record that cannot be written must
never change whether a stop trails, is repaired, or is blocked.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Why a position's stop did or did not trail, recorded on a CHANGE of
#: reason only — see `record_trail_state_changes`.
TRAIL_STATE_KIND = "trail_state"
#: One row per stop repair that did not fully close its gap.
STOP_REPAIR_REFUSAL_KIND = "stop_repair_refusal"
#: One row per protective stop the desk's own kill switch refused to send.
PROTECTIVE_STOP_BLOCKED_KIND = "protective_stop_blocked"
#: One row per exit that ran its cancel-stops→submit window WITHOUT the desk
#: broker-write lock (board item 127 criterion b): the lock timed out or could
#: not be established, so the naked-window serialization against a repair pass
#: was dropped for that symbol.
STOP_SERIALIZATION_DROPPED_KIND = "stop_serialization_dropped"

#: `agent_name` on every row here. The deterministic desk, not a model seat.
RECORD_AGENT = "pipeline"

#: `run_id` for a row written where no session id is in reach (the broker
#: and the stop-repair janitor are called by paths that do not carry one).
#: `specialist_evidence.run_id` is NOT NULL; the row's own timestamp and the
#: `caller` field in its payload say where it came from.
UNATTRIBUTED_RUN_ID = "unattributed"


def _insert(db: Any, *, run_id: str | None, kind: str, symbol: str,
            payload: dict) -> bool:
    if db is None:
        return False
    symbol_u = str(symbol or "").strip().upper()
    if not symbol_u:
        return False
    try:
        db.insert_specialist_evidence(
            run_id=str(run_id or UNATTRIBUTED_RUN_ID), agent_name=RECORD_AGENT,
            kind=kind, scope="symbol", symbol=symbol_u,
            evidence_json=json.dumps(payload, sort_keys=True, default=str),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — a record is never trading authority
        logger.warning(
            "exit-path record %s for %s could not be written: %s",
            kind, symbol_u, exc,
        )
        return False


# ---------------------------------------------------------------------------
# 1. trailing stop: why it did or did not trail, on a change only
# ---------------------------------------------------------------------------

def last_trail_states(db: Any, symbols) -> dict[str, str]:
    """`{symbol: last recorded trail code}`. Empty on any failure — which
    makes the next evaluation record again, the safe direction for a
    record (a duplicate row, never a missing one)."""
    if db is None:
        return {}
    try:
        rows = db.get_latest_symbol_evidence(TRAIL_STATE_KIND, symbols)
    except Exception as exc:  # noqa: BLE001
        logger.warning("trail-state read failed: %s", exc)
        return {}
    out: dict[str, str] = {}
    for symbol, row in (rows or {}).items():
        try:
            payload = json.loads(row.get("evidence_json") or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("code"):
            out[str(symbol).upper()] = str(payload["code"])
    return out


def record_trail_state(
    db: Any, *, run_id: str, symbol: str, code: str, detail: str = "",
    previous_code: str | None = None, **facts: Any,
) -> bool:
    """Write one trail-state row. The caller decides it is a change."""
    payload = {
        "code": str(code), "detail": str(detail or ""),
        "previous_code": previous_code, **facts,
    }
    return _insert(db, run_id=run_id, kind=TRAIL_STATE_KIND, symbol=symbol,
                   payload=payload)


def record_trail_state_if_changed(
    db: Any, last_codes: dict[str, str], *, run_id: str, symbol: str,
    code: str, detail: str = "", **facts: Any,
) -> bool:
    """Record `code` for `symbol` only when it differs from the last one on
    record. Bounded by design: a stop that sits untrailed for the same
    reason all month leaves one row, not one per tick; the day the reason
    changes leaves the next. `last_codes` is updated in place so a second
    evaluation of the same symbol in one pass is compared against the first.
    """
    symbol_u = str(symbol or "").strip().upper()
    previous = last_codes.get(symbol_u)
    if previous == code:
        return False
    written = record_trail_state(
        db, run_id=run_id, symbol=symbol_u, code=code, detail=detail,
        previous_code=previous, **facts,
    )
    if written:
        last_codes[symbol_u] = code
    return written


# ---------------------------------------------------------------------------
# 2. stop repair that did not close its gap
# ---------------------------------------------------------------------------

def record_stop_repair_refusal(
    db: Any, *, symbol: str, code: str, reason: str, uncovered_qty: float,
    is_short: bool, caller: str = "", run_id: str | None = None,
    held_qty: float | None = None, covered_qty: float | None = None,
    resting_stops: list | None = None, stop_price: float | None = None,
    placed: dict | None = None,
) -> bool:
    """One row per repair that left shares uncovered, naming why and what
    the broker was already holding for that symbol when it happened."""
    payload = {
        "code": str(code),
        "reason": str(reason or ""),
        "uncovered_qty": uncovered_qty,
        "is_short": bool(is_short),
        "caller": str(caller or ""),
        "held_qty": held_qty,
        "covered_qty": covered_qty,
        "resting_stops": list(resting_stops or []),
        "stop_price": stop_price,
        "placed": placed,
    }
    return _insert(db, run_id=run_id, kind=STOP_REPAIR_REFUSAL_KIND,
                   symbol=symbol, payload=payload)


# ---------------------------------------------------------------------------
# 3. the kill switch refusing a protective stop
# ---------------------------------------------------------------------------

def kill_switch_blocked_text(symbol: str) -> str:
    """The plain sentence the owner reads. Never 'the broker rejected'."""
    return (
        f"the desk's own kill switch is on and it blocked a protective stop "
        f"for {str(symbol or '').upper()} — the order was never sent to the "
        f"broker, so nothing was placed"
    )


def is_kill_switch_block(result: Any) -> bool:
    """True when a stop-placement result is the kill switch's refusal."""
    return isinstance(result, dict) and result.get("status") == "kill_switch_halted"


def record_protective_stop_blocked(
    db: Any, *, symbol: str, qty: float, stop_price: float, side: str,
    kill_switch_path: str = "", run_id: str | None = None,
) -> bool:
    """One row per protective stop the kill switch refused."""
    payload = {
        "code": "kill_switch",
        "detail": kill_switch_blocked_text(symbol),
        "qty": qty,
        "stop_price": stop_price,
        "side": str(side or ""),
        "kill_switch_path": str(kill_switch_path or ""),
    }
    return _insert(db, run_id=run_id, kind=PROTECTIVE_STOP_BLOCKED_KIND,
                   symbol=symbol, payload=payload)


# ---------------------------------------------------------------------------
# 4. an exit that ran WITHOUT the desk broker-write lock (item 127 b)
# ---------------------------------------------------------------------------

def record_stop_serialization_dropped(
    db: Any, *, symbol: str, label: str, reason: str = "",
    run_id: str | None = None,
) -> bool:
    """One row per exit that ran its cancel-stops→submit window without the
    desk broker-write lock, so the item-127 serialization against a repair
    pass was dropped for that symbol. Observability only — like the rest of
    this module, it never changes whether the exit proceeds."""
    payload = {
        "code": "serialization_dropped",
        "detail": (
            f"{str(label or '').strip() or 'exit'} for "
            f"{str(symbol or '').strip().upper()} ran WITHOUT the desk "
            f"broker-write lock; stop-protection serialization against a "
            f"repair pass was dropped for this symbol (item 127 naked-window "
            f"race briefly reopened)"
        ),
        "label": str(label or ""),
        "reason": str(reason or ""),
    }
    return _insert(db, run_id=run_id, kind=STOP_SERIALIZATION_DROPPED_KIND,
                   symbol=symbol, payload=payload)
