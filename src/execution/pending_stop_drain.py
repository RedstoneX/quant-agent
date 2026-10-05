"""The discharge of stop levels owed from a closed-market session.

Runs as the FIRST protective action of an open market (see the coverage
sweep in `src/pipeline_protection.py`, reached through `stop_repair`): every
row `src/execution/pending_stop_amends.py` remembered is re-tested for
protectiveness and then sent through the one replacement funnel in
`stop_records`. This module sits above that funnel; the memory module sits
below it, so the three never form a cycle.
"""
from __future__ import annotations

import logging
from typing import Any

from src.execution.pending_stop_amends import intent_is_protective
from src.execution.stop_read import UNREADABLE, StopRead, read_stop
from src.execution.stop_records import accepted_stop_order, replace_stop_and_record
from src.sentinel.guarded import record_guarded_pass
from src.storage.trades import pending_stop_amends_store as _store

logger = logging.getLogger("src.execution.broker")


def _resting_stop_level(broker: Any, symbol: str, db: Any) -> StopRead:
    """What rests on `symbol`: found, none, or UNREADABLE -- never a guess.

    Uses the desk's one escalating stop read (retry, bulk read, durable row,
    owner alert). It is a reporting read: it places nothing, because this
    drain is itself what places the owed level. An unreadable answer is NOT
    "no stop": reading it as none would apply a looser level over a tighter
    resting one.
    """
    try:
        level = read_stop(broker, symbol, db=db, context="pending stop drain")
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(broker, "pending_stop_drain.resting_stop", exc, log=logger, context={"symbol": symbol})
        return StopRead(UNREADABLE, None, str(exc) or type(exc).__name__)
    record_guarded_pass(broker, "pending_stop_drain.resting_stop", context={"symbol": symbol})
    return level


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
        record_guarded_pass(db, "pending_stop_drain.db_read", exc, log=logger)
        return 0
    record_guarded_pass(db, "pending_stop_drain.db_read", context={"rows": len(rows)})
    if not rows:
        return 0
    discharged = 0
    for row in rows:
        symbol = str(row.get("symbol") or "")
        try:
            intended = float(row.get("intended_stop"))
        except (TypeError, ValueError):
            logger.error("pending stop drain: unreadable level for %s; row kept", symbol)
            continue
        is_short = bool(row.get("is_short"))
        resting = _resting_stop_level(broker, symbol, db)
        if resting.unreadable:
            logger.error("pending stop drain: %s's resting stop is unreadable; owed level $%.4f KEPT, not applied",
                         symbol, intended)
            continue
        current = resting.price if resting.found else None
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
            # No session id reaches the janitor; the row says so (caller=...).
            order = replace_stop_and_record(broker, db, symbol, intended, caller="pending_stop_drain")
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(
                (db, broker), "pending_stop_drain.apply", exc, log=logger,
                context={"symbol": symbol, "intended": intended,
                         "note": "old stop remains in force; row kept for next pass"})
            continue
        record_guarded_pass((db, broker), "pending_stop_drain.apply", context={"symbol": symbol, "intended": intended})
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


def drain_safely(broker: Any, db: Any) -> None:
    """`drain_pending_stop_amends` that never raises; a row it cannot apply stays owed."""
    try:
        drain_pending_stop_amends(broker, db)
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(
            (db, broker), "pending_stop_drain.drain_safely", exc, log=logger,
            context={"note": "owed levels are STILL owed"})
        return
    record_guarded_pass((db, broker), "pending_stop_drain.drain_safely")
