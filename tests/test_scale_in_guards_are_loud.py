"""Scale-in catch-alls must be LOUD: never reached, ran clean, and swallowed stay apart."""

import logging

from src.execution import scale_in
from src.execution.scale_in import broker_position_qty, pending_scale_in_symbols
from src.execution.scale_in_readers import pending_scale_in_symbols_from_path
from src.sentinel.reconciliation import AGREED, DISAGREED, NOT_RUN, ReconciliationLog
from src.storage.db import Database


class _Broker:
    def __init__(self, positions=None, fail=False):
        self._positions, self._fail = positions, fail

    def get_positions(self):
        if self._fail:
            return dict(a=1, **{"a": 2})  # a real duplicate-argument TypeError
        return self._positions


def _db(tmp_path):
    db = Database(str(tmp_path / "scale_in.db"))
    db.initialize()
    return db


def _status(db, where):
    return ReconciliationLog(conn=db.conn).status(kind=f"guarded:scale_in.{where}")


def test_swallowed_fault_is_loud_and_counted(tmp_path, caplog):
    db = _db(tmp_path)
    broker = _Broker(fail=True)
    broker._recon_db = db
    assert _status(db, "broker_position_qty") == NOT_RUN
    with caplog.at_level(logging.ERROR):
        assert broker_position_qty(broker, "XYZ") is None
    assert _status(db, "broker_position_qty") == DISAGREED
    assert any(r.exc_info for r in caplog.records)


def test_clean_pass_writes_its_own_row(tmp_path):
    db = _db(tmp_path)
    broker = _Broker(positions=[])
    broker._recon_db = db
    assert broker_position_qty(broker, "XYZ") == 0
    assert _status(db, "broker_position_qty") == AGREED


def test_unreached_site_writes_no_row_and_unbound_handler_is_loud(tmp_path, caplog):
    db = _db(tmp_path)

    class _Bad:
        conn = db.conn

        def get_pending_protection_restores(self):
            raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR):
        assert pending_scale_in_symbols(_Bad()) == set()
    assert _status(db, "pending_scale_in_symbols") == DISAGREED
    assert _status(db, "drain.cancel_leftover_entry") == NOT_RUN
    assert any(r.exc_info for r in caplog.records)


def test_path_reader_without_handle_logs_traceback(tmp_path, caplog):
    with caplog.at_level(logging.ERROR):
        assert pending_scale_in_symbols_from_path(str(tmp_path / "missing" / "x.db")) == set()
    assert any(r.exc_info for r in caplog.records)
    assert scale_in.pending_scale_in_symbols_from_path is pending_scale_in_symbols_from_path
