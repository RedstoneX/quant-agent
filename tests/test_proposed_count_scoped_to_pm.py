"""Regression: proposed count must come from specialist_evidence, not trades table."""

from __future__ import annotations

import json
from datetime import datetime

from src.storage.db import Database


def test_proposed_count_from_specialist_evidence_not_trades(tmp_path):
    """proposed count comes from specialist_evidence proposed_order rows.
    
    When PM produces valid proposed_order rows, count them.
    When PM runs but produces no valid decision, count is 0, even if trades exist.
    """
    from ops.rehearsal.report import collect

    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    db.initialize()

    test_run_id = "test-run-regression"

    # PM entered but produced no valid decision (common failure mode)
    db.conn.execute(
        "INSERT INTO agent_logs (run_id, agent_name, timestamp, status) VALUES (?, ?, ?, ?)",
        (test_run_id, "portfolio_manager", datetime.now().isoformat(), "success"),
    )
    db.conn.execute(
        "INSERT INTO agent_logs (run_id, agent_name, timestamp, status) VALUES (?, ?, ?, ?)",
        (test_run_id, "tech_analyst", datetime.now().isoformat(), "success"),
    )

    # A BUY trade exists (from emergency liquidation, not PM)
    db.conn.execute(
        "INSERT INTO trades (run_id, symbol, action, qty, price, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (test_run_id, "AAPL", "BUY", 10, 150.0, datetime.now().isoformat()),
    )

    # PM's decision_error row (no valid proposed_order rows)
    db.conn.execute(
        "INSERT INTO specialist_evidence (run_id, agent_name, kind, scope, symbol, evidence_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (test_run_id, "portfolio_manager", "decision_error", "symbol", None, json.dumps({"error": "invalid"})),
    )
    db.conn.commit()

    class MockLibrary:
        findings = []
        matches = []

    class MockTradingStub:
        submitted = []

    report = collect(
        session="morning",
        rehearsed_date="2026-09-13",
        run_id=test_run_id,
        source_run_id=None,
        result={"status": "pm_agent_failure"},
        db_path=str(db_path),
        library=MockLibrary(),
        trading_stub=MockTradingStub(),
        isolation_checks=[],
        unavailable=[],
        network_attempts=[],
        notes=[],
        fill_model="immediate",
        duration_s=1.0,
    )

    # proposed=0: PM produced no proposed_order rows (this is the fix)
    assert report.proposed == 0
    # candidates=1: the BUY trade still counts
    assert report.candidates == 1
