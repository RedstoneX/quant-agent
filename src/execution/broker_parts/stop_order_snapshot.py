"""`AlpacaBroker._snapshot_stop_order`, lifted verbatim from
src/execution/broker.py."""
from __future__ import annotations


def snapshot_stop_order(order) -> dict | None:
    try:
        qty = float(getattr(order, "qty", 0) or 0)
    except (TypeError, ValueError):
        qty = 0.0
    try:
        stop_price = float(getattr(order, "stop_price", 0) or 0)
    except (TypeError, ValueError):
        stop_price = 0.0
    try:
        limit_price = float(getattr(order, "limit_price", 0) or 0)
    except (TypeError, ValueError):
        limit_price = 0.0
    if qty <= 0 or stop_price <= 0:
        return None
    return {
        "id": str(order.id),
        "qty": qty,
        "stop_price": stop_price,
        "limit_price": limit_price or None,
    }
