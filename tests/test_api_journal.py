"""Stage 5 — Mission Control API functional contract tests for the
journal (`/journal/dates`, `/journal/{date}`) and forensic search
(`/search`) routes. Same pattern as test_api_contract.py / test_api_evidence.py:
seeds a real temp SQLite DB via `src.storage.db.Database`, monkeypatches
`src.api.db_reads.get_db_path`, exercises through a real FastAPI TestClient.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import src.api.db_reads as db_reads
from src.api.server import app
from src.storage.db import Database

RUN_ID = "run-abc12345"
DECISION_ID = f"{RUN_ID}-dec-000001"
DATE = "2026-08-08"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def seeded_journal_db(tmp_path, monkeypatch):
    db_path = tmp_path / "quant_agent_test.db"
    db = Database(str(db_path))
    db.initialize()

    db.insert_agent_log(
        agent_name="tech_analyst", run_id=RUN_ID,
        input_summary="tech input", output_summary="tech output",
        full_response="{}", model="claude-opus-4-7", tokens_used=200,
        cost_usd=0.01,
    )
    db.insert_agent_log(
        agent_name="portfolio_manager", run_id=RUN_ID,
        input_summary="pm input", output_summary="Deployed into AAPL strength",
        full_response='{"targets": []}', model="gpt-5.5", tokens_used=500,
        cost_usd=0.05, decision_id=DECISION_ID,
    )
    db.insert_trade(
        symbol="AAPL", action="BUY", qty=10, price=150.0,
        reasoning="services growth thesis", run_id=RUN_ID,
        stop_loss=140.0, take_profit=170.0,
        broker_order_id="ord-1", fill_status="filled", decision_id=DECISION_ID,
    )
    # insert_agent_log/insert_trade default `timestamp` to `datetime('now')`
    # (real wall-clock UTC), not the fixed DATE this fixture is journaling
    # for. Pin both rows' timestamps into DATE's ET trading day so the
    # day-bounded journal query (src.api.db_reads.get_journal_day) finds
    # them — mirrors tests/test_db.py's today_only ET-day test pattern.
    fixed_ts = f"{DATE} 15:00:00"
    for table in ("agent_logs", "trades", "specialist_evidence"):
        db.conn.execute(
            f"UPDATE {table} SET timestamp = ? WHERE run_id = ?",
            (fixed_ts, RUN_ID),
        )
    db.conn.commit()
    db.insert_daily_pnl(date=DATE, total_value=100_000.0, daily_pnl=500.0, daily_return_pct=0.5)
    db.save_insights(
        date=DATE, tomorrow_outlook="cautiously bullish", lessons="none yet",
        suggested_actions="hold", risk_rating="low",
    )
    db.insert_specialist_evidence(
        run_id=RUN_ID, agent_name="tech_analyst", kind="analysis", scope="symbol",
        symbol="AAPL", evidence_json='{"symbol": "AAPL", "rating": "buy"}',
    )

    db.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(db_path))
    return db_path


def test_journal_dates_lists_seeded_date(client, seeded_journal_db):
    r = client.get("/journal/dates")
    assert r.status_code == 200
    assert r.json()["dates"] == [DATE]


def test_journal_day_assembles_full_day(client, seeded_journal_db):
    r = client.get(f"/journal/{DATE}")
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == DATE
    assert body["has_data"] is True
    assert body["daily_pnl"]["daily_pnl"] == 500.0
    assert body["reflection"]["tomorrow_outlook"] == "cautiously bullish"
    assert len(body["runs"]) == 1
    assert body["runs"][0]["run_id"] == RUN_ID
    assert body["runs"][0]["decision_id"] == DECISION_ID
    assert len(body["trades"]) == 1
    assert body["trades"][0]["symbol"] == "AAPL"
    assert body["candidates"] == ["AAPL"]


def test_journal_day_404_for_date_with_no_data(client, seeded_journal_db):
    r = client.get("/journal/2020-01-01")
    assert r.status_code == 404


def test_journal_day_handles_malformed_date_without_500(client, seeded_journal_db):
    r = client.get("/journal/not-a-date")
    assert r.status_code == 404


def test_search_finds_trade_by_symbol(client, seeded_journal_db):
    r = client.get("/search", params={"q": "AAPL"})
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "AAPL"
    assert any(t["symbol"] == "AAPL" for t in body["trades"])


def test_search_finds_trade_by_reasoning_text(client, seeded_journal_db):
    r = client.get("/search", params={"q": "services growth"})
    assert r.status_code == 200
    assert len(r.json()["trades"]) == 1


def test_search_finds_agent_log_by_output_summary(client, seeded_journal_db):
    r = client.get("/search", params={"q": "Deployed into AAPL"})
    assert r.status_code == 200
    body = r.json()
    assert any(a["agent_name"] == "portfolio_manager" for a in body["agent_logs"])


def test_search_empty_query_returns_no_results(client, seeded_journal_db):
    r = client.get("/search", params={"q": ""})
    assert r.status_code == 200
    assert r.json() == {"query": "", "trades": [], "agent_logs": []}


def test_search_no_match_returns_empty_not_error(client, seeded_journal_db):
    r = client.get("/search", params={"q": "NOSUCHSYMBOLXYZ"})
    assert r.status_code == 200
    assert r.json()["trades"] == []
    assert r.json()["agent_logs"] == []


def test_search_literal_percent_does_not_wildcard_match_everything(client, seeded_journal_db):
    """A literal '%' in the search term must be escaped, not act as a
    SQL LIKE wildcard that matches every row — proves the endpoint can't
    be coaxed into an unbounded scan via LIKE metacharacters."""
    r = client.get("/search", params={"q": "%"})
    assert r.status_code == 200
    assert r.json()["trades"] == []
    assert r.json()["agent_logs"] == []
