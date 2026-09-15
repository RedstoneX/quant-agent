"""Long scale-in path B: cancel-confirm-buy-rearm.

Owner ruling 2026-09-15. These tests pin the objections that killed silent
refuse-adds and that killed the daily-breaker's cancel-restore window:

  * cancel is confirmed via wait_for_order_terminal (trade_updates / REST),
    not assumed from cancel_order_by_id returning;
  * a partial fill of the add rearms at the broker's FULL position qty;
  * rearm failure pages the owner and does not go quiet;
  * short adds are blocked (scale-in is the long path; item 73 closed
    the missing-buy-stop repair, not short adds);
  * execution.repeg_enabled stays false.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.config import ExecutionConfig
from src.execution.scale_in import (
    WAL_SCALE_IN_SENTINEL,
    confirm_protective_cancels,
    cover_qty_for_rearm,
    drain_scale_in_row,
    most_protective_long_stop,
    pending_protection_symbols,
    prepare_long_add,
    scale_in_symbols_to_skip,
    short_add_is_blocked,
)
from src.models import PortfolioDecision, Position, ReasoningChain, TradeDecision
from src.pipeline import TradingPipeline
from src.pipeline_context import RunContext
from src.pipeline_stages import ExecutionStage, _alert_owner_protection_failed
from src.storage.db import Database


def _rc() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="m", news_check="n", earnings_check="e",
        signal_conflicts="s", sizing_logic="z", portfolio_balance="b",
        cash_target="c",
    )


def _cop_position(qty=10.0) -> Position:
    return Position(
        symbol="COP", qty=qty, avg_entry=90.0, current_price=100.0,
        market_value=qty * 100.0, unrealized_pnl=qty * 10.0, sector="Energy",
    )


def _db(tmp_path) -> Database:
    db = Database(str(tmp_path / "scale_in.db"))
    db.initialize()
    return db


def _pipeline(live_price=100.0, cash=50_000.0, positions=None):
    pipeline = MagicMock()
    held = list(positions or [])
    pipeline.broker.get_latest_price.return_value = live_price
    pipeline.broker.get_latest_quote.return_value = {
        "bid_price": round(live_price * 0.999, 4),
        "ask_price": round(live_price * 1.001, 4),
    }
    pipeline.broker.get_fractionability.return_value = {
        "fractionable": False, "reason": "test",
    }
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": cash, "portfolio_value": 100_000.0}, held, {},
    )
    pipeline.risk_engine.check_daily_loss.return_value = None
    return pipeline


def _ctx(decisions, positions=None, cash=50_000.0) -> RunContext:
    ctx = RunContext.start("morning")
    ctx.cash = cash
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = positions or []
    ctx.decision_id = "run-scale-in"
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_rc(), decisions=decisions, portfolio_view="t",
    )
    ctx.symbols_bars = {}
    return ctx


def _buy_cop() -> TradeDecision:
    return TradeDecision(
        action="BUY", symbol="COP", allocation_pct=10,
        entry_price=100.0, stop_loss=90.0, take_profit=120.0,
        reasoning="add to winner",
    )


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------

def test_repeg_enabled_stays_false():
    assert ExecutionConfig().repeg_enabled is False


def test_short_add_is_blocked_only_when_already_short():
    assert short_add_is_blocked([_cop_position(qty=-8)], "COP") is True
    assert short_add_is_blocked([_cop_position(qty=8)], "COP") is False
    assert short_add_is_blocked([], "COP") is False


def test_most_protective_long_stop_is_the_highest_trigger():
    assert most_protective_long_stop([88.0, 92.0, 90.0]) == 92.0
    assert most_protective_long_stop([]) == 0.0


def test_confirm_protective_cancels_requires_terminal_cancelled_not_assumption():
    broker = MagicMock()
    broker.wait_for_order_terminal.return_value = "canceled"
    ok, detail = confirm_protective_cancels(
        broker, [{"id": "stop-1", "qty": 10, "stop_price": 90.0}],
    )
    assert ok is True
    assert detail == ""
    broker.wait_for_order_terminal.assert_called_once_with("stop-1")


def test_confirm_protective_cancels_refuses_when_status_is_unknown():
    broker = MagicMock()
    broker.wait_for_order_terminal.return_value = None
    ok, detail = confirm_protective_cancels(
        broker, [{"id": "stop-1", "qty": 10, "stop_price": 90.0}],
    )
    assert ok is False
    assert "not confirmed cancelled" in detail


def test_confirm_protective_cancels_aborts_when_the_stop_fills():
    broker = MagicMock()
    broker.wait_for_order_terminal.return_value = "filled"
    ok, detail = confirm_protective_cancels(
        broker, [{"id": "stop-1", "qty": 10, "stop_price": 90.0}],
    )
    assert ok is False
    assert "FILLED" in detail


def test_cover_qty_uses_broker_full_position_not_the_add_fill():
    broker = MagicMock()
    broker.get_positions.return_value = [_cop_position(qty=13.0)]
    qty = cover_qty_for_rearm(
        broker, symbol="COP", filled_qty=3.0, held_qty_before=10.0,
    )
    assert qty == 13.0


def test_cover_qty_falls_back_to_filled_plus_held_when_broker_unreadable():
    broker = MagicMock()
    broker.get_positions.side_effect = RuntimeError("broker down")
    qty = cover_qty_for_rearm(
        broker, symbol="COP", filled_qty=3.0, held_qty_before=10.0,
    )
    assert qty == 13.0


# --------------------------------------------------------------------------
# prepare: WAL then cancel then confirm
# --------------------------------------------------------------------------

def test_prepare_long_add_is_noop_when_the_name_is_not_held():
    broker = MagicMock()
    prep = prepare_long_add(
        broker=broker, db=MagicMock(), symbol="COP",
        positions=[], intended_stop=90.0,
    )
    assert prep.is_scale_in is False
    broker.snapshot_protective_stops.assert_not_called()


def test_prepare_skips_buy_when_cancel_is_not_confirmed(tmp_path):
    db = _db(tmp_path)
    broker = MagicMock()
    broker.snapshot_protective_stops.return_value = (
        True, [{"id": "stop-1", "qty": 10, "stop_price": 88.0}],
    )
    broker.cancel_snapshotted_stops.return_value = True
    broker.wait_for_order_terminal.return_value = "pending_cancel"
    broker._restore_stop_orders.return_value = (1, [])

    prep = prepare_long_add(
        broker=broker, db=db, symbol="COP",
        positions=[_cop_position()], intended_stop=90.0,
    )
    assert prep.skip_reason == "scale_in_cancel_unconfirmed"
    assert prep.cancelled is False
    broker.submit_order.assert_not_called()
    broker._restore_stop_orders.assert_called()
    assert db.get_pending_protection_restores() == []
    db.close()


def test_prepare_writes_wal_before_cancel(tmp_path):
    db = _db(tmp_path)
    broker = MagicMock()
    broker.snapshot_protective_stops.return_value = (
        True, [{"id": "stop-1", "qty": 10, "stop_price": 88.0}],
    )
    seen_at_cancel = {}

    def _cancel(symbol, specs):
        seen_at_cancel["rows"] = db.get_pending_protection_restores()
        return True

    broker.cancel_snapshotted_stops.side_effect = _cancel
    broker.wait_for_order_terminal.return_value = "canceled"

    prep = prepare_long_add(
        broker=broker, db=db, symbol="COP",
        positions=[_cop_position()], intended_stop=90.0,
    )
    assert prep.cancelled is True
    assert prep.skip_reason is None
    assert seen_at_cancel["rows"][0]["sell_order_id"] == WAL_SCALE_IN_SENTINEL
    assert prep.intended_stop == 90.0  # add's stop tighter than 88
    db.close()


# --------------------------------------------------------------------------
# ExecutionStage sequence
# --------------------------------------------------------------------------

def test_execution_stage_cancels_then_submits_buy_add():
    held = [_cop_position()]
    pipeline = _pipeline(positions=held)
    stop = {"id": "stop-cop", "qty": 10, "stop_price": 88.0}
    pipeline.broker.snapshot_protective_stops.return_value = (True, [stop])
    pipeline.broker.cancel_snapshotted_stops.return_value = True

    def _wait(order_id, *args, **kwargs):
        return "canceled" if "stop" in str(order_id) else "filled"

    pipeline.broker.wait_for_order_terminal.side_effect = _wait
    pipeline.broker.submit_order.return_value = {
        "id": "buy-cop", "status": "accepted", "symbol": "COP",
        "pending_stop_price": 90.0,
    }
    pipeline.broker.place_entry_protection.return_value = {"id": "stop-new"}
    pipeline.db.insert_pending_protection_restore.return_value = 7
    pipeline.db.insert_trade.return_value = 1

    ctx = _ctx([_buy_cop()], positions=held)
    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    pipeline.broker.cancel_snapshotted_stops.assert_called()
    pipeline.broker.submit_order.assert_called()
    submit_kwargs = pipeline.broker.submit_order.call_args.kwargs
    assert submit_kwargs["symbol"] == "COP"
    assert submit_kwargs["side"] == "buy"
    protect_kwargs = pipeline.broker.place_entry_protection.call_args.kwargs
    assert protect_kwargs["cover_full_position"] is True
    assert protect_kwargs["held_qty_before"] == 10.0
    assert ctx.execution_skips == []


def test_execution_stage_rearms_at_the_tighter_of_cancelled_and_add_stop():
    """A looser add stop must not replace a tighter cancelled protective sell."""
    held = [_cop_position()]
    pipeline = _pipeline(positions=held)
    stop = {"id": "stop-cop", "qty": 10, "stop_price": 92.0}
    pipeline.broker.snapshot_protective_stops.return_value = (True, [stop])
    pipeline.broker.cancel_snapshotted_stops.return_value = True
    pipeline.broker.wait_for_order_terminal.side_effect = (
        lambda order_id, *a, **k: "canceled" if "stop" in str(order_id) else "filled"
    )
    pipeline.broker.submit_order.return_value = {
        "id": "buy-cop", "status": "accepted", "symbol": "COP",
        "pending_stop_price": 90.0,
    }
    pipeline.broker.place_entry_protection.return_value = {"id": "stop-new"}
    pipeline.db.insert_pending_protection_restore.return_value = 7
    pipeline.db.insert_trade.return_value = 1

    ctx = _ctx([_buy_cop()], positions=held)
    ExecutionStage(pipeline=pipeline).run(ctx)

    protect_kwargs = pipeline.broker.place_entry_protection.call_args.kwargs
    assert protect_kwargs["stop_price"] == 92.0
    assert protect_kwargs["cover_full_position"] is True


def test_submit_exception_leaves_wal_instead_of_restoring_old_stop_size():
    """Unknown submit must not put the cancelled stop back at the pre-add qty."""
    held = [_cop_position()]
    pipeline = _pipeline(positions=held)
    stop = {"id": "stop-cop", "qty": 10, "stop_price": 88.0}
    pipeline.broker.snapshot_protective_stops.return_value = (True, [stop])
    pipeline.broker.cancel_snapshotted_stops.return_value = True
    pipeline.broker.wait_for_order_terminal.return_value = "canceled"
    pipeline.broker.submit_order.side_effect = RuntimeError("broker timeout")
    pipeline.broker._restore_stop_orders.return_value = (1, [])
    pipeline.db.insert_pending_protection_restore.return_value = 7
    pipeline.db.insert_trade.return_value = 1

    ctx = _ctx([_buy_cop()], positions=held)
    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert orders == []
    pipeline.broker.submit_order.assert_called()
    pipeline.broker._restore_stop_orders.assert_not_called()
    pipeline.db.delete_pending_protection_restore.assert_not_called()


def test_execution_stage_does_not_submit_when_cancel_unconfirmed():
    held = [_cop_position()]
    pipeline = _pipeline(positions=held)
    pipeline.broker.snapshot_protective_stops.return_value = (
        True, [{"id": "stop-cop", "qty": 10, "stop_price": 88.0}],
    )
    pipeline.broker.cancel_snapshotted_stops.return_value = True
    pipeline.broker.wait_for_order_terminal.return_value = None
    pipeline.broker._restore_stop_orders.return_value = (1, [])
    pipeline.db.insert_pending_protection_restore.return_value = 3

    ctx = _ctx([_buy_cop()], positions=held)
    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert orders == []
    pipeline.broker.submit_order.assert_not_called()
    assert ctx.execution_skips[0]["reason"] == "scale_in_cancel_unconfirmed"


def test_short_add_is_recorded_as_blocked():
    held = [Position(
        symbol="TSLA", qty=-10.0, avg_entry=260.0, current_price=250.0,
        market_value=2500.0, unrealized_pnl=100.0, sector="Consumer",
    )]
    pipeline = _pipeline(live_price=250.0, positions=held)
    pipeline.broker.get_shortability.return_value = {
        "shortable": True, "easy_to_borrow": True, "reason": "eligible",
    }
    ctx = _ctx(
        [TradeDecision(
            action="SHORT", symbol="TSLA", allocation_pct=5,
            entry_price=250.0, stop_loss=275.0, take_profit=200.0,
            reasoning="add to short",
        )],
        positions=held,
    )
    orders = ExecutionStage(pipeline=pipeline).run(ctx)
    assert orders == []
    pipeline.broker.submit_order.assert_not_called()
    assert ctx.execution_skips[0]["reason"] == "short_add_blocked"


# --------------------------------------------------------------------------
# broker: partial fill restore size
# --------------------------------------------------------------------------

@patch("src.execution.broker.TradingClient")
def test_place_entry_protection_scale_in_sizes_stop_to_broker_full_qty(mock_tc_cls):
    from alpaca.trading.requests import StopLimitOrderRequest
    from src.execution.broker import AlpacaBroker

    mock_client = MagicMock()
    mock_client.submit_order.return_value = MagicMock(
        id="s-full", status="new", symbol="COP",
    )
    mock_tc_cls.return_value = mock_client
    broker = AlpacaBroker(api_key="t", secret_key="t", paper=True)
    broker.wait_for_order_terminal = MagicMock(return_value="filled")
    broker.get_order_fill_info = MagicMock(return_value={
        "status": "filled", "filled_qty": 3.0, "filled_avg_price": 100.0,
    })
    broker.get_positions = MagicMock(return_value=[_cop_position(qty=13.0)])

    out = broker.place_entry_protection(
        symbol="COP", order_id="buy-add", stop_price=90.0,
        requested_qty=5, cover_full_position=True, held_qty_before=10.0,
    )
    assert out is not None
    req = mock_client.submit_order.call_args[0][0]
    assert isinstance(req, StopLimitOrderRequest)
    assert float(req.qty) == 13.0
    mock_client.submit_order.reset_mock()
    broker.get_order_fill_info = MagicMock(return_value={
        "status": "canceled", "filled_qty": 0.0, "filled_avg_price": None,
    })
    out_zero = broker.place_entry_protection(
        symbol="COP", order_id="buy-add-unfilled", stop_price=90.0,
        requested_qty=5, cover_full_position=True, held_qty_before=10.0,
    )
    assert out_zero is None
    mock_client.submit_order.assert_not_called()


# --------------------------------------------------------------------------
# rearm failure alert
# --------------------------------------------------------------------------

def test_rearm_failure_after_scale_in_pages_the_owner():
    pipeline = MagicMock()
    pipeline.broker.get_order_fill_info.return_value = {"filled_qty": 3.0}
    spec = {
        "symbol": "COP",
        "stop_price": 90.0,
        "cover_full_position": True,
        "side": "buy",
    }
    with patch("src.notifier.send_owner_alert") as alert:
        _alert_owner_protection_failed(pipeline, spec, None, "buy-add")
    alert.assert_called_once()
    body = alert.call_args.args[0]
    assert "SCALE-IN" in body
    assert "COP" in body
    assert "NO STOP AT ALL" not in body


def test_ordinary_protection_failure_wording_is_unchanged():
    pipeline = MagicMock()
    pipeline.broker.get_order_fill_info.return_value = {"filled_qty": 7.0}
    spec = {"symbol": "NVDA", "stop_price": 95.0, "side": "buy"}
    with patch("src.notifier.send_owner_alert") as alert:
        _alert_owner_protection_failed(pipeline, spec, None, "e1")
    assert "NO STOP AT ALL" in alert.call_args.args[0]


# --------------------------------------------------------------------------
# drain: full current qty, not cancelled-stop size
# --------------------------------------------------------------------------

def test_drain_scale_in_rearms_broker_full_qty(tmp_path):
    db = _db(tmp_path)
    row_id = db.insert_pending_protection_restore(
        symbol="COP", sell_order_id=WAL_SCALE_IN_SENTINEL,
        position_qty_before_sell=10.0,
        specs_json=json.dumps([
            {"id": "stop-1", "qty": 10, "stop_price": 88.0},
            {"id": None, "qty": 0, "stop_price": 90.0, "role": "intended"},
        ]),
        side="sell",
    )
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.db = db
    pipeline.broker = MagicMock()
    pipeline.broker.list_open_entry_order_ids.return_value = []
    pipeline.broker.get_positions.return_value = [_cop_position(qty=13.0)]
    pipeline.broker.snapshot_protective_stops.return_value = (True, [])
    pipeline.broker._submit_protective_stop_retrying.return_value = {
        "id": "stop-rearm", "uncovered_qty": 0.0,
    }
    pipeline.broker.STOP_LIMIT_BUFFER_PCT = 0.03

    drained = pipeline._drain_pending_protection_restores()
    assert drained == 1
    kwargs = pipeline.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["qty"] == 13.0
    assert kwargs["stop_price"] == 90.0
    assert kwargs["side"] == "sell"
    assert db.get_pending_protection_restores() == []
    db.close()
    assert row_id  # used; silence unused in some linters


def test_drain_scale_in_row_waits_for_leftover_entry_cancel():
    broker = MagicMock()
    broker.list_open_entry_order_ids.return_value = ["buy-still-working"]
    broker.wait_for_order_terminal.return_value = "new"  # not confirmed
    row = {
        "id": 1, "symbol": "COP",
        "specs_json": json.dumps([{"id": "s1", "qty": 10, "stop_price": 90.0}]),
    }
    assert drain_scale_in_row(broker, MagicMock(), row) is False
    broker.cancel_entry_order.assert_called_once_with("buy-still-working")
    broker._submit_protective_stop_retrying.assert_not_called()


# --------------------------------------------------------------------------
# races: trail / watchdog skip in-flight scale-in
# --------------------------------------------------------------------------

def test_deterministic_trail_skips_a_symbol_with_scale_in_wal(tmp_path):
    db = _db(tmp_path)
    db.insert_pending_protection_restore(
        symbol="COP", sell_order_id=WAL_SCALE_IN_SENTINEL,
        position_qty_before_sell=10.0,
        specs_json=json.dumps([{"id": "s1", "qty": 10, "stop_price": 90.0}]),
        side="sell",
    )
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.db = db
    pipeline.broker = MagicMock()
    pipeline.market = MagicMock()
    orders = pipeline._apply_deterministic_trails(
        [_cop_position()], run_id="r1",
    )
    assert orders == []
    pipeline.broker.replace_stop_loss.assert_not_called()
    db.close()


def test_watchdog_skips_scale_in_symbol_while_session_lock_held(
    tmp_path, monkeypatch,
):
    from src import coverage_watchdog

    db = _db(tmp_path)
    db.insert_pending_protection_restore(
        symbol="COP", sell_order_id=WAL_SCALE_IN_SENTINEL,
        position_qty_before_sell=10.0,
        specs_json=json.dumps([{"id": "s1", "qty": 10, "stop_price": 90.0}]),
        side="sell",
    )
    db.close()
    monkeypatch.setattr(
        "src.execution.scale_in.trading_session_lock_held", lambda: True,
    )
    broker = MagicMock()
    broker.get_positions.return_value = [
        SimpleNamespace(symbol="COP", qty=10.0, current_price=100.0),
    ]
    broker.snapshot_protective_stops.return_value = (True, [])
    gaps, err = coverage_watchdog.uncovered_positions(
        broker, skip_symbols=coverage_watchdog._scale_in_skip(broker, tmp_path / "scale_in.db"),
    )
    assert err is None
    assert gaps == []
    broker.snapshot_protective_stops.assert_not_called()


def test_pending_protection_symbols_includes_scale_in_wal(tmp_path):
    db = _db(tmp_path)
    db.insert_pending_protection_restore(
        symbol="COP", sell_order_id=WAL_SCALE_IN_SENTINEL,
        position_qty_before_sell=10.0,
        specs_json="[]",
        side="sell",
    )
    assert pending_protection_symbols(db) == {"COP"}
    db.close()


def test_scale_in_symbols_to_skip_when_an_entry_is_still_working():
    db = MagicMock()
    db.get_pending_protection_restores.return_value = [
        {"symbol": "COP", "sell_order_id": WAL_SCALE_IN_SENTINEL},
    ]
    broker = MagicMock()
    broker.list_open_entry_order_ids.return_value = ["buy-1"]
    with patch(
        "src.execution.scale_in.trading_session_lock_held", return_value=False,
    ):
        assert scale_in_symbols_to_skip(broker, db) == {"COP"}
