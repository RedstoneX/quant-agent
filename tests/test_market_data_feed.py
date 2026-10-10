"""Daily history reads ask Alpaca for SIP with an `end` 15+ minutes old; intraday stays IEX.

Measured 2026-10-10 on the free-plan account: feed=sip daily bars return 200,
and IEX carries ~3% of market volume (AAPL median daily $ volume IEX $376M vs
SIP $12.6B). Alpaca serves SIP without a subscription when `end` is at least
15 minutes old (Alpaca market-data FAQ).
"""

import threading
from datetime import datetime, timedelta, timezone

from alpaca.data.enums import DataFeed

from src.execution.broker_parts.market_data import MarketData


class _Host:
    _data_client = None
    _screener_client = None


class _FakeClient:
    def __init__(self):
        self.bar_requests = []
        self.raw_gets = []

    def get_stock_bars(self, req):
        self.bar_requests.append(req)
        return {}

    def get(self, path, data):
        self.raw_gets.append((path, dict(data)))
        return {}


def _md(client):
    host = _Host()
    host._data_client = client
    return MarketData(
        state=host,
        api_key="k",
        secret_key="s",
        closed_bars_cache={},
        closed_bars_cache_lock=threading.Lock(),
    )


def _as_utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert isinstance(value, datetime), f"end must be a timestamp, got {value!r}"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _assert_end_old_enough(end):
    assert _as_utc(end) <= datetime.now(timezone.utc) - timedelta(minutes=15)


def test_single_symbol_daily_history_asks_for_sip_with_delayed_end():
    client = _FakeClient()
    _md(client).get_bars("AAPL", lookback_days=30)
    assert client.bar_requests, "get_bars made no request"
    for req in client.bar_requests:
        assert req.feed == DataFeed.SIP
        _assert_end_old_enough(req.end)


def test_universe_batch_daily_history_asks_for_sip_with_delayed_end():
    client = _FakeClient()
    _md(client).get_bars_batch(["AAPL", "ACNB"], lookback_days=30)
    assert client.raw_gets, "get_bars_batch made no request"
    for path, params in client.raw_gets:
        assert path == "/stocks/bars"
        assert str(params.get("feed")).lower().endswith("sip"), params
        _assert_end_old_enough(params["end"])


def test_intraday_chart_bars_stay_on_iex():
    client = _FakeClient()
    _md(client).get_intraday_chart_bars("AAPL", timeframe="5m", lookback_days=2)
    assert client.bar_requests, "intraday read made no request"
    for req in client.bar_requests:
        assert req.feed == DataFeed.IEX
