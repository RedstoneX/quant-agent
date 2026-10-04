"""Coverage-watchdog catch-alls log a traceback and a counted row; clean passes write their own."""
import logging
from types import SimpleNamespace

from src import coverage_watchdog as cw
from src.coverage_watchdog_records import record_watchdog_pass
from src.sentinel.reconciliation import ReconciliationLog
from src.storage.db import Database


def _db(tmp_path):
    db = Database(str(tmp_path / "w.db"))
    db.initialize()
    return db


def test_swallowed_fault_logs_traceback_and_disagreed_row(caplog, tmp_path):
    db = _db(tmp_path)
    try:
        raise TypeError("boom")
    except TypeError as exc:
        with caplog.at_level(logging.ERROR):
            record_watchdog_pass("replace.placement", exc, db=db)
    assert any(r.exc_info and r.exc_info[0] is TypeError for r in caplog.records)
    log = ReconciliationLog(conn=db.conn)
    assert log.status(kind="guarded:coverage_watchdog.replace.placement") == "disagreed"


def test_clean_pass_is_agreed_and_unreached_site_is_not_run(tmp_path):
    db = _db(tmp_path)
    record_watchdog_pass("replace.placement", db=db)
    log = ReconciliationLog(conn=db.conn)
    assert log.status(kind="guarded:coverage_watchdog.replace.placement") == "agreed"
    assert log.status(kind="guarded:coverage_watchdog.replace.reread_stops") == "not_run"


def test_unbound_handler_fault_reads_the_handled_exception_without_a_handle(caplog):
    with caplog.at_level(logging.ERROR):
        try:
            raise ValueError("x")
        except Exception:
            record_watchdog_pass("scale_in_skip.symbols", fault=True)
    assert any(r.exc_info and r.exc_info[0] is ValueError for r in caplog.records)


def test_broken_positions_read_is_loud_and_behaviour_unchanged(caplog):
    class Broker:
        def get_positions(self):
            raise RuntimeError("down")
    with caplog.at_level(logging.ERROR):
        gaps, err = cw.uncovered_positions(Broker())
    assert gaps == [] and err == "get_positions failed: down"
    assert any(r.exc_info and r.exc_info[0] is RuntimeError for r in caplog.records)


def test_reexport_and_missing_db_unchanged():
    assert cw.measured_window_bound_seconds(None) == (None, 0)
