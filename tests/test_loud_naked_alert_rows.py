"""A failed naked-position alert logs a traceback and, given the pipeline, leaves a counted row."""

from types import SimpleNamespace
from unittest.mock import patch

from src.storage.db import Database
from src.trader_feed.naked import send_naked_position_alert


def _db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    return db


def _rows(db):
    return [r[0] for r in db.conn.execute("SELECT kind FROM reconciliation_runs WHERE agreed = 0").fetchall()]


def test_alert_failure_with_owner_is_recorded(tmp_path, caplog):
    db = _db(tmp_path)
    with patch("src.trader_feed.naked.naked_position_alert", side_effect=RuntimeError("boom")):
        sent = send_naked_position_alert(None, {}, owner=SimpleNamespace(db=db))
    assert sent is False
    assert _rows(db) == ["guarded:trader_feed.naked_position_alert"]
    assert any(r.exc_info for r in caplog.records)


def test_alert_failure_without_owner_is_loud_and_returns_false(caplog):
    with patch("src.trader_feed.naked.naked_position_alert", side_effect=RuntimeError("boom")):
        assert send_naked_position_alert(None, {}) is False
    assert any(r.exc_info for r in caplog.records)
