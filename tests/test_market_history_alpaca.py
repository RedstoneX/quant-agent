"""The live desk's daily history comes from the broker (Alpaca), not Yahoo.

Owner, 2026-10-09: stock data from Alpaca. `MarketDataProvider.get_ohlcv`
reads the wired broker bars method and nothing else; Yahoo is removed from
that path, not demoted to a silent fallback. A failure is reported for the
one symbol (returns [], counted) so callers skip that name — never halt.
"""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.data.market import MarketDataProvider, NoBarsSourceError
from src.models import OHLCV

CUTOFF = date(2026, 10, 8)  # last completed session while 2026-10-09 is still trading


def _bar(d: date, close: float = 10.0) -> OHLCV:
    return OHLCV(date=d, open=9.0, high=11.0, low=8.0, close=close, volume=1000)


@pytest.fixture(autouse=True)
def _yahoo_forbidden():
    """Any Yahoo call on the history path is a test failure."""

    def _boom(*a, **k):
        raise AssertionError("yfinance must not be called for daily history")

    with (
        patch("src.data.market.yf.download", side_effect=_boom),
        patch("src.data.market.yf.Ticker", side_effect=_boom),
        patch("src.data.market.last_completed_bar_date", return_value=CUTOFF),
    ):
        yield


def test_history_comes_from_the_broker_bars_method():
    calls = []

    def broker_bars(symbol, lookback_days):
        calls.append((symbol, lookback_days))
        return [_bar(date(2026, 10, 6)), _bar(date(2026, 10, 7), close=12.0), _bar(CUTOFF, close=13.0)]

    provider = MarketDataProvider()
    provider.set_bars_source(broker_bars)
    bars = provider.get_ohlcv("NVDA", lookback_days=60)
    assert calls == [("NVDA", 60)]
    assert [b.date for b in bars] == [date(2026, 10, 6), date(2026, 10, 7), CUTOFF]
    assert bars[-1].close == 13.0


def test_pipeline_wiring_name_lands_on_the_primary_source():
    """`set_fallback_bars` is how the pipeline wires the broker today; it
    must now feed the PRIMARY read, not a fallback behind Yahoo."""
    provider = MarketDataProvider()
    provider.set_fallback_bars(lambda s, n: [_bar(CUTOFF)])
    assert [b.date for b in provider.get_ohlcv("AAPL", 5)] == [CUTOFF]


def test_todays_partial_bar_is_dropped_in_market_hours():
    today = date(2026, 10, 9)
    provider = MarketDataProvider(bars_source=lambda s, n: [_bar(CUTOFF), _bar(today, close=99.0)])
    bars = provider.get_ohlcv("MSFT", 10)
    assert [b.date for b in bars] == [CUTOFF]


def test_a_broker_failure_is_reported_for_that_symbol_only():
    def broker_bars(symbol, lookback_days):
        if symbol == "BAD":
            raise RuntimeError("alpaca 429")
        return [_bar(CUTOFF)]

    provider = MarketDataProvider(bars_source=broker_bars)
    with patch("src.data.market.record_swallowed") as counted:
        assert provider.get_ohlcv("BAD", 10) == []
        assert provider.get_ohlcv("GOOD", 10) != []
    assert counted.call_count == 1
    assert counted.call_args.kwargs["symbol"] == "BAD"


def test_an_unwired_provider_raises_a_named_error_never_empty():
    """A provider nobody wired to the broker is a wiring defect: it must not
    look like "no data" (which a backtest would silently run on)."""
    with pytest.raises(NoBarsSourceError):
        MarketDataProvider().get_ohlcv("SPY", 10)


def test_backtest_history_reads_through_a_broker_backed_provider():
    from src.backtest.data import fetch_universe_history

    def broker_bars(symbol, lookback_days):
        return [] if symbol == "GONE" else [_bar(date(2026, 10, 6)), _bar(CUTOFF)]

    provider = MarketDataProvider(bars_source=broker_bars)
    bars_by_symbol, missing = fetch_universe_history(["NVDA", "GONE"], lookback_days=30, market=provider)
    assert sorted(bars_by_symbol) == ["NVDA"]
    assert [b.date for b in bars_by_symbol["NVDA"]] == [date(2026, 10, 6), CUTOFF]
    assert missing == ["GONE"]


def test_backtest_script_builds_a_broker_backed_provider():
    """`scripts/backtest.py` wires the broker itself (src.backtest must not
    import the broker seam); credentials patched, bars from the broker."""
    from scripts.backtest import _broker_backed_provider

    with (
        patch("src.api.deps.get_alpaca_credentials", return_value=("k", "s")),
        patch("src.api.deps.get_alpaca_paper", return_value=True),
        patch("src.execution.broker.TradingClient", return_value=MagicMock()),
        patch("src.execution.broker.AlpacaBroker.get_bars", return_value=[_bar(CUTOFF)]) as get_bars,
    ):
        provider = _broker_backed_provider()
        assert [b.date for b in provider.get_ohlcv("SPY", 5)] == [CUTOFF]
    assert get_bars.call_count == 1


# ---------------------------------------------------------------------------
# The broker request itself: adjusted like the Yahoo series it replaced, on
# the feed this account is entitled to.
# ---------------------------------------------------------------------------


def test_broker_daily_bars_request_is_fully_adjusted_on_the_sip_feed():
    from alpaca.data.enums import Adjustment, DataFeed
    from src.execution.broker import AlpacaBroker

    with patch("src.execution.broker.TradingClient") as tc:
        tc.return_value = MagicMock()
        b = AlpacaBroker(api_key="test", secret_key="test", paper=True)
    raw_bar = SimpleNamespace(
        timestamp=datetime(2026, 10, 8, tzinfo=timezone.utc), open=9.0, high=11.0, low=8.0, close=10.0, volume=1
    )
    b._data_client = MagicMock()
    b._data_client.get_stock_bars.return_value = SimpleNamespace(data={"NVDA": [raw_bar]})
    assert len(b.get_bars("NVDA", lookback_days=5)) == 1
    request = b._data_client.get_stock_bars.call_args.args[0]
    assert request.adjustment == Adjustment.ALL
    assert request.feed == DataFeed.SIP
