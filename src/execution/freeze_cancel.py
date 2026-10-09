"""FREEZE step 2: deal with orders already RESTING at the broker that would add exposure.

The broker-door gate (`owner_flags_gate`) refuses NEW entries while frozen, but
it only sees submissions. An entry placed before the freeze -- a resting buy
limit / buy stop for a long, a resting sell-short -- can still fill afterwards
and open exposure the owner froze. This sweep removes that exposure, and only
that exposure.

Classification matches the door's rule (`owner_flags_gate.is_entry`), judged
per order on its UNFILLED quantity, compared as exact Decimals against the
broker's own position read:
  - buy  with no position or a long        -> adds exposure   -> CANCEL
  - sell with no position or a short       -> adds exposure   -> CANCEL
  - sell_short (any)                       -> adds exposure   -> CANCEL
  - sell of at most the long held          -> exit / stop     -> KEEP
  - buy  of at most the short held         -> cover / stop    -> KEEP
  - closing-side order LARGER than held    -> would flip side -> SHRINK to the
    held size. The door refuses such an order as a flip, so it may not be left
    oversized; it is usually a protective stop not resized after a partial
    close, so it may not be cancelled either. When the shrink cannot be done
    (fractional size, broker refusal) it is kept and the named fault
    "oversized_exit_unamended" is recorded.
  - side or size unreadable                -> the door treats it as an entry
    -> CANCEL, EXCEPT a stop-type order: a stop may be the only protection a
    position has, so an unreadable stop is kept with the named fault
    "order_unreadable" rather than stripped on a guess.
Order type decides nothing else: a buy stop with no position is a stop ENTRY
and is cancelled; a sell stop under a long is protection and is kept.

If positions or open orders cannot be read, NOTHING is touched and the result
carries the named fault ("positions_unreadable" / "orders_unreadable").

Every cancel, shrink and fault is written per symbol to the reconciliation
ledger through `record_guarded_pass` (the desk's existing durable channel for
order-path outcomes), not only to the log.

Run where the freeze is observed at session start (after owner intents are
picked up); an UNKNOWN flag behaves like frozen, as at the door.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from src import owner_flags
from src.sentinel.guarded import record_guarded_pass

logger = logging.getLogger(__name__)

POSITIONS_UNREADABLE = "positions_unreadable"
ORDERS_UNREADABLE = "orders_unreadable"
OVERSIZED_EXIT_UNAMENDED = "oversized_exit_unamended"
ORDER_UNREADABLE = "order_unreadable"
CANCEL_FAILED = "cancel_failed"


class FreezeSweepFault(RuntimeError):
    """A named reason the freeze sweep could not settle an order (or any order)."""


@dataclass
class SweepResult:
    cancelled: list = field(default_factory=list)
    shrunk: list = field(default_factory=list)  # (order_id, new_qty)
    kept: list = field(default_factory=list)
    faults: list = field(default_factory=list)  # (fault_name, detail)

    @property
    def ok(self) -> bool:
        return not self.faults

    @property
    def acted(self) -> bool:
        return bool(self.cancelled or self.shrunk or self.faults)


def _norm(symbol) -> str:
    return str(symbol).strip().upper().replace("/", "")


def _enum_text(value) -> str:
    return str(getattr(value, "value", value)).lower()


def _record(broker, what: str, symbol, order_id, fault: str | None = None, detail: str = "") -> None:
    exc = FreezeSweepFault(f"{fault}: {detail}") if fault else None
    record_guarded_pass(
        broker, f"freeze_cancel.{what}", exc, context={"symbol": str(symbol or ""), "order": str(order_id or "")}
    )


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


def _is_stop_type(order) -> bool:
    return "stop" in _enum_text(getattr(order, "order_type", None) or getattr(order, "type", ""))


def classify(order, held: dict) -> tuple[str, Decimal | None]:
    """('cancel'|'keep'|'shrink'|ORDER_UNREADABLE, held size for a shrink)."""
    try:
        side = _enum_text(order.side)
        remaining = Decimal(str(order.qty)) - Decimal(str(getattr(order, "filled_qty", None) or 0))
        symbol = _norm(order.symbol)
    except (AttributeError, TypeError, ValueError, InvalidOperation):
        return ("keep_unreadable" if _is_stop_type(order) else "cancel"), None
    if not remaining.is_finite() or remaining <= 0 or side not in ("buy", "sell", "sell_short"):
        return ("keep_unreadable" if _is_stop_type(order) else "cancel"), None
    if side == "sell_short":
        return "cancel", None
    pos = held.get(symbol, Decimal(0))
    closing = (side == "sell" and pos > 0) or (side == "buy" and pos < 0)
    if not closing:
        return "cancel", None
    return ("keep", None) if remaining <= abs(pos) else ("shrink", abs(pos) + Decimal(str(order.filled_qty or 0)))


def _shrink(broker, order_id: str, new_total: Decimal) -> str | None:
    """Amend the order's quantity in place; None on success, else why not."""
    if new_total != new_total.to_integral_value():
        return f"held size {new_total} is fractional; the broker amends whole shares only"
    from alpaca.trading.requests import ReplaceOrderRequest

    try:
        broker.client.replace_order_by_id(order_id, ReplaceOrderRequest(qty=int(new_total)))
    except Exception as exc:  # noqa: BLE001 - surfaced as a named fault and recorded
        return f"broker refused the amend: {type(exc).__name__}: {exc}"
    return None


def _settle(broker, order, held: dict, result: SweepResult) -> None:
    """Cancel, shrink or keep one resting order, recording every action and fault."""
    oid = str(getattr(order, "id", "") or "")
    symbol = getattr(order, "symbol", None)
    verdict, size = classify(order, held)
    if verdict == "keep":
        result.kept.append(oid)
        return
    if verdict == "cancel" and oid and broker.cancel_entry_order(oid):
        result.cancelled.append(oid)
        _record(broker, "cancelled", symbol, oid)
        return
    why = _shrink(broker, oid, size) if verdict == "shrink" else None
    if verdict == "shrink" and why is None:
        result.shrunk.append((oid, size))
        _record(broker, "shrunk", symbol, oid)
        return
    fault, detail = {
        "cancel": (CANCEL_FAILED, "broker did not accept the cancel"),
        "shrink": (OVERSIZED_EXIT_UNAMENDED, why),
    }.get(verdict, (ORDER_UNREADABLE, "stop-type order with unreadable side or size kept"))
    logger.error("freeze sweep order %s (%s): %s -- %s", oid or "?", symbol, fault, detail)
    result.kept.append(oid)
    result.faults.append((fault, oid))
    _record(broker, fault, symbol, oid, fault, detail)


def cancel_resting_entries(broker) -> SweepResult:
    """Cancel resting exposure-adding orders, shrink oversized exits, keep exits and stops."""
    result = SweepResult()
    state = []
    for name, read in ((POSITIONS_UNREADABLE, _held_by_symbol), (ORDERS_UNREADABLE, _open_orders)):
        try:
            state.append(read(broker))
        except Exception as exc:  # noqa: BLE001 - surfaced as a named fault, nothing touched
            detail = f"{type(exc).__name__}: {exc}"
            logger.error("freeze sweep touched nothing: %s (%s)", name, detail)
            result.faults.append((name, detail))
            _record(broker, name, None, None, name, detail)
            return result
    held, orders = state
    for order in orders:
        _settle(broker, order, held, result)
    if result.acted:
        logger.warning("freeze sweep: cancelled=%s shrunk=%s faults=%s", result.cancelled, result.shrunk, result.faults)
    return result


def sweep_if_frozen(broker, db_path) -> SweepResult | None:
    """Session-start seam: run the sweep when the owner flag is frozen or UNKNOWN."""
    flags = owner_flags.read_flags(db_path)
    if not (flags.frozen or flags.unknown):
        return None
    return cancel_resting_entries(broker)
