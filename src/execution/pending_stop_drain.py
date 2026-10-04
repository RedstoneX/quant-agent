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
from src.execution.stop_records import accepted_stop_order, replace_stop_and_record
from src.storage.trades import pending_stop_amends_store as _store

logger = logging.getLogger("src.execution.broker")


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



def drain_safely(broker: Any, db: Any) -> None:
    """`drain_pending_stop_amends` that never raises; a row it cannot apply stays owed."""
    try:
        drain_pending_stop_amends(broker, db)
    except Exception as exc:  # noqa: BLE001
        logger.error("coverage sweep: the pending stop-amend drain failed (%s) - owed levels are STILL owed", exc)
