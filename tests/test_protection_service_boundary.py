"""Boundary test for conversion step 12: `ProtectionService` is built from explicit
stand-ins and exercised without the pipeline (docs/ARCHITECTURE.md section 3,
clause 5). One stop placement, one stop amendment, one refusal.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.pipeline_protection import ProtectionService
from src.trading_calendar import et_today
from tests.fake_event_journal import InMemoryEventJournal


def _service(*, broker=None, db=None, market=None, config=None):
    return ProtectionService(
        broker=broker if broker is not None else MagicMock(name="broker"),
        db=db if db is not None else MagicMock(name="db"),
        journal=InMemoryEventJournal(), market=market, config=config,
        format_qty=lambda q: str(int(q)) if float(q).is_integer() else f"{q:.6f}",
        record_exit_refusal=MagicMock(name="record_exit_refusal"),
        sweeper=lambda: None, retired_cash_park_symbol=lambda: None,
    )


def test_stands_alone_and_keeps_the_terminal_set():
    svc = _service()
    assert "filled" in svc._TERMINAL_ORDER_STATUSES
    assert svc.journal.rows == []


def test_placement_reprotects_the_residual_after_a_partial_sell():
    broker = MagicMock(name="broker")
    broker._list_open_sell_stop_orders.return_value = []
    broker._submit_protective_stop_retrying.return_value = {
        "id": "stop-1", "status": "accepted", "covered_qty": 4.0, "uncovered_qty": 0.0,
    }
    svc = _service(broker=broker)
    ok = svc._reprotect_residual_after_partial_sell(
        "ABCD", 4.0, [{"id": "old-1", "stop_price": 95.0}, {"id": "old-2", "stop_price": 97.5}],
    )
    assert ok is True
    # Most protective of the cancelled stops for a long: the HIGHEST sell stop.
    broker._submit_protective_stop_retrying.assert_called_once_with(
        symbol="ABCD", qty=4.0, stop_price=97.5, limit_price=None, side="sell",
    )


def test_amendment_shifts_the_stop_down_by_the_dividend():
    broker = MagicMock(name="broker")
    broker.is_trading_day.return_value = True
    broker.get_current_stop_price.return_value = 90.0
    broker.shift_stops_down.return_value = {
        "status": "complete", "mode": "replace", "shifted": 1, "total": 1, "legs": [],
    }
    db = MagicMock(name="db")
    db.get_trades.return_value = []
    ex_date = et_today() + timedelta(days=1)  # tomorrow is ex-div: shift today
    market = SimpleNamespace(get_upcoming_ex_dividend=lambda sym: {"amount": 0.5, "date": ex_date})
    svc = _service(broker=broker, db=db, market=market)
    position = SimpleNamespace(symbol="ABCD", qty=10, current_price=100.0)
    svc._handle_ex_dividends([position], run_id="run-1")
    broker.shift_stops_down.assert_called_once_with("ABCD", 0.5)


def test_refusal_when_the_stop_book_is_unreadable():
    broker = MagicMock(name="broker")
    broker.snapshot_protective_stops.return_value = (False, [])
    svc = _service(broker=broker)
    assert svc._cancel_stops_with_write_ahead("ABCD", 10.0) == (False, [], None)
    assert svc._last_stop_clear_refusal == "unreadable"
    broker.cancel_snapshotted_stops.assert_not_called()
