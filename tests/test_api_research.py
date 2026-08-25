"""Research Daily API truth, degradation, and secret-boundary tests."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

import src.api.db_reads as db_reads
from src.api.server import app
from src.storage.db import Database

DATE = "2026-08-24"


def _seed(tmp_path, monkeypatch):
    path = tmp_path / "research.db"
    db = Database(str(path))
    db.initialize()
    db.conn.execute(
        "INSERT INTO agent_logs (agent_name,run_id,output_summary,full_response,status,"
        "decision_id,timestamp) VALUES (?,?,?,?,?,?,?)",
        ("smart_money_analyst", "run-1", "Insider cluster; disclosed promptly",
         "RAW SECRET-LIKE PROSE", "success", "d-1", "2026-08-24 14:00:00"),
    )
    db.conn.execute(
        "INSERT INTO specialist_evidence (run_id,decision_id,agent_name,kind,scope,symbol,"
        "evidence_json,timestamp) VALUES (?,?,?,?,?,?,?,?)",
        ("run-1", "d-1", "smart_money_analyst", "analysis", "symbol", "AAPL",
         json.dumps({"source": "SEC Form 4", "known_at": "2026-08-24T12:00:00Z",
                     "lag_days": 1, "stance": "confirmatory"}),
         "2026-08-24 14:00:01"),
    )
    db.conn.execute(
        "INSERT INTO specialist_evidence (run_id,decision_id,agent_name,kind,scope,symbol,"
        "evidence_json,timestamp) VALUES (?,?,?,?,?,?,?,?)",
        ("run-1", "d-1", "portfolio_manager", "proposed_order", "symbol", "AAPL",
         json.dumps({"symbol": "AAPL", "action": "BUY"}), "2026-08-24 14:00:02"),
    )
    db.conn.execute(
        "INSERT INTO specialist_evidence (run_id,decision_id,agent_name,kind,scope,symbol,"
        "evidence_json,timestamp) VALUES (?,?,?,?,?,?,?,?)",
        ("run-1", "d-1", "pipeline", "pipeline_event", "symbol", "AAPL",
         json.dumps({"stage": "execution", "outcome": "skipped", "reason": "stale_entry"}),
         "2026-08-24 14:00:03"),
    )
    db.conn.commit()
    db.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(path))
    return path


def test_daily_research_preserves_calls_evidence_and_delta_without_raw_prose(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    response = TestClient(app).get(f"/research/daily/{DATE}")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "complete"
    assert body["freshness"]["label"] == "historical"
    run = body["runs"][0]
    assert run["agent_calls"][0]["agent_name"] == "smart_money_analyst"
    assert "full_response" not in run["agent_calls"][0]
    assert "input_message" not in run["agent_calls"][0]
    assert "RAW SECRET-LIKE PROSE" not in response.text
    smart = next(e for e in run["evidence"] if e["agent_name"] == "smart_money_analyst")
    assert smart["payload"]["source"] == "SEC Form 4"
    assert smart["payload"]["lag_days"] == 1
    assert run["decision_delta"]["state"] == "proposed_not_executed"
    assert run["decision_delta"]["deterministic_events"][0]["payload"]["reason"] == "stale_entry"


def test_daily_research_empty_is_explicit(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    body = TestClient(app).get("/research/daily/2026-08-23").json()
    assert body["state"] == "empty"
    assert body["runs"] == []
    assert body["read_error"] is None


def test_daily_research_db_failure_is_not_empty(monkeypatch):
    monkeypatch.setattr(db_reads, "get_db_path", lambda: "/missing/research.db")
    body = TestClient(app).get(f"/research/daily/{DATE}").json()
    assert body["state"] == "error"
    assert body["read_error"] == "research data unavailable"
    assert body["missing_sources"] == ["canonical_database"]


def test_daily_research_invalid_date_is_422():
    assert TestClient(app).get("/research/daily/not-a-date").status_code == 422


def test_daily_research_malformed_evidence_is_partial(tmp_path, monkeypatch):
    path = _seed(tmp_path, monkeypatch)
    conn = Database(str(path))
    conn.initialize()
    conn.conn.execute(
        "INSERT INTO specialist_evidence (run_id,agent_name,kind,scope,evidence_json,timestamp) "
        "VALUES (?,?,?,?,?,?)", ("run-1", "news_analyst", "analysis", "run", "not-json",
                                  "2026-08-24 14:00:04"),
    )
    conn.conn.commit()
    conn.close()
    body = TestClient(app).get(f"/research/daily/{DATE}").json()
    assert body["state"] == "partial"
    assert any(e["state"] == "invalid" for e in body["runs"][0]["evidence"])


def test_daily_research_provider_and_analysis_errors_are_partial_with_missing_seats(
    tmp_path, monkeypatch,
):
    path = _seed(tmp_path, monkeypatch)
    conn = Database(str(path))
    conn.initialize()
    for agent, kind in (
        ("smart_money_analyst", "provider_error"),
        ("earnings_analyst", "analysis_error"),
    ):
        conn.conn.execute(
            "INSERT INTO specialist_evidence "
            "(run_id,agent_name,kind,scope,evidence_json,timestamp) VALUES (?,?,?,?,?,?)",
            ("run-1", agent, kind, "run", json.dumps({"error": "unavailable"}),
             "2026-08-24 14:00:05"),
        )
    conn.conn.commit()
    conn.close()

    body = TestClient(app).get(f"/research/daily/{DATE}").json()
    assert body["state"] == "partial"
    assert body["missing_sources"] == [
        "earnings_analyst/analysis", "smart_money_analyst/provider",
    ]
    # Failure evidence stays visible rather than being converted to prose.
    kinds = {e["kind"] for e in body["runs"][0]["evidence"]}
    assert {"provider_error", "analysis_error"} <= kinds
