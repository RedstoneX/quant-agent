"""Stage 2 Mission Control API — functional contract tests.

Exercises every route through a real FastAPI TestClient against seeded
data, proving the endpoint contract actually returns what it claims to
(not just "doesn't crash"). Two data sources are stubbed independently,
matching the architectural split the routes themselves make:

- SQLite history routes (`/trades`, `/runs`, `/runs/{id}`,
  `/decisions/{id}`, `/agents/{name}`, `/reflections`) read through a real
  temp SQLite DB seeded via `src.storage.db.Database` (the trading
  process's own writer — perfectly fine to use for building test fixtures,
  the constraint is only that `src/api/*.py` itself never writes).
- Broker-live routes (`/account`, `/positions`, `/orders`) have their
  `src.api.broker_reads` calls monkeypatched directly, since exercising a
  real Alpaca connection isn't appropriate for a unit test.

IMPORTANT monkeypatch-target note: every module under `src/api/` does
`from src.api.deps import X` / `from src.api.broker_reads import Y` at
module load time, binding a name into ITS OWN namespace — patching the
*origin* module's attribute (e.g. `src.api.deps.get_db_path`) has no
effect on a module that already imported that name by value
(`src.api.db_reads.get_db_path`). Tests below patch at the *consuming*
module, matching each module's actual import statement. The two
exceptions are `deps.agent_roster()`/`db_reads.*` accessed via
`routes_history.py`'s `from src.api import db_reads, deps` (a *module*
import, not a name import) — those really can be patched at the origin
module, since `routes_history.py` looks the attribute up dynamically
through the module object at call time.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import src.api.db_reads as db_reads
import src.api.deps as deps
import src.api.routes_live as routes_live
from src.api.server import app
from src.storage.db import Database

RUN_ID = "run-abc12345"
DECISION_ID = f"{RUN_ID}-dec-000001"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """A real trading DB with one full research->PM->RM->trade chain, plus
    a daily_pnl row and an insights row, written through the same
    `Database` class the trading process uses. `src.api.db_reads`'s own
    `get_db_path` is monkeypatched to point at it (patched at the
    *consuming* module — see module docstring)."""
    db_path = tmp_path / "quant_agent_test.db"
    db = Database(str(db_path))
    db.initialize()

    db.insert_agent_log(
        agent_name="tech_analyst", run_id=RUN_ID,
        input_summary="tech input", output_summary="tech output",
        full_response="{}", model="claude-opus-4-7", tokens_used=200,
        input_message="tech prompt", cost_usd=0.01,
    )
    db.insert_agent_log(
        agent_name="portfolio_manager", run_id=RUN_ID,
        input_summary="pm input", output_summary="pm output",
        full_response='{"targets": []}', model="gpt-5.5", tokens_used=500,
        input_message="pm prompt", cost_usd=0.05, decision_id=DECISION_ID,
        requested_provider="openai", requested_model="gpt-5.5",
        actual_provider="openai",
    )
    db.insert_agent_log(
        agent_name="risk_manager", run_id=RUN_ID,
        input_summary="rm input", output_summary="Approved: True",
        full_response='{"approved": true}', model="gpt-5.5", tokens_used=300,
        input_message="rm prompt", cost_usd=0.03, decision_id=DECISION_ID,
    )
    db.insert_trade(
        symbol="AAPL", action="BUY", qty=10, price=150.0,
        reasoning="test buy", run_id=RUN_ID, stop_loss=140.0, take_profit=170.0,
        broker_order_id="ord-1", fill_status="filled", decision_id=DECISION_ID,
    )
    db.insert_daily_pnl(date="2026-08-08", total_value=100_000.0, daily_pnl=500.0, daily_return_pct=0.5)
    db.save_insights(
        date="2026-08-08", tomorrow_outlook="cautiously bullish", lessons="none yet",
        suggested_actions="hold", risk_rating="low",
    )
    db.close()

    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(db_path))
    return db_path


@pytest.fixture
def stub_roster(monkeypatch):
    """Bypasses full AppConfig loading for /agents tests — patches the
    accessor functions `deps.agent_roster()` itself calls (intra-module
    references, so patching them on the `deps` module object works)."""
    monkeypatch.setattr(deps, "get_agent_model", lambda name: f"{name}-test-model")
    monkeypatch.setattr(deps, "get_agent_provider", lambda name: None)


@pytest.fixture
def stub_broker(monkeypatch):
    """Patches the broker-read functions at routes_live's own namespace
    (it imported them by name), returning deterministic fixtures instead
    of hitting a real Alpaca connection."""
    monkeypatch.setattr(routes_live, "read_account", lambda: {
        "cash": 5000.0, "portfolio_value": 105_000.0, "last_equity": 100_000.0, "error": None,
    })
    monkeypatch.setattr(routes_live, "read_positions", lambda: {
        "positions": [{
            "symbol": "AAPL", "qty": 10, "avg_entry": 150.0, "current_price": 155.0,
            "market_value": 1550.0, "unrealized_pnl": 50.0,
            "unrealized_intraday_pnl": 5.0, "sector": "Technology",
        }],
        "error": None,
    })
    monkeypatch.setattr(routes_live, "read_orders", lambda status="open", limit=50: {
        "orders": [{
            "id": "ord-1", "symbol": "AAPL", "side": "buy", "qty": 10.0,
            "order_type": "limit", "status": "open", "limit_price": 150.0,
            "stop_price": None, "filled_qty": 0.0, "filled_avg_price": None,
            "submitted_at": "2026-08-08T14:30:00Z", "filled_at": None,
        }],
        "error": None,
    })
    monkeypatch.setattr(routes_live, "check_broker_reachable", lambda: True)
    monkeypatch.setattr(routes_live, "get_alpaca_paper", lambda: True)


# ---------------------------------------------------------------------------
# /trades
# ---------------------------------------------------------------------------

def test_trades_returns_seeded_row(client, seeded_db):
    r = client.get("/trades")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["trades"][0]["symbol"] == "AAPL"
    assert body["trades"][0]["decision_id"] == DECISION_ID
    assert body["trades"][0]["run_id"] == RUN_ID


def test_trades_filters_by_symbol(client, seeded_db):
    assert client.get("/trades", params={"symbol": "AAPL"}).json()["count"] == 1
    assert client.get("/trades", params={"symbol": "TSLA"}).json()["count"] == 0


def test_trades_filters_by_run_id_and_decision_id(client, seeded_db):
    assert client.get("/trades", params={"run_id": RUN_ID}).json()["count"] == 1
    assert client.get("/trades", params={"run_id": "run-doesnotexist"}).json()["count"] == 0
    assert client.get("/trades", params={"decision_id": DECISION_ID}).json()["count"] == 1


# ---------------------------------------------------------------------------
# /runs, /runs/{run_id}
# ---------------------------------------------------------------------------

def test_runs_lists_seeded_run(client, seeded_db):
    r = client.get("/runs")
    assert r.status_code == 200
    runs = r.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["run_id"] == RUN_ID
    assert runs[0]["session_prefix"] == "run"
    assert runs[0]["agent_count"] == 3
    assert runs[0]["decision_id"] == DECISION_ID
    assert runs[0]["total_cost_usd"] == pytest.approx(0.09)


def test_run_detail_reconstructs_full_chain(client, seeded_db):
    r = client.get(f"/runs/{RUN_ID}")
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == RUN_ID
    assert body["decision_id"] == DECISION_ID
    assert {log["agent_name"] for log in body["agent_logs"]} == {
        "tech_analyst", "portfolio_manager", "risk_manager",
    }
    assert len(body["trades"]) == 1
    assert body["trades"][0]["symbol"] == "AAPL"
    # Known Stage 2 limitation — never fabricated, always False.
    assert body["hard_risk_block_recorded"] is False
    # Journal/replay use case: the full prompt text must round-trip.
    pm_log = next(l for l in body["agent_logs"] if l["agent_name"] == "portfolio_manager")
    assert pm_log["input_message"] == "pm prompt"
    assert pm_log["full_response"] == '{"targets": []}'


def test_run_detail_404_for_unknown_run(client, seeded_db):
    r = client.get("/runs/run-doesnotexist")
    assert r.status_code == 404


def test_total_cost_is_none_when_any_call_has_unknown_cost(client, tmp_path, monkeypatch):
    """Matches Database.sum_session_cost's convention: a partial sum that
    looks precise but omits an unpriced call is worse than an honest
    unknown. One agent_logs row with cost_usd=None must null out the
    whole run's total, not just be skipped in the sum."""
    db_path = tmp_path / "partial_cost.db"
    db = Database(str(db_path))
    db.initialize()
    db.insert_agent_log(
        agent_name="tech_analyst", run_id="run-partial01",
        input_summary="x", output_summary="x", full_response="{}",
        model="claude-opus-4-7", tokens_used=100, cost_usd=0.02,
    )
    db.insert_agent_log(
        agent_name="portfolio_manager", run_id="run-partial01",
        input_summary="x", output_summary="x", full_response="{}",
        model="some-unpriced-model", tokens_used=100, cost_usd=None,
    )
    db.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(db_path))

    runs = client.get("/runs").json()["runs"]
    assert len(runs) == 1
    assert runs[0]["total_cost_usd"] is None

    detail = client.get("/runs/run-partial01").json()
    assert detail["total_cost_usd"] is None


# ---------------------------------------------------------------------------
# /decisions/{decision_id}
# ---------------------------------------------------------------------------

def test_decision_detail_returns_pm_rm_and_trades(client, seeded_db):
    r = client.get(f"/decisions/{DECISION_ID}")
    assert r.status_code == 200
    body = r.json()
    assert body["portfolio_manager"]["agent_name"] == "portfolio_manager"
    assert body["risk_manager"]["output_summary"] == "Approved: True"
    assert len(body["trades"]) == 1


def test_decision_detail_404_for_unknown_decision(client, seeded_db):
    assert client.get("/decisions/dec-doesnotexist").status_code == 404


# ---------------------------------------------------------------------------
# /agents, /agents/{agent_name}
# ---------------------------------------------------------------------------

def test_agents_roster_lists_all_nine(client, stub_roster):
    r = client.get("/agents")
    assert r.status_code == 200
    names = {a["agent_name"] for a in r.json()["agents"]}
    assert names == {
        "tech_analyst", "news_analyst", "macro_analyst", "earnings_analyst",
        "portfolio_manager", "risk_manager", "position_reviewer",
        "evening_analyst", "meta_reflector",
    }


def test_agent_detail_returns_recent_calls(client, seeded_db, stub_roster):
    r = client.get("/agents/portfolio_manager")
    assert r.status_code == 200
    body = r.json()
    assert body["agent_name"] == "portfolio_manager"
    assert len(body["recent_calls"]) == 1
    assert body["recent_calls"][0]["run_id"] == RUN_ID


def test_agent_detail_404_for_unknown_agent(client, stub_roster):
    assert client.get("/agents/not_a_real_agent").status_code == 404


# ---------------------------------------------------------------------------
# /reflections
# ---------------------------------------------------------------------------

def test_reflections_returns_seeded_insight(client, seeded_db):
    r = client.get("/reflections")
    assert r.status_code == 200
    body = r.json()
    assert len(body["insights"]) == 1
    assert body["insights"][0]["date"] == "2026-08-08"
    assert body["insights"][0]["risk_rating"] == "low"
    assert body["meta_periods"] == []  # no data/evolution/ dir in tmp cwd


# ---------------------------------------------------------------------------
# /account, /positions, /orders (broker-live, stubbed)
# ---------------------------------------------------------------------------

def test_account_computes_daily_pnl(client, stub_broker, seeded_db):
    r = client.get("/account")
    assert r.status_code == 200
    body = r.json()
    assert body["cash"] == 5000.0
    assert body["portfolio_value"] == 105_000.0
    assert body["daily_pnl"] == pytest.approx(5000.0)
    assert body["daily_pnl_pct"] == pytest.approx(5.0)
    assert body["paper"] is True
    assert body["error"] is None
    assert len(body["history"]) == 1
    assert body["history"][0]["date"] == "2026-08-08"


def test_account_surfaces_broker_error_without_crashing(client, seeded_db, monkeypatch):
    monkeypatch.setattr(routes_live, "read_account", lambda: {
        "cash": None, "portfolio_value": None, "last_equity": None,
        "error": "connection refused",
    })
    monkeypatch.setattr(routes_live, "get_alpaca_paper", lambda: True)
    r = client.get("/account")  # noqa: F821 (client fixture not used here on purpose)
    assert r.status_code == 200
    body = r.json()
    assert body["error"] == "connection refused"
    assert body["cash"] is None
    assert body["daily_pnl"] is None


def test_positions_returns_seeded_position(client, stub_broker):
    r = client.get("/positions")
    assert r.status_code == 200
    body = r.json()
    assert len(body["positions"]) == 1
    assert body["positions"][0]["symbol"] == "AAPL"
    assert body["error"] is None


def test_orders_returns_seeded_order(client, stub_broker):
    r = client.get("/orders")
    assert r.status_code == 200
    body = r.json()
    assert len(body["orders"]) == 1
    assert body["orders"][0]["id"] == "ord-1"


def test_orders_rejects_invalid_status(client, stub_broker):
    r = client.get("/orders", params={"status": "not-a-real-status"})
    assert r.status_code == 422  # FastAPI Literal/Query validation


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

def test_health_reports_ok_with_seeded_run_today(client, seeded_db, stub_broker, monkeypatch):
    from datetime import datetime, timezone

    # session_prefixes_logged_on() filters by today's ET date — the seeded
    # agent_logs rows were inserted with SQLite's `datetime('now')`, i.e.
    # "now", so they should already be within today's ET window without
    # needing to fake the clock.
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db_reachable"] is True
    assert "run" in body["sessions_logged_today"]
    assert body["broker_reachable"] is True
    assert body["paper"] is True


def test_health_never_crashes_when_db_path_is_bogus(client, monkeypatch):
    monkeypatch.setattr(db_reads, "get_db_path", lambda: "/nonexistent/path/does/not/exist.db")
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db_reachable"] is False
    assert body["sessions_logged_today"] == []
