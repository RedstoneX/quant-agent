"""Cash-read and stop-placement catch-alls must be loud: traceback + counted row.

Three states stay apart: never reached (no row), ran clean (agreed row),
ran and swallowed (traceback plus disagreed row).
"""
import logging
from types import SimpleNamespace

from src.execution.broker_parts.account_reads import AccountReads
from src.execution.broker_parts.stop_place import StopPlacer
from src.sentinel.guarded import attach_reconciliation_db
from src.sentinel.reconciliation import AGREED, DISAGREED, NOT_RUN, ReconciliationLog
from src.storage.db import Database

_KIND = "guarded:broker.account_reads.margin_interest_activities"


def _broker(tmp_path, client):
    """The standalone owner the handlers live on, lent a real ledger."""
    broker = AccountReads(client=client, shortable_cache={}, fractionable_cache={},
                          trading_day_cache={}, session_open_cache={})
    db = Database(str(tmp_path / "loud.db"))
    db.initialize()
    attach_reconciliation_db(broker, lambda: db.conn)
    return broker, db


def _status(db, kind):
    return ReconciliationLog(conn=db.conn).status(kind=kind)


def test_swallowed_cash_read_defect_logs_traceback_and_counted_row(tmp_path, caplog):
    def boom(*a, **k):
        raise TypeError("programming error, not a broker outage")
    broker, db = _broker(tmp_path, SimpleNamespace(get=boom))
    assert _status(db, _KIND) == NOT_RUN
    with caplog.at_level(logging.ERROR):
        assert broker.get_margin_interest_activities() == []   # behaviour unchanged
    assert any(r.exc_info is not None for r in caplog.records if r.levelno >= logging.ERROR)
    assert _status(db, _KIND) == DISAGREED
    assert "TypeError" in ReconciliationLog(conn=db.conn).latest(kind=_KIND)["detail"]


def test_clean_cash_read_writes_its_own_row(tmp_path):
    broker, db = _broker(tmp_path, SimpleNamespace(get=lambda *a, **k: []))
    assert _status(db, _KIND) == NOT_RUN
    assert broker.get_margin_interest_activities() == []
    assert _status(db, _KIND) == AGREED


def test_swallowed_stop_listing_defect_is_loud(tmp_path, caplog):
    broker = StopPlacer.__new__(StopPlacer)
    db = Database(str(tmp_path / "loud2.db"))
    db.initialize()
    attach_reconciliation_db(broker, lambda: db.conn)
    def boom(symbol, side=None):
        raise TypeError("defect")
    broker._list_open_protective_stop_orders = boom
    with caplog.at_level(logging.ERROR):
        assert broker._existing_stop_covering_qty("SYM", qty=1, side="sell", stop_price=5.0) is None
    assert any(r.exc_info is not None for r in caplog.records if r.levelno >= logging.ERROR)
    assert _status(db, "guarded:broker.stop_place.existing_stop_covering_qty.list") == DISAGREED
