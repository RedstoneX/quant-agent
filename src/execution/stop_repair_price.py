"""Price read for the stop repair: retry, snapshot fallback, then blind.

An unknown price is never a reason to leave shares naked (owner ruling
2026-10-02). When every source fails, the RECORDED stop (a sourced level) is
placed without the wrong-side test and the fact is recorded durably.
"""
from __future__ import annotations

import logging

from src.execution.price_read import single_attempt_reads

logger = logging.getLogger("src.execution.stop_repair")


def read_repair_price(
    broker, symbol, *, stop_price, uncovered_qty, is_short, caller, db,
    outcome, resting_stops, rec, live_price_cls,
):
    """`(stamped, price, price_error)`; `price_error` set => place blind.

    `live_price_cls` is the stamped-reading type, handed in by the caller
    rather than imported here: importing the broker from this module closed
    an import cycle (broker -> scale_in -> coverage_watchdog -> stop_repair
    -> here). Passing the collaborator in is the pattern used elsewhere in
    this package and keeps the isinstance check exactly as strict.
    """
    stamped = None
    price = None
    price_error: Exception | None = None
    # Retry the same read once, then fall back to the intraday snapshot the
    # research path already trusts. An unknown price is never a reason to
    # leave shares naked (owner ruling 2026-10-02).
    # This loop IS the retry: each read inside it is single-attempt, so the
    # broker's own retry does not nest here (2 reads, no backoff, worst case).
    for _attempt in range(2):
        try:
            with single_attempt_reads():
                getter = getattr(broker, "get_latest_price_stamped", None)
                if callable(getter):
                    candidate = getter(symbol)
                    # isinstance, not truthiness: most tests drive this with a
                    # MagicMock broker whose auto-attributes are callable and
                    # whose return value is another MagicMock. Only a real
                    # reading may carry the freshness verdict.
                    if isinstance(candidate, live_price_cls):
                        stamped = candidate
                price = stamped.price if stamped is not None else broker.get_latest_price(symbol)
            price_error = None
            break
        except Exception as exc:  # noqa: BLE001
            price_error = exc
            logger.warning(
                "coverage repair: price lookup failed for %s: %s", symbol, exc,
            )
    if price_error is not None:
        try:
            snapshots_getter = getattr(broker, "get_intraday_snapshots", None)
            snapshot = None
            if callable(snapshots_getter):
                snapshots = snapshots_getter([symbol])
                snapshot = snapshots.get(symbol) if isinstance(snapshots, dict) else None
            if snapshot:
                from src.data.live_price import resolve_live_price

                resolved = resolve_live_price(snapshot)
                if resolved.price is not None:
                    price = resolved.price
                    stamped = None  # the snapshot IS today's print
                    price_error = None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "coverage repair: snapshot fallback failed for %s: %s", symbol, exc,
            )
    if price_error is not None:
        # Every price source failed. The level to place is the RECORDED stop
        # (sourced: the row the entry wrote). The tape cannot be checked, so
        # the wrong-side test is skipped; the broker validates stop side
        # itself and a rejection is recorded below as broker_did_not_accept.
        logger.error(
            "coverage repair: NO price source readable for %s (%s) — placing "
            "the recorded stop $%.2f blind rather than leaving it naked",
            symbol, price_error, stop_price,
        )
        if isinstance(outcome, dict):
            outcome["repair_blind_placement"] = "price_unreadable"
        from src.execution.exit_path_records import record_stop_repair_refusal
        record_stop_repair_refusal(
            db, code="price_unreadable_placed_blind",
            reason="no live price could be read from any source; the recorded "
            "stop was placed without the wrong-side check",
            symbol=symbol, uncovered_qty=uncovered_qty, is_short=is_short,
            caller=caller, held_qty=rec["held_qty"], covered_qty=rec["covered_qty"],
            resting_stops=resting_stops, stop_price=stop_price,
        )
        price = None

    return stamped, price, price_error
