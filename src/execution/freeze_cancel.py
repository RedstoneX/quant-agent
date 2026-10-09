"""FREEZE step 2: cancel orders already RESTING at the broker that would add exposure.

The broker-door gate (`owner_flags_gate`) refuses NEW entries while frozen, but
it only sees submissions. An entry placed before the freeze -- a resting buy
limit / buy stop for a long, a resting sell-short -- can still fill afterwards
and open exposure the owner froze. This sweep cancels those, and only those.

Classification is position-aware and matches the door's rule exactly
(`owner_flags_gate.is_entry`), judged per order on its UNFILLED quantity,
compared as exact Decimals against the broker's own position read:
  - buy  with no position or a long        -> adds exposure   -> CANCEL
  - sell with no position or a short       -> adds exposure   -> CANCEL
  - sell_short (any)                       -> adds exposure   -> CANCEL
  - sell of at most the long held          -> exit / stop     -> KEEP
  - buy  of at most the short held         -> cover / stop    -> KEEP
  - closing-side order LARGER than held    -> would flip side -> KEEP, named
    fault "oversized_exit". Such an order is almost always a protective stop
    not resized after a partial close; cancelling it would strip the
    position's protection, which the owner ruled must never happen. It is
    reported so it can be resized, never silently cancelled.
Order type does not decide anything: a buy stop with no position is a stop
ENTRY and is cancelled; a sell stop under a long is protection and is kept.

If positions or open orders cannot be read, NOTHING is cancelled and the
result carries the named fault ("positions_unreadable" / "orders_unreadable"):
when the sweep cannot tell, it touches nothing.

Run it where the freeze is observed at session start (after owner intents are
picked up); an UNKNOWN flag behaves like frozen, as at the door.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from src import owner_flags

logger = logging.getLogger(__name__)

POSITIONS_UNREADABLE = "positions_unreadable"
ORDERS_UNREADABLE = "orders_unreadable"
OVERSIZED_EXIT = "oversized_exit"
ORDER_UNREADABLE = "order_unreadable"


@dataclass
class SweepResult:
    cancelled: list = field(default_factory=list)
    kept: list = field(default_factory=list)
    cancel_failed: list = field(default_factory=list)
    faults: list = field(default_factory=list)  # (fault_name, detail)

    @property
    def ok(self) -> bool:
        return not self.faults and not self.cancel_failed


def _norm(symbol) -> str:
    return str(symbol).strip().upper().replace("/", "")


def _enum_text(value) -> str:
    return str(getattr(value, "value", value)).lower()


def _held_by_symbol(broker) -> dict:
    """{symbol: signed Decimal qty}; raises on any read or parse failure."""
    held: dict = {}
    for p in list(broker.get_positions()):
        key = _norm(p.symbol)
        held[key] = held.get(key, Decimal(0)) + Decimal(str(p.qty))
    return held


def _open_orders(broker) -> list:
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    # Flat (nested=False): every working order -- bracket legs included -- is
    # judged on its own, so a filled bracket's stop leg is seen and kept.
    return list(broker.client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=False)) or [])


def classify(order, held: dict) -> str:
    """'cancel', 'keep', OVERSIZED_EXIT or ORDER_UNREADABLE for one resting order."""
    try:
        side = _enum_text(order.side)
        remaining = Decimal(str(order.qty)) - Decimal(str(getattr(order, "filled_qty", None) or 0))
        symbol = _norm(order.symbol)
    except (AttributeError, TypeError, ValueError, InvalidOperation):
        return ORDER_UNREADABLE
    if not remaining.is_finite() or remaining <= 0:
        return ORDER_UNREADABLE
    if side not in ("buy", "sell"):
        return "cancel" if side == "sell_short" else ORDER_UNREADABLE
    pos = held.get(symbol, Decimal(0))
    closing = (side == "sell" and pos > 0) or (side == "buy" and pos < 0)
    if not closing:
        return "cancel"
    return "keep" if remaining <= abs(pos) else OVERSIZED_EXIT


def cancel_resting_entries(broker) -> SweepResult:
    """Cancel every resting exposure-adding order; keep every exit and stop."""
    result = SweepResult()
    try:
        held = _held_by_symbol(broker)
    except Exception as exc:  # noqa: BLE001 - surfaced as a named fault, nothing cancelled
        logger.error("freeze sweep cancelled nothing: %s (%s: %s)", POSITIONS_UNREADABLE, type(exc).__name__, exc)
        result.faults.append((POSITIONS_UNREADABLE, f"{type(exc).__name__}: {exc}"))
        return result
    try:
        orders = _open_orders(broker)
    except Exception as exc:  # noqa: BLE001 - surfaced as a named fault, nothing cancelled
        logger.error("freeze sweep cancelled nothing: %s (%s: %s)", ORDERS_UNREADABLE, type(exc).__name__, exc)
        result.faults.append((ORDERS_UNREADABLE, f"{type(exc).__name__}: {exc}"))
        return result
    for order in orders:
        oid = str(getattr(order, "id", "") or "")
        verdict = classify(order, held)
        if verdict == "keep":
            result.kept.append(oid)
        elif verdict == "cancel" and oid:
            if broker.cancel_entry_order(oid):
                result.cancelled.append(oid)
            else:
                result.cancel_failed.append(oid)
        else:
            fault = verdict if verdict != "cancel" else ORDER_UNREADABLE
            logger.error("freeze sweep kept order %s: %s", oid or "?", fault)
            result.kept.append(oid)
            result.faults.append((fault, oid))
    if result.cancelled or result.faults or result.cancel_failed:
        logger.warning(
            "freeze sweep: cancelled=%s cancel_failed=%s faults=%s",
            result.cancelled,
            result.cancel_failed,
            result.faults,
        )
    return result


def sweep_if_frozen(broker, db_path) -> SweepResult | None:
    """Session-start seam: run the sweep when the owner flag is frozen or UNKNOWN."""
    flags = owner_flags.read_flags(db_path)
    if not (flags.frozen or flags.unknown):
        return None
    return cancel_resting_entries(broker)
