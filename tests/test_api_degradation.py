"""Mission Control's degradation paths — every route stays 200 + `error`.

`tests/test_api_contract.py` proves each route returns what it claims when
its data sources are healthy. This file proves the other half of the
contract stated in `src/api/routes_live.py`'s module docstring: no handler
may ever surface an unhandled 500, because a 500 leaks a stack trace with
internal file paths and, for the operator, turns a degraded read into an
unreadable dashboard.

That matters concretely for commissioning: the credential gateway is a new
runtime dependency, and Mission Control is the surface the operator will be
looking at when it misbehaves. A health endpoint that dies alongside the
thing it is reporting on is worse than useless.

Monkeypatch targets follow `tests/test_api_contract.py`'s documented
convention: patch at the *consuming* module's namespace (`routes_live`),
because it imported those names by value at module load.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import src.api.routes_live as routes_live
from src.api.server import app


class _Boom(Exception):
    """Distinct type so a test can tell a real failure from a stray one."""


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def stub_broker(monkeypatch):
    """Healthy broker reads — individual tests break one seam at a time."""
    monkeypatch.setattr(routes_live, "read_account", lambda: {
        "cash": 1.0, "portfolio_value": 2.0, "last_equity": 2.0, "error": None,
    })
    monkeypatch.setattr(routes_live, "read_positions", lambda: {"positions": [], "error": None})
    monkeypatch.setattr(routes_live, "read_orders",
                        lambda status="open", limit=50: {"orders": [], "error": None})
    monkeypatch.setattr(routes_live, "check_broker_reachable", lambda: True)
    monkeypatch.setattr(routes_live, "get_alpaca_paper", lambda: True)


# ---------------------------------------------------------------------------
# /health — last-run markers and the session lock
# ---------------------------------------------------------------------------

def test_health_reports_all_modes_unknown_when_the_cache_dir_is_missing(
    client, stub_broker, monkeypatch, tmp_path,
):
    """The cache dir only exists once a session has run. Before that,
    `/health` must report "nothing known" per mode rather than failing."""
    monkeypatch.setattr(routes_live, "_cache_dir", lambda: tmp_path / "does-not-exist")
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert set(body["last_run_files"]) == set(routes_live._LAST_RUN_MODES)
    assert all(v is None for v in body["last_run_files"].values())


def test_health_reports_a_present_last_run_marker(client, stub_broker, monkeypatch, tmp_path):
    """Control: the mtime path must actually work, or the None-cases below
    would pass for the wrong reason."""
    monkeypatch.setattr(routes_live, "_cache_dir", lambda: tmp_path)
    (tmp_path / "last-morning").write_text("")
    body = client.get("/health").json()
    assert body["last_run_files"]["morning"] is not None
    assert body["last_run_files"]["midday"] is None


def test_health_degrades_one_unreadable_marker_without_losing_the_others(
    client, stub_broker, monkeypatch, tmp_path,
):
    """A per-mode OSError degrades that mode to None; the rest survive."""
    monkeypatch.setattr(routes_live, "_cache_dir", lambda: tmp_path)
    (tmp_path / "last-morning").write_text("")
    (tmp_path / "last-close").write_text("")

    real_stat = routes_live.Path.stat

    def _selective_stat(self, *args, **kwargs):
        if self.name == "last-close":
            raise OSError("simulated stat failure")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(routes_live.Path, "stat", _selective_stat)
    body = client.get("/health").json()
    assert body["last_run_files"]["morning"] is not None
    assert body["last_run_files"]["close"] is None


def test_health_reports_the_cache_dir_itself_failing_as_all_unknown(
    client, stub_broker, monkeypatch,
):
    def _boom():
        raise _Boom("cache dir resolution blew up")

    monkeypatch.setattr(routes_live, "_cache_dir", _boom)
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert all(v is None for v in body["last_run_files"].values())
    # `_session_lock_active` uses the same helper, so it degrades too.
    assert body["session_lock_active"] is None


def test_health_reports_session_lock_unknown_on_oserror(client, stub_broker, monkeypatch):
    """`None` means "couldn't tell", which is distinct from `False`
    ("checked, no lock") — the operator must not read one as the other."""
    monkeypatch.setattr(routes_live.os.path, "isdir",
                        lambda p: (_ for _ in ()).throw(OSError("simulated")))
    assert client.get("/health").json()["session_lock_active"] is None


def test_health_reports_an_active_session_lock(client, stub_broker, monkeypatch, tmp_path):
    monkeypatch.setattr(routes_live, "_cache_dir", lambda: tmp_path)
    (tmp_path / "active-session.lock").mkdir()
    assert client.get("/health").json()["session_lock_active"] is True


@pytest.mark.parametrize(
    ("circuit", "decision_status", "overall"),
    [
        (
            {
                "available": True, "suspended": True,
                "suspension_class": "quota", "hold_scope": "day",
                "requires_operator_reset": False, "auto_rearm": True,
                "active_quota_holds": [{"scope": "day"}],
            },
            "paid_analysis_suspended", "degraded",
        ),
        (
            {
                "available": True, "suspended": False,
                "suspension_class": "quota", "hold_scope": "session",
                "requires_operator_reset": False, "auto_rearm": False,
                "active_quota_holds": [{"scope": "session"}],
            },
            "paid_analysis_scoped_quota_hold", "degraded",
        ),
        (
            {
                "available": True, "suspended": False,
                "suspension_class": None, "hold_scope": None,
                "requires_operator_reset": False, "auto_rearm": False,
                "active_quota_holds": [],
                "recent_recovery": {"release_reason": "ET budget window advanced"},
            },
            "ok", "ok",
        ),
    ],
)
def test_health_classifies_quota_scope_and_recovery(
    client, stub_broker, monkeypatch, circuit, decision_status, overall,
):
    import src.api.db_reads as db_reads

    monkeypatch.setattr(db_reads, "get_llm_circuit_health", lambda: circuit)
    monkeypatch.setattr(db_reads, "session_prefixes_logged_on", lambda: [])
    body = client.get("/health").json()
    assert body["decision_path_status"] == decision_status
    assert body["status"] == overall
    assert body["llm_circuit"] == circuit


# ---------------------------------------------------------------------------
# /account
# ---------------------------------------------------------------------------

def test_account_returns_200_with_error_when_the_read_raises(client, monkeypatch):
    """`read_account` is documented never to raise, but the route's own
    guard is the belt behind that brace."""
    def _boom():
        raise _Boom("gateway unreachable")

    monkeypatch.setattr(routes_live, "read_account", _boom)
    r = client.get("/account")
    assert r.status_code == 200
    assert "gateway unreachable" in r.json()["error"]


def test_account_omits_history_when_the_db_read_fails(client, stub_broker, monkeypatch):
    """Broker data and history come from independent sources; losing the
    SQLite half must not cost the operator the live half."""
    import src.api.db_reads as db_reads

    def _boom(limit=30):
        raise _Boom("db locked")

    monkeypatch.setattr(db_reads, "get_recent_daily_pnl", _boom)
    body = client.get("/account").json()
    assert body["history"] == []
    assert body["portfolio_value"] == 2.0     # the live half survived
    assert body["error"] is None


def test_account_leaves_pnl_none_when_last_equity_is_zero(client, monkeypatch):
    """Guard against a divide-by-zero on a brand-new/unfunded account."""
    monkeypatch.setattr(routes_live, "read_account", lambda: {
        "cash": 0.0, "portfolio_value": 0.0, "last_equity": 0.0, "error": None,
    })
    monkeypatch.setattr(routes_live, "get_alpaca_paper", lambda: True)
    body = client.get("/account").json()
    assert body["daily_pnl"] is None
    assert body["daily_pnl_pct"] is None


# ---------------------------------------------------------------------------
# /positions and /orders
# ---------------------------------------------------------------------------

def test_positions_returns_200_with_error_when_the_read_raises(client, monkeypatch):
    def _boom():
        raise _Boom("positions unavailable")

    monkeypatch.setattr(routes_live, "read_positions", _boom)
    r = client.get("/positions")
    assert r.status_code == 200
    assert r.json()["positions"] == []
    assert "positions unavailable" in r.json()["error"]


def test_orders_returns_200_with_error_when_the_read_raises(client, monkeypatch):
    def _boom(status="open", limit=50):
        raise _Boom("orders unavailable")

    monkeypatch.setattr(routes_live, "read_orders", _boom)
    r = client.get("/orders")
    assert r.status_code == 200
    assert r.json()["orders"] == []
    assert "orders unavailable" in r.json()["error"]


def test_orders_drops_a_row_with_no_id_or_symbol(client, monkeypatch):
    """This is the route-layer half of a behaviour that starts in
    `broker_reads`: an order object whose attribute access raises degrades
    to a row of nulls there (it is not skipped), and is then dropped HERE,
    because an order line with no id and no symbol is not something the
    operator can act on. The two layers together give "malformed orders are
    omitted" — worth pinning across the seam, since neither layer states it
    alone.
    """
    monkeypatch.setattr(routes_live, "read_orders", lambda status="open", limit=50: {
        "orders": [
            {"id": "ord-1", "symbol": "AAPL", "side": "buy", "qty": 1.0},
            {"id": None, "symbol": None, "side": None, "qty": None},
            {"id": "ord-3", "symbol": "MSFT", "side": "sell", "qty": 2.0},
        ],
        "error": None,
    })
    body = client.get("/orders").json()
    assert [o["id"] for o in body["orders"]] == ["ord-1", "ord-3"]


def test_orders_surfaces_a_read_error_without_dropping_the_rows_it_did_get(
    client, monkeypatch,
):
    monkeypatch.setattr(routes_live, "read_orders", lambda status="open", limit=50: {
        "orders": [{"id": "ord-1", "symbol": "AAPL", "side": "buy", "qty": 1.0}],
        "error": "partial broker failure",
    })
    body = client.get("/orders").json()
    assert len(body["orders"]) == 1
    assert body["error"] == "partial broker failure"


@pytest.mark.parametrize("status", ["bogus", "OPEN", ""])
def test_orders_rejects_an_unsupported_status_at_the_validation_layer(client, status):
    """FastAPI's Literal rejects these before any broker call happens —
    an unvalidated status string must never reach `read_orders`."""
    assert client.get("/orders", params={"status": status}).status_code == 422


def test_orders_rejects_an_out_of_range_limit(client):
    assert client.get("/orders", params={"limit": 0}).status_code == 422
    assert client.get("/orders", params={"limit": 501}).status_code == 422


# ---------------------------------------------------------------------------
# The invariant behind all of the above
# ---------------------------------------------------------------------------

def test_no_live_route_can_return_a_500_when_every_source_is_broken(client, monkeypatch):
    """The whole point: with every broker read raising at once — what a
    dead credential gateway looks like — Mission Control still answers."""
    def _boom(*args, **kwargs):
        raise _Boom("total outage")

    for name in ("read_account", "read_positions", "read_orders",
                 "check_broker_reachable"):
        monkeypatch.setattr(routes_live, name, _boom)

    for path in ("/health", "/account", "/positions", "/orders"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} returned {r.status_code}"


def test_health_reports_db_unreachable_when_the_sqlite_read_raises(
    client, stub_broker, monkeypatch,
):
    """`db_reachable` is a real diagnostic, not decoration: it is how the
    operator tells "the API is up but its history store is gone" from "the
    API is down". A raise inside the SQLite read must set it False and
    leave the rest of the payload intact, not take /health with it."""
    import src.api.db_reads as db_reads

    def _boom():
        raise _Boom("database is locked")

    monkeypatch.setattr(db_reads, "session_prefixes_logged_on", _boom)
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["db_reachable"] is False
    assert body["sessions_logged_today"] == []
    assert body["broker_reachable"] is True   # the broker half is unaffected


def test_orders_guard_rejects_a_status_that_bypassed_request_validation(monkeypatch):
    """Defense in depth, called directly rather than over HTTP.

    FastAPI's `Literal` rejects a bad status at the request layer, so this
    branch is unreachable through the client — which is exactly why it
    needs a direct test. If a future change ever widens the query type,
    this guard is the only thing standing between an unvalidated string and
    the broker call.
    """
    from fastapi import HTTPException

    called: list = []
    monkeypatch.setattr(routes_live, "read_orders",
                        lambda **kw: called.append(kw) or {"orders": [], "error": None})

    with pytest.raises(HTTPException) as excinfo:
        routes_live.get_orders(status="'; DROP TABLE trades; --", limit=50)

    assert excinfo.value.status_code == 400
    assert called == [], "the broker read must not run for an invalid status"
