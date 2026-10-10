"""Historical OHLCV loading for the backtester.

Reuses `MarketDataProvider` (src/data/market.py) exactly as it already
exists — this adds no new data source. `src/data/levels.py` and
`src/data/context.py` (which the engine calls directly) are pure functions
of `list[OHLCV]`; `MarketDataProvider.get_ohlcv` is how every live agent's
bars get to those functions in the first place (see
`src/agents/tech_analyst.py`), so the backtest reads prices through the
identical path the live system does.

`MarketDataProvider.get_ohlcv(symbol, lookback_days)` reads COMPLETED daily
bars from the broker (Alpaca; owner 2026-10-09: stock data from Alpaca, not
Yahoo) — ending at the previous session while the market is open, today
after the 16:00 ET close — going back `lookback_days` calendar days. The
provider must be wired to a broker; the caller passes a broker-backed
 provider raises
`NoBarsSourceError` rather than reporting every symbol as "no data".
"""

from __future__ import annotations

import logging

from src.data.market import MarketDataProvider
from src.models import OHLCV

logger = logging.getLogger(__name__)


def fetch_universe_history(
    symbols: list[str],
    *,
    lookback_days: int,
    market: MarketDataProvider,
) -> tuple[dict[str, list[OHLCV]], list[str]]:
    """Fetch daily OHLCV for every symbol in `symbols`.

    Returns `(bars_by_symbol, symbols_with_no_data)`. A symbol the broker
    returns nothing for is reported in the second list rather than silently
    vanishing from the run — the caller is expected to surface it in the
    tool's own caveats output. `market` is the broker-backed provider.
    """
    provider = market
    bars_by_symbol: dict[str, list[OHLCV]] = {}
    missing: list[str] = []
    for symbol in symbols:
        bars = provider.get_ohlcv(symbol, lookback_days=lookback_days)
        if bars:
            bars_by_symbol[symbol] = bars
        else:
            missing.append(symbol)
            logger.warning("backtest: no bars returned for %s — excluded from the run", symbol)
    return bars_by_symbol, missing
