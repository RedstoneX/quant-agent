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
from src.sentinel.guarded import record_guarded_pass

logger = logging.getLogger(__name__)

#: Why a position's stop did or did not trail, recorded on a CHANGE of
#: reason only — see `record_trail_state_changes`.
TRAIL_STATE_KIND = "trail_state"
#: One row per run holding the COUNT of every trail outcome that run
#: produced. `record_trail_state_if_changed` deliberately writes nothing
#: when a stock refuses for the same reason two runs running, so it can
#: say WHY a stop has not moved but never HOW OFTEN — which is why the
#: frequency of `inside_noise_band` was unmeasurable (item 196). The
#: census is bounded the other way: one row per run regardless of book
#: size, and no per-stock detail.
TRAIL_CENSUS_KIND = "trail_code_census"
#: `_insert` needs a non-empty symbol; the census is portfolio-scoped.
CENSUS_SYMBOL = "PORTFOLIO"
#: One row per stop repair that did not fully close its gap.
STOP_REPAIR_REFUSAL_KIND = "stop_repair_refusal"
#: One row per protective stop the desk's own kill switch refused to send.
PROTECTIVE_STOP_BLOCKED_KIND = "protective_stop_blocked"
#: One row per ex-dividend stop shift, carrying the PER-LEG outcome. Item 201:
#: a log line is not a record, and the shift is the one path that moves several
#: protective stops at once, so "which legs actually moved" has to survive.
STOP_SHIFT_KIND = "stop_shift_legs"

#: One row per live stop the desk could not read from the broker.
STOP_READ_UNREADABLE_KIND = "stop_read_unreadable"

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
        record_guarded_pass(db, "exit_path_records.insert", context={"kind": kind})
        return True
    except Exception as exc:  # noqa: BLE001 — a record is never trading authority
        record_guarded_pass(db, "exit_path_records.insert", exc, log=logger, context={"kind": kind})
        return False


# ---------------------------------------------------------------------------
# 1. trailing stop: why it did or did not trail, on a change only
# ---------------------------------------------------------------------------

def _state_key(code: str, structural_code: Any = None) -> str:
    """The dedupe identity of a trail state: its code plus the structural
    leg's code when there is one. Two states that differ only in why the
    STRUCTURAL leg refused are different states and each deserves its row."""
    extra = str(structural_code or "")
    return f"{code}|{extra}" if extra else str(code)


def last_trail_states(db: Any, symbols) -> dict[str, str]:
    """`{symbol: last recorded trail code}`. Empty on any failure — which
    makes the next evaluation record again, the safe direction for a
    record (a duplicate row, never a missing one)."""
    if db is None:
        return {}
    try:
        rows = db.get_latest_symbol_evidence(TRAIL_STATE_KIND, symbols)
        record_guarded_pass(db, "exit_path_records.last_trail_states")
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(db, "exit_path_records.last_trail_states", exc, log=logger)
        return {}
    out: dict[str, str] = {}
    for symbol, row in (rows or {}).items():
        try:
            payload = json.loads(row.get("evidence_json") or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("code"):
            # Item 212: the dedupe key is the code AND the structural leg's
            # own code. On a range name the R-ratchet supplies `code`, so a
            # changed structural reason under an unchanged `code` would
            # otherwise never be written at all.
            out[str(symbol).upper()] = _state_key(
                str(payload["code"]), payload.get("structural_code"),
            )
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


def record_trail_code_census(
    db: Any, *, run_id: str, counts: dict[str, int],
) -> bool:
    """Write one row counting every trail outcome this run produced.

    Recording only — nothing reads it back to decide anything. Written
    even when a count is zero-length is pointless, so an empty census
    writes nothing.
    """
    tally = {str(k): int(v) for k, v in (counts or {}).items() if int(v) > 0}
    if not tally:
        return False
    return _insert(
        db, run_id=run_id, kind=TRAIL_CENSUS_KIND, symbol=CENSUS_SYMBOL,
        payload={"counts": tally, "evaluations": sum(tally.values())},
    )


def record_trail_state_if_changed(
    db: Any, last_codes: dict[str, str], *, run_id: str, symbol: str,
    code: str, detail: str = "", structural_code: str | None = None,
    **facts: Any,
) -> bool:
    """Record `code` for `symbol` only when it differs from the last one on
    record. Bounded by design: a stop that sits untrailed for the same
    reason all month leaves one row, not one per tick; the day the reason
    changes leaves the next. `last_codes` is updated in place so a second
    evaluation of the same symbol in one pass is compared against the first.
    """
    symbol_u = str(symbol or "").strip().upper()
    key = _state_key(code, structural_code)
    previous = last_codes.get(symbol_u)
    if previous == key:
        return False
    written = record_trail_state(
        db, run_id=run_id, symbol=symbol_u, code=code, detail=detail,
        previous_code=(previous.split("|")[0] if previous else None),
        structural_code=(str(structural_code) if structural_code else None),
        **facts,
    )
    if written:
        last_codes[symbol_u] = key
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
# 4. the ex-dividend stop shift, leg by leg
# ---------------------------------------------------------------------------

def record_stop_shift_legs(
    db: Any, *, symbol: str, amount: float, mode: str, status: str,
    shifted: int, total: int, legs: list | None = None,
    run_id: str | None = None,
) -> bool:
    """One row per ex-dividend stop shift, naming every leg's own outcome.

    `legs` carries, per resting stop, its id, quantity, old level, new level,
    the replacement id and whether the broker confirmed it, refused it, or
    never answered. That is the evidence that settles whether a fractional
    position's two hybrid legs both amend in place — a question no log line
    can answer later, because logs rotate.
    """
    payload = {
        "code": f"stop_shift_{str(status or 'unknown')}",
        "amount": amount,
        "mode": str(mode or ""),
        "status": str(status or ""),
        "shifted": int(shifted),
        "total": int(total),
        "legs": list(legs or []),
    }
    return _insert(db, run_id=run_id, kind=STOP_SHIFT_KIND,
                   symbol=symbol, payload=payload)


def record_stop_read_unreadable(
    db: Any, *, symbol: str, reason: str, action: str = "",
    context: str = "", run_id: str | None = None,
) -> bool:
    """A live stop the broker would not read: not the same as having none."""
    return _insert(db, run_id=run_id, kind=STOP_READ_UNREADABLE_KIND,
                   symbol=symbol, payload={"code": "stop_read_unreadable",
                                           "reason": reason, "action": action,
                                           "context": context})

# Lifted out; re-exported (bottom, after `_insert`/`STOP_SHIFT_KIND` exist).
from src.execution.exdiv_shift_outcome import (  # noqa: E402,F401
    record_shift_outcome, stop_shift_incomplete_text,
)
