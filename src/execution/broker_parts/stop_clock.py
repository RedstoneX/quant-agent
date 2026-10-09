"""The broker clock, and what a protective-stop change does when it is shut.

Its own module, not a few more lines inside `stop_amend` or `stop_place`,
because both of those are at the file-size ratchet and a guard refusing a
change is the guard telling the truth.

WHY IT EXISTS. Two paths still CANCEL a live protective stop and then submit
a replacement: `replace_stop_loss`'s fallback (for order shapes the in-place
amend does not cover) and `shift_stops_down`'s ex-dividend fallback. That
sequence is not atomic. Out of hours it is the dangerous direction: the
cancel can land while the resubmit is refused, and the position opens the
next morning with NO protective order. Meanwhile the thing the cancel was
bought for is worthless, because a shut tape cannot elect the stop that is
already resting.

OWNER RULING 2026-10-02, "if the market is closed, the market is closed":
out of hours, cancel nothing, say the level is owed, apply it at the open.
"""

from __future__ import annotations

import logging
from src.sentinel.counted import record_swallowed

logger = logging.getLogger("src.execution.broker")

#: `amend_status` / `status` for the one outcome that is NOT a failure.
#: Callers read it as "not done, not lost, owed at the next open" — never as
#: a completed move, and never as a reason to cancel a live protective order.
AMEND_DEFERRED_MARKET_CLOSED = "market_closed"


def market_is_closed(client) -> bool | None:
    """Is the tape shut right now, per the BROKER's own clock?

    `None` means the clock could not be read. Callers then proceed as if the
    market were open: an unreadable clock must never be the reason a stop
    fails to tighten while the market is trading.
    """
    try:
        clock = client.get_clock()
    except Exception as exc:  # noqa: BLE001
        record_swallowed("execution.broker_parts.stop_clock.market_is_closed", exc, log=logger)
        return None
    is_open = getattr(clock, "is_open", None)
    if is_open is None:
        return None
    return not bool(is_open)


def deferred_payload(symbol: str, specs: list[dict], intended: float | None) -> dict:
    """What a stop change returns instead of cancelling out of hours.

    `id` is None so `accepted_stop_order` still rejects it — nothing is
    written back as though the stop had moved — while `intended_stop` carries
    the level the desk decided on, and `amend_status` routes to the same
    evidence row and owner alert the other non-success outcomes use.
    """
    legs = [
        {
            "id": str(spec.get("id") or ""),
            "qty": spec.get("qty"),
            "old_stop": spec.get("stop_price"),
            "new_stop": intended,
            "new_id": None,
            "outcome": "deferred",
            "detail": "market closed: nothing cancelled, the level is owed to the open",
        }
        for spec in specs
    ]
    return {
        "id": None,
        "status": AMEND_DEFERRED_MARKET_CLOSED,
        "amend_status": AMEND_DEFERRED_MARKET_CLOSED,
        "symbol": symbol,
        "legs": legs,
        "intended_stop": intended,
        "shifted": 0,
        "total": len(legs),
    }


def defer_if_closed(placer, symbol: str, specs: list[dict], intended: float, fresh: list) -> dict | None:
    """Gate `replace_stop_loss`'s cancel+resubmit fallback on the tape.

    Returns the deferred payload when the market is shut (and the caller must
    then return it without cancelling anything), or None when the fallback may
    proceed — including when the clock is unreadable.
    """
    if market_is_closed(placer.client) is not True:
        return None
    logger.warning(
        "replace_stop_loss: the market is CLOSED, so %s's %d resting stop(s) "
        "were NOT cancelled and no replacement was submitted. The in-place "
        "amend does not cover this shape, a cancel+resubmit out of hours "
        "risks opening with NO stop, and a shut tape cannot elect the stop "
        "already resting — so the intended level $%.4f is owed, not lost.",
        symbol,
        len(specs),
        intended,
    )
    return deferred_payload(symbol, specs, intended)


def defer_shift_if_closed(placer, symbol: str, specs: list[dict], shifted: list[dict], amount: float) -> dict | None:
    """The same gate for the ex-dividend shift's cancel+resubmit fallback.

    An un-shifted stop across a shut tape is simply an un-shifted stop; a
    cancel whose re-place is refused is a naked position on the ex-dividend
    morning, which is precisely when the gap that triggers it opens.
    """
    if market_is_closed(placer.client) is not True:
        return None
    logger.warning(
        "shift_stops_down: the market is CLOSED, so %s's %d resting stop(s) "
        "were NOT cancelled and not shifted by $%.4f. Nothing is exposed "
        "meanwhile; the shift is owed to the next open.",
        symbol,
        len(specs),
        amount,
    )
    payload = deferred_payload(symbol, specs, shifted[0]["stop_price"] if shifted else None)
    payload["mode"] = "deferred_market_closed"
    return payload


def reprotect_or_naked(
    placer, symbol: str, qty, side: str, intended: float, cancelled_specs: list[dict], window
) -> dict | None:
    """Last resort after a cancel landed, the resubmit failed AND the rollback
    of the original stop failed: the broker holds a real position with NO
    protective order.

    Returning a bare `None` was the defect. `None` is also `replace_stop_loss`'s
    answer for "the broker refused, the original stop is still resting", so
    every caller read a naked position as a harmless no-op. Two things change
    here: the desk RE-PROTECTS immediately at the level it just decided on,
    rather than waiting for the next session's coverage sweep, and if that
    also fails it returns a payload that SAYS unprotected.
    """
    logger.error(
        "replace_stop_loss: %s has no confirmed stop protection after a failed "
        "replacement and a failed rollback — re-protecting immediately at $%.4f",
        symbol,
        intended,
    )
    legs: list = []
    try:
        legs = placer._submit_stop_legs(symbol=symbol, qty=qty, stop_price=intended, side=side)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "replace_stop_loss: the immediate re-protect of %s at $%.4f ALSO failed (%s)",
            symbol,
            intended,
            exc,
        )
    if legs and legs[0]:
        logger.warning(
            "replace_stop_loss: %s was left unprotected by a failed replacement "
            "and is protected again at $%.4f on the immediate re-protect",
            symbol,
            intended,
        )
        window.close("reprotected")
        return legs[0]
    logger.error(
        "replace_stop_loss: %s is UNPROTECTED — the stop was cancelled, the "
        "replacement failed, the rollback failed and the immediate re-protect "
        "failed. The next coverage sweep MUST place a stop on %s.",
        symbol,
        symbol,
    )
    window.close("no_stop_confirmed")
    payload = deferred_payload(symbol, cancelled_specs, intended)
    payload["status"] = payload["amend_status"] = "naked"
    for leg in payload["legs"]:
        leg["outcome"] = "naked"
        leg["detail"] = "cancelled; resubmit, rollback and re-protect all failed"
    return payload
