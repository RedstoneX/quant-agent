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
    most_protective_short_stop,
    pending_protection_symbols,
    prepare_long_add,
    prepare_short_add,
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


def _short_cop_position(qty=-10.0) -> Position:
    """A held SHORT in COP (negative broker-signed qty)."""
    return Position(
        symbol="COP", qty=qty, avg_entry=100.0, current_price=90.0,
        market_value=qty * 90.0, unrealized_pnl=abs(qty) * 10.0, sector="Energy",
    )


def _short_cop() -> TradeDecision:
    # Short stop is ABOVE entry; add's own stop 110, resting buy-stop set per-test.
    return TradeDecision(
        action="SHORT", symbol="COP", allocation_pct=10,
        entry_price=100.0, stop_loss=110.0, take_profit=80.0,
        reasoning="add to short",
    )


def _shortable(pipeline):
    pipeline.broker.get_shortability.return_value = {
        "shortable": True, "easy_to_borrow": True, "reason": "eligible",
    }
    # The wash-trade guard asks (fail-closed) for foreign working BUYs;
    # (True, []) = listing succeeded, none present.
    pipeline.broker.list_open_entry_orders_checked.return_value = (True, [])
    pipeline.broker.list_open_entry_order_ids.return_value = []
    return pipeline


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


def test_scale_in_add_carries_the_pinned_setup_type_not_todays_reread():
    """Item 82: `setup_type` is pinned on the trade row at ENTRY and must
    never be reclassified by a later scale-in add.

    COP was originally entered as a "breakout" (Type B — no overhead
    structure, progress/pace disabled, managed by trailing only). Today's
    fresh Technical read for the same symbol has drifted to "range" as the
    chart evolved — a routine, unremarkable occurrence, not itself a bug.
    Before the fix, the execution stage re-derived `setup_type` from
    TODAY's `ctx.analyses` on every BUY, including this scale-in add, and
    wrote "range" onto the new (and now newest) trade row — silently
    stripping the breakout's protection from every consumer that reads
    `get_symbol_last_buy` (position-review pace/progress, the trailing
    stop, target revision). The fix carries the EXISTING pinned value
    forward unchanged on a scale-in add.
    """
    held = [_cop_position()]
    pipeline = _pipeline(positions=held)
    stop = {"id": "stop-cop", "qty": 10, "stop_price": 88.0}
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
    # The ORIGINAL entry's pinned row — this is what a real
    # `get_symbol_last_buy` would return before today's add is recorded.
    pipeline.db.get_symbol_last_buy.return_value = {
        "setup_type": "breakout", "take_profit": 120.0,
    }

    from src.models import TechAnalysisResult, TechReasoningChain
    todays_reread = TechAnalysisResult(
        symbol="COP", rating="buy", conviction="medium",
        entry_price=100.0, stop_loss=95.0, reference_target=110.0,
        support_levels=[95.0], resistance_levels=[110.0],
        # Deliberately DIFFERENT from the pinned "breakout" — this is
        # today's independent re-classification, not a re-assertion of
        # the entry-day one.
        setup_type="range", expected_horizon_sessions=10,
        reasoning_chain=TechReasoningChain(
            trend="x", momentum="x", volatility="x", volume="x",
            support_resistance="x",
        ),
        reasoning="test", thesis_invalid_if="closes below support",
    )

    ctx = _ctx([_buy_cop()], positions=held)
    ctx.analyses = [todays_reread]
    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    insert_kwargs = pipeline.db.insert_trade.call_args.kwargs
    assert insert_kwargs["setup_type"] == "breakout", (
        "scale-in add reclassified a pinned setup_type instead of "
        f"carrying it forward: got {insert_kwargs['setup_type']!r}"
    )


def test_new_entry_reads_setup_type_from_the_decision_not_a_second_lookup():
    """Item 82: a genuinely new entry has no prior pinned row, so
    `setup_type` must come from the single value the constructor already
    classified onto `TradeDecision.setup_type` — not from a second,
    independent lookup into `ctx.analyses`. `isinstance` on the resulting
    value keeps this gated on a real classification, never a MagicMock
    truthiness accident (~58 existing tests drive a MagicMock broker)."""
    pipeline = _pipeline(positions=[])
    pipeline.broker.submit_order.return_value = {
        "id": "buy-cop", "status": "accepted", "symbol": "COP",
        "pending_stop_price": 90.0,
    }
    pipeline.broker.place_entry_protection.return_value = {"id": "stop-new"}
    pipeline.db.insert_trade.return_value = 1

    decision = TradeDecision(
        action="BUY", symbol="COP", allocation_pct=10,
        entry_price=100.0, stop_loss=90.0, take_profit=120.0,
        reasoning="new breakout entry", setup_type="breakout",
    )
    from src.models import TechAnalysisResult, TechReasoningChain
    # A DIFFERENT analysis for the same symbol, standing in for whatever
    # `ctx.analyses` happens to hold by execution time — the point is that
    # it must not be consulted for setup_type on a new entry either.
    mismatched_analysis = TechAnalysisResult(
        symbol="COP", rating="buy", conviction="medium",
        entry_price=100.0, stop_loss=95.0, reference_target=110.0,
        support_levels=[95.0], resistance_levels=[110.0],
        setup_type="range", expected_horizon_sessions=10,
        reasoning_chain=TechReasoningChain(
            trend="x", momentum="x", volatility="x", volume="x",
            support_resistance="x",
        ),
        reasoning="test", thesis_invalid_if="closes below support",
    )

    ctx = _ctx([decision], positions=[])
    ctx.analyses = [mismatched_analysis]
    ExecutionStage(pipeline=pipeline).run(ctx)

    insert_kwargs = pipeline.db.insert_trade.call_args.kwargs
    assert isinstance(insert_kwargs["setup_type"], str)
    assert insert_kwargs["setup_type"] == "breakout"


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


# --------------------------------------------------------------------------
# SHORT scale-in: mirror of the long path (owner-approved)
# --------------------------------------------------------------------------

def test_most_protective_short_stop_is_the_lowest_trigger():
    # Lowest trigger above entry covers the short SOONEST.
    assert most_protective_short_stop([112.0, 108.0, 110.0]) == 108.0
    assert most_protective_short_stop([]) == 0.0


def test_cover_qty_short_fallback_covers_full_enlarged_short_not_just_add():
    """Adversary bug: a short's held_qty_before is NEGATIVE, so the old
    fallback ``filled + max(0.0, held)`` covered only the ADD and left the
    existing short leg naked. The fix covers |filled| + |held|."""
    broker = MagicMock()
    broker.get_positions.side_effect = RuntimeError("broker down")
    qty = cover_qty_for_rearm(
        broker, symbol="COP", filled_qty=5.0, held_qty_before=-10.0,
    )
    assert qty == 15.0  # NOT 5.0 (the add alone), NOT 0.0


def test_prepare_short_add_is_noop_when_the_name_is_not_short():
    broker = MagicMock()
    # Held LONG: not a short scale-in — caller opens a new short unchanged.
    prep = prepare_short_add(
        broker=broker, db=MagicMock(), symbol="COP",
        positions=[_cop_position(qty=10.0)], intended_stop=110.0,
    )
    assert prep.is_scale_in is False
    broker.snapshot_protective_stops.assert_not_called()


def test_prepare_short_add_refuses_on_a_foreign_working_buy(tmp_path):
    """Wash-trade guard: a resting BUY (cover-limit / take-profit) would
    collide with the SELL add, so the add is refused BEFORE any buy-stop is
    cancelled."""
    db = _db(tmp_path)
    broker = MagicMock()
    broker.list_open_entry_orders_checked.return_value = (True, ["foreign-buy-1"])

    prep = prepare_short_add(
        broker=broker, db=db, symbol="COP",
        positions=[_short_cop_position()], intended_stop=110.0,
    )
    assert prep.skip_reason == "short_add_foreign_buy"
    assert prep.cancelled is False
    broker.snapshot_protective_stops.assert_not_called()
    broker.cancel_snapshotted_stops.assert_not_called()
    broker.list_open_entry_orders_checked.assert_called_once_with("COP", side="buy")
    db.close()


def test_prepare_short_add_fails_closed_when_the_order_listing_errors(tmp_path):
    """H4: the wash guard must NOT cancel protection when it cannot VERIFY
    there is no colliding BUY — a listing failure (ok=False) refuses the add
    rather than betting Alpaca bounces a self-cross."""
    db = _db(tmp_path)
    broker = MagicMock()
    # Listing could not be read (API error surfaced as ok=False, not empty).
    broker.list_open_entry_orders_checked.return_value = (False, [])

    prep = prepare_short_add(
        broker=broker, db=db, symbol="COP",
        positions=[_short_cop_position()], intended_stop=110.0,
    )
    assert prep.skip_reason == "short_add_wash_guard_unverified"
    assert prep.cancelled is False
    broker.snapshot_protective_stops.assert_not_called()
    broker.cancel_snapshotted_stops.assert_not_called()
    assert db.get_pending_protection_restores() == []
    db.close()


def test_prepare_short_add_aborts_when_the_buy_stop_fills(tmp_path):
    """A BUY-stop that FIRED during the cancel means the short was COVERED.
    The add aborts, the buy-stop is NOT restored onto a (now flat) name, and
    the WAL row is discharged."""
    db = _db(tmp_path)
    broker = MagicMock()
    broker.list_open_entry_orders_checked.return_value = (True, [])
    broker.snapshot_protective_stops.return_value = (
        True, [{"id": "bstop-1", "qty": 10, "stop_price": 108.0}],
    )
    broker.cancel_snapshotted_stops.return_value = True
    broker.wait_for_order_terminal.return_value = "filled"
    # Position re-read confirms FLAT — the stop covered the whole short.
    broker.get_positions.return_value = []

    prep = prepare_short_add(
        broker=broker, db=db, symbol="COP",
        positions=[_short_cop_position()], intended_stop=110.0,
    )
    assert prep.skip_reason == "scale_in_stop_filled"
    assert prep.cancelled is False
    broker._restore_stop_orders.assert_not_called()  # do NOT re-arm a flat name
    assert db.get_pending_protection_restores() == []
    db.close()


def test_prepare_short_add_fill_but_not_flat_does_not_leave_a_sibling_naked(tmp_path):
    """H2: with two protective buy-stops, one FIRES during the cancel and the
    other was cancelled — the short is only PARTLY covered. The prep must NOT
    discharge the WAL restoring nothing: it rearms a buy-stop over the
    remaining short (and keeps the WAL if it cannot), so no lot is left naked.
    """
    db = _db(tmp_path)
    broker = MagicMock()
    broker.list_open_entry_orders_checked.return_value = (True, [])
    broker.snapshot_protective_stops.return_value = (
        True, [
            {"id": "bstop-lot1", "qty": 10, "stop_price": 108.0},
            {"id": "bstop-lot2", "qty": 5, "stop_price": 112.0},
        ],
    )
    broker.cancel_snapshotted_stops.return_value = True
    # First spec FILLED, sibling canceled — abort-on-fill triggers.
    broker.wait_for_order_terminal.side_effect = (
        lambda oid, *a, **k: "filled" if "lot1" in str(oid) else "canceled"
    )
    # Position re-read: still SHORT 5 (lot2 not covered).
    broker.get_positions.return_value = [_short_cop_position(qty=-5.0)]
    broker._submit_protective_stop_retrying.return_value = {
        "id": "bstop-rearm", "uncovered_qty": 0.0,
    }
    broker.STOP_LIMIT_BUFFER_PCT = 0.03

    prep = prepare_short_add(
        broker=broker, db=db, symbol="COP",
        positions=[_short_cop_position(qty=-15.0)], intended_stop=110.0,
    )
    assert prep.skip_reason == "scale_in_stop_filled_partial"
    assert prep.cancelled is False
    # The remaining short (5) was rearmed on the BUY side — not left naked.
    kwargs = broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["qty"] == 5.0
    assert kwargs["side"] == "buy"
    db.close()


def test_prepare_short_add_fill_not_flat_and_no_rearm_keeps_the_wal(tmp_path):
    """H2: if the short is not confirmed flat AND the in-session rearm cannot
    land, the WAL row is KEPT (not discharged) so drain finishes the job."""
    db = _db(tmp_path)
    broker = MagicMock()
    broker.list_open_entry_orders_checked.return_value = (True, [])
    broker.snapshot_protective_stops.return_value = (
        True, [{"id": "bstop-1", "qty": 15, "stop_price": 108.0}],
    )
    broker.cancel_snapshotted_stops.return_value = True
    broker.wait_for_order_terminal.return_value = "filled"
    # Broker position UNREADABLE (cannot confirm flat) and rearm fails.
    broker.get_positions.side_effect = RuntimeError("broker down")
    broker._submit_protective_stop_retrying.return_value = None
    broker.STOP_LIMIT_BUFFER_PCT = 0.03

    prep = prepare_short_add(
        broker=broker, db=db, symbol="COP",
        positions=[_short_cop_position(qty=-15.0)], intended_stop=110.0,
    )
    assert prep.skip_reason == "scale_in_stop_filled_partial"
    # WAL row kept — the remaining short is not stranded without recovery.
    assert db.get_pending_protection_restores() != []
    db.close()


def test_prepare_short_add_snapshots_and_wals_on_the_buy_side(tmp_path):
    db = _db(tmp_path)
    broker = MagicMock()
    broker.list_open_entry_orders_checked.return_value = (True, [])
    broker.snapshot_protective_stops.return_value = (
        True, [{"id": "bstop-1", "qty": 10, "stop_price": 108.0}],
    )
    broker.cancel_snapshotted_stops.return_value = True
    broker.wait_for_order_terminal.return_value = "canceled"

    prep = prepare_short_add(
        broker=broker, db=db, symbol="COP",
        positions=[_short_cop_position()], intended_stop=110.0,
    )
    assert prep.cancelled is True
    assert prep.side == "buy"
    assert prep.held_qty_before == -10.0
    # Most-protective short = LOWEST of the resting 108 and the add's 110.
    assert prep.intended_stop == 108.0
    broker.snapshot_protective_stops.assert_called_once_with("COP", side="buy")
    rows = db.get_pending_protection_restores()
    assert rows[0]["side"] == "buy"
    assert rows[0]["sell_order_id"] == WAL_SCALE_IN_SENTINEL
    db.close()


def test_execution_stage_cancels_buy_stop_then_submits_sell_add_and_rearms():
    """Full short scale-in through the stage: cancel the buy-stop, confirm,
    submit the SELL add, rearm a buy-stop over the FULL short."""
    held = [_short_cop_position(qty=-10.0)]
    pipeline = _shortable(_pipeline(positions=held))
    # Resting buy-stop at 108; add's own stop 110 → most-protective = 108.
    stop = {"id": "bstop-cop", "qty": 10, "stop_price": 108.0}
    pipeline.broker.snapshot_protective_stops.return_value = (True, [stop])
    pipeline.broker.cancel_snapshotted_stops.return_value = True
    pipeline.broker.wait_for_order_terminal.side_effect = (
        lambda order_id, *a, **k: "canceled" if "stop" in str(order_id) else "filled"
    )
    pipeline.broker.submit_order.return_value = {
        "id": "sell-cop", "status": "accepted", "symbol": "COP",
        "pending_stop_price": 110.0,
    }
    pipeline.broker.place_entry_protection.return_value = {"id": "bstop-new"}
    pipeline.db.insert_pending_protection_restore.return_value = 7
    pipeline.db.insert_trade.return_value = 1

    ctx = _ctx([_short_cop()], positions=held)
    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    pipeline.broker.cancel_snapshotted_stops.assert_called()
    submit_kwargs = pipeline.broker.submit_order.call_args.kwargs
    assert submit_kwargs["symbol"] == "COP"
    assert submit_kwargs["side"] == "sell_short"
    protect_kwargs = pipeline.broker.place_entry_protection.call_args.kwargs
    assert protect_kwargs["cover_full_position"] is True
    assert protect_kwargs["held_qty_before"] == -10.0
    assert protect_kwargs["side"] == "sell_short"
    assert protect_kwargs["stop_price"] == 108.0  # lowest = most protective
    # snapshot for a short reads the BUY-stop side.
    pipeline.broker.snapshot_protective_stops.assert_any_call("COP", side="buy")
    assert ctx.execution_skips == []


def test_short_add_below_floor_is_dropped_before_any_buy_stop_is_cancelled():
    """The min-order floor MUST run before the protective buy-stop comes off."""
    held = [_short_cop_position(qty=-10.0)]
    pipeline = _shortable(_pipeline(positions=held))
    pipeline.broker.snapshot_protective_stops.return_value = (
        True, [{"id": "bstop-cop", "qty": 10, "stop_price": 108.0}],
    )

    ctx = _ctx([_short_cop()], positions=held)
    # Force a tiny order: 3 shares * $100 = $300 < the $500 floor.
    with patch("src.pipeline_stages._size_shares", return_value=3.0):
        orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert orders == []
    pipeline.broker.submit_order.assert_not_called()
    # The protection is still standing — the floor gate ran PRE-cancel.
    pipeline.broker.cancel_snapshotted_stops.assert_not_called()
    assert ctx.execution_skips[0]["reason"] == "below_min_notional"


def test_drain_scale_in_rearms_broker_full_short_qty_on_the_buy_side(tmp_path):
    """Buy-side WAL crash recovery: rearm a buy-stop over the full short."""
    db = _db(tmp_path)
    row_id = db.insert_pending_protection_restore(
        symbol="COP", sell_order_id=WAL_SCALE_IN_SENTINEL,
        position_qty_before_sell=-10.0,
        specs_json=json.dumps([
            {"id": "bstop-1", "qty": 10, "stop_price": 112.0},
            {"id": None, "qty": 0, "stop_price": 110.0, "role": "intended"},
        ]),
        side="buy",
    )
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.db = db
    pipeline.broker = MagicMock()
    pipeline.broker.list_open_entry_order_ids.return_value = []
    pipeline.broker.get_positions.return_value = [_short_cop_position(qty=-13.0)]
    pipeline.broker.snapshot_protective_stops.return_value = (True, [])
    pipeline.broker._submit_protective_stop_retrying.return_value = {
        "id": "bstop-rearm", "uncovered_qty": 0.0,
    }
    pipeline.broker.STOP_LIMIT_BUFFER_PCT = 0.03

    drained = pipeline._drain_pending_protection_restores()
    assert drained == 1
    kwargs = pipeline.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["qty"] == 13.0            # full enlarged short, not the add
    assert kwargs["stop_price"] == 110.0    # LOWEST stored = most protective
    assert kwargs["side"] == "buy"          # buy-stop protects a short
    # Buy needs UP-headroom: limit ABOVE the trigger.
    assert kwargs["limit_price"] == 110.0 * 1.03
    assert db.get_pending_protection_restores() == []
    db.close()
    assert row_id


def test_drain_classifies_short_from_qty_sign_not_the_side_column(tmp_path):
    """H3: a scale-in row must never be read with the generic position/close
    side meaning. Even with a MISLEADING `side` column, drain rearms on the
    buy side because it derives long/short from the SIGN of the stored qty."""
    db = _db(tmp_path)
    db.insert_pending_protection_restore(
        symbol="COP", sell_order_id=WAL_SCALE_IN_SENTINEL,
        position_qty_before_sell=-13.0,           # NEGATIVE => short
        specs_json=json.dumps([{"id": "b1", "qty": 13, "stop_price": 110.0}]),
        side="sell",                               # deliberately WRONG column
    )
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.db = db
    pipeline.broker = MagicMock()
    pipeline.broker.list_open_entry_order_ids.return_value = []
    pipeline.broker.get_positions.return_value = [_short_cop_position(qty=-13.0)]
    pipeline.broker.snapshot_protective_stops.return_value = (True, [])
    pipeline.broker._submit_protective_stop_retrying.return_value = {
        "id": "b-rearm", "uncovered_qty": 0.0,
    }
    pipeline.broker.STOP_LIMIT_BUFFER_PCT = 0.03

    drained = pipeline._drain_pending_protection_restores()
    assert drained == 1
    kwargs = pipeline.broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["side"] == "buy"          # short, despite side="sell" column
    assert kwargs["qty"] == 13.0
    pipeline.broker.snapshot_protective_stops.assert_called_with("COP", side="buy")
    db.close()


def test_emergency_cover_after_rearm_fails_covers_the_full_enlarged_short():
    """H1: when the full-position rearm returns None on a short SCALE-IN, the
    D7 emergency market cover must buy the ENLARGED short (broker current qty,
    e.g. 15), NOT just the add's fill (5) — the pre-existing leg's buy-stop was
    already cancelled and would otherwise be left naked."""
    held = [_short_cop_position(qty=-10.0)]
    pipeline = _shortable(_pipeline(positions=held))
    stop = {"id": "bstop-cop", "qty": 10, "stop_price": 108.0}
    pipeline.broker.snapshot_protective_stops.return_value = (True, [stop])
    pipeline.broker.cancel_snapshotted_stops.return_value = True
    pipeline.broker.wait_for_order_terminal.side_effect = (
        lambda oid, *a, **k: "canceled" if "stop" in str(oid) else "filled"
    )

    def _submit(*args, **kwargs):
        if kwargs.get("side") == "buy":                 # emergency cover leg
            return {"id": "cover-1", "status": "accepted"}
        return {                                        # the SELL add leg
            "id": "sell-cop", "status": "accepted", "symbol": "COP",
            "pending_stop_price": 110.0,
        }

    pipeline.broker.submit_order.side_effect = _submit
    # Rearm FAILS — protection could not be placed.
    pipeline.broker.place_entry_protection.return_value = None
    # The add filled 5; the broker now shows the ENLARGED short of 15.
    pipeline.broker.get_order_fill_info.return_value = {"filled_qty": 5.0}
    pipeline.broker.get_positions.return_value = [_short_cop_position(qty=-15.0)]
    pipeline.db.insert_pending_protection_restore.return_value = 7
    pipeline.db.insert_trade.return_value = 1

    ctx = _ctx([_short_cop()], positions=held)
    ExecutionStage(pipeline=pipeline).run(ctx)

    cover_calls = [
        c for c in pipeline.broker.submit_order.call_args_list
        if c.kwargs.get("side") == "buy"
    ]
    assert len(cover_calls) == 1, "expected exactly one emergency cover"
    assert cover_calls[0].kwargs["qty"] == 15.0, (
        "emergency cover sized to the add alone leaves the old short leg naked"
    )


# --------------------------------------------------------------------------
# broker: partial fill restore size
# --------------------------------------------------------------------------

@patch("src.execution.broker.TradingClient")
def test_place_entry_protection_scale_in_sizes_stop_to_broker_full_qty(mock_tc_cls):
    from alpaca.trading.requests import StopOrderRequest
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
    assert isinstance(req, StopOrderRequest)   # primary protective = stop-MARKET
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
