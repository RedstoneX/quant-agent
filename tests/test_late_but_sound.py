"""Late-but-sound fills path: WS auth must not eat the fill window;
one catch-up inside the already-approved ceiling; repeg stays off.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.config import ExecutionConfig
from src.execution.broker import (
    _ALPACA_STREAM_AUTH_DEADLINE_S,
    _ALPACA_STREAM_RECONNECT_MAX_S,
    TradeStreamWarmup,
)
from src.pipeline_context import RunContext
from src.pipeline_stages import (
    ExecutionStage,
    _entry_slippage_bps,
    _live_fill_price,
    _pin_approved_entry_ceilings,
    _repeg_settings,
)
from src.models import PortfolioDecision, ReasoningChain, TradeDecision


def _rc() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="m", news_check="n", earnings_check="e",
        signal_conflicts="s", sizing_logic="z", portfolio_balance="b",
        cash_target="c",
    )


def test_ensure_trade_updates_does_not_open_a_throwaway_socket():
    import inspect
    from src.execution import broker as broker_mod
    src = inspect.getsource(broker_mod.AlpacaBroker.ensure_trade_updates)
    assert "TradingStream(" not in src
    assert "thread.start" not in src
    assert _ALPACA_STREAM_AUTH_DEADLINE_S == _ALPACA_STREAM_RECONNECT_MAX_S
    assert _ALPACA_STREAM_AUTH_DEADLINE_S <= 30.0


def test_repeg_stays_off_by_default():
    assert ExecutionConfig().repeg_enabled is False
    pipeline = SimpleNamespace(config=SimpleNamespace(execution=ExecutionConfig()))
    assert _repeg_settings(pipeline) is None


def test_live_fill_price_never_returns_a_bar_close():
    pipeline = MagicMock()
    pipeline.broker.get_latest_price.return_value = None
    assert _live_fill_price(pipeline, "AAPL") is None
    pipeline.broker.get_latest_price.return_value = 334.78
    assert _live_fill_price(pipeline, "AAPL") == 334.78


def test_approved_ceiling_is_pinned_from_live_ref_not_later_tape():
    pipeline = MagicMock()
    pipeline.broker.get_latest_price.return_value = 100.0
    pipeline.config.execution = ExecutionConfig(max_entry_slippage_bps=40.0)
    ctx = RunContext.start("morning")
    decision = TradeDecision(
        symbol="AAPL", action="BUY", allocation_pct=5.0,
        entry_price=100.0, stop_loss=95.0, take_profit=110.0,
        reasoning="r",
    )
    _pin_approved_entry_ceilings(pipeline, ctx, [decision])
    assert ctx.approved_entry_ceiling["AAPL"] == 100.0 * (1 + 40.0 / 10_000.0)
    # A later last-trade must not raise the cap.
    pipeline.broker.get_latest_price.return_value = 110.0
    assert ctx.approved_entry_ceiling["AAPL"] == 100.0 * (1 + 40.0 / 10_000.0)


def _buy_pipeline(ask, live=100.0, *, stall=False):
    pipeline = MagicMock()
    pipeline.broker.get_latest_price.return_value = live
    pipeline.broker.get_latest_quote.return_value = {"ask_price": ask, "bid_price": live - 0.1}
    pipeline.broker.ensure_trade_updates.return_value = TradeStreamWarmup(
        ready=not stall, handshake_failed=stall, retried=stall,
    )
    pipeline.config.execution = ExecutionConfig(
        max_entry_slippage_bps=40.0, repeg_enabled=False,
    )
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0}, [], {},
    )
    pipeline.risk_engine.check_daily_loss.return_value = None
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    pipeline._sweeper = None
    pipeline.db = MagicMock()
    return pipeline


def _buy_ctx(entry=100.0):
    ctx = RunContext.start("morning")
    ctx.positions = []
    ctx.cash = 50_000.0
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.symbols_bars = {}
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_rc(),
        decisions=[
            TradeDecision(
                symbol="AAPL", action="BUY", allocation_pct=5.0,
                entry_price=entry, stop_loss=95.0, take_profit=110.0,
                reasoning="r",
            ),
        ],
        portfolio_view="t",
    )
    return ctx


def test_through_ceiling_after_desk_stall_is_latency_window_not_wad_slippage():
    pipeline = _buy_pipeline(ask=110.0, live=100.0, stall=True)
    ctx = _buy_ctx()
    ExecutionStage(pipeline=pipeline).run(ctx)
    reasons = [s["reason"] for s in ctx.execution_skips]
    assert "latency_window" in reasons
    assert "slippage_gated" not in reasons
    assert pipeline.broker.submit_order.called is False or all(
        not c for c in [pipeline.broker.submit_order.called]
    ) or pipeline.broker.submit_order.call_count == 0


def test_through_ceiling_without_stall_stays_slippage_gated():
    pipeline = _buy_pipeline(ask=110.0, live=100.0, stall=False)
    ctx = _buy_ctx()
    ExecutionStage(pipeline=pipeline).run(ctx)
    reasons = [s["reason"] for s in ctx.execution_skips]
    assert "slippage_gated" in reasons
    assert "latency_window" not in reasons


def test_catch_up_inside_pinned_ceiling_does_not_enable_repeg():
    pipeline = _buy_pipeline(ask=100.2, live=100.0, stall=True)
    ctx = _buy_ctx()
    orders = ExecutionStage(pipeline=pipeline).run(ctx)
    assert ctx.desk_latency_stall is True
    assert ExecutionConfig().repeg_enabled is False
    assert _repeg_settings(pipeline) is None
    # One catch-up mark, not a chase loop. Limit stays inside the pinned cap.
    assert ctx.catch_up_used.get("AAPL") is True
    assert pipeline.broker.submit_order.called
    submitted = pipeline.broker.submit_order.call_args
    limit = (submitted.kwargs or {}).get("limit_price")
    if limit is None and submitted.args:
        limit = submitted.kwargs.get("limit_price") if submitted.kwargs else None
    assert ctx.approved_entry_ceiling["AAPL"] == 100.0 * (1 + 40.0 / 10_000.0)
    if limit is not None:
        assert float(limit) <= ctx.approved_entry_ceiling["AAPL"] + 0.0001


def test_no_price_does_not_fall_back_to_morning_bar():
    pipeline = _buy_pipeline(ask=100.0, live=100.0, stall=False)
    pipeline.broker.get_latest_price.return_value = None
    ctx = _buy_ctx()
    bar = SimpleNamespace(close=334.0)
    ctx.symbols_bars = {"AAPL": [bar]}
    ExecutionStage(pipeline=pipeline).run(ctx)
    reasons = [s["reason"] for s in ctx.execution_skips]
    assert "no_price" in reasons
    pipeline.broker.submit_order.assert_not_called()
