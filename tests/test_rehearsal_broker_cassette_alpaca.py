"""Real Alpaca-model and identifier fidelity proofs for broker cassettes."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.models import Snapshot, Trade
from alpaca.data.requests import StockSnapshotRequest

from ops.rehearsal.broker_cassette import (
    BrokerCassette,
    RecordedBrokerError,
    RecordingBrokerClient,
    ReplayBrokerCassette,
)
from ops.rehearsal.public_bundle import assert_public_safe
from src.execution.broker_parts.market_data import MarketData
from src.execution.order_idempotency import (
    _is_duplicate_client_order_id_rejection,
)


def test_real_alpaca_trade_numeric_print_id_is_tokenized_and_public_safe():
    trade = Trade(
        "SPY",
        {
            "t": "2026-10-05T14:30:00Z",
            "x": "V",
            "p": 670.25,
            "s": 10,
            "i": 987654321012345,
            "c": ["@"],
            "z": "C",
        },
    )
    cassette = BrokerCassette()
    recorder = RecordingBrokerClient(
        SimpleNamespace(get_stock_latest_trade=lambda _request: {"SPY": trade}),
        cassette,
        "stock_historical_data",
    )
    recorder.get_stock_latest_trade({"symbol_or_symbols": ["SPY"]})
    payload = cassette.to_payload()

    assert "987654321012345" not in json.dumps(payload)
    assert_public_safe(payload)
    replay = ReplayBrokerCassette(json.loads(json.dumps(payload)))
    answer = replay.client("stock_historical_data").get_stock_latest_trade({"symbol_or_symbols": ["SPY"]})
    assert answer["SPY"].id == "<QAMC:broker_id:0001>"
    replay.assert_consumed()


def test_real_alpaca_snapshot_revives_nested_models_for_the_real_consumer():
    snapshot = Snapshot(
        "SPY",
        {
            "latestTrade": {
                "t": "2026-10-05T14:30:00Z",
                "x": "V",
                "p": 670.25,
                "s": 10,
                "i": 987654321012345,
                "c": ["@"],
                "z": "C",
            },
            "latestQuote": {
                "t": "2026-10-05T14:30:01Z",
                "ax": "V",
                "ap": 670.30,
                "as": 4,
                "bx": "V",
                "bp": 670.20,
                "bs": 5,
                "c": ["R"],
                "z": "C",
            },
            "minuteBar": {
                "t": "2026-10-05T14:30:00Z",
                "o": 670.00,
                "h": 670.40,
                "l": 669.90,
                "c": 670.22,
                "v": 1200,
                "n": 80,
                "vw": 670.18,
            },
            "dailyBar": {
                "t": "2026-10-05T13:30:00Z",
                "o": 668.00,
                "h": 671.00,
                "l": 667.50,
                "c": 670.10,
                "v": 2000000,
                "n": 12000,
                "vw": 669.50,
            },
            "prevDailyBar": {
                "t": "2026-10-02T13:30:00Z",
                "o": 665.00,
                "h": 669.00,
                "l": 664.00,
                "c": 667.00,
                "v": 1800000,
                "n": 11000,
                "vw": 666.80,
            },
        },
    )
    request = StockSnapshotRequest(symbol_or_symbols=["SPY"])
    cassette = BrokerCassette()
    recorder = RecordingBrokerClient(
        SimpleNamespace(get_stock_snapshot=lambda _request: {"SPY": snapshot}),
        cassette,
        "stock_historical_data",
    )
    # One call verifies the revived SDK response shape; the second feeds the
    # production snapshot consumer without bypassing its request construction.
    recorder.get_stock_snapshot(request)
    recorder.get_stock_snapshot(request)
    payload = cassette.to_payload()

    assert "987654321012345" not in json.dumps(payload)
    assert_public_safe(payload)
    replay = ReplayBrokerCassette(json.loads(json.dumps(payload)))
    data_client = replay.client("stock_historical_data")
    revived = data_client.get_stock_snapshot(request)["SPY"]
    assert revived.latest_trade.price == 670.25
    assert revived.latest_trade.id == "<QAMC:broker_id:0001>"
    assert revived.latest_quote.bid_price == 670.20
    assert revived.latest_quote.ask_price == 670.30
    assert revived.minute_bar.close == 670.22
    assert revived.daily_bar.volume == 2000000.0
    assert revived.previous_daily_bar.close == 667.0

    state = SimpleNamespace(_data_client=data_client, _screener_client=None)
    market_data = MarketData(
        state=state,
        api_key="unused",
        secret_key="unused",
        closed_bars_cache={},
        closed_bars_cache_lock=threading.Lock(),
    )
    actual = market_data.get_intraday_snapshots(["SPY"])["SPY"]
    assert actual["last_price"] == 670.25
    assert actual["last_trade_at"] == datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc)
    assert actual["minute_close"] == 670.22
    assert actual["minute_bar_at"] == datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc)
    assert actual["session_open"] == 668.0
    assert actual["session_close"] == 670.10
    assert actual["prev_close"] == 667.0
    replay.assert_consumed()


def test_identifier_tokens_distinguish_category_and_raw_type_but_repeat_stably():
    class Identifiers:
        def order_int(self):
            return SimpleNamespace(id=7)

        def order_string(self):
            return SimpleNamespace(id="7")

        def order_int_again(self):
            return SimpleNamespace(id=7)

        def asset_int(self):
            return SimpleNamespace(id=7)

    cassette = BrokerCassette()
    recorder = RecordingBrokerClient(Identifiers(), cassette, "trading")
    recorder.order_int()
    recorder.order_string()
    recorder.order_int_again()
    recorder.asset_int()
    payload = cassette.to_payload()
    answers = [entry["answer"]["fields"]["id"] for entry in payload["entries"]]

    assert answers == [
        "<QAMC:order_id:0001>",
        "<QAMC:order_id:0002>",
        "<QAMC:order_id:0001>",
        "<QAMC:asset_id:0001>",
    ]
    assert_public_safe(payload)

    replay = ReplayBrokerCassette(json.loads(json.dumps(payload)))
    client = replay.client("trading")
    assert client.order_int().id == answers[0]
    assert client.order_string().id == answers[1]
    assert client.order_int_again().id == answers[2]
    assert client.asset_int().id == answers[3]
    replay.assert_consumed()


def test_real_alpaca_api_error_preserves_display_text_and_parsed_message():
    original = APIError(
        json.dumps(
            {"code": 42210000, "message": "client_order_id must be unique"},
            separators=(",", ":"),
        ),
        SimpleNamespace(
            response=SimpleNamespace(status_code=422),
            request=None,
        ),
    )

    class FailingClient:
        def submit_order(self):
            raise original

    cassette = BrokerCassette()
    recorder = RecordingBrokerClient(FailingClient(), cassette, "trading")
    with pytest.raises(APIError):
        recorder.submit_order()
    payload = cassette.to_payload()
    recorded_error = payload["entries"][0]["error"]
    assert recorded_error["text"] == str(original)
    assert recorded_error["message"] == original.message
    assert_public_safe(payload)
    replay = ReplayBrokerCassette(json.loads(json.dumps(payload)))
    with pytest.raises(RecordedBrokerError) as error:
        replay.client("trading").submit_order()

    assert str(error.value) == str(original)
    assert error.value.message == original.message
    assert error.value.code == original.code
    assert error.value.status_code == original.status_code
    assert error.value.error_type == "alpaca.common.exceptions.APIError"
    assert _is_duplicate_client_order_id_rejection(error.value)
    replay.assert_consumed()
