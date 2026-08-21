"""Mission Control correctness tranche (2026-08-21) — `GET /quotes`.

Same monkeypatch convention as test_api_contract.py / test_api_degradation.py:
patch `src.api.routes_live.read_live_quotes`, the name `routes_live.py`
actually imported by value from `src.api.broker_reads`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import src.api.routes_live as routes_live
from src.api.server import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_quotes_returns_seeded_snapshot(client, monkeypatch):
    monkeypatch.setattr(routes_live, "read_live_quotes", lambda symbols: {
        "quotes": {
            "AAPL": {
                "last_price": 231.5, "prev_close": 229.0,
                "session_open": 230.0, "session_high": 232.1, "session_low": 229.8,
            },
        },
        "error": None,
    })
    r = client.get("/quotes", params={"symbols": "aapl"})
    assert r.status_code == 200
    body = r.json()
    assert body["error"] is None
    assert len(body["quotes"]) == 1
    q = body["quotes"][0]
    assert q["symbol"] == "AAPL"
    assert q["last_price"] == 231.5
    assert q["prev_close"] == 229.0
    assert body["as_of"]  # a real timestamp string, not fabricated per-quote


def test_quotes_handles_multiple_symbols_and_preserves_request_order(client, monkeypatch):
    monkeypatch.setattr(routes_live, "read_live_quotes", lambda symbols: {
        "quotes": {
            "MSFT": {"last_price": 410.0, "prev_close": 408.0, "session_open": None, "session_high": None, "session_low": None},
            "AAPL": {"last_price": 231.5, "prev_close": 229.0, "session_open": None, "session_high": None, "session_low": None},
        },
        "error": None,
    })
    r = client.get("/quotes", params={"symbols": "AAPL,MSFT"})
    assert r.status_code == 200
    symbols = [q["symbol"] for q in r.json()["quotes"]]
    assert symbols == ["AAPL", "MSFT"]


def test_quotes_symbol_with_no_snapshot_data_reports_all_none_not_dropped(client, monkeypatch):
    """A symbol Alpaca couldn't price must still appear (every field None),
    never silently disappear from the response — the client asked for it."""
    monkeypatch.setattr(routes_live, "read_live_quotes", lambda symbols: {"quotes": {}, "error": None})
    r = client.get("/quotes", params={"symbols": "ZZZZ"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["quotes"]) == 1
    q = body["quotes"][0]
    assert q["symbol"] == "ZZZZ"
    assert q["last_price"] is None
    assert q["prev_close"] is None


def test_quotes_degrades_to_error_without_crashing(client, monkeypatch):
    def _boom(symbols):
        raise RuntimeError("data client unreachable")
    monkeypatch.setattr(routes_live, "read_live_quotes", _boom)
    r = client.get("/quotes", params={"symbols": "AAPL"})
    assert r.status_code == 200
    body = r.json()
    assert body["quotes"] == []
    assert "unreachable" in body["error"]


def test_quotes_rejects_empty_symbols_without_a_broker_call(client, monkeypatch):
    def _boom(symbols):
        raise AssertionError("must not call the broker with zero symbols")
    monkeypatch.setattr(routes_live, "read_live_quotes", _boom)
    r = client.get("/quotes", params={"symbols": "  ,  "})
    assert r.status_code == 200
    body = r.json()
    assert body["quotes"] == []
    assert body["error"]


def test_quotes_caps_symbol_count(client, monkeypatch):
    seen: list[list[str]] = []

    def _capture(symbols):
        seen.append(symbols)
        return {"quotes": {}, "error": None}

    monkeypatch.setattr(routes_live, "read_live_quotes", _capture)
    many = ",".join(f"SYM{i}" for i in range(40))
    r = client.get("/quotes", params={"symbols": many})
    assert r.status_code == 200
    assert len(seen[0]) == 25
    assert len(r.json()["quotes"]) == 25


def test_quotes_is_get_only(client):
    assert client.post("/quotes", params={"symbols": "AAPL"}).status_code == 405
