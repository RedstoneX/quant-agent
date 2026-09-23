"""The evening report survives its own run and can be re-rendered.

`run_evening` used to compute the evening message's inputs from live
broker state, hand them to the notifier and drop them: only daily_pnl and
insights reached disk, so last night's report could not be re-read
without paying for a fresh pipeline run.

Three properties, which is the whole feature:

  1. what the run produced is what comes back out (round trip), and the
     re-rendered message is the one the live formatter would have built;
  2. re-rendering touches neither the broker nor a model;
  3. a missing or partial stored row says so in words — it is never
     filled with a zero, a default or a placeholder.
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import trader_feed  # noqa: E402
from src.storage.db import Database  # noqa: E402

# A realistic evening result dict: every field the evening formatter reads.
_PAYLOAD = {
    "status": "analyzed",
    "run_id": "evening_20260917_200100",
    "total_value": 101_234.56,
    "daily_pnl": 1_234.56,
    "daily_return_pct": 1.23,
    "equity_close": 101_000.00,
    "pnl_4pm": 1_000.00,
    "pnl_4pm_pct": 1.0,
    "risk_capital_dollars": 4_500.00,
    "total_pnl": 5_678.90,
    "total_return_pct": 5.9,
    "total_pnl_since": "2026-09-02",
    "missing_sessions": [],
    "stop_coverage_gaps": [],
    "stop_proximity": [
        {"symbol": "CRM", "status": "near", "price": 250.0,
         "stop": 245.0, "gap": 5.0, "atr": 7.5},
    ],
    "earnings_proximity": [
        {"symbol": "CRM", "sessions_away": 1, "status": "ok"},
    ],
    "auto_meta": {"status": "skipped"},
    "analysis": {
        "risk_rating": "moderate",
        "tomorrow_bias": "bullish",
        "tomorrow_conviction": "medium",
        "tomorrow_outlook": "Breadth improving; no change planned.",
        "daily_summary": "Quiet session, one add.",
        "tomorrow_key_risks": ["CPI print"],
        "suggested_actions": [],
    },
}

_POSITION_ROW = {
    "symbol": "CRM", "qty": 10.0, "avg_entry": 240.0,
    "current_price": 250.0, "market_value": 2_500.0,
    "unrealized_pnl": 100.0,
}


def _db(tmp_path) -> Database:
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    return db


def _store(tmp_path, payload=_PAYLOAD, positions=(_POSITION_ROW,)) -> Database:
    db = _db(tmp_path)
    for row in positions:
        db.conn.execute(
            "INSERT INTO positions (symbol, qty, avg_entry, current_price, "
            "market_value, unrealized_pnl) VALUES (?, ?, ?, ?, ?, ?)",
            (row["symbol"], row["qty"], row["avg_entry"], row["current_price"],
             row["market_value"], row["unrealized_pnl"]),
        )
    db.conn.commit()
    db.save_evening_report(
        date="2026-09-17", run_id=payload.get("run_id"), payload=payload,
    )
    return db


def test_stored_evening_round_trips_and_rerenders(tmp_path, monkeypatch):
    db = _store(tmp_path)

    record = db.get_evening_report("2026-09-17")
    assert record is not None
    assert record["run_id"] == _PAYLOAD["run_id"]
    # Verbatim: every field the formatter reads comes back unchanged.
    assert record["payload"] == _PAYLOAD
    # ...and the book as it stood that night, not as it stands now.
    assert record["positions"] == [_POSITION_ROW]

    # The read-only path the on-demand command uses sees the same thing.
    ro = trader_feed.read_stored_evening("2026-09-17", db_path=db.db_path)
    assert ro == record
    assert trader_feed.read_stored_evening(db_path=db.db_path) == record

    # Re-rendering must produce the live formatter's own message. Point the
    # formatter's forensic DB read at an empty file so it cannot supply the
    # book from the current positions table — the stored snapshot must.
    empty = tmp_path / "empty.db"
    Database(str(empty)).initialize()
    monkeypatch.setattr(trader_feed, "_DB_PATH", str(empty))

    message = trader_feed.render_stored_evening(record)
    expected_body = trader_feed.format_session_result(
        "evening", {**_PAYLOAD, "_positions": [_POSITION_ROW]}, 0.0,
    )
    assert expected_body in message
    assert "STORED EVENING REPORT · 2026-09-17" in message
    # No run identifier, here or anywhere else (owner review 2026-09-18).
    assert _PAYLOAD["run_id"] not in message
    # Content that only exists because the inputs were persisted.
    assert "CRM" in message
    assert "$5,678.90" in message
    assert "NOT AVAILABLE" not in message


def test_partial_stored_row_says_unavailable_and_invents_nothing(tmp_path):
    # A row with no book snapshot at all (a legacy/partially-written row).
    # NOT the same as a recorded empty book, which `save_evening_report`
    # stores as [] and which renders as the flat book it actually was.
    db = _db(tmp_path)
    db.conn.execute(
        "INSERT INTO evening_reports (date, run_id, payload_json, "
        "positions_json) VALUES (?, ?, ?, NULL)",
        ("2026-09-16", "evening_partial",
         '{"status": "analyzed", "run_id": "evening_partial"}'),
    )
    db.conn.commit()

    record = db.get_evening_report("2026-09-16")
    message = trader_feed.render_stored_evening(record)

    assert "NOT AVAILABLE" in message
    for words in (
        "the book as it stood that night",
        "the stop-coverage audit result",
        "which holdings were sitting near their stops",
        "which holdings had earnings due",
    ):
        assert words in message
    # No figure is invented for a P&L the run never recorded.
    assert "not available" in message
    assert "$0.00" not in message
    assert "0.00%" not in message


def test_absent_report_is_absent(tmp_path):
    db = _db(tmp_path)
    assert db.get_evening_report("2026-09-15") is None
    assert db.get_evening_report() is None
    assert trader_feed.read_stored_evening("2026-09-15", db_path=db.db_path) is None
    # An unreadable row is an absent report, never a partial render.
    db.conn.execute(
        "INSERT INTO evening_reports (date, run_id, payload_json) "
        "VALUES (?, ?, ?)", ("2026-09-15", "r", "{not json"),
    )
    db.conn.commit()
    assert db.get_evening_report("2026-09-15") is None
    assert trader_feed.read_stored_evening("2026-09-15", db_path=db.db_path) is None
    with pytest.raises(ValueError):
        trader_feed.render_stored_evening({"date": "2026-09-15", "payload": None})


def test_same_day_rerun_replaces_the_row(tmp_path):
    db = _store(tmp_path)
    second = {**_PAYLOAD, "run_id": "evening_rerun", "daily_pnl": -42.0}
    db.save_evening_report(date="2026-09-17", run_id="evening_rerun",
                           payload=second)
    record = db.get_evening_report("2026-09-17")
    assert record["run_id"] == "evening_rerun"
    assert record["payload"]["daily_pnl"] == -42.0
    rows = db.conn.execute(
        "SELECT COUNT(*) FROM evening_reports WHERE date = '2026-09-17'"
    ).fetchone()[0]
    assert rows == 1
