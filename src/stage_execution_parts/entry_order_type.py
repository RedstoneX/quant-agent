"""Which order type an ENTRY (BUY / SHORT) is sent as: market or marketable limit.

Owner ruling 2026-10-09 (paper): market orders on everything for now. Exits
became plain DAY market orders the same day (src/exit_quote.py); this is the
entry side. ONE switch, `execution.entry_order_type`, read here and nowhere
else, so the limit path (the 40bp ceiling, its `entry_slippage_check`
recorder and the single-shot re-peg) stays reachable for go-live testing
without governing the path the desk actually trades. Anything not spelled
`limit` is market, so an absent or malformed setting can never turn the
ceiling back on silently.
"""

from __future__ import annotations

MARKET = "market"
LIMIT = "limit"


def entry_order_type(pipeline) -> str:
    """`"market"` (default) or `"limit"`, from `execution.entry_order_type`."""
    raw = getattr(getattr(getattr(pipeline, "config", None), "execution", None), "entry_order_type", None)
    return LIMIT if isinstance(raw, str) and raw.strip().lower() == LIMIT else MARKET


def entry_orders_are_market(pipeline) -> bool:
    return entry_order_type(pipeline) == MARKET
