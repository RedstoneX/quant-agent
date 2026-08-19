"""Alpaca market-data and open-order accounting paths on `AlpacaBroker`.

`get_latest_price`, `get_bars` and `open_buy_notional` were the three
entirely-uncovered blocks in `src/execution/broker.py`. They matter for
commissioning specifically: they are the read paths that go to
`data.alpaca.markets` (a *different* host from the trading API, and a
separate credential-routing case — see
`docs/architecture/CREDENTIAL_DELIVERY_EVIDENCE.md`), and each has a
deliberate degradation contract that a broker outage exercises.

Conventions follow `tests/test_broker.py`: patch
`src.execution.broker.TradingClient`, drive everything else with
MagicMock. `_data_client` is assigned directly in most tests because the
SDK client is lazily imported inside each method; one test covers that
lazy construction explicitly.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.execution.broker import AlpacaBroker


def _broker() -> AlpacaBroker:
    with patch("src.execution.broker.TradingClient") as tc:
        tc.return_value = MagicMock()
        return AlpacaBroker(api_key="test", secret_key="test", paper=True)


# ---------------------------------------------------------------------------
# get_latest_price — trade first, then quote midpoint, then one-sided quote
# ---------------------------------------------------------------------------

def _price_client(trade=None, quote=None, raises=None):
    client = MagicMock()
    if raises is not None:
        client.get_stock_latest_trade.side_effect = raises
    else:
        client.get_stock_latest_trade.return_value = {"NVDA": trade}
    client.get_stock_latest_quote.return_value = {"NVDA": quote}
    return client


def test_latest_price_prefers_the_last_trade():
    b = _broker()
    b._data_client = _price_client(trade=SimpleNamespace(price=181.25))
    assert b.get_latest_price("NVDA") == 181.25
    # A usable trade price must short-circuit the quote call entirely.
    b._data_client.get_stock_latest_quote.assert_not_called()


def test_latest_price_falls_back_to_the_quote_midpoint():
    b = _broker()
    b._data_client = _price_client(
        trade=SimpleNamespace(price=0),
        quote=SimpleNamespace(ask_price=101.0, bid_price=99.0),
    )
    assert b.get_latest_price("NVDA") == 100.0


@pytest.mark.parametrize("ask,bid,expected", [
    (101.0, 0, 101.0),   # ask-only book
    (0, 99.0, 99.0),     # bid-only book
])
def test_latest_price_accepts_a_one_sided_quote(ask, bid, expected):
    b = _broker()
    b._data_client = _price_client(
        trade=SimpleNamespace(price=0),
        quote=SimpleNamespace(ask_price=ask, bid_price=bid),
    )
    assert b.get_latest_price("NVDA") == expected


def test_latest_price_returns_none_when_nothing_is_quotable():
    """No price is an honest `None`; callers gate on `price is not None and
    price > 0` before sizing an order."""
    b = _broker()
    b._data_client = _price_client(
        trade=SimpleNamespace(price=0),
        quote=SimpleNamespace(ask_price=0, bid_price=0),
    )
    assert b.get_latest_price("NVDA") is None


def test_latest_price_returns_none_when_the_data_api_raises():
    """A market-data outage must not propagate — it degrades to "no price"."""
    b = _broker()
    b._data_client = _price_client(raises=ConnectionError("data.alpaca.markets down"))
    assert b.get_latest_price("NVDA") is None


def test_latest_price_lazily_builds_one_data_client():
    """The data client is constructed on first use and then reused — the
    market-data host is a separate credentialed endpoint, so a fresh client
    per quote would multiply connections through the credential gateway."""
    b = _broker()
    fake = _price_client(trade=SimpleNamespace(price=5.0))
    with patch(
        "alpaca.data.historical.stock.StockHistoricalDataClient",
        return_value=fake,
    ) as ctor:
        assert b.get_latest_price("NVDA") == 5.0
        assert b.get_latest_price("NVDA") == 5.0
    assert ctor.call_count == 1


@pytest.mark.parametrize("payload,expected", [
    ({"NVDA": SimpleNamespace(price=1.0)}, 1.0),          # dict form
    (SimpleNamespace(NVDA=SimpleNamespace(price=2.0)), 2.0),  # attribute form
])
def test_extract_symbol_payload_handles_both_sdk_shapes(payload, expected):
    b = _broker()
    client = MagicMock()
    client.get_stock_latest_trade.return_value = payload
    b._data_client = client
    assert b.get_latest_price("NVDA") == expected


# ---------------------------------------------------------------------------
# get_bars — the yfinance fallback path
# ---------------------------------------------------------------------------

def _bar(day: int, close: float = 10.0, **overrides):
    base = dict(
        timestamp=datetime(2026, 8, day, tzinfo=timezone.utc),
        open=9.0, high=11.0, low=8.0, close=close, volume=1000,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _bars_client(raw):
    client = MagicMock()
    client.get_stock_bars.return_value = raw
    return client


def test_get_bars_maps_a_barset_into_ohlcv():
    b = _broker()
    b._data_client = _bars_client(SimpleNamespace(data={"NVDA": [_bar(3), _bar(4, 12.0)]}))
    bars = b.get_bars("NVDA", lookback_days=5)
    assert [x.date for x in bars] == [date(2026, 8, 3), date(2026, 8, 4)]
    assert bars[1].close == 12.0
    assert bars[0].volume == 1000


def test_get_bars_accepts_a_plain_dict_response():
    b = _broker()
    b._data_client = _bars_client({"NVDA": [_bar(3)]})
    assert len(b.get_bars("NVDA")) == 1


def test_get_bars_returns_empty_when_the_symbol_has_no_data():
    b = _broker()
    b._data_client = _bars_client(SimpleNamespace(data={"NVDA": []}))
    assert b.get_bars("NVDA") == []


def test_get_bars_skips_a_bar_with_no_timestamp():
    """A dateless bar has nothing to index on — drop it, keep the rest,
    rather than failing the whole lookback."""
    b = _broker()
    b._data_client = _bars_client(
        SimpleNamespace(data={"NVDA": [_bar(3), _bar(4, timestamp=None)]})
    )
    assert [x.date for x in b.get_bars("NVDA")] == [date(2026, 8, 3)]


def test_get_bars_skips_a_bar_with_unparseable_values():
    b = _broker()
    b._data_client = _bars_client(
        SimpleNamespace(data={"NVDA": [_bar(3, close="not-a-number"), _bar(4)]})
    )
    assert [x.date for x in b.get_bars("NVDA")] == [date(2026, 8, 4)]


def test_get_bars_returns_empty_when_the_data_api_raises():
    """`get_bars` is itself the fallback for an empty yfinance result — it
    must degrade to "no bars" so the caller stays on its own empty-data
    path instead of crashing the session."""
    b = _broker()
    client = MagicMock()
    client.get_stock_bars.side_effect = ConnectionError("data.alpaca.markets down")
    b._data_client = client
    assert b.get_bars("NVDA") == []


# ---------------------------------------------------------------------------
# open_buy_notional — the None-vs-0.0 distinction the cash sweeper relies on
# ---------------------------------------------------------------------------

def _open_order(symbol="NVDA", side="buy", qty="10",
                limit_price=None, stop_price=None):
    return SimpleNamespace(
        symbol=symbol, side=SimpleNamespace(value=side), qty=qty,
        limit_price=limit_price, stop_price=stop_price,
    )


def _with_orders(orders):
    b = _broker()
    b.client.get_orders.return_value = orders
    return b


def test_open_buy_notional_sums_limit_priced_buys():
    b = _with_orders([
        _open_order(qty="10", limit_price="100"),
        _open_order(qty="5", limit_price="20"),
    ])
    assert b.open_buy_notional() == pytest.approx(1100.0)


def test_open_buy_notional_uses_stop_price_when_there_is_no_limit():
    b = _with_orders([_open_order(qty="10", stop_price="50")])
    assert b.open_buy_notional() == pytest.approx(500.0)


def test_open_buy_notional_prices_a_market_order_from_the_live_quote():
    b = _with_orders([_open_order(qty="4")])
    b.get_latest_price = lambda symbol: 25.0
    assert b.open_buy_notional() == pytest.approx(100.0)


def test_open_buy_notional_returns_none_when_a_market_order_cannot_be_priced():
    """The None-vs-0.0 distinction is load-bearing: Alpaca's `cash` does not
    subtract open-order holds, so "unknowable" must make the sweeper skip
    parking. Reporting 0.0 would let it sweep cash a pending fill needs."""
    b = _with_orders([_open_order(qty="4")])
    b.get_latest_price = lambda symbol: None
    assert b.open_buy_notional() is None


def test_open_buy_notional_ignores_non_buy_rows():
    """The SDK filter already asks for BUYs; the in-loop side check is the
    second belt, and it must not count a SELL toward the hold."""
    b = _with_orders([
        _open_order(qty="10", limit_price="100"),
        _open_order(side="sell", qty="10", limit_price="100"),
    ])
    assert b.open_buy_notional() == pytest.approx(1000.0)


def test_open_buy_notional_is_zero_when_there_are_no_open_buys():
    """Genuinely no pending buys is 0.0 — distinct from the None above."""
    assert _with_orders([]).open_buy_notional() == 0.0


def test_open_buy_notional_returns_none_when_the_query_fails():
    b = _broker()
    b.client.get_orders.side_effect = ConnectionError("broker unreachable")
    assert b.open_buy_notional() is None


def test_open_buy_notional_treats_an_unparseable_qty_as_zero():
    b = _with_orders([_open_order(qty="junk", limit_price="100")])
    assert b.open_buy_notional() == 0.0


def test_open_buy_notional_skips_a_non_positive_price_and_uses_the_quote():
    """A zero/negative limit is not a usable price — fall through to the
    live quote rather than booking a $0 hold."""
    b = _with_orders([_open_order(qty="2", limit_price="0")])
    b.get_latest_price = lambda symbol: 30.0
    assert b.open_buy_notional() == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# get_intraday_snapshots — 2026-08-19 intraday opportunity-discovery fix.
# One bulk snapshot call for the whole watchlist (never one call per
# symbol) — this is what makes running a scan every intra_check tick cheap.
# ---------------------------------------------------------------------------

def _snapshot_client(snapshots: dict):
    client = MagicMock()
    client.get_stock_snapshot.return_value = snapshots
    return client


def test_intraday_snapshots_reads_last_trade_and_prior_close():
    b = _broker()
    b._data_client = _snapshot_client({
        "NVDA": SimpleNamespace(
            symbol="NVDA",
            latest_trade=SimpleNamespace(price=185.0),
            previous_daily_bar=SimpleNamespace(close=180.0),
        ),
    })
    out = b.get_intraday_snapshots(["NVDA"])
    assert out == {"NVDA": {"last_price": 185.0, "prev_close": 180.0}}


def test_intraday_snapshots_is_a_single_bulk_call_for_many_symbols():
    b = _broker()
    b._data_client = _snapshot_client({
        "NVDA": SimpleNamespace(
            symbol="NVDA", latest_trade=SimpleNamespace(price=185.0),
            previous_daily_bar=SimpleNamespace(close=180.0),
        ),
        "AAPL": SimpleNamespace(
            symbol="AAPL", latest_trade=SimpleNamespace(price=210.0),
            previous_daily_bar=SimpleNamespace(close=200.0),
        ),
    })
    out = b.get_intraday_snapshots(["NVDA", "AAPL"])
    assert out["NVDA"]["last_price"] == 185.0
    assert out["AAPL"]["prev_close"] == 200.0
    # Exactly one network call regardless of watchlist size.
    assert b._data_client.get_stock_snapshot.call_count == 1


def test_intraday_snapshots_degrades_to_none_fields_for_a_missing_symbol():
    b = _broker()
    b._data_client = _snapshot_client({})  # SGOV not in the response at all
    out = b.get_intraday_snapshots(["SGOV"])
    assert out == {"SGOV": {"last_price": None, "prev_close": None}}


def test_intraday_snapshots_returns_empty_dict_on_total_failure():
    b = _broker()
    b._data_client = MagicMock()
    b._data_client.get_stock_snapshot.side_effect = ConnectionError("data.alpaca.markets down")
    assert b.get_intraday_snapshots(["NVDA"]) == {}


def test_intraday_snapshots_empty_symbol_list_short_circuits():
    b = _broker()
    b._data_client = MagicMock()
    assert b.get_intraday_snapshots([]) == {}
    b._data_client.get_stock_snapshot.assert_not_called()
