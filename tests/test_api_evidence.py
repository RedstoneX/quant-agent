"""Stage 4 — Mission Control API functional contract tests for the
per-candidate specialist-evidence routes (`/runs/{run_id}/candidates`,
`/runs/{run_id}/candidates/{symbol}`).

Mirrors test_api_contract.py's pattern: seeds a real temp SQLite DB through
`src.storage.db.Database` (the trading process's own writer — fine for
building fixtures; the constraint is only that `src/api/*.py` itself never
writes), monkeypatches `src.api.db_reads.get_db_path`, and exercises the
routes through a real FastAPI TestClient.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import src.api.db_reads as db_reads
from src.api.server import app
from src.storage.db import Database

RUN_ID = "run-abc12345"
DECISION_ID = f"{RUN_ID}-dec-000001"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _tech_reasoning_chain():
    return {
        "trend": "uptrend", "momentum": "rsi 62", "volatility": "bb mid",
        "volume": "confirming", "support_resistance": "200dma support",
    }


def _pm_reasoning_chain():
    return {
        "macro_filter": "x", "news_check": "x", "earnings_check": "x",
        "signal_conflicts": "x", "sizing_logic": "x",
        "portfolio_balance": "x", "cash_target": "x",
    }


def _risk_reasoning_chain():
    return {
        "rr_audit": "x", "signal_fidelity": "x", "correlation_check": "x",
        "event_risk": "x", "sizing_sanity": "x", "overall": "x",
    }


@pytest.fixture
def seeded_evidence_db(tmp_path, monkeypatch):
    db_path = tmp_path / "quant_agent_test.db"
    db = Database(str(db_path))
    db.initialize()

    db.insert_trade(
        symbol="AAPL", action="BUY", qty=10, price=150.0,
        reasoning="test buy", run_id=RUN_ID, stop_loss=140.0, take_profit=170.0,
        broker_order_id="ord-1", fill_status="filled", decision_id=DECISION_ID,
    )

    # Run-scoped research evidence — no decision_id.
    db.insert_specialist_evidence(
        run_id=RUN_ID, agent_name="macro_analyst", kind="analysis", scope="run",
        evidence_json=json.dumps({
            "reasoning_chain": {
                "volatility_analysis": "a", "yield_curve_analysis": "b",
                "monetary_policy_analysis": "c", "inflation_labor_credit": "d",
                "cross_signal_synthesis": "e", "sector_implications": "f",
            },
            "regime": "risk-on", "confidence": "high", "equity_outlook": "bullish",
            "position_guidance": {
                "target_invested_pct": 70, "cash_recommendation_pct": 30, "reasoning": "y",
            },
            "sector_guidance": [
                {"sector": "Technology", "stance": "overweight", "reason": "AI capex"},
            ],
            "summary": "risk-on regime",
        }),
    )
    db.insert_specialist_evidence(
        run_id=RUN_ID, agent_name="news_analyst", kind="analysis", scope="run",
        evidence_json=json.dumps({
            "macro_narrative": {
                "last_updated": "2026-08-08", "era_themes": ["AI capex"],
                "current_regime": "risk-on expansion",
            },
            "state_changes": [{
                "event": "Fed pause", "previous_state": "hiking", "new_state": "paused",
                "market_impact": "bullish", "affected_symbols": ["AAPL"], "conviction": "high",
            }],
            "stock_news": {
                "AAPL": [{
                    "headline": "AAPL beats on services growth",
                    "sentiment": "bullish", "conviction": "high",
                    "impact_summary": "services margin expansion",
                }],
            },
            "pm_briefing": "Quiet tape, AI capex theme intact.",
            "market_sentiment": "bullish", "confidence": "medium",
        }),
    )
    db.insert_specialist_evidence(
        run_id=RUN_ID, agent_name="tech_analyst", kind="analysis", scope="symbol",
        symbol="AAPL",
        evidence_json=json.dumps({
            "symbol": "AAPL", "rating": "buy", "conviction": "high",
            "entry_price": 150.0, "reference_target": 170.0, "stop_loss": 140.0,
            "reasoning_chain": _tech_reasoning_chain(),
            "reasoning": "Breakout above 200dma with volume confirmation.",
        }),
    )

    # Decision-phase evidence — correlated to DECISION_ID.
    db.insert_specialist_evidence(
        run_id=RUN_ID, agent_name="portfolio_manager", kind="reasoning", scope="run",
        decision_id=DECISION_ID,
        evidence_json=json.dumps({
            "portfolio_view": "Deploying into AAPL strength.",
            "reasoning_chain": _pm_reasoning_chain(),
        }),
    )
    db.insert_specialist_evidence(
        run_id=RUN_ID, agent_name="portfolio_manager", kind="target", scope="symbol",
        symbol="AAPL", decision_id=DECISION_ID,
        evidence_json=json.dumps({
            "symbol": "AAPL", "target_weight_pct": 10.0, "conviction": "high",
            "thesis": "Services growth re-rating.",
        }),
    )
    db.insert_specialist_evidence(
        run_id=RUN_ID, agent_name="portfolio_manager", kind="proposed_order", scope="symbol",
        symbol="AAPL", decision_id=DECISION_ID,
        evidence_json=json.dumps({
            "action": "BUY", "symbol": "AAPL", "allocation_pct": 10.0,
            "entry_price": 150.0, "stop_loss": 140.0, "take_profit": 170.0,
            "reasoning": "constructed order",
        }),
    )
    db.insert_specialist_evidence(
        run_id=RUN_ID, agent_name="risk_manager", kind="verdict", scope="run",
        decision_id=DECISION_ID,
        evidence_json=json.dumps({
            "approved": True, "reasoning_chain": _risk_reasoning_chain(),
            "reasoning": "Clean approval, no mods.",
        }),
    )

    db.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(db_path))
    return db_path


def test_run_candidates_lists_symbols_with_evidence_or_trades(client, seeded_evidence_db):
    r = client.get(f"/runs/{RUN_ID}/candidates")
    assert r.status_code == 200
    assert r.json() == {"run_id": RUN_ID, "candidates": ["AAPL"]}


def test_run_candidates_empty_for_unknown_run(client, seeded_evidence_db):
    r = client.get("/runs/run-doesnotexist/candidates")
    assert r.status_code == 200
    assert r.json()["candidates"] == []


def test_candidate_detail_assembles_full_chain(client, seeded_evidence_db):
    r = client.get(f"/runs/{RUN_ID}/candidates/AAPL")
    assert r.status_code == 200
    body = r.json()

    assert body["run_id"] == RUN_ID
    assert body["symbol"] == "AAPL"
    assert body["decision_id"] == DECISION_ID

    assert body["tech"]["rating"] == "buy"
    assert body["earnings"] is None  # never fabricated — no earnings evidence seeded

    assert len(body["news_symbol"]) == 1
    assert body["news_symbol"][0]["sentiment"] == "bullish"

    assert body["macro_context"]["regime"] == "risk-on"
    assert body["macro_context"]["sector_guidance"][0]["sector"] == "Technology"

    assert body["news_context"]["market_sentiment"] == "bullish"
    assert body["news_context"]["pm_briefing"] == "Quiet tape, AI capex theme intact."
    assert len(body["news_context"]["relevant_state_changes"]) == 1

    assert body["pm_reasoning"]["portfolio_view"] == "Deploying into AAPL strength."
    assert body["pm_target"]["target_weight_pct"] == 10.0
    assert body["pm_proposed_order"]["action"] == "BUY"
    assert body["risk_verdict"]["verdict"]["approved"] is True
    assert body["risk_modification"] is None  # never fabricated — no mod seeded

    assert body["trade"]["symbol"] == "AAPL"
    assert body["trade"]["fill_status"] == "filled"

    # tech (bullish) + earnings (none) + news_symbol (bullish) => 2 aligned signals
    sources = {s["source"] for s in body["consensus"]["signals"]}
    assert sources == {"tech_analyst", "news_analyst"}
    assert body["consensus"]["agreement"] == "aligned"


def test_candidate_detail_consensus_all_neutral_is_not_reported_as_aligned(client, tmp_path, monkeypatch):
    """Regression: >=2 signals firing but every one neutral must NOT be
    reported as "aligned" — that would claim consensus that doesn't
    exist. See ConsensusSummary.agreement docstring."""
    db_path = tmp_path / "quant_agent_test.db"
    db = Database(str(db_path))
    db.initialize()
    db.insert_trade(
        symbol="AAPL", action="HOLD", qty=0, price=150.0,
        reasoning="no signal", run_id=RUN_ID,
    )
    db.insert_specialist_evidence(
        run_id=RUN_ID, agent_name="tech_analyst", kind="analysis", scope="symbol",
        symbol="AAPL",
        evidence_json=json.dumps({
            "symbol": "AAPL", "rating": "neutral", "conviction": "low",
            "reasoning_chain": _tech_reasoning_chain(),
            "reasoning": "No clear trend either way.",
        }),
    )
    db.insert_specialist_evidence(
        run_id=RUN_ID, agent_name="news_analyst", kind="analysis", scope="run",
        evidence_json=json.dumps({
            "macro_narrative": {
                "last_updated": "2026-08-08", "era_themes": ["AI capex"],
                "current_regime": "risk-on expansion",
            },
            "stock_news": {
                "AAPL": [{
                    "headline": "AAPL trades in line with sector",
                    "sentiment": "neutral", "conviction": "low",
                    "impact_summary": "no notable move",
                }],
            },
            "pm_briefing": "Quiet tape.",
            "market_sentiment": "neutral", "confidence": "low",
        }),
    )
    db.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(db_path))

    r = client.get(f"/runs/{RUN_ID}/candidates/AAPL")
    assert r.status_code == 200
    body = r.json()
    assert len(body["consensus"]["signals"]) == 2
    assert {s["direction"] for s in body["consensus"]["signals"]} == {"neutral"}
    assert body["consensus"]["agreement"] == "no_directional_signal"
    assert body["consensus"]["agreement"] != "aligned"


def test_candidate_detail_shows_rejected_verdict_and_modification_delta(client, tmp_path, monkeypatch):
    """Rejected RM verdict + a per-symbol modification must surface
    correctly, and the proposed-order-vs-executed(trade) delta must be
    visible (no trade row here since the decision was rejected)."""
    db_path = tmp_path / "quant_agent_test.db"
    db = Database(str(db_path))
    db.initialize()
    db.insert_specialist_evidence(
        run_id=RUN_ID, agent_name="portfolio_manager", kind="proposed_order",
        scope="symbol", symbol="AAPL", decision_id=DECISION_ID,
        evidence_json=json.dumps({
            "action": "BUY", "symbol": "AAPL", "allocation_pct": 15.0,
            "entry_price": 150.0, "stop_loss": 140.0, "take_profit": 170.0,
            "reasoning": "constructed order",
        }),
    )
    db.insert_specialist_evidence(
        run_id=RUN_ID, agent_name="risk_manager", kind="modification",
        scope="symbol", symbol="AAPL", decision_id=DECISION_ID,
        evidence_json=json.dumps({
            "symbol": "AAPL", "field": "allocation_pct",
            "original_value": 15.0, "new_value": 5.0, "reason": "oversized",
        }),
    )
    db.insert_specialist_evidence(
        run_id=RUN_ID, agent_name="risk_manager", kind="verdict", scope="run",
        decision_id=DECISION_ID,
        evidence_json=json.dumps({
            "approved": False, "reasoning_chain": _risk_reasoning_chain(),
            "reasoning": "Rejected — correlation risk too high after modification.",
        }),
    )
    db.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(db_path))

    r = client.get(f"/runs/{RUN_ID}/candidates/AAPL")
    assert r.status_code == 200
    body = r.json()
    assert body["risk_verdict"]["verdict"]["approved"] is False
    assert body["risk_modification"]["original_value"] == 15.0
    assert body["risk_modification"]["new_value"] == 5.0
    assert body["pm_proposed_order"]["allocation_pct"] == 15.0
    assert body["trade"] is None  # rejected — never executed, never fabricated


def test_candidate_detail_404_for_symbol_never_considered(client, seeded_evidence_db):
    r = client.get(f"/runs/{RUN_ID}/candidates/TSLA")
    assert r.status_code == 404


def test_external_research_scope_does_not_become_trading_candidate(
    client, seeded_evidence_db,
):
    db = Database(str(seeded_evidence_db))
    db.initialize()
    db.insert_specialist_evidence(
        run_id=RUN_ID, agent_name="smart_money_analyst", kind="finding",
        scope="research", symbol="VENU",
        evidence_json=json.dumps({"source": "SEC Form 4", "stance": "bullish"}),
    )
    db.close()

    candidates = client.get(f"/runs/{RUN_ID}/candidates")
    assert candidates.status_code == 200
    assert "AAPL" in candidates.json()["candidates"]
    assert "VENU" not in candidates.json()["candidates"]
    assert client.get(f"/runs/{RUN_ID}/candidates/VENU").status_code == 404


def test_candidate_detail_404_for_unknown_run(client, seeded_evidence_db):
    r = client.get("/runs/run-doesnotexist/candidates/AAPL")
    assert r.status_code == 404


def test_candidate_detail_degrades_gracefully_on_malformed_evidence_row(client, tmp_path, monkeypatch):
    """A corrupted/legacy evidence row must degrade that one field to None,
    never 500 the whole response."""
    db_path = tmp_path / "quant_agent_test.db"
    db = Database(str(db_path))
    db.initialize()
    db.insert_specialist_evidence(
        run_id=RUN_ID, agent_name="tech_analyst", kind="analysis", scope="symbol",
        symbol="AAPL", evidence_json='{"not": "a valid TechAnalysisResult"}',
    )
    db.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(db_path))

    r = client.get(f"/runs/{RUN_ID}/candidates/AAPL")
    assert r.status_code == 200
    assert r.json()["tech"] is None
