"""Conviction-ledger backfill: recovery numbers + idempotency.

Split out of tests/test_risk_outcome_logging.py (its section 8, moved
unchanged) so that file stays under its size baseline.
"""

from src.storage.db import Database


def _db(tmp_path, name="test.db") -> Database:
    db = Database(str(tmp_path / name))
    db.initialize()
    return db


# ===========================================================================
# 8. Backfill — recovery numbers + idempotency
# ===========================================================================


def _pm_full_response(targets: list[dict]) -> str:
    import json

    return json.dumps(
        {
            "reasoning_chain": {
                "macro_filter": "x",
                "news_check": "x",
                "earnings_check": "x",
                "signal_conflicts": "x",
                "sizing_logic": "x",
                "portfolio_balance": "x",
                "cash_target": "x",
            },
            "targets": targets,
            "portfolio_view": "test",
        }
    )


def test_backfill_recovers_conviction_risk_and_model_for_entries(tmp_path):
    db = _db(tmp_path)
    db.insert_agent_log(
        agent_name="portfolio_manager",
        run_id="r1",
        input_summary="x",
        output_summary="x",
        full_response=_pm_full_response(
            [
                {
                    "symbol": "NVDA",
                    "target_weight_pct": 8.0,
                    "conviction": "high",
                    "thesis": "x",
                    "thesis_invalid_if": "",
                    "catalyst": "",
                },
            ]
        ),
        model="google/gemini-3.5-flash-lite",
        tokens_used=100,
        decision_id="r1-dec-1",
    )
    # Entry row predates the ledger columns — written with no conviction/
    # risk/model, exactly like real historical rows.
    row_id = db.insert_trade(
        symbol="NVDA",
        action="BUY",
        qty=10,
        price=100.0,
        reasoning="t",
        run_id="r1",
        decision_id="r1-dec-1",
        fill_status="filled",
        stop_loss=90.0,
    )

    result = db.backfill_conviction_ledger(dry_run=True)
    assert result["entry_recovered"] == 1
    assert result["allocated_risk_pct_recoverable"] == 0

    result_applied = db.backfill_conviction_ledger(dry_run=False)
    assert result_applied["entry_recovered"] == 1
    row = dict(db.conn.execute("SELECT * FROM trades WHERE id = ?", (row_id,)).fetchone())
    assert row["conviction"] == "high"
    assert row["decision_model"] == "google/gemini-3.5-flash-lite"
    assert row["allocated_risk_pct"] is None  # never backfilled — see docstring
    db.close()


def test_backfill_labels_exit_rows_honestly(tmp_path):
    db = _db(tmp_path)
    # Simulate a pre-migration exit row: decision_id_status not yet derived.
    # (insert_trade always derives it now, so hand-write via raw SQL to
    # reproduce a genuinely pre-migration row.)
    db.conn.execute(
        "INSERT INTO trades (symbol, action, qty, price, reasoning, run_id, "
        "fill_status, decision_id) VALUES ('AAPL', 'SELL', 5, 100, 'x', 'r1', "
        "'filled', NULL)"
    )
    db.conn.execute(
        "INSERT INTO trades (symbol, action, qty, price, reasoning, run_id, "
        "fill_status, decision_id) VALUES ('MSFT', 'SELL', 5, 100, 'x', 'r1', "
        "'filled', 'r1-dec-9')"
    )
    db.conn.commit()

    result = db.backfill_conviction_ledger(dry_run=False)
    assert result["exit_no_originating_decision"] == 1
    assert result["exit_linked"] == 1

    rows = {
        r["symbol"]: r["decision_id_status"]
        for r in db.conn.execute("SELECT symbol, decision_id_status FROM trades").fetchall()
    }
    assert rows["AAPL"] == "no_originating_decision"
    assert rows["MSFT"] == "linked"
    db.close()


def test_backfill_is_idempotent(tmp_path):
    db = _db(tmp_path)
    db.insert_agent_log(
        agent_name="portfolio_manager",
        run_id="r1",
        input_summary="x",
        output_summary="x",
        full_response=_pm_full_response(
            [
                {
                    "symbol": "NVDA",
                    "target_weight_pct": 8.0,
                    "conviction": "high",
                    "thesis": "x",
                    "thesis_invalid_if": "",
                    "catalyst": "",
                },
            ]
        ),
        model="m1",
        tokens_used=100,
        decision_id="r1-dec-1",
    )
    db.insert_trade(
        symbol="NVDA",
        action="BUY",
        qty=10,
        price=100.0,
        reasoning="t",
        run_id="r1",
        decision_id="r1-dec-1",
        fill_status="filled",
        stop_loss=90.0,
    )

    first = db.backfill_conviction_ledger(dry_run=False)
    assert first["entry_recovered"] == 1
    second = db.backfill_conviction_ledger(dry_run=False)
    assert second["entry_recovered"] == 0  # nothing left to recover
    assert second["entry_rows_considered"] == 0
    db.close()


def test_backfill_reports_unrecoverable_entries_honestly(tmp_path):
    """An entry with a decision_id that matches NO agent_logs row (or whose
    PM response has no target for this symbol) must be counted as
    unrecoverable, not silently skipped or guessed."""
    db = _db(tmp_path)
    row_id = db.insert_trade(
        symbol="GHOST",
        action="BUY",
        qty=1,
        price=10.0,
        reasoning="t",
        run_id="r1",
        decision_id="r1-dec-missing",
        fill_status="filled",
        stop_loss=90.0,
    )
    result = db.backfill_conviction_ledger(dry_run=True)
    assert result["entry_unrecoverable_no_agent_log"] == 1
    assert result["entry_recovered"] == 0
    row = dict(db.conn.execute("SELECT conviction FROM trades WHERE id = ?", (row_id,)).fetchone())
    assert row["conviction"] is None
    db.close()
