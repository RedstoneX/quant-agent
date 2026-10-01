"""Every order submission carries a DETERMINISTIC `client_order_id`.

Alpaca uses `client_order_id` as the idempotency key for POST /v2/orders:
a second POST for an id an active order already holds is refused with
HTTP 422 "client_order_id must be unique"
(https://alpaca.markets/learn/how-to-fix-common-trading-api-errors-at-alpaca).
These tests prove (a) the key is a pure function of the intent, (b) a
timeout-then-retry leaves ONE order at a stub broker, not two, and (c) no
other request field changed. No real account id, no live desk output.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.requests import (
    LimitOrderRequest, MarketOrderRequest, StopLimitOrderRequest,
    StopOrderRequest,
)

from src.execution import broker as broker_mod
from src.execution.broker import (
    AlpacaBroker, _CLIENT_ORDER_ID_MAX_LEN, _client_order_id,
    _is_duplicate_client_order_id_rejection,
)

DAY = "2026-10-01"
_FIELDS = dict(symbol="AAPL", side="buy", session_date=DAY, qty=10.0, price=100.0)


# ---------------------------------------------------------------- (a) key
def test_same_intent_same_key_and_each_changed_fact_changes_it():
    base = _client_order_id(purpose="ENT", **_FIELDS)
    assert base == _client_order_id(purpose="ENT", **_FIELDS)
    assert base != _client_order_id(purpose="ENT", **{**_FIELDS, "side": "sell"})
    assert base != _client_order_id(purpose="ENT", **{**_FIELDS, "session_date": "2026-10-02"})
    assert base != _client_order_id(purpose="ENT", **{**_FIELDS, "qty": 11.0})
    assert base != _client_order_id(purpose="ENT", **{**_FIELDS, "price": 101.0})
    assert base != _client_order_id(purpose="ENT", **{**_FIELDS, "price": None})
    assert base != _client_order_id(purpose="STP", **_FIELDS)
    assert base != _client_order_id(purpose="ENT", **{**_FIELDS, "symbol": "MSFT"})
    # 'sell' (reduce a long) and 'sell_short' (open a short) are different intents.
    assert (_client_order_id(purpose="ENT", **{**_FIELDS, "side": "sell"})
            != _client_order_id(purpose="ENT", **{**_FIELDS, "side": "sell_short"}))


def test_key_contains_no_time_or_randomness():
    with patch("src.execution.broker.time") as t, patch("src.execution.broker.random") as r:
        a = _client_order_id(purpose="ENT", **_FIELDS)
        b = _client_order_id(purpose="ENT", **_FIELDS)
    assert a == b
    assert not t.method_calls and not r.method_calls


def test_key_respects_cited_length_limit_and_hashes_deterministically():
    long_fields = dict(symbol="ABCDEFGHIJ", side="sell_short", session_date=DAY,
                       qty=1234.56789, price=98765.4321)
    natural = f"ENT-ABCDEFGHIJ-sellshort-{DAY}-1234.56789-98765.4321"
    assert len(natural) > _CLIENT_ORDER_ID_MAX_LEN  # so the hash path is exercised
    k1 = _client_order_id(purpose="ENT", **long_fields)
    k2 = _client_order_id(purpose="ENT", **long_fields)
    assert k1 == k2
    assert len(k1) == _CLIENT_ORDER_ID_MAX_LEN
    assert k1.startswith(f"ENT-ABCDEFGHIJ-{DAY}-")      # readable prefix kept
    assert k1 != _client_order_id(purpose="ENT", **{**long_fields, "qty": 1234.5679})
    short = _client_order_id(purpose="ENT", **_FIELDS)
    assert len(short) <= _CLIENT_ORDER_ID_MAX_LEN
    import re
    assert re.fullmatch(r"[A-Za-z0-9.\-]+", k1) and re.fullmatch(r"[A-Za-z0-9.\-]+", short)


# ----------------------------------------------------------- (b) retry
def _duplicate_error() -> APIError:
    resp = SimpleNamespace(status_code=422)
    http_err = SimpleNamespace(response=resp, request=None)
    body = json.dumps({"code": 40010001, "message": "client_order_id must be unique"})
    return APIError(body, http_err)


class _StubBroker:
    """Alpaca stand-in: stores orders by client_order_id and refuses a
    duplicate with the real 422 text. `timeout_next` makes the next POST
    ACCEPT the order and then raise, as a dropped connection would."""

    def __init__(self):
        self.orders: dict[str, SimpleNamespace] = {}
        self.timeout_next = False
        self.posts = 0

    def submit_order(self, req):
        self.posts += 1
        if req.client_order_id in self.orders:
            raise _duplicate_error()
        order = SimpleNamespace(
            id=f"srv-{len(self.orders) + 1}", status="accepted",
            symbol=req.symbol, client_order_id=req.client_order_id,
        )
        self.orders[req.client_order_id] = order
        if self.timeout_next:
            self.timeout_next = False
            raise TimeoutError("read timed out after broker accepted")
        return order

    def get_order_by_client_id(self, cid):
        return self.orders[cid]


def _broker_with(stub):
    with patch("src.execution.broker.TradingClient", return_value=stub):
        b = AlpacaBroker(api_key="test", secret_key="test", paper=True)
    b.client = stub
    return b


@patch("src.execution.broker._session_date_key", return_value=DAY)
def test_entry_timeout_then_retry_leaves_one_order(_d):
    stub = _StubBroker()
    b = _broker_with(stub)
    stub.timeout_next = True
    with pytest.raises(TimeoutError):            # ambiguous failure propagates (unchanged)
        b.submit_order(symbol="AAPL", qty=10, side="buy", limit_price=100.0)
    assert len(stub.orders) == 1
    result = b.submit_order(symbol="AAPL", qty=10, side="buy", limit_price=100.0)
    assert stub.posts == 2
    assert len(stub.orders) == 1, "retry must NOT create a second position"
    assert result["id"] == "srv-1" and result["status"] == "accepted"
    assert result["qty"] == 10 and result["limit_price"] == 100.0
    # A genuinely new intent the same day is still accepted.
    b.submit_order(symbol="AAPL", qty=10, side="buy", limit_price=101.0)
    assert len(stub.orders) == 2


@patch("src.execution.broker._session_date_key", return_value=DAY)
def test_entry_duplicate_is_not_reported_as_broker_rejection(_d):
    stub = _StubBroker()
    b = _broker_with(stub)
    b.submit_order(symbol="AAPL", qty=5, side="buy")
    again = b.submit_order(symbol="AAPL", qty=5, side="buy")
    assert again["status"] == "accepted" and again["id"] == "srv-1"
    assert again["status"] != "rejected_by_broker"


@patch("src.execution.broker._session_date_key", return_value=DAY)
def test_stop_timeout_then_retry_leaves_one_stop(_d):
    stub = _StubBroker()
    b = _broker_with(stub)
    stub.timeout_next = True
    with pytest.raises(TimeoutError):
        b._submit_stop_limit_order("AAPL", 10, 95.0)
    res = b._submit_stop_limit_order("AAPL", 10, 95.0)
    assert len(stub.orders) == 1 and res["id"] == "srv-1"
    # A ratcheted trail (new trigger) is a NEW intent.
    b._submit_stop_limit_order("AAPL", 10, 96.0)
    assert len(stub.orders) == 2


def test_duplicate_classifier_requires_both_code_and_text():
    assert _is_duplicate_client_order_id_rejection(_duplicate_error())
    other_422 = APIError(json.dumps({"code": 1, "message": "qty must be > 0"}),
                         SimpleNamespace(response=SimpleNamespace(status_code=422), request=None))
    assert not _is_duplicate_client_order_id_rejection(other_422)
    assert not _is_duplicate_client_order_id_rejection(TimeoutError("client_order_id must be unique"))


@patch("src.execution.broker._session_date_key", return_value=DAY)
def test_duplicate_whose_lookup_fails_raises_not_rejects(_d):
    stub = _StubBroker()
    b = _broker_with(stub)
    b.submit_order(symbol="AAPL", qty=5, side="buy")
    stub.get_order_by_client_id = MagicMock(side_effect=ConnectionError("down"))
    with pytest.raises(RuntimeError, match="treat as PLACED"):
        b.submit_order(symbol="AAPL", qty=5, side="buy")


# ------------------------------------------------- (c) fields unchanged
@patch("src.execution.broker._session_date_key", return_value=DAY)
def test_every_other_request_field_is_unchanged(_d):
    client = MagicMock()
    client.submit_order.return_value = SimpleNamespace(id="o", status="accepted", symbol="BRK.B")
    with patch("src.execution.broker.TradingClient", return_value=client):
        b = AlpacaBroker(api_key="test", secret_key="test", paper=True)
    b.submit_order(symbol="BRK-B", qty=2, side="buy", limit_price=500.0)
    b.submit_order(symbol="AAPL", qty=3, side="sell")
    b._submit_stop_limit_order("AAPL", 3, 95.0)
    b._submit_stop_limit_order("MSFT", 2.5, 95.0, side="buy")  # fractional -> DAY
    reqs = [c.args[0] for c in client.submit_order.call_args_list]
    assert [type(r) for r in reqs] == [LimitOrderRequest, MarketOrderRequest,
                                       StopOrderRequest, StopOrderRequest]
    expected = [
        {"symbol": "BRK.B", "qty": 2.0, "side": "buy", "type": "limit",
         "time_in_force": "day", "limit_price": 500.0},
        {"symbol": "AAPL", "qty": 3.0, "side": "sell", "type": "market", "time_in_force": "day"},
        {"symbol": "AAPL", "qty": 3.0, "side": "sell", "type": "stop", "time_in_force": "gtc",
         "stop_price": 95.0},
        {"symbol": "MSFT", "qty": 2.5, "side": "buy", "type": "stop", "time_in_force": "day",
         "stop_price": 95.0},
    ]
    for r, exp in zip(reqs, expected):
        dumped = r.model_dump(exclude_none=True, mode="json")
        cid = dumped.pop("client_order_id")
        assert dumped == exp, dumped          # byte-for-byte the pre-change request
        assert 0 < len(cid) <= _CLIENT_ORDER_ID_MAX_LEN
    assert reqs[0].client_order_id == f"ENT-BRK.B-buy-{DAY}-2.0-500.0"
    assert reqs[2].client_order_id == f"STP-AAPL-sell-{DAY}-3.0-95.0"


@patch("src.execution.broker._session_date_key", return_value=DAY)
def test_stop_limit_fallback_also_carries_key(_d):
    client = MagicMock()
    unsupported = APIError(json.dumps({"code": 1, "message": "order type stop is not supported for gtc"}),
                           SimpleNamespace(response=SimpleNamespace(status_code=422), request=None))
    client.submit_order.side_effect = [
        unsupported, SimpleNamespace(id="o2", status="accepted", symbol="AAPL"),
    ]
    with patch("src.execution.broker.TradingClient", return_value=client), \
         patch.object(broker_mod, "_is_unsupported_stop_market_rejection", return_value=True):
        b = AlpacaBroker(api_key="test", secret_key="test", paper=True)
        b._submit_stop_limit_order("AAPL", 3, 95.0)
    limit_req = client.submit_order.call_args_list[1].args[0]
    assert isinstance(limit_req, StopLimitOrderRequest)
    assert limit_req.client_order_id == f"STL-AAPL-sell-{DAY}-3.0-95.0"
