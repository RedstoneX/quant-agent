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


def test_latest_quote_returns_truthful_sides():
    b = _broker()
    b._data_client = _price_client(
        trade=None, quote=SimpleNamespace(ask_price=101.25, bid_price=101.0),
    )
    assert b.get_latest_quote("NVDA") == {
        "bid_price": 101.0, "ask_price": 101.25,
    }


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


def test_get_intraday_chart_bars_preserves_timestamp_and_timeframe():
    b = _broker()
    b._data_client = _bars_client(
        SimpleNamespace(data={"MRVL": [_bar(
            21, close=237.07,
            timestamp=datetime(2026, 8, 21, 13, 30, tzinfo=timezone.utc),
        )]})
    )
    bars = b.get_intraday_chart_bars("MRVL", timeframe="5m", lookback_days=1)
    assert bars == [{
        "date": "2026-08-21",
        "timestamp": "2026-08-21T13:30:00+00:00",
        "open": 9.0, "high": 11.0, "low": 8.0,
        "close": 237.07, "volume": 1000,
    }]
    request = b._data_client.get_stock_bars.call_args.args[0]
    assert str(request.timeframe) == "5Min"


def test_get_intraday_chart_bars_requests_the_iex_feed():
    """2026-08-24 regression: every intraday timeframe (5m/15m/1h) came back
    empty for every symbol, at every lookback window, while daily bars
    worked fine — because the request left `feed` unset, which resolves to
    SIP server-side for sub-daily bars and this account's plan is only
    entitled to IEX. Pins the fix so it can't silently regress back to the
    same all-symbols, all-timeframes empty-bars failure."""
    from alpaca.data.enums import DataFeed

    b = _broker()
    b._data_client = _bars_client(SimpleNamespace(data={"MRVL": [_bar(21)]}))
    b.get_intraday_chart_bars("MRVL", timeframe="1h", lookback_days=5)
    request = b._data_client.get_stock_bars.call_args.args[0]
    assert request.feed == DataFeed.IEX


def test_get_intraday_chart_bars_rejects_unknown_timeframe_without_a_call():
    b = _broker()
    b._data_client = _bars_client(SimpleNamespace(data={"MRVL": [_bar(21)]}))
    assert b.get_intraday_chart_bars("MRVL", timeframe="2m", lookback_days=1) == []
    b._data_client.get_stock_bars.assert_not_called()


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


def test_intraday_snapshots_reads_last_trade_prior_close_and_session_bar():
    """Also carries TODAY's still-forming bar under a distinct `session_*`
    namespace so no caller can mistake it for a completed daily bar."""
    b = _broker()
    from datetime import datetime as _dt

    from src.trading_calendar import ET

    trade_at = _dt(2026, 9, 17, 10, 30, tzinfo=ET)
    bar_at = _dt(2026, 9, 17, 0, 0, tzinfo=ET)
    minute_at = _dt(2026, 9, 17, 10, 29, tzinfo=ET)
    b._data_client = _snapshot_client({
        "NVDA": SimpleNamespace(
            symbol="NVDA",
            latest_trade=SimpleNamespace(price=185.0, timestamp=trade_at),
            previous_daily_bar=SimpleNamespace(close=180.0),
            minute_bar=SimpleNamespace(close=184.9, timestamp=minute_at),
            daily_bar=SimpleNamespace(open=181.0, high=186.0, low=180.5,
                                      close=185.0, volume=1_250_000,
                                      timestamp=bar_at),
        ),
    })
    out = b.get_intraday_snapshots(["NVDA"])
    # `session_bar_at`, `minute_close`, `minute_bar_at` and `session_close`
    # are board item 120: without the two timestamps nothing downstream can
    # tell WHICH session the `session_*` block belongs to, and without the
    # two prices a name whose last trade is a prior session's has no today
    # print to fall back to on the same entitled venue.
    assert out == {"NVDA": {
        "last_price": 185.0, "last_trade_at": trade_at, "prev_close": 180.0,
        "session_bar_at": bar_at,
        "minute_close": 184.9, "minute_bar_at": minute_at,
        "session_open": 181.0, "session_close": 185.0, "session_high": 186.0,
        "session_low": 180.5, "session_volume": 1_250_000.0,
    }}


def test_intraday_snapshots_report_an_absent_minute_or_daily_bar_as_none():
    """A snapshot with no `minute_bar`/`daily_bar` must not invent stamps —
    the resolver reads a missing timestamp as not-today, which fails visible."""
    b = _broker()
    b._data_client = _snapshot_client({
        "NVDA": SimpleNamespace(
            symbol="NVDA",
            latest_trade=SimpleNamespace(price=185.0),
            previous_daily_bar=SimpleNamespace(close=180.0),
        ),
    })
    out = b.get_intraday_snapshots(["NVDA"])["NVDA"]
    for field in ("session_bar_at", "minute_close", "minute_bar_at",
                  "session_close", "session_open"):
        assert out[field] is None, field


def test_intraday_snapshots_carries_the_trades_own_timestamp():
    """docs/WORK.md item 15 — `broker_reads._quote_freshness` needs the
    provider's own per-trade timestamp, not a fabricated one. Alpaca's
    `Trade` model does carry `timestamp` (verified against the installed
    SDK); this pins that it survives the flatten unmodified."""
    from datetime import datetime, timezone
    trade_ts = datetime(2026, 9, 13, 14, 30, tzinfo=timezone.utc)
    b = _broker()
    b._data_client = _snapshot_client({
        "NVDA": SimpleNamespace(
            symbol="NVDA",
            latest_trade=SimpleNamespace(price=185.0, timestamp=trade_ts),
            previous_daily_bar=SimpleNamespace(close=180.0),
        ),
    })
    out = b.get_intraday_snapshots(["NVDA"])
    assert out["NVDA"]["last_trade_at"] == trade_ts


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


def test_intraday_snapshots_normalizes_class_share_for_alpaca():
    b = _broker()
    b._data_client = _snapshot_client({
        "BRK.B": SimpleNamespace(
            symbol="BRK.B", latest_trade=SimpleNamespace(price=500.0),
            previous_daily_bar=SimpleNamespace(close=495.0),
        ),
    })

    out = b.get_intraday_snapshots(["BRK-B"])

    assert out["BRK-B"]["last_price"] == 500.0
    request = b._data_client.get_stock_snapshot.call_args.args[0]
    assert request.symbol_or_symbols == ["BRK.B"]


def test_intraday_snapshots_isolates_one_rejected_symbol():
    """A malformed/unavailable ticker cannot poison every valid candidate."""
    b = _broker()
    b._data_client = MagicMock()

    def snapshots(request):
        symbols = request.symbol_or_symbols
        if "BAD" in symbols:
            raise ValueError("invalid symbol: BAD")
        return {
            symbol: SimpleNamespace(
                symbol=symbol, latest_trade=SimpleNamespace(price=100.0),
                previous_daily_bar=SimpleNamespace(close=95.0),
            )
            for symbol in symbols
        }

    b._data_client.get_stock_snapshot.side_effect = snapshots

    out = b.get_intraday_snapshots(["NVDA", "BAD", "AAPL"])

    assert out["NVDA"]["last_price"] == 100.0
    assert out["AAPL"]["prev_close"] == 95.0
    assert out["BAD"]["last_price"] is None
    assert b._data_client.get_stock_snapshot.call_count > 1


def test_intraday_snapshots_degrades_to_none_fields_for_a_missing_symbol():
    b = _broker()
    b._data_client = _snapshot_client({})  # SGOV not in the response at all
    out = b.get_intraday_snapshots(["SGOV"])
    assert out == {"SGOV": {
        "last_price": None, "last_trade_at": None, "prev_close": None,
        "session_bar_at": None, "minute_close": None, "minute_bar_at": None,
        "session_open": None, "session_close": None, "session_high": None,
        "session_low": None, "session_volume": None,
    }}


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


# ---------------------------------------------------------------------------
# get_latest_price_stamped — provenance and freshness (2026-09-17)
#
# `get_latest_price` answered "what is it worth" and threw away how it knew.
# It never looked at WHEN the trade happened, so yesterday's last print on a
# thin name, and a quote the tape never confirmed, both came back looking
# exactly like a live price — to callers that place and move real orders.
# These pin the two freshness answers apart: `is_today` (the provider stamped
# it today, trade or quote) and `is_today_print` (additionally a real trade).
# ---------------------------------------------------------------------------

def _at(day: date, hour: int = 15) -> datetime:
    """An aware ET-comparable timestamp on `day` (UTC, as Alpaca sends)."""
    return datetime(day.year, day.month, day.day, hour, 30, tzinfo=timezone.utc)


def test_stamped_price_marks_a_today_trade_as_a_today_print():
    b = _broker()
    now = datetime(2026, 9, 17, 17, 30, tzinfo=timezone.utc)
    b._data_client = _price_client(
        trade=SimpleNamespace(price=181.25, timestamp=_at(date(2026, 9, 17))),
    )
    with patch("src.trading_calendar.et_now", return_value=now):
        stamped = b.get_latest_price_stamped("NVDA")
    assert stamped.price == 181.25
    assert stamped.source == "last_trade"
    assert stamped.is_today is True
    assert stamped.is_today_print is True


def test_stamped_price_refuses_to_call_yesterdays_print_today():
    """The defect this exists for: a thin name whose last trade was a prior
    session came back indistinguishable from a live price."""
    b = _broker()
    now = datetime(2026, 9, 17, 17, 30, tzinfo=timezone.utc)
    b._data_client = _price_client(
        trade=SimpleNamespace(price=181.25, timestamp=_at(date(2026, 9, 16))),
    )
    with patch("src.trading_calendar.et_now", return_value=now):
        stamped = b.get_latest_price_stamped("NVDA")
    assert stamped.price == 181.25          # still reported, never invented
    assert stamped.is_today is False        # but not as today's price
    assert stamped.is_today_print is False


def test_stamped_price_never_calls_a_quote_a_print():
    """A quote mid is a usable fill reference while the session is live, but
    it is not evidence the tape traded there — which is what deciding where
    a stop belongs requires."""
    b = _broker()
    now = datetime(2026, 9, 17, 17, 30, tzinfo=timezone.utc)
    b._data_client = _price_client(
        trade=SimpleNamespace(price=0),
        quote=SimpleNamespace(
            ask_price=101.0, bid_price=99.0, timestamp=_at(date(2026, 9, 17)),
        ),
    )
    with patch("src.trading_calendar.et_now", return_value=now):
        stamped = b.get_latest_price_stamped("NVDA")
    assert stamped.price == 100.0
    assert stamped.source == "quote_mid"
    assert stamped.is_today is True
    assert stamped.is_today_print is False


def test_stamped_price_treats_a_missing_timestamp_as_not_today():
    """Unknown freshness fails visible rather than passing as live."""
    b = _broker()
    b._data_client = _price_client(trade=SimpleNamespace(price=181.25))
    stamped = b.get_latest_price_stamped("NVDA")
    assert stamped.is_today is False
    assert stamped.is_today_print is False


def test_stamped_price_is_none_when_nothing_is_quotable():
    b = _broker()
    b._data_client = _price_client(
        trade=SimpleNamespace(price=0),
        quote=SimpleNamespace(ask_price=0, bid_price=0),
    )
    assert b.get_latest_price_stamped("NVDA") is None


def test_bare_get_latest_price_keeps_its_old_shape():
    """Reporting callers ("how far has this moved since we sold it") keep a
    bare float and their own last-close degradation; only order-placing
    callers take the stamped reader."""
    b = _broker()
    b._data_client = _price_client(
        trade=SimpleNamespace(price=181.25, timestamp=_at(date(2026, 9, 16))),
    )
    assert b.get_latest_price("NVDA") == 181.25
