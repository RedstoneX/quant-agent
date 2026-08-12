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

import src.api.broker_reads as broker_reads
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


# ---------------------------------------------------------------------------
# Stage 2 Checkpoint C — hard-risk-block forensic reconstruction.
#
# `TradingPipeline._persist_hard_risk_block` writes an `agent_logs` row with
# the sentinel `agent_name="risk_gate"` (not the real LLM `"risk_manager"`
# name) when the deterministic hard-risk gate blocks EVERY candidate before
# risk_manager is ever called. These tests seed that exact row directly
# (mirroring what the pipeline now does) and prove `/runs/{run_id}` and
# `/decisions/{decision_id}` surface it, closing the reconstruction gap
# `RunDetailResponse.hard_risk_block_recorded` previously always reported
# as unrecorded (hardcoded False).
# ---------------------------------------------------------------------------

HARD_BLOCK_RUN_ID = "run-hardblock1"
HARD_BLOCK_DECISION_ID = f"{HARD_BLOCK_RUN_ID}-dec-000002"


@pytest.fixture
def hard_risk_block_db(tmp_path, monkeypatch):
    """A run where every candidate was blocked by the deterministic hard-risk
    gate before risk_manager ever ran: a portfolio_manager row (PM always
    logs before RiskStage runs) plus the risk_gate forensic sentinel row,
    no risk_manager row, no trades."""
    db_path = tmp_path / "hard_risk_block.db"
    db = Database(str(db_path))
    db.initialize()
    db.insert_agent_log(
        agent_name="portfolio_manager", run_id=HARD_BLOCK_RUN_ID,
        input_summary="pm input", output_summary="pm output",
        full_response='{"targets": []}', model="gpt-5.5", tokens_used=500,
        input_message="pm prompt", cost_usd=0.05, decision_id=HARD_BLOCK_DECISION_ID,
    )
    db.insert_agent_log(
        agent_name="risk_gate", run_id=HARD_BLOCK_RUN_ID,
        input_summary="deterministic hard-risk gate blocked all candidates (pre_rm)",
        input_message="",
        output_summary="HARD_RISK_BLOCK: AAPL position would be 25.0% and exceed max 20%",
        full_response="AAPL position would be 25.0% and exceed max 20%",
        model="deterministic", tokens_used=0, input_tokens=0, output_tokens=0,
        cost_usd=0.0, decision_id=HARD_BLOCK_DECISION_ID, status="hard_risk_block",
    )
    db.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(db_path))
    return db_path


def test_run_detail_records_hard_risk_block(client, hard_risk_block_db):
    r = client.get(f"/runs/{HARD_BLOCK_RUN_ID}")
    assert r.status_code == 200
    body = r.json()
    assert body["hard_risk_block_recorded"] is True
    assert {log["agent_name"] for log in body["agent_logs"]} == {
        "portfolio_manager", "risk_gate",
    }
    gate_log = next(l for l in body["agent_logs"] if l["agent_name"] == "risk_gate")
    assert gate_log["status"] == "hard_risk_block"
    assert "exceed max 20%" in gate_log["full_response"]
    # Known-zero, not unknown: the run's total cost must stay computable
    # (PM's 0.05 + risk_gate's known 0.0), never nulled out by the
    # any-unknown-means-unknown convention.
    assert body["total_cost_usd"] == pytest.approx(0.05)
    assert body["trades"] == []


def test_decision_detail_exposes_hard_risk_block_not_risk_manager(client, hard_risk_block_db):
    r = client.get(f"/decisions/{HARD_BLOCK_DECISION_ID}")
    assert r.status_code == 200
    body = r.json()
    assert body["portfolio_manager"]["agent_name"] == "portfolio_manager"
    assert body["risk_manager"] is None
    assert body["hard_risk_block"] is not None
    assert body["hard_risk_block"]["agent_name"] == "risk_gate"
    assert body["hard_risk_block"]["status"] == "hard_risk_block"
    assert body["trades"] == []


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
# /candidates — Stage 2 Checkpoint C candidates/watchlist API contract gap.
#
# Reads the same canonical `insights.missed_opportunities_json` rows
# `TradingPipeline._build_watchlist_candidates` reads, via the shared pure
# aggregator in `src.watchlist_candidates` — never imports TradingPipeline.
# ---------------------------------------------------------------------------

@pytest.fixture
def candidates_db(tmp_path, monkeypatch):
    db_path = tmp_path / "candidates.db"
    db = Database(str(db_path))
    db.initialize()
    db.save_evening_snapshot(
        date="2026-08-08", total_value=100_000.0, daily_pnl=500.0, daily_return_pct=0.5,
        tomorrow_outlook="x", lessons="x", suggested_actions="x", risk_rating="low",
        missed_opportunities=[
            {"symbol": "VST", "miss_category": "theme_blindspot",
             "theme_if_any": "nuclear/power",
             "universe_addition_recommendation": "add",
             "universe_addition_reason": "20d $vol $180M; vol_conf 2.1x",
             "lesson": "x"},
            {"symbol": "NOISE", "miss_category": "noise_rally",
             "universe_addition_recommendation": "no", "lesson": "thin volume"},
        ],
    )
    db.save_evening_snapshot(
        date="2026-08-07", total_value=99_500.0, daily_pnl=-200.0, daily_return_pct=-0.2,
        tomorrow_outlook="x", lessons="x", suggested_actions="x", risk_rating="low",
        missed_opportunities=[
            {"symbol": "VST", "miss_category": "theme_blindspot",
             "theme_if_any": "nuclear/power",
             "universe_addition_recommendation": "watch",
             "universe_addition_reason": "vol_conf 1.6x; 1d conc 45%",
             "lesson": "x"},
        ],
    )
    db.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(db_path))
    return db_path


def test_candidates_aggregates_add_and_watch_counts(client, candidates_db):
    r = client.get("/candidates")
    assert r.status_code == 200
    body = r.json()
    assert body["lookback_days"] == 30
    assert len(body["candidates"]) == 1  # NOISE's "no" recommendation contributes nothing
    vst = body["candidates"][0]
    assert vst["symbol"] == "VST"
    assert vst["add_count"] == 1
    assert vst["watch_count"] == 1
    assert vst["total_flags"] == 2
    assert vst["themes"] == ["nuclear/power"]
    assert vst["dates"] == ["2026-08-08", "2026-08-07"]
    assert "$180M" in vst["latest_reason"]


def test_candidates_empty_when_no_insights(client, tmp_path, monkeypatch):
    db_path = tmp_path / "no_insights.db"
    db = Database(str(db_path))
    db.initialize()
    db.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(db_path))

    r = client.get("/candidates")
    assert r.status_code == 200
    assert r.json() == {"candidates": [], "lookback_days": 30}


def test_candidates_respects_lookback_days_query_param(client, candidates_db):
    r = client.get("/candidates?lookback_days=1")
    assert r.status_code == 200
    body = r.json()
    assert body["lookback_days"] == 1
    # Only the newest row (2026-08-08, add) is in the 1-day lookback window.
    vst = body["candidates"][0]
    assert vst["add_count"] == 1
    assert vst["watch_count"] == 0


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


# ---------------------------------------------------------------------------
# check_broker_reachable() failure paths — these are what a credential-gateway
# outage (or an unwired/unconfigured proxy) actually looks like at runtime,
# and had no direct test coverage: only the "healthy" path (stub_broker's
# `check_broker_reachable: lambda: True`) was previously exercised.
# ---------------------------------------------------------------------------

def test_check_broker_reachable_returns_none_when_credentials_missing(monkeypatch):
    monkeypatch.setattr(broker_reads, "get_alpaca_credentials", lambda: ("", ""))
    assert broker_reads.check_broker_reachable() is None


def test_check_broker_reachable_returns_false_when_get_account_raises(monkeypatch):
    class _BrokenBroker:
        def get_account(self):
            raise ConnectionError("simulated credential-gateway outage")

    monkeypatch.setattr(broker_reads, "get_alpaca_credentials", lambda: ("placeholder-key", "placeholder-secret"))
    monkeypatch.setattr(broker_reads, "_get_broker", lambda: _BrokenBroker())
    assert broker_reads.check_broker_reachable() is False


def test_health_never_crashes_when_broker_check_raises_unexpectedly(client, seeded_db, monkeypatch):
    """Defense in depth: check_broker_reachable() is documented to never
    raise (see broker_reads.py's module docstring), but /health's outermost
    guard should still hold even if that invariant is ever violated by a
    future change — proving `broker_reachable: None` beats a 500."""
    def _boom():
        raise RuntimeError("should never happen, but /health must survive it anyway")

    monkeypatch.setattr(routes_live, "check_broker_reachable", _boom)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["broker_reachable"] is None
