"""Execution-phase BUY skips must leave durable evidence.

2026-08-19 production forensics: three risk-approved BUYs (XLE/XLF/XLI)
were skipped as unfunded on log-only `continue` statements. The DB showed
no trace, the funnel displayed `proposed_not_executed` with no reason, the
run reported status='executed' with zero orders, and the evening analyst
graded the day as a deliberate no-trade ("need more proactive idea
generation"). Every deterministic skip now appends to ctx.execution_skips
and persists an `execution_skip` evidence row; a morning whose approved
BUYs ALL died on the funding race reports the retryable `buys_unfunded`.
"""
from unittest.mock import MagicMock

from src.models import PortfolioDecision, ReasoningChain, TradeDecision
from src.pipeline_context import RunContext
from src.pipeline_stages import ExecutionStage


def _rc() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="m", news_check="n", earnings_check="e",
        signal_conflicts="s", sizing_logic="z", portfolio_balance="b",
        cash_target="c",
    )


def _pipeline(live_price=100.0, cash=50_000.0):
    pipeline = MagicMock()
    pipeline.broker.get_latest_price.return_value = live_price
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": cash, "portfolio_value": 100_000.0}, [], {},
    )
    pipeline.risk_engine.check_daily_loss.return_value = None
    return pipeline


def _ctx(decisions, cash=50_000.0) -> RunContext:
    ctx = RunContext.start("morning")
    ctx.cash = cash
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = []
    ctx.decision_id = "run-x-dec-abc123"
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_rc(), decisions=decisions, portfolio_view="t",
    )
    ctx.symbols_bars = {}
    return ctx


def _evidence_kinds(pipeline) -> list[str]:
    return [
        call.kwargs.get("kind")
        for call in pipeline.db.insert_specialist_evidence.call_args_list
    ]


def test_stale_entry_skip_is_recorded():
    pipeline = _pipeline(live_price=100.0)
    ctx = _ctx([TradeDecision(
        action="BUY", symbol="SPY", allocation_pct=10,
        entry_price=80.0, stop_loss=72.0, take_profit=130.0,
        reasoning="stale entry",
    )])

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert orders == []
    assert [s["reason"] for s in ctx.execution_skips] == ["stale_entry"]
    assert ctx.execution_skips[0]["symbol"] == "SPY"
    assert "execution_skip" in _evidence_kinds(pipeline)


def test_insufficient_cash_skip_is_recorded():
    """The 2026-08-19 shape: approved BUY, available cash can't cover it."""
    pipeline = _pipeline(live_price=100.0, cash=145.11)
    ctx = _ctx([TradeDecision(
        action="BUY", symbol="XLE", allocation_pct=10,
        entry_price=100.0, stop_loss=95.0, take_profit=115.0,
        reasoning="approved but unfunded",
    )], cash=145.11)

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert orders == []
    reasons = [s["reason"] for s in ctx.execution_skips]
    assert reasons == ["insufficient_cash"]
    assert "145.11" in ctx.execution_skips[0]["detail"]
    assert "execution_skip" in _evidence_kinds(pipeline)
    pipeline.broker.submit_order.assert_not_called()


def test_daily_loss_recheck_records_every_blocked_buy():
    pipeline = _pipeline()
    violation = MagicMock()
    violation.message = "daily loss -3.4% breaches 3.0% limit"
    pipeline.risk_engine.check_daily_loss.return_value = violation
    ctx = _ctx([
        TradeDecision(action="BUY", symbol="XLE", allocation_pct=5,
                      entry_price=100.0, stop_loss=95.0, take_profit=112.0,
                      reasoning="r"),
        TradeDecision(action="BUY", symbol="XLF", allocation_pct=5,
                      entry_price=50.0, stop_loss=47.0, take_profit=57.0,
                      reasoning="r"),
    ])

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert orders == []
    assert sorted(s["symbol"] for s in ctx.execution_skips) == ["XLE", "XLF"]
    assert {s["reason"] for s in ctx.execution_skips} == {"daily_loss_recheck"}


def test_successful_buy_records_no_skip():
    pipeline = _pipeline(live_price=100.0)
    pipeline.broker.submit_order.return_value = {
        "id": "ord-1", "status": "accepted",
    }
    ctx = _ctx([TradeDecision(
        action="BUY", symbol="SPY", allocation_pct=10,
        entry_price=100.0, stop_loss=95.0, take_profit=112.0,
        reasoning="clean",
    )])

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    assert ctx.execution_skips == []


def test_evidence_failure_never_blocks_the_skip_decision():
    """Trading-core rule: forensic persistence failure must not change
    deterministic behavior — the skip still happens, the run continues."""
    pipeline = _pipeline(live_price=100.0, cash=100.0)
    pipeline.db.insert_specialist_evidence.side_effect = RuntimeError("disk full")
    ctx = _ctx([TradeDecision(
        action="BUY", symbol="XLE", allocation_pct=10,
        entry_price=100.0, stop_loss=95.0, take_profit=115.0,
        reasoning="r",
    )], cash=100.0)

    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert orders == []
    assert [s["reason"] for s in ctx.execution_skips] == ["insufficient_cash"]


def test_buys_unfunded_is_retryable_in_main():
    import main as main_mod
    assert "buys_unfunded" in main_mod._RETRYABLE_RESULT_STATUSES
