"""Stop levels the desk decided on while the market was shut, and the drain
the rule that decides whether one is still protective.

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
the level to the next open. This module is that memory; the discharge lives in
`src/execution/pending_stop_drain.py`, which sits ABOVE the stop funnel
(`stop_records`) and applies these rows through it. Nothing here imports the
funnel, so the funnel can import this module to record a deferral.
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


def record_deferred_amend(db: Any, symbol: str, new_stop_price: float, order: dict, *, is_short: bool) -> dict:
    """Persist the level owed from a shut-tape amend (nothing was attempted or cancelled).

    A write failure is the one way the intent can vanish, so it is logged as an
    error, never swallowed silently. Returns the deferred payload unchanged.
    """
    try:
        _store.record(db._trades(), symbol, new_stop_price,
                      is_short=bool(is_short), reason="market_closed")
        logger.warning("stop amend for %s DEFERRED to the next open at $%.4f: the market is closed and a "
                       "shut tape cannot elect the resting stop", symbol, new_stop_price)
    except Exception as exc:  # noqa: BLE001
        logger.error("stop amend for %s could not be recorded as pending (%s) - the intended level $%.4f "
                     "is NOT owed to the next open and must be re-decided", symbol, exc, new_stop_price)
    return order


