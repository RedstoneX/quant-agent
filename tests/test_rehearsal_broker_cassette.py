from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.models import Snapshot, Trade
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest

from ops.rehearsal.broker_cassette import (
    BrokerCassette,
    CassetteMismatch,
    MissingRecordedBrokerCall,
    RecordedBrokerError,
    RecordingBrokerClient,
    ReplayBrokerCassette,
    UnusedRecordedBrokerCalls,
)
from ops.rehearsal.public_bundle import assert_public_safe
from src.execution.broker import AlpacaBroker
from src.execution.broker_parts.market_data import MarketData
from src.execution.broker_parts.stop_amend_pure import (
    _is_terminal_broker_rejection,
)
from src.execution.broker_parts.stop_rejections import (
    _is_held_for_orders_error,
    _is_unsupported_stop_market_rejection,
)
from src.execution.broker_parts.submission_rejection import (
    _is_terminal_submission_rejection,
)
from src.execution.order_idempotency import (
    _is_duplicate_client_order_id_rejection,
)


ACCOUNT_NUMBER = "PASECONDARY123456"
ACCOUNT_ID = UUID("11111111-1111-4111-8111-111111111111")
ORDER_ID = UUID("22222222-2222-4222-8222-222222222222")
MISSING_ORDER_ID = UUID("33333333-3333-4333-8333-333333333333")
ACTIVITY_ID = "20261005000000001::abcd"


@dataclass
class _OrderRequest:
    symbol: str
    client_order_id: str


class _FakeAPIError(RuntimeError):
    status_code = 404


class _TradingClient:
    def get_account(self):
        return SimpleNamespace(
            account_number=ACCOUNT_NUMBER,
            id=ACCOUNT_ID,
            cash="8000.00",
            portfolio_value="10000.00",
            last_equity="9900.00",
            non_marginable_buying_power="7500.00",
        )

    def submit_order(self, *, order_data):
        return SimpleNamespace(
            id=ORDER_ID,
            client_order_id=order_data.client_order_id,
            symbol=order_data.symbol,
            status="accepted",
        )

    def get_order_by_id(self, order_id):
        assert order_id == ORDER_ID
        return SimpleNamespace(id=ORDER_ID, symbol="SPY", status="filled")

    def get_activities(self):
        return [SimpleNamespace(id=ACTIVITY_ID, activity_type="FILL")]

    def get_missing_order(self, order_id):
        raise _FakeAPIError(f"order {order_id} does not exist")

    def get_all_positions(self):
        return [
            SimpleNamespace(
                symbol="SPY",
                qty="2",
                avg_entry_price="500",
                current_price="510",
                market_value="1020",
                unrealized_pl="20",
                unrealized_intraday_pl="5",
            )
        ]


def _record_full_cassette():
    cassette = BrokerCassette()
    client = RecordingBrokerClient(_TradingClient(), cassette, "trading")
    account = client.get_account()
    order = client.submit_order(
        order_data=_OrderRequest("SPY", "qamc-capture-client-order-raw")
    )
    client.get_order_by_id(order.id)
    client.get_activities()
    with pytest.raises(_FakeAPIError):
        client.get_missing_order(MISSING_ORDER_ID)
    client.get_all_positions()
    return cassette.to_payload(), account, order


def test_capture_tokenizes_identifiers_and_replay_is_strict_and_shape_compatible(monkeypatch):
    payload, captured_account, captured_order = _record_full_cassette()

    # Recording is observational: callers still receive the real SDK objects.
    assert captured_account.account_number == ACCOUNT_NUMBER
    assert captured_order.id == ORDER_ID

    published = json.dumps(payload, sort_keys=True)
    for raw in (
        ACCOUNT_NUMBER,
        str(ACCOUNT_ID),
        str(ORDER_ID),
        str(MISSING_ORDER_ID),
        ACTIVITY_ID,
        "qamc-capture-client-order-raw",
    ):
        assert raw not in published
    assert "<QAMC:account:0001>" in published
    assert "<QAMC:order_id:0001>" in published
    assert "<QAMC:activity_id:0001>" in published
    assert "<QAMC:client_order_id:0001>" in published
    assert_public_safe(
        payload,
        secrets=("secondary-api-key", "secondary-secret-key"),
        account_ids=(ACCOUNT_NUMBER, str(ACCOUNT_ID)),
    )

    # Public cassettes are loaded from JSON, not passed as Python objects.
    replay = ReplayBrokerCassette(json.loads(json.dumps(payload)))
    client = replay.client("trading")
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker.client = client
    broker._shortable_cache = {}
    broker._fractionable_cache = {}
    broker._trading_day_cache = {}
    broker._session_open_cache = {}
    monkeypatch.setattr(
        "src.execution.broker._sector_reference._get_sector",
        lambda _symbol: "ETF",
    )
    account = broker.get_account()
    order = client.submit_order(
        order_data=_OrderRequest("SPY", "different-generated-client-order")
    )
    fetched = client.get_order_by_id(order.id)
    activities = client.get_activities()

    assert account["cash"] == 8000.0
    assert account["non_marginable_buying_power"] == 7500.0
    assert order.id == "<QAMC:order_id:0001>"
    assert order.client_order_id == "<QAMC:client_order_id:0001>"
    assert fetched.id == order.id
    assert activities[0].id == "<QAMC:activity_id:0001>"
    with pytest.raises(RecordedBrokerError) as error:
        client.get_missing_order(UUID("44444444-4444-4444-8444-444444444444"))
    assert error.value.status_code == 404
    assert str(MISSING_ORDER_ID) not in str(error.value)

    # The revived SimpleNamespace objects work through the actual AlpacaBroker
    # account and position seams; no SDK-only fake model path is introduced.
    positions = broker.get_positions()
    assert positions[0].symbol == "SPY"
    assert positions[0].unrealized_pnl == 20.0
    replay.assert_consumed()


def test_replay_rejects_out_of_order_extra_and_unused_calls():
    payload, _, _ = _record_full_cassette()

    out_of_order = ReplayBrokerCassette(payload)
    with pytest.raises(CassetteMismatch):
        out_of_order.client("trading").get_activities()

    unused = ReplayBrokerCassette(payload)
    with pytest.raises(UnusedRecordedBrokerCalls):
        unused.assert_consumed()

    # A one-entry cassette proves that any call after the recording is exhausted
    # fails rather than silently reaching a network client or returning None.
    one_call = BrokerCassette()
    RecordingBrokerClient(_TradingClient(), one_call, "trading").get_account()
    exhausted = ReplayBrokerCassette(one_call.to_payload())
    exhausted.client("trading").get_account()
    with pytest.raises(MissingRecordedBrokerCall):
        exhausted.client("trading").get_account()


def test_two_sdk_clients_share_one_global_order():
    request = StockBarsRequest(
        symbol_or_symbols=["SPY"],
        timeframe=TimeFrame.Day,
        start=datetime(2026, 10, 1, tzinfo=timezone.utc),
        end=datetime(2026, 10, 2, tzinfo=timezone.utc),
    )
    cassette = BrokerCassette()
    trading = RecordingBrokerClient(_TradingClient(), cassette, "trading")
    market_data = RecordingBrokerClient(
        SimpleNamespace(get_stock_bars=lambda request: {"SPY": [request]}),
        cassette,
        "stock_historical_data",
    )
    trading.get_account()
    market_data.get_stock_bars(request)

    replay = ReplayBrokerCassette(json.loads(json.dumps(cassette.to_payload())))
    with pytest.raises(CassetteMismatch):
        replay.client("stock_historical_data").get_stock_bars(request)
    replay.client("trading").get_account()
    replay.client("stock_historical_data").get_stock_bars(request)
    replay.assert_consumed()


def test_real_alpaca_request_model_has_stable_json_call_shape():
    request = LimitOrderRequest(
        symbol="SPY",
        qty=1,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=500,
        client_order_id="capture-generated-id",
    )
    cassette = BrokerCassette()
    recorder = RecordingBrokerClient(
        SimpleNamespace(submit_order=lambda *, order_data: SimpleNamespace(
            id=ORDER_ID,
            client_order_id=order_data.client_order_id,
        )),
        cassette,
        "trading",
    )
    recorder.submit_order(order_data=request)

    replay = ReplayBrokerCassette(json.loads(json.dumps(cassette.to_payload())))
    replay.client("trading").submit_order(
        order_data=LimitOrderRequest(
            symbol="SPY",
            qty=1,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=500,
            client_order_id="replay-generated-id",
        )
    )
    replay.assert_consumed()


def test_response_serialization_failure_never_turns_success_into_a_retry_signal():
    class UnsupportedResponse:
        __slots__ = ()

    answer = UnsupportedResponse()
    cassette = BrokerCassette()
    recorder = RecordingBrokerClient(
        SimpleNamespace(submit_order=lambda: answer), cassette, "trading"
    )

    assert recorder.submit_order() is answer
    with pytest.raises(RuntimeError, match="cannot export cassette"):
        cassette.to_payload()


def _round_tripped_error(message: str, status_code: int, code: int):
    class APIErrorStandIn(RuntimeError):
        def __init__(self):
            super().__init__(message)
            self.status_code = status_code
            self.code = code

    class FailingClient:
        def submit_order(self):
            raise APIErrorStandIn()

    cassette = BrokerCassette()
    recorder = RecordingBrokerClient(FailingClient(), cassette, "trading")
    with pytest.raises(APIErrorStandIn):
        recorder.submit_order()
    replay = ReplayBrokerCassette(json.loads(json.dumps(cassette.to_payload())))
    with pytest.raises(RecordedBrokerError) as error:
        replay.client("trading").submit_order()
    replay.assert_consumed()
    return error.value


def test_replayed_errors_preserve_every_current_rest_classifier_surface():
    duplicate = _round_tripped_error(
        "client_order_id must be unique", 422, 42210000
    )
    assert str(duplicate) == "client_order_id must be unique"
    assert duplicate.message == str(duplicate)
    assert duplicate.status_code == 422
    assert duplicate.status == 422
    assert duplicate.code == 42210000
    assert duplicate.error_type.endswith("APIErrorStandIn")
    assert _is_duplicate_client_order_id_rejection(duplicate)
    assert _is_terminal_submission_rejection(duplicate)

    held = _round_tripped_error("insufficient qty held_for_orders", 403, 40310000)
    assert _is_held_for_orders_error(held)
    assert not _is_unsupported_stop_market_rejection(held)

    unsupported = _round_tripped_error(
        "order type is not supported for this time_in_force", 422, 42210001
    )
    assert _is_unsupported_stop_market_rejection(unsupported)

    missing = _round_tripped_error("invalid symbol", 404, 40410000)
    assert _is_terminal_broker_rejection(missing)
    assert "invalid symbol" in str(missing).lower()


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
    answer = replay.client("stock_historical_data").get_stock_latest_trade(
        {"symbol_or_symbols": ["SPY"]}
    )
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
    assert actual["last_trade_at"] == datetime(
        2026, 10, 5, 14, 30, tzinfo=timezone.utc
    )
    assert actual["minute_close"] == 670.22
    assert actual["minute_bar_at"] == datetime(
        2026, 10, 5, 14, 30, tzinfo=timezone.utc
    )
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
    answers = [
        entry["answer"]["fields"]["id"] for entry in payload["entries"]
    ]

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
            {
                "code": 42210000,
                "message": "client_order_id must be unique",
            },
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
