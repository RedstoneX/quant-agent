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
provider must be wired to a broker; `broker_backed_provider()` builds one
from the same credentials every standalone script uses (paper account,
read-only — this tool never places an order). An unwired provider raises
`NoBarsSourceError` rather than reporting every symbol as "no data".
"""

from __future__ import annotations

import logging

from src.data.market import MarketDataProvider
from src.models import OHLCV

logger = logging.getLogger(__name__)


def broker_backed_provider() -> MarketDataProvider:
    """A provider reading daily bars from the broker, wired exactly as the
    live pipeline wires it (`set_fallback_bars(broker.get_bars)`). Market
    data reads only; the broker object is never asked to trade."""
    from src.api.deps import get_alpaca_credentials, get_alpaca_paper
    from src.execution.broker import AlpacaBroker

    key, secret = get_alpaca_credentials()
    broker = AlpacaBroker(api_key=key, secret_key=secret, paper=get_alpaca_paper())
    return MarketDataProvider(bars_source=broker.get_bars)


def fetch_universe_history(
    symbols: list[str],
    *,
    lookback_days: int,
    market: MarketDataProvider | None = None,
) -> tuple[dict[str, list[OHLCV]], list[str]]:
    """Fetch daily OHLCV for every symbol in `symbols`.

    Returns `(bars_by_symbol, symbols_with_no_data)`. A symbol the broker
    returns nothing for is reported in the second list rather than silently
    vanishing from the run — the caller is expected to surface it in the
    tool's own caveats output. `market` defaults to `broker_backed_provider()`.
    """
    provider = market or broker_backed_provider()
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
