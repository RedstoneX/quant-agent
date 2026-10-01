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


# --- the three methods the capture proof never recorded: proven directly ---

def _memory_db():
    from src.storage.db import Database
    db = Database(":memory:")
    db.initialize()
    return db


def test_repeg_drain_deletes_its_row_so_a_second_drain_does_not_repeg_again():
    db = _memory_db()
    db.insert_pending_repeg(trade_row_id=7, symbol="ABCD", old_order_id="old-1", new_order_id="new-1")
    db.repoint_trade_broker_order_id = MagicMock(wraps=db.repoint_trade_broker_order_id)
    broker = MagicMock(name="broker")
    svc = _service(broker=broker, db=db)
    assert svc._drain_pending_repegs() == 1
    assert db.get_pending_repegs() == []  # the write-ahead row is gone
    assert svc._drain_pending_repegs() == 0
    assert db.repoint_trade_broker_order_id.call_count == 1  # not re-pegged
    broker.resolve_replacement_chain.assert_not_called()  # the id was known


def test_delete_repeg_row_removes_only_that_row_and_never_raises():
    db = _memory_db()
    keep = db.insert_pending_repeg(trade_row_id=1, symbol="ABCD", old_order_id="o-1", new_order_id="n-1")
    gone = db.insert_pending_repeg(trade_row_id=2, symbol="EFGH", old_order_id="o-2", new_order_id="n-2")
    svc = _service(db=db)
    svc._delete_repeg_row(gone)
    assert [r["id"] for r in db.get_pending_repegs()] == [keep]
    svc._delete_repeg_row(gone)  # already gone: a no-op, not an error
    failing = MagicMock(name="db")
    failing.delete_pending_repeg.side_effect = RuntimeError("locked")
    _service(db=failing)._delete_repeg_row(keep)  # swallowed: the drain must finish


def test_reprotect_identity_gap_is_written_through_the_exit_refusal_record():
    svc = _service()
    svc._record_reprotect_identity_gap("abcd", "two stops rest; which is live is unprovable")
    svc._record_exit_refusal.assert_called_once()
    kw = svc._record_exit_refusal.call_args.kwargs
    assert kw["symbol"] == "abcd" and kw["action"] == "REPROTECT"
    assert kw["code"] == "reprotect_broker_state_unprovable" and kw["dropped"] is False
    assert kw["detail"] == "two stops rest; which is live is unprovable"
    assert kw["run_id"].startswith("reprotect-ABCD-") and kw["layer"] == "execution"
    svc._record_exit_refusal.side_effect = RuntimeError("disk full")
    svc._record_reprotect_identity_gap("abcd", "x")  # forensic write never unwinds the SELL


def test_pending_acceptance_alert_pages_once_and_keeps_the_no_stop_claim_free(tmp_path, monkeypatch):
    from unittest.mock import patch
    from src import coverage_watchdog
    from src.coverage_watchdog import claim_typed_alert

    monkeypatch.setattr(coverage_watchdog, "STATE_PATH", tmp_path / "heartbeat.json")
    with patch("src.notifier.send_owner_alert", return_value=True) as send:
        ProtectionService._alert_owner_stop_pending_acceptance("abcd", "3", "stop-9", "accepted", 97.5)
        ProtectionService._alert_owner_stop_pending_acceptance("abcd", "3", "stop-9", "accepted", 97.5)
        ProtectionService._alert_owner_stop_pending_acceptance("", "3", "stop-9", "accepted", 97.5)
    send.assert_called_once()
    text = send.call_args.args[0]
    assert "NOT YET WORKING" in text and "ABCD" in text and "stop-9" in text and "$97.50" in text
    assert "NO STOP AT ALL" not in text and send.call_args.kwargs["symbols"] == ["ABCD"]
    # The weaker page must never consume the claim that belongs to a naked position.
    assert claim_typed_alert("no_stop_at_all", ["ABCD"]) == ["ABCD"]
