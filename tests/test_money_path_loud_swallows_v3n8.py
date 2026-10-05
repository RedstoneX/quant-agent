"""owner_alerts catch-alls: traceback at ERROR AND a counted row, behaviour unchanged."""
from __future__ import annotations

import logging
import sqlite3
from types import SimpleNamespace

from src.protection.owner_alerts import OwnerAlerts


def _ledger():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE reconciliation_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ran_at TEXT NOT NULL DEFAULT (datetime('now')), kind TEXT NOT NULL,"
        " agreed INTEGER NOT NULL, detail TEXT, run_id TEXT)")
    return conn


class _Broker:
    def __init__(self, conn):
        self._recon_db = SimpleNamespace(conn=conn)

    def snapshot_protective_stops(self, symbol, side="sell"):
        raise RuntimeError("stops down")


def _alerts(broker):
    return OwnerAlerts(broker=broker, still_uncovered=None, alert_owner_no_stop=None, format_qty=None)


def test_still_uncovered_reread_failure_is_loud_and_counted(caplog):
    conn = _ledger()
    with caplog.at_level(logging.ERROR):
        assert OwnerAlerts._still_uncovered(_alerts(_Broker(conn)), {"symbol": "AAA", "held_qty": 1}) is True
    assert any(r.exc_info for r in caplog.records)
    rows = conn.execute("SELECT kind, agreed FROM reconciliation_runs").fetchall()
    assert rows == [("guarded:broker.owner_alerts.still_uncovered_reread", 0)]
