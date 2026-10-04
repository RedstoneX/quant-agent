"""Stop levels the desk decided on while the market was shut, and the drain
that applies them at the next open.

WHY THIS EXISTS. Alpaca refuses `replace_order_by_id` on an order whose
status is `accepted` -- HTTP 422, "cannot replace order in accepted status"
-- and after the 16:00 ET regular close every resting protective stop sits
in exactly that status. Measured against the paper broker on 2026-10-02.
The close session's window is 15:30-16:00 ET and its deterministic trails
run late in the run, so a session that starts at 15:30 and straddles the
bell reaches the amend after the tape has shut.

OWNER RULING 2026-10-02: a shut tape cannot elect a stop, so an amend that
cannot be made out of hours has cost the position nothing, and a
cancel-and-resubmit fallback would trade a harmless non-event for a real
unprotected moment. The defect is the desk's UNDERSTANDING: it must know
the amend was not made, know the position is not at risk meanwhile, and owe
the level to the next open. This module is that memory and that discharge.
"""
from __future__ import annotations

import logging
from typing import Any

from src.storage.trades import pending_stop_amends_store as _store

logger = logging.getLogger("src.execution.broker")


def intent_is_protective(intended: float, current: float | None, *, is_short: bool) -> bool:
    """Would applying `intended` reduce what can be lost?

    The standing rule, enforced at the moment of application and not only at
    the moment of decision: a protective stop NEVER moves in the direction
    that increases the loss. An overnight-aged intent can be stale -- the
    live stop may already be tighter than what the close session decided --
    so a pending level is re-tested here before it is sent, and a level that
    is no longer protective is voided rather than applied.
    """
    if current is None:
        return True  # nothing resting to loosen; placing protection is safe
    return current < intended if not is_short else current > intended


def _resting_stop_level(broker: Any, symbol: str) -> float | None:
    """The level currently resting on `symbol`, or None when none is readable.

    Uses the desk's one escalating stop read rather than a raw order list, so
    an unreadable broker answers None (treat the intent as still owed) instead
    of a fabricated zero.
    """
    try:
        return broker.get_current_stop_price(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pending stop drain: could not read %s's resting stop: %s", symbol, exc)
        return None


def drain_pending_stop_amends(broker: Any, db: Any) -> int:
    """Apply every stop level owed from a closed-market session. Returns the
    number of rows discharged (applied or voided as no-longer-protective).

    Called from the coverage preamble that every session runs before any
    trading, so the open's FIRST protective action is the one the desk
    already decided on last night. A row whose application fails is LEFT in
    place: an owed stop that quietly disappears is the dangerous failure
    this whole mechanism exists to prevent.
    """
    try:
        rows = _store.get_all(db._trades())
    except Exception as exc:  # noqa: BLE001
        logger.warning("pending stop drain: DB read failed: %s", exc)
        return 0
    if not rows:
        return 0
    from src.execution.stop_records import accepted_stop_order, replace_stop_and_record
    discharged = 0
    for row in rows:
        symbol = str(row.get("symbol") or "")
        try:
            intended = float(row.get("intended_stop"))
        except (TypeError, ValueError):
            logger.error("pending stop drain: unreadable level for %s; row kept", symbol)
            continue
        is_short = bool(row.get("is_short"))
        current = _resting_stop_level(broker, symbol)
        if not intent_is_protective(intended, current, is_short=is_short):
            logger.info(
                "pending stop drain: %s's owed level $%.4f is no longer more "
                "protective than the resting $%s, so it is VOIDED, not applied.",
                symbol, intended, current,
            )
            try:
                _store.delete(db._trades(), row.get("id"))
                discharged += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("pending stop drain: could not delete %s's row: %s", symbol, exc)
            continue
        try:
            order = replace_stop_and_record(broker, db, symbol, intended)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "pending stop drain: applying %s's owed stop $%.4f RAISED (%s) "
                "— the old stop remains in force and the row is kept for the "
                "next pass", symbol, intended, exc,
            )
            continue
        if accepted_stop_order(order):
            logger.info(
                "pending stop drain: %s's owed stop from the closed market is "
                "now live at $%.4f", symbol, intended,
            )
            try:
                _store.delete(db._trades(), row.get("id"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("pending stop drain: could not delete %s's row: %s", symbol, exc)
            discharged += 1
        else:
            logger.error(
                "pending stop drain: %s's owed stop $%.4f was NOT applied at "
                "the open — the row is KEPT and retried next pass",
                symbol, intended,
            )
    return discharged


def record_deferred_amend(db: Any, broker: Any, symbol: str, new_stop_price: float, order: dict) -> dict:
    """Persist the level owed from a shut-tape amend (nothing was attempted or cancelled).

    A write failure is the one way the intent can vanish, so it is logged as an
    error, never swallowed silently. Returns the deferred payload unchanged.
    """
    try:
        from src.execution.stop_records import _holding_is_short
        _store.record(db._trades(), symbol, new_stop_price,
                      is_short=bool(_holding_is_short(broker, symbol)), reason="market_closed")
        logger.warning("stop amend for %s DEFERRED to the next open at $%.4f: the market is closed and a "
                       "shut tape cannot elect the resting stop", symbol, new_stop_price)
    except Exception as exc:  # noqa: BLE001
        logger.error("stop amend for %s could not be recorded as pending (%s) - the intended level $%.4f "
                     "is NOT owed to the next open and must be re-decided", symbol, exc, new_stop_price)
    return order


def drain_safely(broker: Any, db: Any) -> None:
    """`drain_pending_stop_amends` that never raises; a row it cannot apply stays owed."""
    try:
        drain_pending_stop_amends(broker, db)
    except Exception as exc:  # noqa: BLE001
        logger.error("coverage sweep: the pending stop-amend drain failed (%s) - owed levels are STILL owed", exc)
