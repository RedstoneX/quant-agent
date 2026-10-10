"""src.intraday.tick_trail -- the deterministic trail, run at every 30-minute intra check.

Owner ruling 2026-10-08: keep the 30-minute intra checks AND move (trail) stops
at every one of them. Before this, the deterministic trail ran only at the
midday (13:00) and close (15:30) reviews.

This module adds NO trail maths and NO number. It calls the one existing trail
(`_apply_deterministic_trails`, handed in), which already:
  * only ever tightens (`src/risk/trailing.py` ratchets toward price only),
  * mirrors a short exactly like a long (the side comes from `qty`),
  * replaces a stop through `replace_stop_and_record` -> `replace_stop_loss`,
    the amend path that carries the stop-quantity invariant (PR 1556).
No paid model is called; the trail is arithmetic.

What this adds is the tick's own discipline:
  * it never trails while a live morning/midday/close session owns the desk, or
    while another process holds the broker-write lock -- the same two refusals
    the tick's safety preamble obeys, re-checked under the lock here because
    the preamble released it on return;
  * a name whose price or ATR cannot be read is SKIPPED, loudly, and never
    handed to the trail -- an unreadable input is never treated as zero, and a
    missing ATR would otherwise let the trail run without its noise band.
"""

from __future__ import annotations

import logging
import math

#: Same logger the intra check itself writes under (see src/intraday/session.py).
logger = logging.getLogger("src.pipeline")

__all__ = ["trail_on_tick"]


def _readable(value) -> float | None:
    """A finite, positive float, else None. Never coerces an unreadable value to zero."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _skip(skipped: list, symbol: str, reason: str) -> None:
    logger.warning("tick trail: skipping %s -- %s; its stop is left as it is", symbol, reason)
    skipped.append({"symbol": symbol, "reason": reason})


def _readable_positions(positions, atr_for_symbol, skipped: list) -> list:
    """The positions whose price AND ATR both read; every other one is skipped loudly."""
    ready = []
    for position in positions:
        symbol = getattr(position, "symbol", "?")
        if _readable(getattr(position, "current_price", None)) is None:
            _skip(skipped, symbol, "price_unreadable")
            continue
        try:
            atr = atr_for_symbol(symbol)
        except Exception:  # noqa: BLE001 — one name's read must not stop the others
            logger.exception("tick trail: ATR read raised for %s", symbol)
            _skip(skipped, symbol, "atr_read_raised")
            continue
        if _readable(atr) is None:
            _skip(skipped, symbol, "atr_unreadable")
            continue
        ready.append(position)
    return ready


def _run_trend_exit(trend_exit, positions, run_id: str, total_value, summary: dict) -> set:
    """Run the tick trend exit; return the symbols it placed a sell for (the wired callable never raises)."""
    if trend_exit is None:
        summary["trend_exit"] = {"status": "unavailable", "reason": "trend exit not wired on this host"}
        return set()
    result = trend_exit(positions, run_id=run_id, total_value=total_value)
    summary["trend_exit"] = result
    return set(result.get("sold") or [])


def trail_on_tick(
    *,
    positions,
    run_id: str,
    preamble_deferred: str = "",
    apply_deterministic_trails=None,
    atr_for_symbol=None,
    process_lock=None,
    blocking_owner_session=None,
    split_positions=None,
    trend_exit=None,
    total_value=None,
) -> dict:
    """Run the existing deterministic trail over this tick's broker positions.

    Returns a summary for the tick's report: `status` is one of `ran`,
    `deferred`, `unavailable` or `error`; `orders` counts broker replacements;
    `skipped` names every position left alone and why. Never raises.

    Owner ruling 2026-10-09: the trend exit (`trend_exit`, see
    src/intraday/trend_exit_tick.py) runs FIRST, under the same lock and owner
    check, and a holding it placed a sell for is not trailed this tick. Its
    result is `summary["trend_exit"]`; a failure there is recorded durably by
    the wired step and the trail still runs (nothing was sold, so none skipped).
    """
    summary: dict = {"status": "ran", "orders": 0, "skipped": []}
    if preamble_deferred:
        summary.update(status="deferred", reason=preamble_deferred)
        return summary
    if None in (apply_deterministic_trails, atr_for_symbol, process_lock, blocking_owner_session):
        logger.warning("tick trail: not wired on this host; no stop trailed this tick")
        summary.update(status="unavailable", reason="trail collaborators not wired")
        return summary
    try:
        investable = positions
        if split_positions is not None:
            investable, _parked = split_positions(positions)
        ready = _readable_positions(investable or [], atr_for_symbol, summary["skipped"])
        if not ready and (trend_exit is None or not investable):
            return summary
        with process_lock() as held:
            if not held:
                summary.update(status="deferred", reason="another desk process holds the broker-write lock")
                return summary
            blocking = blocking_owner_session()
            if blocking is not None:
                summary.update(status="deferred", reason=f"desk owner session unreadable or live: {blocking}")
                return summary
            selling = _run_trend_exit(trend_exit, investable, run_id, total_value, summary)
            ready = [p for p in ready if (getattr(p, "symbol", "") or "").strip().upper() not in selling]
            orders = (apply_deterministic_trails(ready, run_id=run_id) or []) if ready else []
        summary["orders"] = len(orders)
    except Exception as exc:  # noqa: BLE001 — never turn a routine tick into a failed run
        logger.exception("tick trail: failed; every stop is left as it was")
        summary.update(status="error", reason=str(exc))
    return summary
