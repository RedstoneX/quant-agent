"""The ONE rounding step a sell quantity gets before it reaches the broker.

Every sell path computes its quantity raw — a percentage of the held
quantity, a halving, a post-sale residual subtraction — so a sell can arrive
carrying more decimal places than an order may (2026-10-09 09:33 ET: partial
sells of ETN 2.6703218688 and META 0.9719239536 were refused by the desk's own
quantity gate). Rather than round at each caller, `OrderDesk.submit_order`
floors every SELL here, once, onto the gate's own grid
(`ORDER_QTY_DECIMALS` in `src/execution/order_gates.py`).

FLOOR, never round-to-nearest: flooring can only shrink a sell, so it can
never ask for more than the caller computed, and the caller's quantity is
itself bounded by what is held. A quantity that floors to zero is refused
loudly with a recorded reason; it is never silently sent as zero.

Anything that is not a finite positive number is returned untouched, so the
gate refuses it in its own words — this module rounds, it does not judge.
"""

import logging
import math
from decimal import ROUND_FLOOR, Decimal

from src.execution.order_gates import ORDER_QTY_DECIMALS

# The broker's log channel, the same one the quantity gate logs on.
logger = logging.getLogger("src.execution.broker")

_GRID = Decimal(1).scaleb(-ORDER_QTY_DECIMALS)


def floor_sell_qty(qty):
    """Return `(qty_to_submit, refusal)`.

    `refusal` is None when the quantity may go on to the gate, else the
    plain-words reason the sell is refused. `repr` is the float's shortest
    exact spelling, the same one the gate measures decimals on.
    """
    try:
        value = float(qty)
    except (TypeError, ValueError):
        return qty, None
    if not math.isfinite(value) or value <= 0:
        return qty, None
    floored = float(Decimal(repr(value)).quantize(_GRID, rounding=ROUND_FLOOR))
    if floored <= 0:
        return 0.0, (
            f"the sell quantity {value!r} is below the smallest share fraction an order can carry "
            f"({ORDER_QTY_DECIMALS} decimal places), so there is nothing to sell"
        )
    # Already on the grid: hand back the caller's own value, so a legitimate
    # request is built with exactly the fields it always was.
    return (qty if floored == value else floored), None


def floor_sell_qty_at_submission(qty, *, side: str, symbol: str):
    """`floor_sell_qty` for a SELL, logged; any other side passes untouched.

    The desk's one call site. A refusal is logged at ERROR in the gate's own
    shape and returned for the desk to record as `rejected_bad_qty`.
    """
    if side.lower() != "sell":
        return qty, None
    floored, refusal = floor_sell_qty(qty)
    if refusal is not None:
        logger.error("Quantity gate: SELL %s %s — %s. Order REJECTED.", qty, symbol, refusal)
    elif floored is not qty:
        logger.info("Sell quantity for %s floored from %r to %r (order decimal limit).", symbol, qty, floored)
    return floored, refusal
