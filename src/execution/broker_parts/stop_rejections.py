"""Broker-rejection classifiers lifted out of `stop_place` so that file does not grow.

Re-exported there under the same names.
"""
from __future__ import annotations


def _is_held_for_orders_error(exc: BaseException) -> bool:
    """True when the broker refused because shares are reserved by an open order.

    Alpaca surfaces this as `held_for_orders` and/or `insufficient qty
    available` (2026-04-25 AMZN, 2026-09-16 BRK-B DAY sliver).
    """
    text = str(exc).lower()
    return "held_for_orders" in text or "insufficient qty" in text


def _is_unsupported_stop_market_rejection(exc: BaseException) -> bool:
    """True when the broker refused a stop-MARKET specifically because the
    order TYPE / TIME-IN-FORCE combination is not supported — the one
    rejection that must degrade to a stop-LIMIT rather than to no stop.

    Owner ratified 2026-09-25: protective stops are stop-MARKET (a guaranteed
    exit — an elected stop fills instead of resting unfilled past a limit).
    Every combo this desk submits (whole-share GTC, fractional DAY) is a
    plain stop order and should be accepted (STOP/DAY was proven accepted by
    the 2026-09-01 live probe); this classifier exists ONLY so a position is
    never left unprotected if some combo turns out refused — a market-stop
    refusal degrades to a stop-limit, never to no stop.

    Deliberately NARROW so it cannot swallow an unrelated rejection:

      * a held_for_orders / insufficient-qty refusal is NOT this — it is
        handled by the retry / existing-stop path and must propagate;
      * a garbage stop price is short-circuited before submit;
      * only a 400/422 whose message names the order TYPE / CLASS or the
        TIME-IN-FORCE as the problem qualifies.

    A false positive here is harmless anyway: the stop-limit fallback submit
    is UNGUARDED, so a rejection that was not really a type/tif problem still
    surfaces as an exception from that second attempt — never swallowed,
    only retried once as a stop-limit.
    """
    if _is_held_for_orders_error(exc):
        return False
    status_code = getattr(exc, "status_code", None)
    if status_code not in (400, 422):
        return False
    text = str(exc).lower()
    type_terms = (
        "order type", "order_type", "order class", "order_class",
        "time_in_force", "time in force",
        "not supported", "unsupported",
        "not permitted", "not allowed", "invalid order",
    )
    return any(term in text for term in type_terms)


def real_broker_order_id(value: object) -> str:
    """The broker order id in `value`, or "" when there ISN'T one.

    `_snapshot_stop_order` stamps `"id": str(order.id)`, so an order that
    reached it without an id carries the four-character string "None" —
    which is TRUTHY. Every `if spec.get("id")` filter therefore counted a
    missing id as a present one, and the resulting "id" then matched no
    open order at the broker, ever. Judge the value, don't test the
    stringified None for truthiness.
    """
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "null", "nan"}:
        return ""
    return text
