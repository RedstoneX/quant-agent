"""Reconnect-guard flag handlers: swallowed fault, clean pass, unreached site, no ledger handle."""

import logging
from types import SimpleNamespace

from src.execution.broker_parts import trade_stream_flags as tf
from src.sentinel.guarded import attach_reconciliation_db
from src.sentinel.reconciliation import AGREED, DISAGREED, NOT_RUN, ReconciliationLog
from src.storage.db import Database

WHERE = "execution.broker_parts.trade_stream.reconnect.authed_set"


class _Boom:
    def set(self):
        raise RuntimeError("boom")


class _Ok:
    def set(self):
        pass


def _owner(tmp_path):
    db = Database(str(tmp_path / "flags.db"))
    db.initialize()
    owner = SimpleNamespace()
    attach_reconciliation_db(owner, lambda: db.conn)
    return owner, db


def _status(db):
    return ReconciliationLog(conn=db.conn).status(kind=f"guarded:{WHERE}")


def test_swallowed_fault_logs_traceback_and_does_not_raise(caplog):
    with caplog.at_level(logging.ERROR):
        tf._signal_event(SimpleNamespace(_qamc_authed=_Boom()), "_qamc_authed", "set")
    assert any(r.exc_info for r in caplog.records)


def test_clean_pass_and_swallow_write_separate_rows(tmp_path):
    owner, db = _owner(tmp_path)
    tf._signal_event(SimpleNamespace(_qamc_authed=_Ok()), "_qamc_authed", "set", owner)
    assert _status(db) == AGREED
    tf._signal_event(SimpleNamespace(_qamc_authed=_Boom()), "_qamc_authed", "set", owner)
    assert _status(db) == DISAGREED


def test_unreached_site_writes_nothing(tmp_path):
    owner, db = _owner(tmp_path)
    tf._signal_event(SimpleNamespace(), "_qamc_authed", "set", owner)
    assert _status(db) == NOT_RUN


def test_missing_ledger_handle_still_loud_no_row(caplog):
    with caplog.at_level(logging.ERROR):
        tf._signal_event(SimpleNamespace(_qamc_authed=_Boom()), "_qamc_authed", "set", SimpleNamespace())
    assert any(r.exc_info for r in caplog.records)
