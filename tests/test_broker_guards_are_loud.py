"""The execution broker's broad catch-alls must be LOUD, not bland.

The defect this file exists to stop: an argument passed twice raised a
TypeError, a broad `except Exception` swallowed it as a one-line log with no
traceback, the record was never written, and nothing complained. An audit then
counted 98 such handlers on the money-path files, none logging a traceback.

These tests pin the three states apart on the broker itself — never reached,
ran clean, ran and swallowed — because a counter that only fires on failure
cannot tell a site that worked from a site nothing ever called.
"""

import logging

from src.execution.broker import AlpacaBroker
from src.sentinel.guarded import (
    attach_reconciliation_db,
    record_guarded_pass,
)
from src.sentinel.reconciliation import AGREED, DISAGREED, NOT_RUN, ReconciliationLog
from src.storage.db import Database


def _broker(tmp_path):
    """A real AlpacaBroker object with no network client, lent a real ledger."""
    broker = AlpacaBroker.__new__(AlpacaBroker)
    db = Database(str(tmp_path / "broker_guards.db"))
    db.initialize()
    attach_reconciliation_db(broker, lambda: db.conn)
    return broker, db


def _status(db, where: str) -> str:
    return ReconciliationLog(conn=db.conn).status(kind=f"guarded:execution.{where}")


def _detail(db, where: str) -> str:
    row = ReconciliationLog(conn=db.conn).latest(kind=f"guarded:execution.{where}")
    return "" if row is None else row["detail"]


def test_a_swallowed_programming_error_in_the_broker_is_loud(tmp_path, caplog):
    broker, db = _broker(tmp_path)
    assert _status(db, "broker_parts.cancel_stray_protective_stops.list") == NOT_RUN

    def _duplicate_argument(symbol, side=None):
        return dict(a=1, **{"a": 2})  # a real duplicate-argument TypeError

    broker.snapshot_protective_stops = _duplicate_argument
    with caplog.at_level(logging.ERROR):
        assert broker.cancel_stray_protective_stops("AAPL", side="sell") == 0

    loud = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert loud, "a swallowed programming error must log at ERROR"
    assert any(r.exc_info is not None for r in loud), "the full traceback must be attached"
    assert _status(db, "broker_parts.cancel_stray_protective_stops.list") == DISAGREED
    assert "TypeError" in _detail(db, "broker_parts.cancel_stray_protective_stops.list")
    assert "AAPL" in _detail(db, "broker_parts.cancel_stray_protective_stops.list")


def test_a_clean_broker_pass_writes_its_own_row(tmp_path):
    broker, db = _broker(tmp_path)
    assert _status(db, "broker_parts.cancel_stray_protective_stops.list") == NOT_RUN
    broker.snapshot_protective_stops = lambda symbol, side=None: (True, [])
    assert broker.cancel_stray_protective_stops("AAPL", side="sell") == 0
    assert _status(db, "broker_parts.cancel_stray_protective_stops.list") == AGREED, (
        "a clean pass must be distinguishable from a site never reached"
    )


def test_a_broker_without_a_lent_ledger_still_logs_the_traceback(caplog):
    broker = AlpacaBroker.__new__(AlpacaBroker)
    with caplog.at_level(logging.ERROR):
        try:
            dict(a=1, **{"a": 2})
        except TypeError as exc:
            record_guarded_pass(broker, "unit.demo", exc)
    loud = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert loud and loud[0].exc_info is not None


def test_the_observer_never_breaks_the_money_path(tmp_path, caplog):
    """A ledger that raises on read must not abort the handler it observes."""
    broker = AlpacaBroker.__new__(AlpacaBroker)

    def _explode():
        raise RuntimeError("ledger is locked")

    attach_reconciliation_db(broker, _explode)
    with caplog.at_level(logging.ERROR):
        record_guarded_pass(broker, "unit.demo")  # must not raise
