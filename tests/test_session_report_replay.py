"""Morning/midday/close/intra_check survive their own run, same as evening.

2026-09-18 gap sweep: `run_morning`, `run_position_review` and
`run_intra_check` compute `leverage` (the section 11.2 gross-ceiling
snapshot) and `stop_coverage_gaps` (the broker-truth stop audit) from live
state, hand them to the notifier, and dropped them — no other durable
table held either one (unlike PM/RM reasoning and orders, already kept in
`specialist_evidence`/`trades`). This mirrors `tests/
test_evening_report_replay.py`'s three properties for the three new
tables: round trip + re-render through the live formatter, no invented
figures, and (for intra_check specifically) every tick kept rather than
only the last one.
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import trader_feed  # noqa: E402
from src.storage.db import Database  # noqa: E402

_MORNING_PAYLOAD = {
    "status": "no_trades",
    "run_id": "morning_20260917_093000",
    "data_status": {"macro": "ok", "news": "ok", "tech": "ok", "earnings": "ok"},
    "leverage": {"gross_usd": 45_000.0, "gross_x": 0.45, "ceiling_x": 2.0,
                 "rung": "standing", "alert_owner": False},
    "stop_coverage_gaps": [],
}

_MIDDAY_PAYLOAD = {
    "status": "reviewed",
    "session": "midday",
    "run_id": "midday_20260917_130000",
    "positions": 1,
    "review": {"risk_level": "normal", "overall_assessment": "steady", "actions": []},
    "orders": [],
    "stop_coverage_gaps": [],
    "target_revisions": [],
    "leverage": {"gross_usd": 45_000.0, "gross_x": 0.45, "ceiling_x": 2.0,
                 "rung": "standing", "alert_owner": False},
    "daily_pnl": 123.45, "daily_return_pct": 0.12,
    "total_pnl": 999.0, "total_return_pct": 1.0, "total_pnl_since": "2026-09-01",
}

_INTRA_PAYLOAD = {
    "status": "ok",
    "run_id": "intra_check_20260917_1200",
    "daily_pnl": 50.0, "daily_return_pct": 0.05,
    "total_pnl": 900.0, "total_return_pct": 0.9, "total_pnl_since": "2026-09-01",
    "positions": 1,
    "stop_coverage_gaps": [],
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


def _with_position(db: Database) -> Database:
    db.conn.execute(
        "INSERT INTO positions (symbol, qty, avg_entry, current_price, "
        "market_value, unrealized_pnl) VALUES (?, ?, ?, ?, ?, ?)",
        (_POSITION_ROW["symbol"], _POSITION_ROW["qty"], _POSITION_ROW["avg_entry"],
         _POSITION_ROW["current_price"], _POSITION_ROW["market_value"],
         _POSITION_ROW["unrealized_pnl"]),
    )
    db.conn.commit()
    return db


def _empty_db(tmp_path) -> str:
    """A separate, positions-less DB path to point `_read_run`'s forensic
    read at, so a passing test proves the STORED snapshot supplied the
    book — not a live query landing on the same file by coincidence.
    """
    empty = tmp_path / "empty.db"
    Database(str(empty)).initialize()
    return str(empty)


def test_morning_report_round_trips_and_rerenders(tmp_path, monkeypatch):
    db = _with_position(_db(tmp_path))
    db.save_session_report(mode="morning", date="2026-09-17",
                           run_id=_MORNING_PAYLOAD["run_id"], payload=_MORNING_PAYLOAD)

    record = db.get_session_report("morning", "2026-09-17")
    assert record["payload"] == _MORNING_PAYLOAD
    assert record["positions"] == [_POSITION_ROW]

    ro = trader_feed.read_stored_session_report("morning", "2026-09-17", db_path=db.db_path)
    assert ro == record

    monkeypatch.setattr(trader_feed, "_DB_PATH", _empty_db(tmp_path))
    message = trader_feed.render_stored_session_report("morning", record)
    expected_body = trader_feed.format_session_result(
        "morning", {**_MORNING_PAYLOAD, "_positions": [_POSITION_ROW]}, 0.0,
    )
    assert expected_body in message
    assert "STORED MORNING REPORT · 2026-09-17" in message
    assert "$2,500" in message  # the stored book's market value, not today's
    assert "NOT AVAILABLE" not in message


def test_midday_and_close_get_independent_rows_same_day(tmp_path, monkeypatch):
    db = _with_position(_db(tmp_path))
    close_payload = {**_MIDDAY_PAYLOAD, "session": "close",
                     "run_id": "close_20260917_153000", "daily_pnl": -10.0}
    db.save_session_report(mode="midday", date="2026-09-17",
                           run_id=_MIDDAY_PAYLOAD["run_id"], payload=_MIDDAY_PAYLOAD)
    db.save_session_report(mode="close", date="2026-09-17",
                           run_id=close_payload["run_id"], payload=close_payload)

    midday = db.get_session_report("midday", "2026-09-17")
    close = db.get_session_report("close", "2026-09-17")
    assert midday["payload"]["daily_pnl"] == 123.45
    assert close["payload"]["daily_pnl"] == -10.0

    monkeypatch.setattr(trader_feed, "_DB_PATH", _empty_db(tmp_path))
    message = trader_feed.render_stored_session_report("midday", midday)
    assert "STORED MIDDAY REVIEW REPORT · 2026-09-17" in message
    assert "1 position(s) held now" in message
    assert "NOT AVAILABLE" not in message


def test_intra_check_keeps_every_tick_not_just_the_last(tmp_path):
    db = _with_position(_db(tmp_path))
    first = {**_INTRA_PAYLOAD, "run_id": "intra_check_20260917_1000"}
    second = {**_INTRA_PAYLOAD, "run_id": "intra_check_20260917_1200",
              "daily_pnl": -75.0}
    db.save_intra_check_report(run_id=first["run_id"], date="2026-09-17", payload=first)
    db.save_intra_check_report(run_id=second["run_id"], date="2026-09-17", payload=second)

    rows = db.conn.execute(
        "SELECT COUNT(*) FROM intra_check_reports WHERE date = '2026-09-17'"
    ).fetchone()[0]
    assert rows == 2, "each tick must be its own row, not one date-keyed overwrite"

    assert db.get_intra_check_report(run_id=first["run_id"])["payload"]["daily_pnl"] == 50.0
    latest = db.get_intra_check_report(date="2026-09-17")
    assert latest["payload"]["daily_pnl"] == -75.0


def test_intra_check_renders_without_invented_figures(tmp_path, monkeypatch):
    db = _with_position(_db(tmp_path))
    db.save_intra_check_report(run_id=_INTRA_PAYLOAD["run_id"], date="2026-09-17",
                               payload=_INTRA_PAYLOAD)
    record = db.get_intra_check_report(run_id=_INTRA_PAYLOAD["run_id"])

    ro = trader_feed.read_stored_intra_check(run_id=_INTRA_PAYLOAD["run_id"],
                                             db_path=db.db_path)
    assert ro == record

    monkeypatch.setattr(trader_feed, "_DB_PATH", _empty_db(tmp_path))
    message = trader_feed.render_stored_intra_check(record)
    assert "STORED HALF-HOURLY CHECK · 2026-09-17" in message
    assert "CRM" not in message  # tick renderer reports a count, not names
    assert "Positions held: 1" in message
    assert "NOT AVAILABLE" not in message


def test_absent_session_report_is_absent(tmp_path):
    db = _db(tmp_path)
    assert db.get_session_report("morning", "2026-09-16") is None
    assert trader_feed.read_stored_session_report(
        "morning", "2026-09-16", db_path=db.db_path,
    ) is None
    assert db.get_intra_check_report(run_id="nope") is None
    with pytest.raises(ValueError):
        trader_feed.render_stored_session_report("morning", {"payload": None})
    with pytest.raises(ValueError):
        trader_feed.render_stored_intra_check({"payload": None})


def test_partial_session_report_says_unavailable(tmp_path):
    db = _db(tmp_path)
    partial = {"status": "no_trades", "run_id": "morning_partial"}
    db.save_session_report(mode="morning", date="2026-09-16",
                           run_id="morning_partial", payload=partial)
    # Simulate a row written before the book/leverage/coverage fields
    # existed: no positions_json at all.
    db.conn.execute(
        "UPDATE session_reports SET positions_json = NULL WHERE date = ?",
        ("2026-09-16",),
    )
    db.conn.commit()
    record = db.get_session_report("morning", "2026-09-16")
    message = trader_feed.render_stored_session_report("morning", record)
    assert "NOT AVAILABLE" in message
    assert "the book as it stood at the end of that session" in message
    assert "the stop-coverage audit result" in message
    assert "the gross-exposure ceiling snapshot" in message
    assert "$0.00" not in message
