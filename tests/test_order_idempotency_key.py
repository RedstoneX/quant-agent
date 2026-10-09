"""The idempotency KEY is a pure function of the order intent, and the
duplicate-key reply is recognised only by code AND text.

Pure tests for `src/execution/order_idempotency.py`; the stub-broker
integration tests (timeout-then-retry, dead read-backs, superseding keys)
live in tests/test_idempotent_client_order_id.py. No real account id, no
live desk output.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from alpaca.common.exceptions import APIError

from src.execution.order_idempotency import (
    _CLIENT_ORDER_ID_MAX_LEN,
    _client_order_id,
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
    assert _client_order_id(purpose="ENT", **{**_FIELDS, "side": "sell"}) != _client_order_id(
        purpose="ENT", **{**_FIELDS, "side": "sell_short"}
    )


def test_key_contains_no_time_or_randomness():
    with (
        patch("src.execution.order_idempotency.time", create=True) as t,
        patch("src.execution.order_idempotency.random", create=True) as r,
    ):
        a = _client_order_id(purpose="ENT", **_FIELDS)
        b = _client_order_id(purpose="ENT", **_FIELDS)
    assert a == b
    assert not t.method_calls and not r.method_calls


def test_key_respects_cited_length_limit_and_hashes_deterministically():
    long_fields = dict(symbol="ABCDEFGHIJ", side="sell_short", session_date=DAY, qty=1234.56789, price=98765.4321)
    natural = f"ENT-ABCDEFGHIJ-sellshort-{DAY}-1234.56789-98765.4321"
    assert len(natural) > _CLIENT_ORDER_ID_MAX_LEN  # so the hash path is exercised
    k1 = _client_order_id(purpose="ENT", **long_fields)
    k2 = _client_order_id(purpose="ENT", **long_fields)
    assert k1 == k2
    assert len(k1) == _CLIENT_ORDER_ID_MAX_LEN
    assert k1.startswith(f"ENT-ABCDEFGHIJ-{DAY}-")  # readable prefix kept
    assert k1 != _client_order_id(purpose="ENT", **{**long_fields, "qty": 1234.5679})
    short = _client_order_id(purpose="ENT", **_FIELDS)
    assert len(short) <= _CLIENT_ORDER_ID_MAX_LEN
    import re

    assert re.fullmatch(r"[A-Za-z0-9.\-]+", k1) and re.fullmatch(r"[A-Za-z0-9.\-]+", short)


def _duplicate_error() -> APIError:
    resp = SimpleNamespace(status_code=422)
    http_err = SimpleNamespace(response=resp, request=None)
    body = json.dumps({"code": 40010001, "message": "client_order_id must be unique"})
    return APIError(body, http_err)


def test_duplicate_classifier_requires_both_code_and_text():
    assert _is_duplicate_client_order_id_rejection(_duplicate_error())
    other_422 = APIError(
        json.dumps({"code": 1, "message": "qty must be > 0"}),
        SimpleNamespace(response=SimpleNamespace(status_code=422), request=None),
    )
    assert not _is_duplicate_client_order_id_rejection(other_422)
    assert not _is_duplicate_client_order_id_rejection(TimeoutError("client_order_id must be unique"))
