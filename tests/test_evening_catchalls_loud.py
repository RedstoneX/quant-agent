"""The evening session's catch-alls log a traceback and count a row."""

import logging
import sqlite3
from types import SimpleNamespace

from src.sessions.evening_record import record_evening_pass, run_evening_housekeeping


def _session():
    conn = sqlite3.connect(":memory:")
    from src.storage.schema.sentinel_tables import ensure_sentinel_tables

    ensure_sentinel_tables(conn=conn)
    return SimpleNamespace(_db=SimpleNamespace(conn=conn))


def _rows(s):
    try:
        return s._db.conn.execute("SELECT kind, agreed FROM reconciliation_runs").fetchall()
    except sqlite3.OperationalError:
        return []


def test_swallowed_fault_logs_traceback_and_counts(caplog):
    s = _session()
    with caplog.at_level(logging.ERROR):
        try:
            raise TypeError("boom")
        except Exception as e:
            record_evening_pass(s, "x", e)
    assert any(r.exc_info for r in caplog.records)
    assert _rows(s) == [("guarded:evening.x", 0)]


def test_clean_pass_writes_its_own_agreed_row():
    s = _session()
    record_evening_pass(s, "x")
    assert _rows(s) == [("guarded:evening.x", 1)]


def test_unreached_site_writes_no_row():
    assert _rows(_session()) == []


def test_no_ledger_handle_still_logs_and_does_not_raise(caplog):
    with caplog.at_level(logging.ERROR):
        record_evening_pass(SimpleNamespace(), "x", ValueError("v"))
    assert any(r.exc_info for r in caplog.records)


def test_housekeeping_fault_is_loud_and_other_prunes_still_run(caplog):
    s = _session()
    ok = lambda **k: 0

    def bad(**k):
        raise RuntimeError("locked")

    for n in ("prune_trades", "prune_specialist_evidence", "prune_pending_protection_restores", "prune_pending_repegs"):
        setattr(s._db, n, ok)
    s._db.prune_agent_logs = bad
    s._news_store = SimpleNamespace(prune=ok)
    s._earnings_provider = SimpleNamespace(prune=ok)
    with caplog.at_level(logging.ERROR):
        run_evening_housekeeping(s)
    rows = dict(_rows(s))
    assert rows["guarded:evening.prune_agent_logs"] == 0
    assert rows["guarded:evening.prune_trades"] == 1
    assert rows["guarded:evening.prune_earnings_files"] == 1
    assert any(r.exc_info for r in caplog.records)
