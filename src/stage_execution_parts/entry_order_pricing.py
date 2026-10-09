"""The entry loop's pricing phase: what the order is sent as, and what the risk budget divides by.

Two order types, one switch (src/stage_execution_parts/entry_order_type.py):

* MARKET (the deployed default, owner ruling 2026-10-09): a plain DAY market
  order, no limit. The RISK divisor is the price the desk is about to pay —
  the live ASK for a BUY, the live BID for a SHORT. No usable quote side
  means the name is REFUSED with `no_price`; it never falls back to the
  today print or the analyst entry. That fallback is where the 2026-10-04
  measurement found positions carrying more than their allotted risk (6.1%
  median BUY overshoot, 25% worst, 10.9% on a short): the budget was divided
  by a number that was not the fill. The ALLOCATION divisor is raised to the
  ask on a BUY (fewer shares, as the limit path raised it to the ceiling)
  and left on the print for a SHORT, which must never divide by a
  below-market number (docs/WORK.md item 120).
* LIMIT (reachable only when `execution.entry_order_type: limit`): the
  marketable limit at the slippage ceiling / floor, priced by
  `entry_limit_from_quote`; the risk divisor is the limit itself, exactly as
  the 2026-10-04 fix left it.

Either way the divisor moves WITH the order type: it is always the worst
price the order can fill at.
"""

from __future__ import annotations

import math

from src.pipeline_stages import _record_execution_skip
from src.stage_execution_parts.entry_order_type import entry_orders_are_market
from src.stage_execution_parts.entry_quote import entry_limit_from_quote
from src.stage_execution_parts.state import SKIP

NO_PRICE = "no_price"


def quote_side_paid(is_short: bool, bid, ask) -> float | None:
    """The live price a market entry pays: ask for a BUY, bid for a SHORT; None when unusable."""
    side = bid if is_short else ask
    if isinstance(side, bool) or not isinstance(side, (int, float)):
        return None
    value = float(side)
    return value if math.isfinite(value) and value > 0 else None


def no_price_detail(action: str, is_short: bool) -> str:
    side = "bid" if is_short else "ask"
    return (
        f"no live {side} at submit; a market {action} is sized against the "
        f"price it pays, never the print or the analyst entry"
    )


def price_entry(run, leg, limit_price, sizing_price, risk_sizing_price):
    """Return `(limit_price, sizing_price, risk_sizing_price)`, or `SKIP` where the name is refused."""
    if entry_orders_are_market(run.pipeline):
        paid = quote_side_paid(leg.is_short, leg.bid, leg.ask)
        if paid is None:
            _record_execution_skip(
                run.pipeline,
                run.ctx,
                leg.decision.symbol,
                NO_PRICE,
                no_price_detail(leg.decision.action, leg.is_short),
            )
            return SKIP
        if not leg.is_short:
            sizing_price = max(sizing_price or 0.0, paid)
        return None, sizing_price, paid
    priced = entry_limit_from_quote(run, leg, limit_price, sizing_price)
    if priced is SKIP:
        return SKIP
    limit_price, sizing_price = priced
    if isinstance(limit_price, (int, float)) and limit_price > 0:
        risk_sizing_price = float(limit_price)
    return limit_price, sizing_price, risk_sizing_price
