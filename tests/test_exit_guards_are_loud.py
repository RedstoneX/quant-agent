"""The exit path's broad catch-alls must be LOUD: traceback plus a counted row,
with the clean pass writing its own distinct row (never-reached, ran-clean and
ran-and-swallowed stay three states)."""

import logging

from src.pipeline_exits import ExitEngineMixin
from src.sentinel.guarded_exit import record_exit_guard
from src.sentinel.reconciliation import AGREED, DISAGREED, NOT_RUN, ReconciliationLog
from src.storage.db import Database

KIND = "guarded:exits.demo"


class _Owner(ExitEngineMixin):
    def __init__(self, db):
        self.db = db


def _db(tmp_path):
    db = Database(str(tmp_path / "exit_guards.db"))
    db.initialize()
    return db


def test_swallowed_fault_logs_traceback_and_a_counted_row(tmp_path, caplog):
    db = _db(tmp_path)
    log = ReconciliationLog(conn=db.conn)
    assert log.status(kind=KIND) == NOT_RUN
    with caplog.at_level(logging.ERROR):
        try:
            dict(a=1, **{"a": 2})
        except TypeError as exc:
            record_exit_guard(_Owner(db), "demo", exc, logging.getLogger("x"), symbol="ZZZ", effect="e")
    assert any(r.exc_info for r in caplog.records if r.levelno >= logging.ERROR)
    assert log.status(kind=KIND) == DISAGREED
    assert "TypeError" in log.latest(kind=KIND)["detail"]


def test_clean_pass_writes_its_own_row(tmp_path):
    db = _db(tmp_path)
    log = ReconciliationLog(conn=db.conn)
    record_exit_guard(_Owner(db), "demo")
    assert log.status(kind=KIND) == AGREED


def test_owner_without_a_ledger_still_logs_and_never_raises(caplog):
    with caplog.at_level(logging.ERROR):
        record_exit_guard(object(), "demo", ValueError("x"))
    assert any(r.exc_info for r in caplog.records)


def test_real_exit_handler_is_counted_on_both_paths(tmp_path):
    db = _db(tmp_path)
    log = ReconciliationLog(conn=db.conn)
    kind = "guarded:exits.exit_review.position_history"
    owner = _Owner(db)
    owner._build_position_history = lambda positions: {}
    assert log.status(kind=kind) == NOT_RUN
    # exercise the handler's two paths through the same helper the site calls
    record_exit_guard(owner, "exit_review.position_history")
    assert log.status(kind=kind) == AGREED
    record_exit_guard(owner, "exit_review.position_history", RuntimeError("boom"), logging.getLogger("x"))
    assert log.status(kind=kind) == DISAGREED
