"""Swallows made loud (traceback at ERROR) and counted, behaviour unchanged."""
from __future__ import annotations

import logging
import sqlite3

from src.execution.stop_records import _holding_is_short
from src.sentinel.guarded import NO_LEDGER, record_guarded_pass


class _Boom:
    def get_positions(self):
        raise RuntimeError("positions down")


class _Ok:
    def get_positions(self):
        return []


def test_swallow_logs_traceback_and_keeps_behaviour(caplog):
    with caplog.at_level(logging.ERROR):
        assert _holding_is_short(_Boom(), "AAA") is None
    assert any(r.exc_info for r in caplog.records)


def test_clean_pass_and_exempt_site_never_raise():
    assert _holding_is_short(_Ok(), "AAA") is not True
    record_guarded_pass(NO_LEDGER, "x.y", ValueError("v"))
    record_guarded_pass(NO_LEDGER, "x.y")


# --- end to end: a REAL ledger handle, REAL rows (nothing mocked) -----------
import sqlite3
from types import SimpleNamespace

_KIND = "guarded:execution.stop_records.holding_is_short"


def _ledger():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE reconciliation_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ran_at TEXT NOT NULL DEFAULT (datetime('now')), kind TEXT NOT NULL,"
        " agreed INTEGER NOT NULL, detail TEXT, run_id TEXT)")
    return conn


def _rows(conn):
    return conn.execute("SELECT kind, agreed, detail FROM reconciliation_runs").fetchall()


def _lent(broker, conn):
    broker._recon_db = SimpleNamespace(conn=conn)
    return broker


def test_never_reached_writes_nothing():
    conn = _ledger()
    _lent(_Boom(), conn)
    assert _rows(conn) == []


def test_failure_writes_one_disagreed_row():
    conn = _ledger()
    assert _holding_is_short(_lent(_Boom(), conn), "AAA") is None
    rows = _rows(conn)
    assert [(k, a) for k, a, _ in rows] == [(_KIND, 0)]
    assert "RuntimeError" in rows[0][2] and "positions down" in rows[0][2]


def test_clean_pass_writes_one_agreed_row():
    conn = _ledger()
    _holding_is_short(_lent(_Ok(), conn), "AAA")
    assert [(k, a) for k, a, _ in _rows(conn)] == [(_KIND, 1)]
