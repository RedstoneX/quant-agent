"""src.exit_quote -- what an ordinary decided exit is sent at, and
the live quote it is measured against.

Every ordinary decided exit (the decision-path SELL and COVER, the midday
SELL/REDUCE/COVER) is a plain DAY MARKET order. They used to be limits at the
broker's last-trade mark less 0.5% (``status: arbitrary``); at the 2026-10-09
open the SELL limits on NET, AAPL and VLO sat unfilled for the whole wait, were
cancelled with the stops restored, and the decided exits never happened. The
owner's rulings -- "sell anything below the bar, always", "act, never just
alert" -- put the fill ahead of a few basis points, and the ledger row for the
pad itself said to delete it and send a plain marketable order. The emergency
de-lever keeps its own live-bid pricer (src/delever/ladder.py), unchanged.

What a market order costs is MEASURED, not assumed: at submit the live bid/ask
is read here and recorded beside the order, and the fill's average price lands
on the trade row through the existing fill reconciliation. A failed quote read
never blocks the exit; it is recorded as unknown.
"""

import logging
import math

from src.sentinel.guarded import record_guarded_pass

logger = logging.getLogger("src.pipeline")


def read_exit_quote(broker, symbol: str) -> dict:
    """The live ``{"bid": ..., "ask": ...}`` at submit, each ``None`` when unknown.

    Uses the same broker quote read as entries. Never raises: a missing,
    non-positive or non-finite side, or a failed read, is ``None``.
    """
    quote: dict = {"bid": None, "ask": None}
    try:
        raw = broker.get_latest_quote(symbol) or {}
        for key, field in (("bid", "bid_price"), ("ask", "ask_price")):
            value = raw.get(field)
            if value is not None:
                value = float(value)
                quote[key] = value if math.isfinite(value) and value > 0 else None
        record_guarded_pass(broker, "exit_pricing.quote_read", context={"symbol": symbol})
    except Exception as exc:  # noqa: BLE001 -- a quote read must never block the exit
        record_guarded_pass(broker, "exit_pricing.quote_read", exc, context={"symbol": symbol})
        quote = {"bid": None, "ask": None}
    logger.info(
        "exit quote at submit: %s bid=%s ask=%s (market order; fill price reconciled onto the trade row)",
        symbol,
        quote["bid"],
        quote["ask"],
    )
    return quote
