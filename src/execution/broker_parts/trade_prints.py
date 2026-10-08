"""ONE batched latest-trades read for many names; only TODAY prints answer.

Used by the entry stage's today-print wait
(`src.price_feed_preflight.wait_for_today_prints`): every waiting name goes
into one request, never one request per name. A free function over the
broker's market-data part rather than a new broker method, so neither
`broker.py` nor `market_data.py` grows. Same retry-then-typed-failure
contract as `get_latest_price_stamped`: a read that FAILS after the ledgered
retry raises `PriceReadFailed`; an empty dict is measured absence; `None`
means this broker has no real market-data part (a test stub), so there is
nothing to re-ask.
"""
from __future__ import annotations

import logging

from src.execution.broker_parts.market_data import LivePrice, MarketData, _install_http_timeout
from src.execution.broker_parts.stop_place import _alpaca_symbol
from src.execution.price_read import read_price_with_retry

logger = logging.getLogger(__name__)


def read_latest_trade_prints(broker, symbols: list[str]) -> dict[str, LivePrice] | None:
    factory = getattr(broker, "_market_data", None)
    market_data = factory() if callable(factory) else broker
    if not isinstance(market_data, MarketData):
        return None

    def once(wanted: list[str]) -> dict[str, LivePrice]:
        from alpaca.data.requests import StockLatestTradeRequest
        from src.trading_calendar import live_price_is_today

        if market_data._data_client is None:
            from alpaca.data.historical.stock import StockHistoricalDataClient

            market_data._data_client = StockHistoricalDataClient(
                market_data.api_key, market_data.secret_key)
            _install_http_timeout(market_data._data_client)
        by_alpaca = {_alpaca_symbol(s): s for s in wanted}
        trade_data = market_data._data_client.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=list(by_alpaca))
        )
        prints: dict[str, LivePrice] = {}
        for alpaca_symbol, symbol in by_alpaca.items():
            trade = market_data._extract_symbol_payload(trade_data, alpaca_symbol)
            trade_price = float(getattr(trade, "price", 0) or 0)
            trade_at = getattr(trade, "timestamp", None)
            if trade_price > 0 and live_price_is_today(trade_at):
                prints[symbol] = LivePrice(
                    price=trade_price, source="last_trade", trade_at=trade_at,
                    is_today=True, is_today_print=True,
                )
        return prints

    return read_price_with_retry(once, list(symbols), log=logger)
