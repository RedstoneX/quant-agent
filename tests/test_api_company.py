"""GET /company/{symbol} — cache-only identity for the chart header.

Never fetches. A missing name is null, not a guessed title.
"""

from __future__ import annotations

from src.data.company import CompanyProfile
import src.api.routes_live as routes_live
from fastapi.testclient import TestClient

from src.api.server import app


def test_company_returns_cached_name(monkeypatch):
    monkeypatch.setattr(
        routes_live,
        "_peek_company",
        lambda symbol: CompanyProfile(symbol="AAPL", name="Apple Inc."),
    )
    r = TestClient(app, raise_server_exceptions=False).get("/company/aapl")
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "AAPL"
    assert body["name"] == "Apple Inc."
    assert body["error"] is None


def test_company_returns_null_name_when_cache_has_none(monkeypatch):
    monkeypatch.setattr(
        routes_live,
        "_peek_company",
        lambda symbol: CompanyProfile(symbol="ZZZZ"),
    )
    r = TestClient(app, raise_server_exceptions=False).get("/company/ZZZZ")
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "ZZZZ"
    assert body["name"] is None
    assert body["error"] is None


def test_company_degrades_to_error_without_crashing(monkeypatch):
    def _boom(symbol):
        raise RuntimeError("cache unreadable")

    monkeypatch.setattr(routes_live, "_peek_company", _boom)
    r = TestClient(app, raise_server_exceptions=False).get("/company/AAPL")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] is None
    assert body["error"]
