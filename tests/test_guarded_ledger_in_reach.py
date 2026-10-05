"""A catch-all with no owner object still writes its counted row.

Three agents converting money-path handlers each hit a site with no handle in
reach -- a module-level function handed only a broker, only a database, or a
bare trading client -- and each invented a different workaround. The settled
answer lives in `src/sentinel/guarded_reach.py`: pass whatever is already in
scope and the ledger is found on it at call time, storing nothing. These tests
pin that, and pin the three states apart at a module-level site.
"""
import logging
import sqlite3
from types import SimpleNamespace

import pytest

from src.execution import pending_stop_drain as drain
from src.sentinel.cancel_attempts import CancelRecordingClient
from src.sentinel.guarded import NO_LEDGER, attach_reconciliation_db, record_guarded_pass
from src.sentinel.guarded_reach import ledger_in_reach
from src.sentinel.reconciliation import AGREED, DISAGREED, NOT_RUN, ReconciliationLog
from src.storage.db import Database


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "reach.db"))
    d.initialize()
    return d


def _status(db, where):
    return ReconciliationLog(conn=db.conn).status(kind=f"guarded:broker.{where}")


def _detail(db, where):
    row = ReconciliationLog(conn=db.conn).latest(kind=f"guarded:broker.{where}")
    return "" if row is None else row["detail"]


def test_three_states_at_a_module_level_site_with_only_a_broker_in_scope(db, caplog, monkeypatch):
    """`_resting_stop_level` has no self; the ledger is reached through the broker."""
    broker = SimpleNamespace()
    attach_reconciliation_db(broker, lambda: db.conn)
    where = "pending_stop_drain.resting_stop"
    assert _status(db, where) == NOT_RUN                      # never reached: no row

    broker.get_current_stop_price = lambda symbol: 12.5
    assert drain._resting_stop_level(broker, "ZZZZ", db).price == 12.5
    assert _status(db, where) == AGREED                       # ran clean: its own row

    def _duplicate_argument(*args, **kwargs):
        return dict(a=1, **{"a": 2})                          # a real TypeError
    monkeypatch.setattr(drain, "read_stop", _duplicate_argument)
    with caplog.at_level(logging.ERROR):
        assert drain._resting_stop_level(broker, "ZZZZ", db).unreadable   # never read as "none"
    assert _status(db, where) == DISAGREED                    # ran and swallowed
    assert "TypeError" in _detail(db, where) and "ZZZZ" in _detail(db, where)
    assert any(r.exc_info is not None for r in caplog.records if r.levelno >= logging.ERROR)


def test_a_site_with_only_a_database_in_scope_reaches_the_ledger_through_it(db, caplog):
    """`drain_pending_stop_amends(broker, db)`: the db read fails, the wrapper runs clean."""
    broken = SimpleNamespace(conn=db.conn, _trades=lambda: (_ for _ in ()).throw(RuntimeError("down")))
    with caplog.at_level(logging.ERROR):
        drain.drain_safely(SimpleNamespace(), broken)        # broker carries nothing
    assert _status(db, "pending_stop_drain.db_read") == DISAGREED
    assert "RuntimeError" in _detail(db, "pending_stop_drain.db_read")
    assert _status(db, "pending_stop_drain.drain_safely") == AGREED
    assert _status(db, "pending_stop_drain.apply") == NOT_RUN


def test_a_bare_trading_client_wrapped_at_the_wiring_site_carries_the_ledger(db):
    client = CancelRecordingClient(inner=SimpleNamespace(), conn_getter=lambda: db.conn)
    assert isinstance(ledger_in_reach(client).conn, sqlite3.Connection)
    broker_shaped = SimpleNamespace(client=client)
    assert isinstance(ledger_in_reach(broker_shaped).conn, sqlite3.Connection)
    assert isinstance(ledger_in_reach(SimpleNamespace(db=db)).conn, sqlite3.Connection)
    assert ledger_in_reach(None, SimpleNamespace(), db) is db   # first carrier wins


def test_the_walk_stores_nothing_on_what_it_walks():
    bare = SimpleNamespace(client=SimpleNamespace())
    before = (dict(vars(bare)), dict(vars(bare.client)))
    assert ledger_in_reach(bare) is None
    assert (dict(vars(bare)), dict(vars(bare.client))) == before


def test_a_declared_exemption_logs_the_traceback_and_writes_no_row(db, caplog):
    with caplog.at_level(logging.ERROR):
        record_guarded_pass(NO_LEDGER, "unit.exempt", ValueError("x"), context={"k": 1})
    assert any(r.exc_info is not None for r in caplog.records if r.levelno >= logging.ERROR)
    assert _status(db, "unit.exempt") == NOT_RUN
    record_guarded_pass(NO_LEDGER, "unit.exempt")              # must not raise
