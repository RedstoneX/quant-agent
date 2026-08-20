"""Stage 6 — Mission Control API functional contract tests for the
decision-funnel route (`/runs/{run_id}/funnel`).

Reuses `test_api_evidence.py`'s `seeded_evidence_db` (a candidate that
reaches PM target -> proposed order -> approved -> filled trade) and
`test_api_contract.py`'s `hard_risk_block_db` (every candidate blocked by
the deterministic gate before risk_manager ever ran, zero
specialist_evidence rows) as two of the funnel's structural states, and
adds fixtures for the "considered but PM stayed neutral" and "proposed but
rejected" states plus an inverse-ETF (bearish hedge) candidate.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import src.api.db_reads as db_reads
from src.api.server import app
from src.storage.db import Database
from tests.test_api_contract import HARD_BLOCK_RUN_ID, hard_risk_block_db  # noqa: F401
from tests.test_api_evidence import RUN_ID as EXECUTED_RUN_ID
from tests.test_api_evidence import (  # noqa: F401
    _pm_reasoning_chain,
    _risk_reasoning_chain,
    _tech_reasoning_chain,
    seeded_evidence_db,
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_funnel_404_for_unknown_run(client, seeded_evidence_db):
    r = client.get("/runs/run-doesnotexist/funnel")
    assert r.status_code == 404


def test_funnel_executed_trade_reports_executed_state(client, seeded_evidence_db):
    r = client.get(f"/runs/{EXECUTED_RUN_ID}/funnel")
    assert r.status_code == 200
    body = r.json()
    assert body["decision_state"] == "executed"
    assert body["candidates_considered"] == 1
    assert body["reached_pm_count"] == 1
    assert body["proposed_order_count"] == 1
    assert body["executed_count"] == 1
    assert body["hard_risk_block"] is False
    assert body["bearish_hedge_considered"] is False
    cand = body["candidates"][0]
    assert cand["symbol"] == "AAPL"
    assert cand["direction"] == "bullish"
    assert cand["is_bearish_hedge"] is False
    assert cand["executed"] is True
    assert cand["trade_action"] == "BUY"
    # PM/RM narrative text is quoted verbatim, not synthesized.
    assert body["pm_reasoning"]["portfolio_view"] == "Deploying into AAPL strength."
    assert body["risk_verdict"]["verdict"]["approved"] is True
    assert body["macro_context"]["regime"] == "risk-on"


def test_funnel_hard_risk_block_reports_hard_risk_block_state(client, hard_risk_block_db):
    r = client.get(f"/runs/{HARD_BLOCK_RUN_ID}/funnel")
    assert r.status_code == 200
    body = r.json()
    assert body["decision_state"] == "hard_risk_block"
    assert body["hard_risk_block"] is True
    # This fixture writes only agent_logs (no specialist_evidence) — the
    # funnel must still render (not 404) with zero candidates recorded.
    assert body["candidates"] == []
    assert body["candidates_considered"] == 0
    assert body["executed_count"] == 0


NEUTRAL_RUN_ID = "run-neutral001"


@pytest.fixture
def neutral_pm_db(tmp_path, monkeypatch):
    """A run that considered a bearish-hedge candidate (SQQQ) all the way
    through tech analysis but the Portfolio Manager never constructed a
    proposed order for it — no PM target row at all (stayed neutral),
    matching the "PM recognized evidence but chose not to act" forensic
    category in docs/WORK.md Workstream A."""
    db_path = tmp_path / "neutral.db"
    db = Database(str(db_path))
    db.initialize()
    db.insert_specialist_evidence(
        run_id=NEUTRAL_RUN_ID, agent_name="portfolio_manager", kind="reasoning", scope="run",
        evidence_json=json.dumps({
            "portfolio_view": "Decline noted; staying neutral pending confirmation.",
            "reasoning_chain": _pm_reasoning_chain(),
        }),
    )
    db.insert_specialist_evidence(
        run_id=NEUTRAL_RUN_ID, agent_name="tech_analyst", kind="analysis", scope="symbol",
        symbol="SQQQ",
        evidence_json=json.dumps({
            "symbol": "SQQQ", "rating": "buy", "conviction": "medium",
            "reasoning_chain": _tech_reasoning_chain(),
            "reasoning": "Breadth deteriorating, inverse setup forming.",
        }),
    )
    db.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(db_path))
    return db_path


def test_funnel_no_proposal_flags_bearish_hedge_candidate(client, neutral_pm_db):
    r = client.get(f"/runs/{NEUTRAL_RUN_ID}/funnel")
    assert r.status_code == 200
    body = r.json()
    assert body["decision_state"] == "no_proposal"
    assert body["candidates_considered"] == 1
    assert body["reached_pm_count"] == 0
    assert body["proposed_order_count"] == 0
    assert body["executed_count"] == 0
    assert body["bearish_hedge_considered"] is True
    cand = body["candidates"][0]
    assert cand["symbol"] == "SQQQ"
    assert cand["is_bearish_hedge"] is True
    assert cand["reached_pm_target"] is False
    assert cand["executed"] is False
    assert body["pm_reasoning"]["portfolio_view"].startswith("Decline noted")


REJECTED_RUN_ID = "run-rejected01"
REJECTED_DECISION_ID = f"{REJECTED_RUN_ID}-dec-000003"


@pytest.fixture
def rejected_proposal_db(tmp_path, monkeypatch):
    """PM constructed a proposed order but the AI Risk Manager rejected it
    before execution — no trade row at all for the symbol."""
    db_path = tmp_path / "rejected.db"
    db = Database(str(db_path))
    db.initialize()
    db.insert_specialist_evidence(
        run_id=REJECTED_RUN_ID, agent_name="portfolio_manager", kind="target", scope="symbol",
        symbol="TSLA", decision_id=REJECTED_DECISION_ID,
        evidence_json=json.dumps({
            "symbol": "TSLA", "target_weight_pct": 8.0, "conviction": "medium",
            "thesis": "Momentum continuation.",
        }),
    )
    db.insert_specialist_evidence(
        run_id=REJECTED_RUN_ID, agent_name="portfolio_manager", kind="proposed_order", scope="symbol",
        symbol="TSLA", decision_id=REJECTED_DECISION_ID,
        evidence_json=json.dumps({
            "action": "BUY", "symbol": "TSLA", "allocation_pct": 8.0,
            "entry_price": 250.0, "stop_loss": 230.0, "take_profit": 290.0,
            "reasoning": "constructed order",
        }),
    )
    db.insert_specialist_evidence(
        run_id=REJECTED_RUN_ID, agent_name="risk_manager", kind="verdict", scope="run",
        decision_id=REJECTED_DECISION_ID,
        evidence_json=json.dumps({
            "approved": False, "reasoning_chain": _risk_reasoning_chain(),
            "reasoning": "Concentration limit already binding.",
        }),
    )
    db.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(db_path))
    return db_path


def test_funnel_proposed_not_executed_state(client, rejected_proposal_db):
    r = client.get(f"/runs/{REJECTED_RUN_ID}/funnel")
    assert r.status_code == 200
    body = r.json()
    assert body["decision_state"] == "proposed_not_executed"
    assert body["proposed_order_count"] == 1
    assert body["executed_count"] == 0
    assert body["risk_verdict"]["verdict"]["approved"] is False
    cand = body["candidates"][0]
    assert cand["symbol"] == "TSLA"
    assert cand["proposed_action"] == "BUY"
    assert cand["executed"] is False
    # No execution_skip evidence in this fixture — the RM veto happened
    # before the execution phase, so the fields stay None.
    assert cand["execution_skip_reason"] is None


UNFUNDED_RUN_ID = "run-unfunded01"
UNFUNDED_DECISION_ID = f"{UNFUNDED_RUN_ID}-dec-000004"


@pytest.fixture
def unfunded_skip_db(tmp_path, monkeypatch):
    """The 2026-08-19 shape: RM approved the BUY, execution skipped it as
    unfunded (funding-sell race), and the trading process persisted an
    `execution_skip` evidence row at the skip site."""
    db_path = tmp_path / "unfunded.db"
    db = Database(str(db_path))
    db.initialize()
    db.insert_specialist_evidence(
        run_id=UNFUNDED_RUN_ID, agent_name="portfolio_manager", kind="target", scope="symbol",
        symbol="XLE", decision_id=UNFUNDED_DECISION_ID,
        evidence_json=json.dumps({
            "symbol": "XLE", "target_weight_pct": 10.0, "conviction": "high",
            "thesis": "Energy tailwind.",
        }),
    )
    db.insert_specialist_evidence(
        run_id=UNFUNDED_RUN_ID, agent_name="portfolio_manager", kind="proposed_order", scope="symbol",
        symbol="XLE", decision_id=UNFUNDED_DECISION_ID,
        evidence_json=json.dumps({
            "action": "BUY", "symbol": "XLE", "allocation_pct": 10.0,
            "entry_price": 63.78, "stop_loss": 59.0, "take_profit": 73.34,
            "reasoning": "constructed order",
        }),
    )
    db.insert_specialist_evidence(
        run_id=UNFUNDED_RUN_ID, agent_name="risk_manager", kind="verdict", scope="run",
        decision_id=UNFUNDED_DECISION_ID,
        evidence_json=json.dumps({
            "approved": True, "reasoning_chain": _risk_reasoning_chain(),
            "reasoning": "Approved.",
        }),
    )
    db.insert_specialist_evidence(
        run_id=UNFUNDED_RUN_ID, agent_name="execution", kind="execution_skip", scope="symbol",
        symbol="XLE", decision_id=UNFUNDED_DECISION_ID,
        evidence_json=json.dumps({
            "symbol": "XLE", "reason": "insufficient_cash",
            "detail": "estimated cost $637.80 exceeds available cash $145.11",
        }),
    )
    db.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(db_path))
    return db_path


def test_funnel_surfaces_execution_skip_reason(client, unfunded_skip_db):
    """An approved-then-skipped BUY must be distinguishable from a
    deliberate no-trade — the funnel quotes the trading process's own
    skip record, closing the 2026-08-19 invisibility gap."""
    r = client.get(f"/runs/{UNFUNDED_RUN_ID}/funnel")
    assert r.status_code == 200
    body = r.json()
    assert body["decision_state"] == "proposed_not_executed"
    cand = body["candidates"][0]
    assert cand["symbol"] == "XLE"
    assert cand["executed"] is False
    assert cand["execution_skip_reason"] == "insufficient_cash"
    assert "145.11" in cand["execution_skip_detail"]
