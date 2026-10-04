"""A converted intraday catch-all logs a traceback and a counted row; a clean pass writes its own."""
import logging
from src.storage.db import Database
from types import SimpleNamespace

from src.sentinel.guarded_site import record_site
from src.sentinel.reconciliation import ReconciliationLog


def _owner(tmp_path):
    db = Database(str(tmp_path / "g.db"))
    db.initialize()
    return SimpleNamespace(db=db)


def test_swallowed_fault_logs_traceback_and_disagreed_row(caplog, tmp_path):
    owner = _owner(tmp_path)
    try:
        raise TypeError("boom")
    except TypeError as exc:
        with caplog.at_level(logging.ERROR):
            record_site(owner, "fill_reconcile", exc, context={"symbol": "XYZ"})
    rec = [r for r in caplog.records if r.exc_info]
    assert rec and rec[0].exc_info[0] is TypeError
    log = ReconciliationLog(conn=owner.db.conn)
    assert log.status(kind="guarded:intraday.fill_reconcile") == "disagreed"


def test_clean_pass_writes_its_own_agreed_row_and_unreached_stays_not_run(tmp_path):
    owner = _owner(tmp_path)
    record_site(owner, "fill_reconcile")
    log = ReconciliationLog(conn=owner.db.conn)
    assert log.status(kind="guarded:intraday.fill_reconcile") == "agreed"
    assert log.status(kind="guarded:intraday.stop_out_reconcile") == "not_run"


def test_no_handle_still_logs_traceback_and_does_not_raise(caplog):
    try:
        raise ValueError("x")
    except ValueError as exc:
        with caplog.at_level(logging.ERROR):
            record_site(SimpleNamespace(db=None), "bar_fetch", exc)
    assert any(r.exc_info for r in caplog.records)
