"""`GET /analysts/target-scorecard` returns the target scorecard counts (record only)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import src.api.db_reads as db_reads
from src.api.server import app
from src.storage.db import Database

NEW = "2026-10-12 14:00:00"
OLD = "2026-10-09 14:00:00"


def _trade(db, sym, action, price, when, **kw):
    tid = db.insert_trade(
        symbol=sym, action=action, qty=10, price=price, reasoning="t", run_id="r1", fill_status="filled", **kw
    )
    db.conn.execute("UPDATE trades SET timestamp = ? WHERE id = ?", (when, tid))
    return tid


def _position(db, sym, entry, target, mfe, when, close=True):
    tid = _trade(db, sym, "BUY", entry, when, take_profit=target, entry_atr=1.0, stop_loss=entry * 0.9)
    if mfe is not None:
        db.conn.execute("UPDATE trades SET max_favourable_excursion = ? WHERE id = ?", (mfe, tid))
    if close:
        _trade(db, sym, "SELL", entry, when.replace(":00:00", ":05:00"))
    db.conn.commit()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    db = Database(str(path))
    db.initialize()
    _position(db, "AAA", 100.0, 110.0, 12.0, NEW)  # reached
    _position(db, "BBB", 100.0, 110.0, 3.0, NEW)  # not reached
    _position(db, "CCC", 100.0, 110.0, 3.0, NEW, close=False)  # open
    _position(db, "OLD", 100.0, 110.0, 12.0, OLD)  # before clean record
    db.conn.commit()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(path))
    return TestClient(app)


def test_endpoint_returns_seeded_counts(client):
    body = client.get("/analysts/target-scorecard").json()
    assert body["read_error"] is None
    assert body["closed_reached"] == 1
    assert body["closed_not_reached"] == 1
    assert body["still_open"] == 1
    assert body["excluded"]["opened_before_clean_record"] == 1
    assert body["best_move_is_lower_bound"] is True


def test_endpoint_is_read_only(client):
    assert client.post("/analysts/target-scorecard").status_code in (404, 405)
