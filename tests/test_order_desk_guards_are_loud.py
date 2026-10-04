"""The order desk's broad catch-alls must be LOUD, not bland.

Same defect as `tests/test_broker_guards_are_loud.py` pins on the broker
itself: an argument passed twice raised a TypeError, a broad `except
Exception` recorded one bland line with no traceback, the record was never
written and nothing complained. `src/execution/broker_parts/order_desk.py`
carried 15 such handlers; 14 are now loud.

The order desk is a COLLABORATOR built per call from the broker's client, so
it can never see a ledger handle lent to the broker. The handle is lent to
the shared client at the same single wiring site instead, which is what these
tests exercise.

Three states must stay apart — never reached, ran clean, ran and swallowed —
because a counter that only fires on failure cannot tell a site that worked
from a site nothing ever called.
"""

import logging

from src.execution.broker_parts.guarded import attach_reconciliation_db
from src.execution.broker_parts.order_desk import OrderDesk
from src.sentinel.reconciliation import AGREED, DISAGREED, NOT_RUN, ReconciliationLog
from src.storage.db import Database


class _Client:
    """A trading client stand-in; `get_order_by_id` is set per test."""

    def __init__(self, get_order_by_id):
        self.get_order_by_id = get_order_by_id


def _desk(tmp_path, get_order_by_id):
    db = Database(str(tmp_path / "order_desk_guards.db"))
    db.initialize()
    client = _Client(get_order_by_id)
    attach_reconciliation_db(client, lambda: db.conn)
    desk = OrderDesk(
        client=client,
        kill_switch_active=lambda: False,
        kill_switch_path="/nonexistent",
        wait_for_order_status=lambda *a, **k: None,
        wait_for_order_status_via_stream=lambda *a, **k: None,
        get_latest_price=lambda symbol: None,
        order_terminal_states={"filled", "canceled"},
        order_replaceable_states={"new"},
        max_replacement_hops=3,
    )
    return desk, db


def _status(db, where: str) -> str:
    return ReconciliationLog(conn=db.conn).status(kind=f"guarded:broker.order_desk.{where}")


def _detail(db, where: str) -> str:
    row = ReconciliationLog(conn=db.conn).latest(kind=f"guarded:broker.order_desk.{where}")
    return "" if row is None else row["detail"]


def test_a_swallowed_programming_error_in_the_order_desk_is_loud(tmp_path, caplog):
    def _duplicate_argument(order_id):
        return dict(a=1, **{"a": 2})          # a real duplicate-argument TypeError

    desk, db = _desk(tmp_path, _duplicate_argument)
    assert _status(db, "get_order_fill_info") == NOT_RUN

    with caplog.at_level(logging.ERROR):
        assert desk.get_order_fill_info("ORD-1") is None

    loud = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert loud, "a swallowed programming error must log at ERROR"
    assert any(r.exc_info is not None for r in loud), "the full traceback must be attached"
    assert _status(db, "get_order_fill_info") == DISAGREED
    assert "TypeError" in _detail(db, "get_order_fill_info")
    assert "ORD-1" in _detail(db, "get_order_fill_info")


def test_a_clean_order_desk_pass_writes_its_own_row(tmp_path):
    class _Order:
        status = "filled"
        filled_qty = 1
        filled_avg_price = 10.0
        filled_at = None
        order_type = None
        qty = 1

    desk, db = _desk(tmp_path, lambda order_id: _Order())
    assert _status(db, "get_order_fill_info") == NOT_RUN
    assert desk.get_order_fill_info("ORD-1") is not None
    assert _status(db, "get_order_fill_info") == AGREED, (
        "a clean pass must be distinguishable from a site never reached"
    )


def test_an_order_desk_without_a_lent_ledger_still_logs_the_traceback(caplog):
    """An isolated construction lends nothing; the traceback must survive that."""
    def _duplicate_argument(order_id):
        return dict(a=1, **{"a": 2})

    client = _Client(_duplicate_argument)          # no attach_reconciliation_db
    desk = OrderDesk(
        client=client,
        kill_switch_active=lambda: False,
        kill_switch_path="/nonexistent",
        wait_for_order_status=lambda *a, **k: None,
        wait_for_order_status_via_stream=lambda *a, **k: None,
        get_latest_price=lambda symbol: None,
        order_terminal_states={"filled"},
        order_replaceable_states={"new"},
        max_replacement_hops=3,
    )
    with caplog.at_level(logging.ERROR):
        assert desk.get_order_fill_info("ORD-1") is None
    loud = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert loud and any(r.exc_info is not None for r in loud)


def test_the_observer_never_breaks_the_order_desk(tmp_path, caplog):
    """A ledger that raises on read must not abort the handler it observes."""
    def _duplicate_argument(order_id):
        return dict(a=1, **{"a": 2})

    def _explode():
        raise RuntimeError("ledger is locked")

    client = _Client(_duplicate_argument)
    attach_reconciliation_db(client, _explode)
    desk = OrderDesk(
        client=client,
        kill_switch_active=lambda: False,
        kill_switch_path="/nonexistent",
        wait_for_order_status=lambda *a, **k: None,
        wait_for_order_status_via_stream=lambda *a, **k: None,
        get_latest_price=lambda symbol: None,
        order_terminal_states={"filled"},
        order_replaceable_states={"new"},
        max_replacement_hops=3,
    )
    with caplog.at_level(logging.ERROR):
        assert desk.get_order_fill_info("ORD-1") is None      # must not raise
