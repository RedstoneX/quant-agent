"""Pipeline stages — Phase 4 #1 infrastructure.

These tests cover the stage pattern itself: stages take explicit
dependencies at construction, accept a RunContext, populate ctx fields,
and return the context. Exhaustive integration testing of the
MorningResearchStage's parallel fan-out is covered indirectly by the
existing pipeline integration tests in test_pipeline.py.
"""

import json
from unittest.mock import MagicMock, patch

from src.data.news import FeedFailure, NewsCoverage
from src.pipeline_context import RunContext
from src.pipeline_stages import (
    DecisionStage,
    ExecutionStage,
    MorningResearchStage,
    RiskStage,
)


def _mock_stop_seam(broker, *, specs=(), snapshot_ok=True, cancel_ok=True):
    """Wire a MagicMock broker's split stop-cancel seam (audit F1 #1):
    snapshot_protective_stops (read) + cancel_snapshotted_stops (mutate),
    plus the composed cancel_protective_stops for any direct caller."""
    specs = list(specs)
    broker.snapshot_protective_stops.return_value = (snapshot_ok, specs)
    broker.cancel_snapshotted_stops.return_value = cancel_ok
    cleared = snapshot_ok and cancel_ok
    broker.cancel_protective_stops.return_value = (
        cleared, specs if cleared else [],
    )


def _mock_stage_seam(pipeline, *, specs=(), ok=True, wal_row_id=None):
    """Full-MagicMock-pipeline tests: ExecutionStage obtains stops via
    pipeline._cancel_stops_with_write_ahead (audit F1 #1); stub its
    3-tuple directly (the broker seam is never reached on a mock)."""
    pipeline._cancel_stops_with_write_ahead.return_value = (
        ok, list(specs), wal_row_id,
    )
    # Callers unpack finalize's (ok, retry_specs) contract; default to
    # "coverage confirmed" so the full-MagicMock pipeline yields a tuple.
    pipeline._finalize_protection_after_sell.return_value = (True, [])
    # Bind the REAL protected-sell helpers onto the mock so the extracted
    # cancel→submit→accept→restore + wait→finalize discipline actually runs
    # against the mocked broker/seams (real integration, not a no-op mock).
    import types as _types
    from src.pipeline import TradingPipeline as _TP
    pipeline._submit_protected_sell = _types.MethodType(
        _TP._submit_protected_sell, pipeline,
    )
    pipeline._finalize_pending_protections = _types.MethodType(
        _TP._finalize_pending_protections, pipeline,
    )


def _carried_forward_macro_dict(target_invested_pct=55.0):
    """The exact shape `macro_store.load_last_state()` hands back to
    `Pipeline._carry_forward_macro` — see `MacroStore.save_last_state`
    (src/data/macro_store.py:69-97): a plain dict, NOT a MacroAnalysis
    model, with sector_guidance already normalized to {sector: direction}
    and no reasoning_chain at all."""
    return {
        "date": "2026-08-26",
        "regime": "risk-on",
        "confidence": "high",
        "equity_outlook": "bullish",
        "summary": "carried forward from yesterday",
        "position_guidance": {
            "target_invested_pct": target_invested_pct,
            "cash_recommendation_pct": 100.0 - target_invested_pct,
            "reasoning": "x",
        },
        "sector_guidance": {"Technology": "bullish"},
    }


def test_persist_evidence_never_raises_on_db_failure():
    """Stage 4: a specialist-evidence write failure must never propagate
    into the research/decision/risk flow it's forensically recording — it's
    additive and non-authoritative (docs/architecture/MISSION_CONTROL_API.md,
    .claude/rules/trading-core.md)."""
    from src.pipeline_stages import _persist_evidence

    db = MagicMock()
    db.insert_specialist_evidence.side_effect = RuntimeError("disk full")

    _persist_evidence(
        db, run_id="run-1", agent_name="macro_analyst", kind="analysis",
        scope="run", evidence_json="{}",
    )
    # No exception raised — that's the assertion. The call was still
    # attempted (not silently skipped).
    db.insert_specialist_evidence.assert_called_once()


def test_stage_classes_take_pipeline_reference():
    """DecisionStage / RiskStage / ExecutionStage wire a pipeline for helpers."""
    fake_pipeline = MagicMock()
    for cls in (DecisionStage, RiskStage, ExecutionStage):
        stage = cls(pipeline=fake_pipeline)
        assert stage._pipeline is fake_pipeline


def _buy(symbol, alloc):
    from src.models import TradeDecision
    return TradeDecision(
        action="BUY", symbol=symbol, allocation_pct=alloc,
        entry_price=100.0, stop_loss=95.0, take_profit=110.0,
        reasoning="x",
    )


def _pm_rc():
    """Minimal valid PM reasoning_chain — every required step a non-empty
    string per PR #89 min_length=1 enforcement."""
    from src.models import ReasoningChain
    return ReasoningChain(
        macro_filter="x", news_check="x", earnings_check="x",
        signal_conflicts="x", sizing_logic="x",
        portfolio_balance="x", cash_target="x",
    )


def _risk_rc():
    """Minimal valid RM reasoning_chain — every required step a non-empty
    string per PR #89 min_length=1 enforcement."""
    from src.models import RiskReasoningChain
    return RiskReasoningChain(
        rr_audit="x", signal_fidelity="x", correlation_check="x",
        event_risk="x", sizing_sanity="x", overall="x",
    )


def _tech_rc():
    """Minimal valid Tech reasoning_chain."""
    from src.models import TechReasoningChain
    return TechReasoningChain(
        trend="x", momentum="x", volatility="x",
        volume="x", support_resistance="x",
    )


def _hold(symbol):
    from src.models import TradeDecision
    return TradeDecision(
        action="HOLD", symbol=symbol, allocation_pct=0.0,
        entry_price=100.0, stop_loss=95.0, take_profit=110.0,
        reasoning="hold",
    )


def _sell(symbol):
    from src.models import TradeDecision
    return TradeDecision(
        action="SELL", symbol=symbol, allocation_pct=100.0,
        entry_price=100.0, stop_loss=95.0, take_profit=110.0,
        reasoning="exit",
    )


def test_apply_scale_all_buys_zero_drops_every_buy():
    """scale_all_buys=0.0 is the documented full-BUY veto. The pre-fix
    `or 1.0` collapsed 0.0 to 1.0 because Python truthiness, silently
    disabling the veto. Pin: zero passes through and zeros every BUY,
    while HOLD and SELL pass unchanged."""
    from src.models import RiskVerdict
    from src.pipeline_stages import _apply_scale_all_buys

    verdict = RiskVerdict(
        approved=True, scale_all_buys=0.0,
        reasoning_chain=_risk_rc(),
        reasoning="risk-off — kill all BUYs",
    )
    decisions = [_buy("SPY", 10), _buy("QQQ", 8), _hold("MSFT"), _sell("NVDA")]

    scaled, scale = _apply_scale_all_buys(decisions, verdict)

    assert scale == 0.0, "0.0 must propagate, not collapse to 1.0"
    actions = [d.action for d in scaled]
    assert "BUY" not in actions, f"every BUY must be dropped; got {actions}"
    assert "HOLD" in actions and "SELL" in actions


def test_apply_scale_all_buys_partial_scales_buy_allocations():
    """0 < scale < 1 reduces BUY allocations proportionally, keeps HOLD/SELL."""
    from src.models import RiskVerdict
    from src.pipeline_stages import _apply_scale_all_buys

    verdict = RiskVerdict(approved=True, scale_all_buys=0.5, reasoning_chain=_risk_rc(), reasoning="trim")
    decisions = [_buy("SPY", 10), _buy("QQQ", 8), _hold("MSFT")]

    scaled, scale = _apply_scale_all_buys(decisions, verdict)

    assert scale == 0.5
    by_sym = {d.symbol: d for d in scaled}
    assert by_sym["SPY"].allocation_pct == 5.0
    assert by_sym["QQQ"].allocation_pct == 4.0
    assert by_sym["MSFT"].action == "HOLD"


def test_apply_scale_all_buys_one_is_no_op():
    """scale=1.0 (default) leaves decisions untouched."""
    from src.models import RiskVerdict
    from src.pipeline_stages import _apply_scale_all_buys

    verdict = RiskVerdict(approved=True, scale_all_buys=1.0, reasoning_chain=_risk_rc(), reasoning="ok")
    decisions = [_buy("SPY", 10), _buy("QQQ", 8)]

    scaled, scale = _apply_scale_all_buys(decisions, verdict)

    assert scale == 1.0
    assert [(d.symbol, d.allocation_pct) for d in scaled] == [
        ("SPY", 10.0), ("QQQ", 8.0),
    ]


def test_apply_scale_all_buys_handles_missing_attribute_as_one():
    """If a verdict somehow lacks scale_all_buys (legacy or partial parse),
    treat as 1.0 (no scaling) — not as None propagating to a TypeError."""
    from src.pipeline_stages import _apply_scale_all_buys

    class LegacyVerdict:
        approved = True
        # no scale_all_buys attribute
        modifications = []

    decisions = [_buy("SPY", 10)]
    scaled, scale = _apply_scale_all_buys(decisions, LegacyVerdict())

    assert scale == 1.0
    assert len(scaled) == 1





def test_execution_stage_skips_buy_when_entry_price_more_than_5pct_off_market():
    """When LLM's entry_price deviates >5% from live market, the BUY must be
    skipped — not fallback-to-market. A stale entry implies the stop_loss
    (computed against that entry) is also stale, so the whole R/R math is
    unsafe. Better to wait for the next session's fresh signal."""
    from src.models import PortfolioDecision, TradeDecision
    from src.pipeline_context import RunContext

    pipeline = MagicMock()
    pipeline.broker.get_latest_price.return_value = 100.0  # live market
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    # Pre-BUY daily-loss re-check (fix for P1 #1) refreshes account state
    # and consults risk_engine — wire benign defaults so this orthogonal
    # entry-price-stale test isn't entangled with the loss-breach path.
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0}, [], {},
    )
    pipeline.risk_engine.check_daily_loss.return_value = None

    ctx = RunContext.start("morning")
    ctx.cash = 50_000.0
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = []
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[
            # LLM says entry $80, market is $100 → 20% off → must skip.
            TradeDecision(
                action="BUY", symbol="SPY", allocation_pct=10,
                entry_price=80.0, stop_loss=72.0, take_profit=130.0,
                reasoning="stale entry scenario",
            ),
        ],
        portfolio_view="test",
    )
    ctx.symbols_bars = {}

    stage = ExecutionStage(pipeline=pipeline)
    orders = stage.run(ctx)

    assert orders == [], "BUY should have been skipped entirely"
    pipeline.broker.submit_order.assert_not_called()


def test_execution_stage_allows_buy_when_entry_price_within_5pct():
    """A 2% deviation is well within the 5% threshold — BUY proceeds,
    sizing uses the live market price (limit < market → raised to market)."""
    from src.models import PortfolioDecision, TradeDecision
    from src.pipeline_context import RunContext

    pipeline = MagicMock()
    pipeline.broker.get_latest_price.return_value = 100.0
    pipeline.broker.submit_order.return_value = {
        "id": "order-1", "status": "accepted", "symbol": "SPY",
    }
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0}, [], {},
    )
    pipeline.risk_engine.check_daily_loss.return_value = None

    ctx = RunContext.start("morning")
    ctx.cash = 50_000.0
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = []
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[
            # LLM says $98, market $100 → 2% off → proceed. TP=140 keeps
            # the raised-to-market R/R at (140-100)/(100-72)=1.43 — above the
            # audit-round-2 executed-geometry floor of 1.2 (a TP of 130 would
            # now be correctly SKIPPED at 1.07; that case has its own test).
            TradeDecision(
                action="BUY", symbol="SPY", allocation_pct=10,
                entry_price=98.0, stop_loss=72.0, take_profit=140.0,
                reasoning="fresh setup",
            ),
        ],
        portfolio_view="test",
    )
    ctx.symbols_bars = {}

    stage = ExecutionStage(pipeline=pipeline)
    stage.run(ctx)

    pipeline.broker.submit_order.assert_called_once()


def test_execution_stage_blocks_buys_when_daily_loss_breached_during_run():
    """The initial morning circuit breaker (#45) runs before LLM/research
    — but the LLM window is 5-10 min on a slow OpenAI day, plenty of room
    for the tape to gap through the daily-loss limit while PM/RM is
    thinking. With intra_check exempt from the session lock (#46), this
    race is now real: morning's stale snapshot says we can BUY while
    intra is firing emergency sells off the live state.

    Fix is a re-check before the BUY loop: refresh portfolio_value,
    re-run risk_engine.check_daily_loss against ctx.last_equity, and
    drop BUYs if the breach materialised mid-run. SELLs that already
    fired through this session are kept (they reduce exposure, never
    add)."""
    from src.models import PortfolioDecision, Position, TradeDecision
    from src.pipeline_context import RunContext

    pipeline = MagicMock()
    pipeline.broker.get_latest_price.return_value = 100.0
    _mock_stop_seam(pipeline.broker)
    _mock_stage_seam(pipeline)
    # SELL submits cleanly first.
    pipeline.broker.submit_order.return_value = {
        "id": "sell-1", "status": "accepted", "symbol": "JPM",
    }
    pipeline.broker.wait_for_order_terminal.return_value = "filled"
    # After the SELL, refresh shows total_value crashed through the limit.
    pipeline._refresh_account_state.return_value = (
        {"cash": 60_000.0, "portfolio_value": 96_500.0},  # -3.5% from last_equity
        [],
        {},
    )
    loss_violation = MagicMock(message="Daily loss 3.5% exceeds max 3%")
    pipeline.risk_engine.check_daily_loss.return_value = loss_violation
    pipeline._order_accepted.return_value = True
    pipeline._format_qty = lambda q: str(q)
    pipeline._full_sell_qty = lambda q: q
    pipeline.db = MagicMock()

    ctx = RunContext.start("morning")
    ctx.cash = 30_000.0
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = [
        Position(
            symbol="JPM", qty=10.0, avg_entry=300.0, current_price=320.0,
            market_value=3_200.0, unrealized_pnl=200.0, sector="Financial",
        ),
    ]
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[
            TradeDecision(
                action="SELL", symbol="JPM", allocation_pct=100,
                entry_price=300.0, stop_loss=280.0, take_profit=350.0,
                reasoning="thesis broken",
            ),
            TradeDecision(
                action="BUY", symbol="SPY", allocation_pct=10,
                entry_price=99.0, stop_loss=92.0, take_profit=110.0,
                reasoning="dip buy that should be blocked by re-check",
            ),
        ],
        portfolio_view="test",
    )
    ctx.symbols_bars = {}

    stage = ExecutionStage(pipeline=pipeline)
    orders = stage.run(ctx)

    # SELL went through (it fired BEFORE the re-check), BUY blocked.
    submit_calls = pipeline.broker.submit_order.call_args_list
    sides = [c.kwargs.get("side") for c in submit_calls]
    assert "sell" in sides, f"SELL must have fired before the re-check; got {sides}"
    assert "buy" not in sides, (
        f"BUY must be blocked by daily-loss re-check; got submit_calls={submit_calls}"
    )
    pipeline.risk_engine.check_daily_loss.assert_called_with(
        100_000.0, 96_500.0 - 100_000.0,
    )


def test_execution_stage_logs_when_finalize_cannot_confirm_coverage(caplog):
    """Morning SELL path: when finalize can't rebuild coverage (returns
    ok=False, having persisted the recovery intent), the shared helper logs a
    warning so the operator sees protection wasn't confirmed in-session (drain
    rebuilds it next session). With the real protected-sell helpers bound, the
    SELL submits + finalizes against the mocked broker end-to-end."""
    import logging
    from src.models import PortfolioDecision, Position, TradeDecision
    from src.pipeline_context import RunContext

    specs = [{"id": "stop-1", "qty": 10, "stop_price": 280.0, "limit_price": 275.0}]
    pipeline = MagicMock()
    pipeline.broker.get_latest_price.return_value = 320.0
    _mock_stop_seam(pipeline.broker, specs=specs)
    _mock_stage_seam(pipeline, specs=specs)
    # finalize couldn't rebuild coverage → (False, specs).
    pipeline._finalize_protection_after_sell.return_value = (False, list(specs))
    pipeline.broker.submit_order.return_value = {
        "id": "sell-9", "status": "accepted", "symbol": "JPM",
    }
    pipeline.broker.wait_for_order_terminal.return_value = "filled"
    pipeline._refresh_account_state.return_value = (
        {"cash": 60_000.0, "portfolio_value": 100_000.0}, [], {},
    )
    pipeline.risk_engine.check_daily_loss.return_value = None
    pipeline._order_accepted.return_value = True
    pipeline._format_qty = lambda q: str(q)
    pipeline._full_sell_qty = lambda q: q
    pipeline.db = MagicMock()

    ctx = RunContext.start("morning")
    ctx.cash = 30_000.0
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = [
        Position(
            symbol="JPM", qty=10.0, avg_entry=300.0, current_price=320.0,
            market_value=3_200.0, unrealized_pnl=200.0, sector="Financial",
        ),
    ]
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[
            TradeDecision(
                action="SELL", symbol="JPM", allocation_pct=100,
                entry_price=300.0, stop_loss=280.0, take_profit=350.0,
                reasoning="exit",
            ),
        ],
        portfolio_view="test",
    )
    ctx.symbols_bars = {}

    stage = ExecutionStage(pipeline=pipeline)
    # The warning is emitted by _finalize_pending_protections, which lives on
    # TradingPipeline (src.pipeline) now that the loop is extracted.
    with caplog.at_level(logging.WARNING, logger="src.pipeline"):
        stage.run(ctx)

    # The SELL submitted through the real protected-sell helper...
    pipeline.broker.submit_order.assert_called_once()
    assert pipeline.broker.submit_order.call_args.kwargs["side"] == "sell"
    # ...and the finalize-failure warning surfaced.
    assert any(
        "did not confirm stop coverage" in r.getMessage() and "ExecutionStage" in r.getMessage()
        for r in caplog.records
    ), f"expected a finalize-failure warning; got {[r.getMessage() for r in caplog.records]}"


def test_execution_stage_allows_buys_when_daily_loss_not_breached_after_refresh():
    """Sanity check: if the re-check shows no breach, BUYs proceed normally.
    The re-check must not become a permanent BUY block on every morning."""
    from src.models import PortfolioDecision, TradeDecision
    from src.pipeline_context import RunContext

    pipeline = MagicMock()
    pipeline.broker.get_latest_price.return_value = 100.0
    _mock_stop_seam(pipeline.broker)
    pipeline.broker.submit_order.return_value = {
        "id": "buy-1", "status": "accepted", "symbol": "SPY",
    }
    # No sells fired, so refresh runs from inside the re-check branch.
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_500.0},  # +0.5%, no breach
        [],
        {},
    )
    pipeline.risk_engine.check_daily_loss.return_value = None
    pipeline._order_accepted.return_value = True
    pipeline._format_qty = lambda q: str(q)

    ctx = RunContext.start("morning")
    ctx.cash = 50_000.0
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = []
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[
            TradeDecision(
                action="BUY", symbol="SPY", allocation_pct=10,
                entry_price=99.0, stop_loss=92.0, take_profit=110.0,
                reasoning="normal dip buy",
            ),
        ],
        portfolio_view="test",
    )
    ctx.symbols_bars = {}

    stage = ExecutionStage(pipeline=pipeline)
    stage.run(ctx)

    pipeline.broker.submit_order.assert_called_once()
    assert pipeline.broker.submit_order.call_args.kwargs["side"] == "buy"


def test_execution_stage_skips_buy_when_entry_price_above_market_by_more_than_5pct():
    """Symmetric case: LLM proposed entry ABOVE market by >5% — still stale,
    still skip. The direction of the deviation doesn't change the conclusion
    (LLM's thesis was priced at something that isn't the current tape)."""
    from src.models import PortfolioDecision, TradeDecision
    from src.pipeline_context import RunContext

    pipeline = MagicMock()
    pipeline.broker.get_latest_price.return_value = 100.0
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0}, [], {},
    )
    pipeline.risk_engine.check_daily_loss.return_value = None

    ctx = RunContext.start("morning")
    ctx.cash = 50_000.0
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.positions = []
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[
            # LLM says $115, market $100 → 15% above → skip.
            TradeDecision(
                action="BUY", symbol="SPY", allocation_pct=10,
                entry_price=115.0, stop_loss=105.0, take_profit=135.0,
                reasoning="above-market proposal",
            ),
        ],
        portfolio_view="test",
    )
    ctx.symbols_bars = {}

    stage = ExecutionStage(pipeline=pipeline)
    orders = stage.run(ctx)

    assert orders == []
    pipeline.broker.submit_order.assert_not_called()


def test_execution_stage_delegation_runs_pipeline_path():
    """Pipeline's `_execution_stage` thunks into `execution_stage.run(ctx)`."""
    from src.pipeline import TradingPipeline

    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.execution_stage = MagicMock()
    pipeline.execution_stage.run.return_value = ["order-1"]
    ctx = RunContext.start("morning")

    out = TradingPipeline._execution_stage(pipeline, ctx)

    assert out == ["order-1"]
    pipeline.execution_stage.run.assert_called_once_with(ctx)


def test_risk_stage_delegation_returns_early_exit_dict():
    from src.pipeline import TradingPipeline

    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.risk_stage = MagicMock()
    pipeline.risk_stage.run.return_value = {"status": "rejected", "orders": []}
    ctx = RunContext.start("morning")

    out = TradingPipeline._risk_stage(pipeline, ctx)

    assert out["status"] == "rejected"


# ---------------------------------------------------------------------------
# Stage 2 Checkpoint C — deterministic hard-risk block forensic persistence.
#
# Before this, RiskStage.run() returned early with an in-memory
# {"status": "hard_risk_block", "reason": ...} dict whenever the
# deterministic hard-risk gate blocked EVERY candidate before
# risk_manager ever ran — no row of any kind recorded which rule fired.
# `TradingPipeline._persist_hard_risk_block` now writes a forensic
# `agent_logs` row (agent_name="risk_gate") at both early-return sites.
# ---------------------------------------------------------------------------

def test_persist_hard_risk_block_writes_risk_gate_agent_log():
    """Unit test of the persistence helper itself: distinct sentinel
    agent_name (never confused with a real risk_manager LLM call), known-
    zero (not unknown) cost/tokens, decision_id threaded through, reasons
    text preserved verbatim."""
    from src.pipeline import TradingPipeline
    from src.pipeline_context import RunContext as _RunContext

    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.db = MagicMock()
    ctx = _RunContext.start("morning")
    ctx.decision_id = f"{ctx.run_id}-dec-000099"

    TradingPipeline._persist_hard_risk_block(
        pipeline, ctx, "AAPL position would be 25.0% and exceed max 20%",
        stage="pre_rm",
    )

    pipeline.db.insert_agent_log.assert_called_once()
    kwargs = pipeline.db.insert_agent_log.call_args.kwargs
    assert kwargs["agent_name"] == "risk_gate"
    assert kwargs["agent_name"] != "risk_manager"
    assert kwargs["run_id"] == ctx.run_id
    assert kwargs["decision_id"] == ctx.decision_id
    assert kwargs["status"] == "hard_risk_block"
    assert kwargs["model"] == "deterministic"
    assert kwargs["cost_usd"] == 0.0  # known-zero, not None/"unknown"
    assert kwargs["tokens_used"] == 0
    assert "exceed max 20%" in kwargs["full_response"]


def test_persist_hard_risk_block_never_raises_on_db_failure():
    """A persistence failure must never propagate into the risk decision
    path — it's forensic-only, additive, and non-critical."""
    from src.pipeline import TradingPipeline
    from src.pipeline_context import RunContext as _RunContext

    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.db = MagicMock()
    pipeline.db.insert_agent_log.side_effect = RuntimeError("disk full")
    ctx = _RunContext.start("morning")

    TradingPipeline._persist_hard_risk_block(pipeline, ctx, "reason", stage="pre_rm")
    # No exception raised — that's the assertion.


def _risk_stage_pipeline(decisions):
    """A pipeline stub wired just enough for RiskStage.run() to reach the
    hard-risk-block early-return path deterministically, mirroring
    tests/test_bugfixes.py's direct-_filter_hard_risk_decisions style but
    through the full RiskStage.run() so the persistence call site itself
    is exercised, not just the helper."""
    from src.pipeline import TradingPipeline

    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.db = MagicMock()
    pipeline._sweeper = MagicMock(return_value=None)
    pipeline._filter_supported_symbols = MagicMock(return_value=(decisions, []))
    pipeline._clamp_queued_earnings_buys = MagicMock(return_value=decisions)
    return pipeline


def test_risk_stage_persists_hard_risk_block_when_pre_rm_gate_blocks_everything():
    """First early-return site (before risk_manager.review is ever called)."""
    from src.models import PortfolioDecision

    decisions = [_buy("AAPL", 25)]
    pipeline = _risk_stage_pipeline(decisions)
    pipeline._filter_hard_risk_decisions = MagicMock(
        return_value=([], [], ["AAPL position would be 25.0% and exceed max 20%"]),
    )

    ctx = RunContext.start("morning")
    ctx.decision_id = f"{ctx.run_id}-dec-000001"
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(), decisions=decisions, portfolio_view="test",
    )

    stage = RiskStage(pipeline=pipeline)
    result = stage.run(ctx)

    assert result == {
        "status": "hard_risk_block", "orders": [],
        "reason": "AAPL position would be 25.0% and exceed max 20%",
    }
    pipeline.db.insert_agent_log.assert_called_once()
    kwargs = pipeline.db.insert_agent_log.call_args.kwargs
    assert kwargs["agent_name"] == "risk_gate"
    assert kwargs["run_id"] == ctx.run_id
    assert kwargs["decision_id"] == ctx.decision_id
    assert "exceed max 20%" in kwargs["full_response"]


def test_risk_stage_post_rm_refilter_carries_in_drawdown_flag():
    """2026-09-03 audit finding #3: the pre-RM hard-risk filter call passes
    `in_drawdown`, but the post-modifications re-filter call previously
    dropped it (defaulting to False) — the same drawdown state briefly
    became invisible to the gate a second time in the same run, for no
    reason tied to anything RM did. Assert the post-RM call now receives
    the same `in_drawdown` value the pre-RM call computed."""
    from src.models import PortfolioDecision, RiskVerdict

    first_pass_decisions = [_buy("AAPL", 10)]
    pipeline = _risk_stage_pipeline(first_pass_decisions)
    pipeline._apply_risk_modifications = MagicMock(return_value=(first_pass_decisions, []))

    verdict = RiskVerdict(
        approved=True, reasoning_chain=_risk_rc(), reasoning="trim AAPL",
        modifications=[{
            "symbol": "AAPL", "field": "allocation_pct",
            "original_value": 10, "new_value": 5, "reason": "trim sizing",
        }],
    )
    rm_result = MagicMock()
    rm_result.used_fallback = False
    pipeline.risk_manager = MagicMock()
    pipeline.risk_manager.review.return_value = (verdict, rm_result)

    pipeline._filter_hard_risk_decisions = MagicMock(
        side_effect=[
            (first_pass_decisions, [], []),
            (first_pass_decisions, [], []),
        ],
    )

    ctx = RunContext.start("morning")
    ctx.decision_id = f"{ctx.run_id}-dec-000009"
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.recent_performance = {"in_drawdown": True}
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(), decisions=first_pass_decisions, portfolio_view="test",
    )

    stage = RiskStage(pipeline=pipeline)
    stage.run(ctx)

    assert pipeline._filter_hard_risk_decisions.call_count == 2
    pre_rm_kwargs = pipeline._filter_hard_risk_decisions.call_args_list[0].kwargs
    post_rm_kwargs = pipeline._filter_hard_risk_decisions.call_args_list[1].kwargs
    assert pre_rm_kwargs["in_drawdown"] is True
    assert post_rm_kwargs["in_drawdown"] is True


def test_risk_stage_records_visible_event_when_rm_zeroes_a_sell():
    """End-to-end through RiskStage.run() with the REAL (unmocked)
    `_apply_risk_modifications`: an RM modification that zeroes a SELL's
    allocation_pct must not silently cancel the exit, and the refusal must
    be persisted as a visible pipeline event rather than just vanishing.
    Matches the two live 2026-08-24 incidents this fix closes."""
    from src.models import PortfolioDecision, RiskVerdict

    sell = _sell("XLE")
    pipeline = _risk_stage_pipeline([sell])

    verdict = RiskVerdict(
        approved=True, reasoning_chain=_risk_rc(), reasoning="ok",
        modifications=[{
            "symbol": "XLE", "field": "allocation_pct",
            "original_value": 100, "new_value": 0,
            "reason": "RM believes this exit is unnecessary",
        }],
    )
    rm_result = MagicMock()
    rm_result.used_fallback = False
    pipeline.risk_manager = MagicMock()
    pipeline.risk_manager.review.return_value = (verdict, rm_result)

    pipeline._filter_hard_risk_decisions = MagicMock(
        side_effect=[
            ([sell], [], []),
            ([sell], [], []),
        ],
    )

    ctx = RunContext.start("morning")
    ctx.decision_id = f"{ctx.run_id}-dec-000010"
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(), decisions=[sell], portfolio_view="test",
    )

    stage = RiskStage(pipeline=pipeline)
    result = stage.run(ctx)

    # The exit still ships — RM's edit was reverted, not the trade.
    assert result is None
    assert ctx.portfolio_decision.decisions[0].allocation_pct == 100.0

    evidence_calls = pipeline.db.insert_specialist_evidence.call_args_list
    rejection_calls = [
        c for c in evidence_calls
        if c.kwargs.get("kind") == "pipeline_event"
        and "modification_rejected" in c.kwargs.get("evidence_json", "")
    ]
    assert len(rejection_calls) == 1
    assert rejection_calls[0].kwargs["symbol"] == "XLE"


def test_risk_stage_persists_hard_risk_block_when_post_rm_modifications_block_everything():
    """Second early-return site: RM approves with modifications, the
    re-filter after applying them blocks everything. risk_manager.review
    IS reached and logged normally here — the forensic risk_gate row is
    additive on top of (not instead of) the real risk_manager agent_logs
    row RiskStage.run() already writes for a reached RM call."""
    from src.models import PortfolioDecision, RiskVerdict

    first_pass_decisions = [_buy("AAPL", 10)]
    pipeline = _risk_stage_pipeline(first_pass_decisions)
    pipeline._apply_risk_modifications = MagicMock(return_value=(first_pass_decisions, []))

    verdict = RiskVerdict(
        approved=True, reasoning_chain=_risk_rc(), reasoning="trim AAPL",
        modifications=[{
            "symbol": "AAPL", "field": "allocation_pct",
            "original_value": 10, "new_value": 5, "reason": "trim sizing",
        }],
    )
    rm_result = MagicMock()
    rm_result.used_fallback = False
    pipeline.risk_manager = MagicMock()
    pipeline.risk_manager.review.return_value = (verdict, rm_result)

    # First _filter_hard_risk_decisions call (pre-RM) lets the BUY through;
    # second call (post-modifications re-filter) blocks everything.
    pipeline._filter_hard_risk_decisions = MagicMock(
        side_effect=[
            (first_pass_decisions, [], []),
            ([], [], ["AAPL position would be 25.0% and exceed max 20%"]),
        ],
    )

    ctx = RunContext.start("morning")
    ctx.decision_id = f"{ctx.run_id}-dec-000002"
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(), decisions=first_pass_decisions, portfolio_view="test",
    )

    stage = RiskStage(pipeline=pipeline)
    result = stage.run(ctx)

    assert result == {
        "status": "hard_risk_block", "orders": [],
        "reason": "AAPL position would be 25.0% and exceed max 20%",
    }
    # One real risk_manager agent_logs write (RM was reached) + one
    # forensic risk_gate write (post-modifications block).
    assert pipeline.db.insert_agent_log.call_count == 2
    agent_names = [c.kwargs["agent_name"] for c in pipeline.db.insert_agent_log.call_args_list]
    assert agent_names == ["risk_manager", "risk_gate"]
    gate_kwargs = pipeline.db.insert_agent_log.call_args_list[1].kwargs
    assert gate_kwargs["decision_id"] == ctx.decision_id
    assert "exceed max 20%" in gate_kwargs["full_response"]


def test_risk_stage_reads_macro_target_pct_from_carried_forward_dict():
    """Regression for the AttributeError bug on the RiskStage side:
    `macro_analysis.position_guidance.target_invested_pct` blows up when
    ctx.macro_analysis is the plain dict Pipeline._carry_forward_macro
    installs. RiskStage.run() must read it via the dual-shape helper and
    set ctx.macro_target_pct from the carried-forward snapshot instead of
    crashing (which previously killed the whole scan non-fatally, silently
    producing nothing)."""
    from src.models import PortfolioDecision

    decisions = [_buy("AAPL", 25)]
    pipeline = _risk_stage_pipeline(decisions)
    # Block everything on the pre-RM hard gate so run() returns right after
    # macro_target_pct is computed, without needing to mock the RM call.
    pipeline._filter_hard_risk_decisions = MagicMock(
        return_value=([], [], ["AAPL position would be 25.0% and exceed max 20%"]),
    )

    ctx = RunContext.start("intra_check")
    ctx.decision_id = f"{ctx.run_id}-dec-000003"
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.macro_analysis = _carried_forward_macro_dict(target_invested_pct=62.5)
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(), decisions=decisions, portfolio_view="test",
    )

    stage = RiskStage(pipeline=pipeline)
    # The bug: macro_analysis.position_guidance raises AttributeError on a
    # dict. No exception is the primary assertion here.
    result = stage.run(ctx)

    assert result == {
        "status": "hard_risk_block", "orders": [],
        "reason": "AAPL position would be 25.0% and exceed max 20%",
    }
    assert ctx.macro_target_pct == 62.5
    # The hard-risk filter itself must have been invoked WITH that figure —
    # a carried-forward macro snapshot degrading to None here would silently
    # disable the macro-target check for the rest of the run.
    fh_kwargs = pipeline._filter_hard_risk_decisions.call_args.kwargs
    assert fh_kwargs["macro_target_invested_pct"] == 62.5


def test_risk_stage_macro_target_pct_degrades_to_none_when_guidance_missing():
    """A carried-forward snapshot may legitimately lack position_guidance
    (e.g. very old / partially-written state). Must degrade to None — the
    existing "not provided" path — never raise."""
    from src.models import PortfolioDecision

    decisions = [_buy("AAPL", 5)]
    pipeline = _risk_stage_pipeline(decisions)
    pipeline._filter_hard_risk_decisions = MagicMock(
        return_value=([], [], ["blocked"]),
    )

    ctx = RunContext.start("intra_check")
    ctx.decision_id = f"{ctx.run_id}-dec-000004"
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.macro_analysis = {"regime": "risk-on"}  # no position_guidance key
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(), decisions=decisions, portfolio_view="test",
    )

    RiskStage(pipeline=pipeline).run(ctx)

    assert ctx.macro_target_pct is None


def test_risk_parse_failure_is_agent_failure_not_rejection():
    """No validated RiskVerdict means the agent failed; it did not veto."""
    from src.agents.base import AgentResult
    from src.models import PortfolioDecision

    decisions = [_buy("AAPL", 10)]
    pipeline = _risk_stage_pipeline(decisions)
    pipeline._filter_hard_risk_decisions = MagicMock(
        return_value=(decisions, [], []),
    )
    pipeline.risk_manager = MagicMock()
    pipeline.risk_manager.review.return_value = (
        None,
        AgentResult(
            raw_text="not valid json", tokens_used=10, model="test-model",
            user_message="risk input",
        ),
    )

    ctx = RunContext.start("morning")
    ctx.decision_id = f"{ctx.run_id}-dec-failed"
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(), decisions=decisions, portfolio_view="test",
    )

    result = RiskStage(pipeline=pipeline).run(ctx)

    assert result == {
        "status": "agent_failure", "orders": [],
        "reason": "risk_manager_unparseable_output",
    }
    risk_log = pipeline.db.insert_agent_log.call_args.kwargs
    assert risk_log["status"] == "agent_failure"
    evidence = pipeline.db.insert_specialist_evidence.call_args.kwargs
    assert evidence["kind"] == "agent_failure"


def test_decision_stage_delegation_returns_none():
    """Method contract preserved: _decision_stage mutates ctx, returns None."""
    from src.pipeline import TradingPipeline

    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.decision_stage = MagicMock()
    ctx = RunContext.start("morning")

    out = TradingPipeline._decision_stage(pipeline, ctx)

    assert out is None
    pipeline.decision_stage.run.assert_called_once_with(ctx)


def test_decision_stage_passes_carried_forward_macro_dict_to_pm_unchanged():
    """Regression for the AttributeError bug: when Pipeline._carry_forward_macro
    finds today's macro state already on disk, ctx.macro_analysis is a plain
    dict (see _carried_forward_macro_dict), not a MacroAnalysis model.
    DecisionStage.run() must pass it straight through to
    portfolio_manager.decide() — calling .model_dump() on a dict raises
    AttributeError and previously killed the whole intraday scan silently."""
    from src.pipeline import TradingPipeline

    p = TradingPipeline.__new__(TradingPipeline)
    p.db = MagicMock()
    p.db.get_latest_insights.return_value = None
    p._sweeper = MagicMock(return_value=None)
    p._compute_recent_performance = MagicMock(return_value={})
    p._build_position_history = MagicMock(return_value={})
    p._build_weekly_narrative = MagicMock(return_value="")
    p._build_macro_trajectory = MagicMock(return_value="")
    p._build_active_state_changes = MagicMock(return_value="")
    p._build_rm_recent_verdicts = MagicMock(return_value="")
    p._build_pm_recent_decisions = MagicMock(return_value="")
    p._build_projected_portfolio = MagicMock(return_value="")
    p._build_calibration_note = MagicMock(return_value="")
    p._build_macro_tech_alignment = MagicMock(return_value="")
    p._build_recent_missed_lessons = MagicMock(return_value="")
    p._build_recent_loss_pits = MagicMock(return_value="")
    p._build_pm_facts = MagicMock(return_value=MagicMock())
    p._ensure_correlation_matrix = MagicMock(return_value={})
    p.config = MagicMock()
    p.config.risk.allow_margin = False
    p.config.trading.universe = []
    p._last_symbol_sectors = {}
    p.portfolio_manager = MagicMock()
    # Early-return path: DecisionStage bails right after `decide()` when
    # portfolio_decision is falsy, so no need to mock the constructor tail.
    p.portfolio_manager.decide.return_value = (
        None, MagicMock(user_message="m", raw_text="{}", tokens_used=1,
                        input_tokens=1, output_tokens=1, cost_usd=0.0,
                        model="test-model"),
    )

    macro_dict = _carried_forward_macro_dict()
    ctx = RunContext.start("intra_check")
    ctx.positions = []
    ctx.analyses = []
    ctx.macro_analysis = macro_dict
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.deployable_cash = 50_000.0
    ctx.admitted_symbols = set()

    # The bug: macro_analysis.model_dump() on a dict raises AttributeError.
    # No exception is the primary assertion here.
    DecisionStage(pipeline=p).run(ctx)

    kwargs = p.portfolio_manager.decide.call_args.kwargs
    assert kwargs["macro_analysis"] == macro_dict, (
        "carried-forward macro dict must reach the PM call unmodified, "
        "not silently dropped as None"
    )


def test_decision_stage_still_model_dumps_a_fresh_macro_model():
    """Companion regression: when macro DID run this tick, ctx.macro_analysis
    is a real MacroAnalysis (Pydantic) model. The fix must not break that
    path — .model_dump() should still be called so the PM sees a plain
    dict either way."""
    from src.pipeline import TradingPipeline
    from src.models import (
        MacroAnalysis, MacroPositionGuidance, MacroReasoningChain,
    )

    p = TradingPipeline.__new__(TradingPipeline)
    p.db = MagicMock()
    p.db.get_latest_insights.return_value = None
    p._sweeper = MagicMock(return_value=None)
    p._compute_recent_performance = MagicMock(return_value={})
    p._build_position_history = MagicMock(return_value={})
    p._build_weekly_narrative = MagicMock(return_value="")
    p._build_macro_trajectory = MagicMock(return_value="")
    p._build_active_state_changes = MagicMock(return_value="")
    p._build_rm_recent_verdicts = MagicMock(return_value="")
    p._build_pm_recent_decisions = MagicMock(return_value="")
    p._build_projected_portfolio = MagicMock(return_value="")
    p._build_calibration_note = MagicMock(return_value="")
    p._build_macro_tech_alignment = MagicMock(return_value="")
    p._build_recent_missed_lessons = MagicMock(return_value="")
    p._build_recent_loss_pits = MagicMock(return_value="")
    p._build_pm_facts = MagicMock(return_value=MagicMock())
    p._ensure_correlation_matrix = MagicMock(return_value={})
    p.config = MagicMock()
    p.config.risk.allow_margin = False
    p.config.trading.universe = []
    p._last_symbol_sectors = {}
    p.portfolio_manager = MagicMock()
    p.portfolio_manager.decide.return_value = (
        None, MagicMock(user_message="m", raw_text="{}", tokens_used=1,
                        input_tokens=1, output_tokens=1, cost_usd=0.0,
                        model="test-model"),
    )

    macro_model = MacroAnalysis(
        regime="risk-on", confidence="high", equity_outlook="bullish",
        summary="fresh macro run",
        position_guidance=MacroPositionGuidance(
            target_invested_pct=55.0, cash_recommendation_pct=45.0, reasoning="x",
        ),
        sector_guidance=[],
        reasoning_chain=MacroReasoningChain(
            volatility_analysis="x", yield_curve_analysis="x",
            monetary_policy_analysis="x", inflation_labor_credit="x",
            cross_signal_synthesis="x", sector_implications="x",
        ),
    )
    ctx = RunContext.start("morning")
    ctx.positions = []
    ctx.analyses = []
    ctx.macro_analysis = macro_model
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.deployable_cash = 50_000.0
    ctx.admitted_symbols = set()

    DecisionStage(pipeline=p).run(ctx)

    kwargs = p.portfolio_manager.decide.call_args.kwargs
    assert kwargs["macro_analysis"] == macro_model.model_dump()


def test_decision_stage_threads_the_configured_rr_floor_and_starter_size():
    """The sub-floor catalyst gate must run on the SAME two numbers the
    deterministic risk layer downstream uses — the floor the constructor will
    actually enforce, and the size `allocate_risk_budget` will actually grant.
    Re-defaulting them inside the agent would let a settings.yaml override
    move one and not the other (2026-09-02)."""
    from src.pipeline import TradingPipeline

    p = TradingPipeline.__new__(TradingPipeline)
    p.db = MagicMock()
    p.db.get_latest_insights.return_value = None
    p._sweeper = MagicMock(return_value=None)
    p._compute_recent_performance = MagicMock(return_value={})
    p._build_position_history = MagicMock(return_value={})
    p._build_weekly_narrative = MagicMock(return_value="")
    p._build_macro_trajectory = MagicMock(return_value="")
    p._build_active_state_changes = MagicMock(return_value="")
    p._build_rm_recent_verdicts = MagicMock(return_value="")
    p._build_pm_recent_decisions = MagicMock(return_value="")
    p._build_projected_portfolio = MagicMock(return_value="")
    p._build_calibration_note = MagicMock(return_value="")
    p._build_macro_tech_alignment = MagicMock(return_value="")
    p._build_recent_missed_lessons = MagicMock(return_value="")
    p._build_recent_loss_pits = MagicMock(return_value="")
    p._build_pm_facts = MagicMock(return_value=MagicMock())
    p._ensure_correlation_matrix = MagicMock(return_value={})
    p.config = MagicMock()
    p.config.risk.allow_margin = False
    # Deliberately NOT the defaults, so a hardcoded number cannot pass.
    p.config.risk.min_reward_risk_after_widening = 1.9
    p.config.risk.min_position_risk_pct = 0.3
    p.config.trading.universe = []
    p._last_symbol_sectors = {}
    p.portfolio_manager = MagicMock()
    p.portfolio_manager.decide.return_value = (
        None, MagicMock(user_message="m", raw_text="{}", tokens_used=1,
                        input_tokens=1, output_tokens=1, cost_usd=0.0,
                        model="test-model"),
    )

    ctx = RunContext.start("morning")
    ctx.positions = []
    ctx.analyses = []
    ctx.macro_analysis = None
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.deployable_cash = 50_000.0
    ctx.admitted_symbols = set()

    DecisionStage(pipeline=p).run(ctx)

    kwargs = p.portfolio_manager.decide.call_args.kwargs
    assert kwargs["rr_floor"] == 1.9
    assert kwargs["starter_risk_pct"] == 0.3


def test_morning_research_stage_constructs_with_all_deps():
    """Stage wiring — all required dependencies exposed as constructor kwargs."""
    stage = MorningResearchStage(
        config=MagicMock(),
        db=MagicMock(),
        market=MagicMock(),
        macro=MagicMock(),
        news_provider=MagicMock(),
        news_store=MagicMock(),
        macro_store=MagicMock(),
        tech_store=MagicMock(),
        earnings_provider=MagicMock(),
        macro_analyst=MagicMock(),
        news_analyst=MagicMock(),
        tech_analyst=MagicMock(),
        earnings_analyst=MagicMock(),
        has_actionable_signal_fn=lambda *args, **kw: True,
        run_news_update_fn=lambda *a, **kw: (None, None),
        load_earnings_analyses_fn=lambda *a, **kw: ([], []),
    )
    assert stage is not None
    # Dependencies retained as attributes for future tests to swap in
    assert callable(stage._has_actionable_signal)


def test_morning_research_stage_populates_ctx_on_success():
    """Stage.run(ctx) fills in macro_analysis / news_intel / analyses / earnings_results."""
    from src.models import MacroAnalysis, MacroReasoningChain, MacroPositionGuidance
    from src.agents.base import AgentResult

    ma = MacroAnalysis(
        reasoning_chain=MacroReasoningChain(
            volatility_analysis="a", yield_curve_analysis="b",
            monetary_policy_analysis="c", inflation_labor_credit="d",
            cross_signal_synthesis="e", sector_implications="f",
        ),
        regime="risk-on", confidence="high", equity_outlook="bullish",
        position_guidance=MacroPositionGuidance(
            target_invested_pct=70, cash_recommendation_pct=30, reasoning="y",
        ),
        summary="z",
    )
    agent_result = AgentResult(raw_text="{}", tokens_used=100, model="test", user_message="x")

    mock_config = MagicMock()
    mock_config.trading.universe = ["NVDA"]
    mock_config.trading.lookback_days = 30
    mock_config.llm.macro_analyst_model = "claude-opus-4-6"
    mock_config.llm.tech_analyst_model = "claude-opus-4-6"

    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (ma, agent_result)

    market = MagicMock()
    market.get_ohlcv.return_value = []  # Skip NVDA → empty symbols_data → tech returns empty

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None

    stage = MorningResearchStage(
        config=mock_config,
        db=MagicMock(),
        market=market,
        macro=MagicMock(),
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=MagicMock(),
        earnings_provider=MagicMock(),
        macro_analyst=macro_agent,
        news_analyst=MagicMock(),
        tech_analyst=MagicMock(),
        earnings_analyst=MagicMock(),
        has_actionable_signal_fn=lambda *args, **kw: False,
        run_news_update_fn=lambda run_id, session: (None, None),
        load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
    )

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.macro_analysis is not None
    assert result_ctx.macro_analysis.regime == "risk-on"
    assert result_ctx.data_status["macro"] == "ok"
    # News / earnings returned None / empty — should be handled gracefully
    assert result_ctx.news_intel is None
    assert result_ctx.analyses == []
    assert result_ctx.earnings_results == []


def test_morning_research_stage_records_admission_reason_without_collision():
    """A qualified external SEC candidate must not abort morning research.

    Admission details deliberately contain their own ``reason`` field.  The
    lifecycle event also has a canonical reason, so the two values must be
    stored under distinct keys instead of being passed twice to the recorder.
    """
    from types import SimpleNamespace

    from src.agents.base import AgentResult

    config = SimpleNamespace(
        trading=SimpleNamespace(universe=["SPY"], lookback_days=30),
        smart_money=SimpleNamespace(enabled=True),
    )
    market = MagicMock()
    market.get_ohlcv.return_value = []
    macro = MagicMock()
    macro.get_macro_summary.return_value = {}
    macro_analyst = MagicMock()
    macro_analyst.analyze.return_value = (
        None,
        AgentResult(raw_text="{}", tokens_used=0, model="test", user_message="x"),
    )
    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None
    smart_money_provider = MagicMock()
    smart_money_provider.fetch.return_value = ([SimpleNamespace(symbol="RSG")], None)
    admission = {
        "temporary": True,
        "reason": "material_sec_form4_purchase",
        "transaction_value_usd": 87_980_000.0,
    }
    db = MagicMock()

    stage = MorningResearchStage(
        config=config,
        db=db,
        market=market,
        macro=macro,
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=MagicMock(),
        earnings_provider=MagicMock(),
        macro_analyst=macro_analyst,
        news_analyst=MagicMock(),
        tech_analyst=MagicMock(),
        earnings_analyst=MagicMock(),
        smart_money_provider=smart_money_provider,
        smart_money_analyst=None,
        admit_smart_money_candidates_fn=lambda _observations: (
            {"RSG"}, {"RSG": admission},
        ),
        has_actionable_signal_fn=lambda *args, **kwargs: False,
        run_news_update_fn=lambda *args, **kwargs: (None, None),
        load_earnings_analyses_fn=lambda *args, **kwargs: ([], []),
    )
    ctx = RunContext.start("morning")
    ctx.positions = []

    result_ctx = stage.run(ctx)

    assert result_ctx.admitted_symbols == {"RSG"}
    assert [call.args[0] for call in market.get_ohlcv.call_args_list[:2]] == [
        "RSG", "SPY",
    ]
    event_payloads = [
        json.loads(call.kwargs["evidence_json"])
        for call in db.insert_specialist_evidence.call_args_list
        if call.kwargs["agent_name"] == "pipeline"
        and call.kwargs["kind"] == "pipeline_event"
        and call.kwargs["symbol"] == "RSG"
    ]
    admission_event = next(
        payload for payload in event_payloads
        if payload["outcome"] == "admitted"
    )
    assert admission_event["reason"] == "smart_money_form4_admission"
    assert admission_event["admission_reason"] == "material_sec_form4_purchase"
    assert admission_event["transaction_value_usd"] == 87_980_000.0


def test_morning_research_stage_smart_money_truncated_marks_status_truncated():
    """2026-09-02: smart_money_analyst_max_tokens was measured against real
    production truncations (finish_reason=length). A truncated response can
    still parse to syntactically valid-but-incomplete JSON, which must not
    be silently recorded as "ok" (clean run) or "empty" (no signal) —
    those are both currently-reachable branches in this stage's status
    logic. It must land as its own "truncated" status so the notifier's
    degraded banner picks it up.
    """
    from types import SimpleNamespace

    from src.agents.base import AgentResult

    config = SimpleNamespace(
        trading=SimpleNamespace(universe=["SPY"], lookback_days=30),
        smart_money=SimpleNamespace(enabled=True),
    )
    market = MagicMock()
    market.get_ohlcv.return_value = []
    macro = MagicMock()
    macro.get_macro_summary.return_value = {}
    macro_analyst = MagicMock()
    macro_analyst.analyze.return_value = (
        None,
        AgentResult(raw_text="{}", tokens_used=0, model="test", user_message="x"),
    )
    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None

    smart_money_provider = MagicMock()
    smart_money_provider.fetch.return_value = ([SimpleNamespace(symbol="RSG")], None)

    truncated_result = AgentResult(
        raw_text='{"findings":[]}', tokens_used=3000, model="test",
        user_message="x", output_tokens=3000, finish_reason="length",
        truncated=True,
    )
    smart_money_analyst = MagicMock()
    # Findings list is empty (as a truncated call would plausibly look
    # like "no signal") and there's no analysis_error — the parse
    # succeeded on the incomplete-but-valid JSON.
    smart_money_analyst.analyze.return_value = ([], truncated_result, None)

    db = MagicMock()

    stage = MorningResearchStage(
        config=config,
        db=db,
        market=market,
        macro=macro,
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=MagicMock(),
        earnings_provider=MagicMock(),
        macro_analyst=macro_analyst,
        news_analyst=MagicMock(),
        tech_analyst=MagicMock(),
        earnings_analyst=MagicMock(),
        smart_money_provider=smart_money_provider,
        smart_money_analyst=smart_money_analyst,
        admit_smart_money_candidates_fn=lambda _observations: (set(), {}),
        has_actionable_signal_fn=lambda *args, **kwargs: False,
        run_news_update_fn=lambda *args, **kwargs: (None, None),
        load_earnings_analyses_fn=lambda *args, **kwargs: ([], []),
    )
    ctx = RunContext.start("morning")
    ctx.positions = []

    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["smart_money"] == "truncated"


@patch("src.pipeline_stages.compute_indicators")
def test_morning_research_stage_tech_partial_batch_marks_status_partial(mock_compute_indicators):
    """2026-08-19 Tech batch-response symbol-loss fix, pipeline-level: when
    tech_analyst.analyze_batch comes back with some symbols unresolved
    (None), MorningResearchStage must (a) still populate ctx.analyses with
    the symbols that DID resolve — never crash on the None entries when
    updating tech_store/computing ages — and (b) set
    data_status['tech'] = 'partial' rather than a plain 'ok' that hides
    the loss. This is the exact production incident (1/10 symbols parsed)
    reaching the pipeline layer, not just the agent layer."""
    mock_compute_indicators.return_value = MagicMock()

    mock_config = MagicMock()
    mock_config.trading.universe = ["AAPL", "MSFT"]
    mock_config.trading.lookback_days = 30

    market = MagicMock()
    market.get_ohlcv.return_value = [MagicMock()]

    tech_analyst = MagicMock()
    from src.models import TechAnalysisResult, TechReasoningChain
    resolved = TechAnalysisResult(
        symbol="AAPL", rating="buy", conviction="medium",
        entry_price=100.0, stop_loss=95.0, reference_target=110.0,
        support_levels=[95.0], resistance_levels=[110.0],
        setup_type="range", expected_horizon_sessions=10,
        reasoning_chain=TechReasoningChain(
            trend="x", momentum="x", volatility="x", volume="x",
            support_resistance="x",
        ),
        reasoning="test",
    )
    # AAPL resolved, MSFT explicitly failed even after tech_analyst's own
    # retry — the sentinel this whole fix introduces.
    tech_analyst.analyze_batch.return_value = (
        {"AAPL": resolved, "MSFT": None},
        MagicMock(user_message="m", raw_text="{}", tokens_used=1,
                  input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"),
    )

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None
    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (None, MagicMock(
        user_message="m", raw_text="{}", tokens_used=1, model="t",
        input_tokens=1, output_tokens=1, cost_usd=0.0,
    ))
    tech_store = MagicMock()
    tech_store.load.return_value = {}
    tech_store.compute_ages.return_value = {}

    stage = MorningResearchStage(
        config=mock_config,
        db=MagicMock(),
        market=market,
        macro=MagicMock(),
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=tech_store,
        earnings_provider=MagicMock(),
        macro_analyst=macro_agent,
        news_analyst=MagicMock(),
        tech_analyst=tech_analyst,
        earnings_analyst=MagicMock(),
        has_actionable_signal_fn=lambda *args, **kw: True,
        run_news_update_fn=lambda run_id, session: (None, None),
        load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
    )

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    # AAPL (resolved) is a real analysis; MSFT (None) never becomes a fake
    # TechAnalysisResult and never crashes tech_store.update/compute_ages.
    assert [a.symbol for a in result_ctx.analyses] == ["AAPL"]
    assert result_ctx.data_status["tech"] == "partial"
    tech_store.update.assert_called_once_with([resolved])


def _tech_stage_for_conviction_test(analyses_map):
    """Shared MorningResearchStage wiring for the low-conviction tests below
    — every symbol resolves (no None entries), only `conviction` varies."""
    mock_config = MagicMock()
    mock_config.trading.universe = ["AAPL", "MSFT"]
    mock_config.trading.lookback_days = 30

    market = MagicMock()
    market.get_ohlcv.return_value = [MagicMock()]

    tech_analyst = MagicMock()
    tech_analyst.analyze_batch.return_value = (
        analyses_map,
        MagicMock(user_message="m", raw_text="{}", tokens_used=1,
                  input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"),
    )

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None
    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (None, MagicMock(
        user_message="m", raw_text="{}", tokens_used=1, model="t",
        input_tokens=1, output_tokens=1, cost_usd=0.0,
    ))
    tech_store = MagicMock()
    tech_store.load.return_value = {}
    tech_store.compute_ages.return_value = {}

    return MorningResearchStage(
        config=mock_config,
        db=MagicMock(),
        market=market,
        macro=MagicMock(),
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=tech_store,
        earnings_provider=MagicMock(),
        macro_analyst=macro_agent,
        news_analyst=MagicMock(),
        tech_analyst=tech_analyst,
        earnings_analyst=MagicMock(),
        has_actionable_signal_fn=lambda *args, **kw: True,
        run_news_update_fn=lambda run_id, session: (None, None),
        load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
    )


@patch("src.pipeline_stages.compute_indicators")
def test_morning_research_stage_tech_full_batch_low_conviction_marks_low_confidence(
    mock_compute_indicators,
):
    """2026-09-04 data-honesty fix: a batch where every symbol resolves
    (data_status would otherwise be a plain 'ok') must not hide a read the
    model itself flagged as low-conviction. One low-conviction read among
    otherwise-resolved symbols downgrades data_status['tech'] to the new
    'low_confidence' value — real, visible, but distinct from 'partial'/
    'failed' since the content is present, just self-reportedly shaky."""
    mock_compute_indicators.return_value = MagicMock()

    from src.models import TechAnalysisResult, TechReasoningChain

    def _mk(symbol, conviction):
        return TechAnalysisResult(
            symbol=symbol, rating="buy", conviction=conviction,
            entry_price=100.0, stop_loss=95.0, reference_target=110.0,
            support_levels=[95.0], resistance_levels=[110.0],
            setup_type="range", expected_horizon_sessions=10,
            reasoning_chain=TechReasoningChain(
                trend="x", momentum="x", volatility="x", volume="x",
                support_resistance="x",
            ),
            reasoning="test",
        )

    analyses_map = {"AAPL": _mk("AAPL", "medium"), "MSFT": _mk("MSFT", "low")}
    stage = _tech_stage_for_conviction_test(analyses_map)

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    # Both symbols resolved — this is NOT a partial/failed batch.
    assert {a.symbol for a in result_ctx.analyses} == {"AAPL", "MSFT"}
    assert result_ctx.data_status["tech"] == "low_confidence"


@patch("src.pipeline_stages.compute_indicators")
def test_morning_research_stage_tech_full_batch_high_conviction_stays_ok(
    mock_compute_indicators,
):
    """Control case: a fully-resolved batch with no low-conviction reads is
    unaffected by the new check and still reports plain 'ok'."""
    mock_compute_indicators.return_value = MagicMock()

    from src.models import TechAnalysisResult, TechReasoningChain

    def _mk(symbol, conviction):
        return TechAnalysisResult(
            symbol=symbol, rating="buy", conviction=conviction,
            entry_price=100.0, stop_loss=95.0, reference_target=110.0,
            support_levels=[95.0], resistance_levels=[110.0],
            setup_type="range", expected_horizon_sessions=10,
            reasoning_chain=TechReasoningChain(
                trend="x", momentum="x", volatility="x", volume="x",
                support_resistance="x",
            ),
            reasoning="test",
        )

    analyses_map = {"AAPL": _mk("AAPL", "high"), "MSFT": _mk("MSFT", "medium")}
    stage = _tech_stage_for_conviction_test(analyses_map)

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert {a.symbol for a in result_ctx.analyses} == {"AAPL", "MSFT"}
    assert result_ctx.data_status["tech"] == "ok"


def _minimal_news_report(confidence="medium"):
    from src.models import MacroNarrative, NewsIntelligenceReport
    return NewsIntelligenceReport(
        macro_narrative=MacroNarrative(
            last_updated="2026-08-28", era_themes=["AI capex"],
            current_regime="risk-on",
        ),
        pm_briefing="Quiet tape.",
        market_sentiment="neutral", confidence=confidence,
    )


def _news_coverage_stage(run_news_update_fn):
    """Minimal MorningResearchStage wiring shared by the coverage tests
    below — only the news branch varies between them."""
    mock_config = MagicMock()
    mock_config.trading.universe = ["AAPL"]
    mock_config.trading.lookback_days = 30

    market = MagicMock()
    market.get_ohlcv.return_value = []  # skip tech entirely, not under test here

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None
    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (None, MagicMock(
        user_message="m", raw_text="{}", tokens_used=1, model="t",
        input_tokens=1, output_tokens=1, cost_usd=0.0,
    ))

    return MorningResearchStage(
        config=mock_config,
        db=MagicMock(),
        market=market,
        macro=MagicMock(),
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=MagicMock(),
        earnings_provider=MagicMock(),
        macro_analyst=macro_agent,
        news_analyst=MagicMock(),
        tech_analyst=MagicMock(),
        earnings_analyst=MagicMock(),
        has_actionable_signal_fn=lambda *args, **kw: False,
        run_news_update_fn=run_news_update_fn,
        load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
    )


def test_morning_research_stage_news_partial_coverage_marks_status_partial():
    """2026-08-28 coverage fix, pipeline level: two feeds down (Reuters 404,
    AP 403) out of nine configured, but the seven survivors were enough for
    the analyst to produce a valid report. Before the fix, data_status['news']
    was 'ok' purely because the LLM call parsed — this asserts it is now
    'partial', which is the whole point of tracking coverage at all."""
    report = _minimal_news_report()
    coverage = NewsCoverage(
        configured=9, succeeded=7,
        failed=[
            FeedFailure(name="Reuters Business", reason="HTTP Error 404: Not Found"),
            FeedFailure(name="AP Business", reason="HTTP Error 403: Forbidden"),
        ],
    )
    stage = _news_coverage_stage(lambda run_id, session: (report, coverage))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.news_intel is report
    assert result_ctx.data_status["news"] == "partial"
    assert result_ctx.data_status["news"] != "ok"


def test_morning_research_stage_news_total_feed_failure_marks_status_failed_even_when_report_parses():
    """The exact scenario the fix targets: ALL configured feeds fail (a
    total wire outage), yet the analyst still returns a technically-valid
    report (e.g. 'no fresh headlines, neutral sentiment'). Coverage failure
    must dominate — this must read as 'failed', never 'ok', regardless of
    whether the LLM call itself succeeded on empty input. This is the
    'coverage is NOT reported complete' assertion at the pipeline layer."""
    report = _minimal_news_report()
    coverage = NewsCoverage(
        configured=9, succeeded=0,
        failed=[FeedFailure(name=f"Feed {i}", reason="timed out") for i in range(9)],
    )
    stage = _news_coverage_stage(lambda run_id, session: (report, coverage))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["news"] == "failed"
    assert result_ctx.data_status["news"] != "ok"


def test_morning_research_stage_news_full_coverage_marks_status_ok():
    """Control case: 9/9 feeds returned data and the report parsed — this
    is the one scenario that legitimately reads as 'ok'."""
    report = _minimal_news_report()
    coverage = NewsCoverage(configured=9, succeeded=9, failed=[])
    stage = _news_coverage_stage(lambda run_id, session: (report, coverage))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["news"] == "ok"


# === Earnings content-honesty (2026-09-04) ===
#
# Real incident this closes: `data_status["earnings"]` used to be "ok"
# whenever `earnings_future.result()` returned without raising, with zero
# check of what the returned `earnings_results` actually contained — the
# exact shape of a real production incident where 12/67 filings once came
# back schema-valid with ZERO extracted figures. See
# `_classify_earnings_status` in src/pipeline_stages.py.

def _valid_earnings_analysis(symbol="AAPL", **field_overrides) -> dict:
    """A schema-valid `EarningsAnalysis.model_dump()` with real figures in
    every checked field — the clean control case. Tests override just the
    fields they care about via dotted-path shortcuts below."""
    from src.models import (
        EarningsAnalysis, EarningsBalanceSheet, EarningsCashFlow,
        EarningsInvestmentImplications, EarningsProfitability,
        EarningsReasoningChain, EarningsRevenue,
    )
    analysis = EarningsAnalysis(
        symbol=symbol, form_type="10-Q", filing_date="2026-09-01",
        revenue=EarningsRevenue(total="$50B", yoy_growth="12%"),
        profitability=EarningsProfitability(
            gross_margin="45%", operating_margin="20%",
            net_income="$5B", eps="$2.50",
        ),
        cash_flow=EarningsCashFlow(
            operating_cf="$8B", free_cf="$6B", capex="$2B",
        ),
        balance_sheet=EarningsBalanceSheet(
            cash_and_equivalents="$10B", total_debt="$0",
            assessment="strong balance sheet",
        ),
        guidance="Raised FY guidance.",
        investment_implications=EarningsInvestmentImplications(
            sentiment="bullish", conviction="medium",
            reasoning_chain=EarningsReasoningChain(
                fundamental_quality="x", growth_trajectory="x",
                strategic_risks="x", management_execution="x",
                valuation_context="x",
            ),
            key_thesis="Strong quarter.",
        ),
        data_quality="All figures sourced directly from the filing.",
    )
    dumped = analysis.model_dump()
    for dotted_path, value in field_overrides.items():
        parts = dotted_path.split(".")
        target = dumped
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
    return dumped


def _empty_earnings_analysis(symbol="AAPL", **field_overrides) -> dict:
    """A schema-valid `EarningsAnalysis.model_dump()` that structurally has
    NO real figures anywhere — every checked field left at its
    'not disclosed' default. Reproduces the real 12/67-zero-figures
    incident: the LLM call succeeds, the schema validates, but nothing
    usable came back."""
    return _valid_earnings_analysis(
        symbol=symbol,
        **{
            "revenue.total": "not disclosed",
            "revenue.yoy_growth": "not disclosed",
            "profitability.gross_margin": "not disclosed",
            "profitability.operating_margin": "not disclosed",
            "profitability.net_income": "not disclosed",
            "profitability.eps": "not disclosed",
            "cash_flow.operating_cf": "not disclosed",
            "cash_flow.free_cf": "not disclosed",
            "cash_flow.capex": "not disclosed",
            "balance_sheet.cash_and_equivalents": "not disclosed",
            "balance_sheet.total_debt": "not disclosed",
            **field_overrides,
        },
    )


def _earnings_stage_for(load_earnings_analyses_fn):
    """Minimal MorningResearchStage wiring shared by the earnings
    content-honesty tests below — only the earnings branch varies."""
    mock_config = MagicMock()
    mock_config.trading.universe = ["AAPL"]
    mock_config.trading.lookback_days = 30

    market = MagicMock()
    market.get_ohlcv.return_value = []  # skip tech entirely, not under test here

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None
    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (None, MagicMock(
        user_message="m", raw_text="{}", tokens_used=1, model="t",
        input_tokens=1, output_tokens=1, cost_usd=0.0,
    ))

    return MorningResearchStage(
        config=mock_config,
        db=MagicMock(),
        market=market,
        macro=MagicMock(),
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=MagicMock(),
        earnings_provider=MagicMock(),
        macro_analyst=macro_agent,
        news_analyst=MagicMock(),
        tech_analyst=MagicMock(),
        earnings_analyst=MagicMock(),
        has_actionable_signal_fn=lambda *args, **kw: False,
        run_news_update_fn=lambda run_id, session: (None, None),
        load_earnings_analyses_fn=load_earnings_analyses_fn,
    )


def test_earnings_status_ok_when_no_new_filing_today():
    """The ordinary, most-common day: no filings at all. This is legitimately
    benign, not a failure — must stay 'ok', never read as 'empty' or worse."""
    stage = _earnings_stage_for(lambda run_id, session, ctx=None: ([], []))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["earnings"] == "ok"


def test_earnings_status_ok_when_filing_has_real_figures():
    """Control case: a filing was analyzed and the structured fields carry
    real content — clean 'ok', matching the pre-fix behavior for the
    genuinely-good case."""
    results = [{"symbol": "AAPL", "analysis": _valid_earnings_analysis(), "is_new": True}]
    stage = _earnings_stage_for(lambda run_id, session, ctx=None: ([], results))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["earnings"] == "ok"


def test_earnings_status_content_missing_when_structured_fields_all_empty():
    """The real incident, reproduced: the future resolves without raising and
    the schema validates, but every checked figure is the 'not disclosed'
    sentinel — no real content was actually extracted. Before this fix,
    data_status['earnings'] would have read 'ok'. Must alert (excluded from
    the notifier's ('ok', 'empty') non-alerting set), so it must NOT be
    literally 'empty'."""
    results = [{"symbol": "AAPL", "analysis": _empty_earnings_analysis(), "is_new": True}]
    stage = _earnings_stage_for(lambda run_id, session, ctx=None: ([], results))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["earnings"] == "content_missing"
    assert result_ctx.data_status["earnings"] not in ("ok", "empty")


def test_earnings_status_content_missing_when_self_reported_data_quality_flags_problem():
    """Second real bug: the structured fields technically parse (real
    figures present) but the LLM's own `data_quality` self-report says it
    could not actually get the data. The owner's own framing: a seat must
    never claim 'ok' while its own free-text field says something is wrong.
    This must downgrade status even though structured content looks fine."""
    results = [{
        "symbol": "AAPL",
        "analysis": _valid_earnings_analysis(
            data_quality="Unable to extract segment detail; filing incomplete in the excerpt provided.",
        ),
        "is_new": True,
    }]
    stage = _earnings_stage_for(lambda run_id, session, ctx=None: ([], results))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["earnings"] == "content_missing"


def test_earnings_status_partial_when_some_filings_good_some_not():
    """Mixed batch: one filing genuinely has figures, another came back
    content-empty. Mirrors tech's 'partial' semantics for a mixed batch —
    not blanket 'ok', not blanket failure either."""
    results = [
        {"symbol": "AAPL", "analysis": _valid_earnings_analysis(symbol="AAPL"), "is_new": True},
        {"symbol": "MSFT", "analysis": _empty_earnings_analysis(symbol="MSFT"), "is_new": True},
    ]
    stage = _earnings_stage_for(lambda run_id, session, ctx=None: ([], results))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["earnings"] == "partial"


def test_earnings_status_ok_when_only_queued_placeholders():
    """A filing exists but preprocess hasn't analyzed it yet (`queued=True`,
    `analysis=None`) — already surfaced honestly via the `queued` flag and
    sized around downstream. This pass does not repurpose that state; a
    queued-only run stays 'ok' rather than inventing a new status for it."""
    results = [{
        "symbol": "AAPL", "analysis": None, "is_new": True,
        "queued": True, "form_type": "10-Q", "filing_date": "2026-09-01",
    }]
    stage = _earnings_stage_for(lambda run_id, session, ctx=None: ([], results))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["earnings"] == "ok"


def test_classify_earnings_status_real_zero_is_not_treated_as_missing():
    """A legitimately zero figure (e.g. a company that carries no debt,
    reported as an explicit '$0') must NOT be conflated with the 'not
    disclosed' absence sentinel — the exact 'zero is overloaded' mistake
    this codebase already paid for once (docs/WORK.md item 13, PR #255)."""
    from src.pipeline_stages import _classify_earnings_status

    analysis = _valid_earnings_analysis(**{"balance_sheet.total_debt": "$0"})
    results = [{"symbol": "AAPL", "analysis": analysis, "is_new": True}]

    assert _classify_earnings_status(results) == "ok"


def test_earnings_data_quality_flags_problem_matches_real_failure_phrases():
    from src.pipeline_stages import _earnings_data_quality_flags_problem

    assert _earnings_data_quality_flags_problem(
        "Unable to find revenue breakdown in the excerpt.",
    ) is True
    assert _earnings_data_quality_flags_problem(
        "All figures sourced directly from the filing.",
    ) is False
    assert _earnings_data_quality_flags_problem("not disclosed") is False
    assert _earnings_data_quality_flags_problem("") is False


def test_morning_research_stage_news_low_self_reported_confidence_marks_status_low_confidence():
    """The confidence half of the honesty fix: full coverage AND a clean
    parse (the one case the coverage fix alone reads as 'ok'), but the
    model's own `confidence` field says 'low' — a thin or contradictory
    read on headlines it did actually receive. Coverage can't catch this;
    it only counts whether feeds returned data, not whether the model made
    sense of what they returned. Before this, that combination was
    indistinguishable from a clean, trustworthy 'ok' run anywhere
    data_status['news'] is read (Telegram data-quality alert, status
    board). This must read as its own distinct status, not 'ok'."""
    report = _minimal_news_report(confidence="low")
    coverage = NewsCoverage(configured=9, succeeded=9, failed=[])
    stage = _news_coverage_stage(lambda run_id, session: (report, coverage))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["news"] == "low_confidence"
    assert result_ctx.data_status["news"] != "ok"


def test_morning_research_stage_news_high_confidence_full_coverage_stays_ok():
    """Control case for the confidence check: high self-reported confidence
    on full coverage must NOT be touched by the new override — only 'low'
    downgrades an otherwise-'ok' status."""
    report = _minimal_news_report(confidence="high")
    coverage = NewsCoverage(configured=9, succeeded=9, failed=[])
    stage = _news_coverage_stage(lambda run_id, session: (report, coverage))

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["news"] == "ok"


@patch("src.pipeline_stages.compute_indicators")
def test_morning_research_stage_persists_specialist_evidence(mock_compute_indicators, tmp_path):
    """Stage 4: MorningResearchStage persists already-validated macro/news/
    tech evidence into `specialist_evidence` with natural scope (run for
    macro/news, symbol for tech) and no decision_id (research-phase,
    generated later in DecisionStage) — read back via a real DB file."""
    import sqlite3

    from src.agents.base import AgentResult
    from src.models import (
        MacroAnalysis,
        MacroNarrative,
        MacroPositionGuidance,
        MacroReasoningChain,
        NewsIntelligenceReport,
        TechAnalysisResult,
    )
    from src.storage.db import Database

    mock_compute_indicators.return_value = MagicMock()

    ma = MacroAnalysis(
        reasoning_chain=MacroReasoningChain(
            volatility_analysis="a", yield_curve_analysis="b",
            monetary_policy_analysis="c", inflation_labor_credit="d",
            cross_signal_synthesis="e", sector_implications="f",
        ),
        regime="risk-on", confidence="high", equity_outlook="bullish",
        position_guidance=MacroPositionGuidance(
            target_invested_pct=70, cash_recommendation_pct=30, reasoning="y",
        ),
        summary="z",
    )
    news_intel = NewsIntelligenceReport(
        macro_narrative=MacroNarrative(
            last_updated="2026-04-17", era_themes=["AI capex"],
            current_regime="risk-on expansion",
        ),
        pm_briefing="Quiet tape.",
        market_sentiment="bullish", confidence="medium",
    )
    agent_result = AgentResult(raw_text="{}", tokens_used=100, model="test", user_message="x")

    mock_config = MagicMock()
    mock_config.trading.universe = ["NVDA"]
    mock_config.trading.lookback_days = 30
    mock_config.llm.macro_analyst_model = "claude-opus-4-6"
    mock_config.llm.tech_analyst_model = "claude-opus-4-6"

    market = MagicMock()
    market.get_ohlcv.return_value = [
        MagicMock(date="2026-04-17", open=99, high=101, low=98, close=100, volume=1000)
    ]
    market.get_valuation_metrics.return_value = {}

    macro_provider = MagicMock()
    macro_provider.get_macro_summary.return_value = {
        "vix": {"current": 18.0}, "credit_spread": {"current_bps": 300},
        "inflation": {"core_cpi_yoy": 3.0}, "unemployment": {"current": 4.2},
    }

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = {}
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None

    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (ma, agent_result)

    tech_agent = MagicMock()
    tech_agent.analyze_batch.return_value = (
        {"NVDA": TechAnalysisResult(
            symbol="NVDA", rating="buy", conviction="high",
            entry_price=100.0, reference_target=110.0, stop_loss=95.0,
            support_levels=[95.0], resistance_levels=[110.0],
            setup_type="range", expected_horizon_sessions=10,
            reasoning="fresh setup", reasoning_chain=_tech_rc(),
        )},
        agent_result,
    )
    tech_store = MagicMock()
    tech_store.load.return_value = {}
    tech_store.compute_ages.return_value = {}

    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    try:
        stage = MorningResearchStage(
            config=mock_config,
            db=db,
            market=market,
            macro=macro_provider,
            news_provider=MagicMock(),
            news_store=news_store,
            macro_store=macro_store,
            tech_store=tech_store,
            earnings_provider=MagicMock(),
            macro_analyst=macro_agent,
            news_analyst=MagicMock(),
            tech_analyst=tech_agent,
            earnings_analyst=MagicMock(),
            has_actionable_signal_fn=lambda *args, **kw: True,
            run_news_update_fn=lambda run_id, session: (news_intel, None),
            load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
        )
        ctx = RunContext.start("morning")
        ctx.positions = []
        stage.run(ctx)

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT agent_name, kind, scope, symbol, decision_id, evidence_json "
            "FROM specialist_evidence ORDER BY id"
        ).fetchall()
        by_key = {(r["agent_name"], r["kind"], r["symbol"]): r for r in rows}
        conn.close()

        macro_row = by_key[("macro_analyst", "analysis", None)]
        assert macro_row["scope"] == "run" and macro_row["decision_id"] is None

        news_row = by_key[("news_analyst", "analysis", None)]
        assert news_row["scope"] == "run" and news_row["decision_id"] is None
        assert json.loads(news_row["evidence_json"])["pm_briefing"] == "Quiet tape."

        tech_row = by_key[("tech_analyst", "analysis", "NVDA")]
        assert tech_row["scope"] == "symbol" and tech_row["decision_id"] is None
        assert json.loads(tech_row["evidence_json"])["rating"] == "buy"
    finally:
        db.close()


@patch("src.pipeline_stages.compute_indicators")
def test_morning_research_stage_tech_uses_prior_macro_snapshot(mock_compute_indicators):
    from src.agents.base import AgentResult
    from src.models import (
        MacroAnalysis,
        MacroPositionGuidance,
        MacroReasoningChain,
        TechAnalysisResult,
    )

    mock_compute_indicators.return_value = MagicMock()

    ma = MacroAnalysis(
        reasoning_chain=MacroReasoningChain(
            volatility_analysis="a", yield_curve_analysis="b",
            monetary_policy_analysis="c", inflation_labor_credit="d",
            cross_signal_synthesis="e", sector_implications="f",
        ),
        regime="risk-on", confidence="high", equity_outlook="bullish",
        position_guidance=MacroPositionGuidance(
            target_invested_pct=70, cash_recommendation_pct=30, reasoning="y",
        ),
        summary="z",
    )
    agent_result = AgentResult(raw_text="{}", tokens_used=100, model="test", user_message="x")

    mock_config = MagicMock()
    mock_config.trading.universe = ["NVDA"]
    mock_config.trading.lookback_days = 30
    mock_config.llm.macro_analyst_model = "claude-opus-4-6"
    mock_config.llm.tech_analyst_model = "claude-opus-4-6"

    market = MagicMock()
    market.get_ohlcv.return_value = [
        MagicMock(date="2026-04-17", open=99, high=101, low=98, close=100, volume=1000)
    ]
    market.get_valuation_metrics.return_value = {}

    macro_provider = MagicMock()
    macro_provider.get_macro_summary.return_value = {
        "vix": {"current": 18.0},
        "credit_spread": {"current_bps": 300},
        "inflation": {"core_cpi_yoy": 3.0},
        "unemployment": {"current": 4.2},
    }

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = {
        "regime": "risk-off",
        "equity_outlook": "bearish",
    }
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = "prior narrative"

    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (ma, agent_result)

    tech_agent = MagicMock()
    tech_agent.analyze_batch.return_value = (
        {
            "NVDA": TechAnalysisResult(
                symbol="NVDA", rating="buy", conviction="high",
                entry_price=100.0, reference_target=110.0, stop_loss=95.0,
                support_levels=[95.0], resistance_levels=[110.0],
                setup_type="range", expected_horizon_sessions=10,
                reasoning="fresh setup", reasoning_chain=_tech_rc(),
            )
        },
        agent_result,
    )

    tech_store = MagicMock()
    tech_store.load.return_value = {}
    tech_store.compute_ages.return_value = {}

    stage = MorningResearchStage(
        config=mock_config,
        db=MagicMock(),
        market=market,
        macro=macro_provider,
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=tech_store,
        earnings_provider=MagicMock(),
        macro_analyst=macro_agent,
        news_analyst=MagicMock(),
        tech_analyst=tech_agent,
        earnings_analyst=MagicMock(),
        has_actionable_signal_fn=lambda *args, **kw: True,
        run_news_update_fn=lambda run_id, session: (None, None),
        load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
    )

    ctx = RunContext.start("morning")
    ctx.positions = []
    stage.run(ctx)

    assert macro_store.load_last_state.call_count == 1
    tech_kwargs = tech_agent.analyze_batch.call_args.kwargs
    assert tech_kwargs["prior_macro_regime"] == "risk-off"
    assert tech_kwargs["prior_macro_outlook"] == "bearish"


# --------------------------------------------------------------------------
# `_build_active_state_changes` — Phase 13 catalyst-gate fix: rendering the
# per-symbol `(direction)` suffix `PortfolioManagerAgent.
# _state_change_symbols_by_date` parses back out.
# --------------------------------------------------------------------------

def test_build_active_state_changes_renders_direction_per_symbol():
    from src.pipeline import TradingPipeline

    fake_pipeline = MagicMock()
    fake_pipeline.news_store.recent_state_changes.return_value = [{
        "first_seen_date": "2026-08-31",
        "event": "Oil majors expand footprint, bearish for airlines",
        "affected_symbols": ["XOM", "CVX", "COST"],
        "symbol_direction": {"XOM": "bullish", "CVX": "bullish", "COST": "bearish"},
    }]
    rendered = TradingPipeline._build_active_state_changes(fake_pipeline)
    assert rendered == (
        "- [2026-08-31] Oil majors expand footprint, bearish for airlines "
        "→ XOM(bullish), CVX(bullish), COST(bearish)"
    )


def test_build_active_state_changes_renders_unknown_for_a_symbol_with_no_direction():
    """A symbol named in `affected_symbols` but absent from
    `symbol_direction` (older persisted report predating this field, or a
    genuine analyst omission) renders as `(unknown)` — never silently
    treated as agreeing with any trade direction."""
    from src.pipeline import TradingPipeline

    fake_pipeline = MagicMock()
    fake_pipeline.news_store.recent_state_changes.return_value = [{
        "first_seen_date": "2026-08-30",
        "event": "Some older event",
        "affected_symbols": ["FOO"],
        # no "symbol_direction" key at all — pre-Phase-13 persisted shape
    }]
    rendered = TradingPipeline._build_active_state_changes(fake_pipeline)
    assert rendered == "- [2026-08-30] Some older event → FOO(unknown)"


# === Levels coverage — closing the "silent data failure looks like a quiet
# day" hole (2026-09-02). See `_check_levels_coverage`'s own docstring in
# src/pipeline_stages.py for why 2026-09-01 is NOT the day this reproduces
# (computed_levels did not exist in the code that ran that morning) and what
# actually caused that day's zero trades (the R/R-geometry defect, unrelated
# to structure). These tests cover the new mechanism on its own terms: does
# it record coverage every run, and does it alert on the cases it claims to
# catch and stay quiet on the cases it doesn't. ===

def _levels_analysis(symbol, levels):
    """A minimal resolved TechAnalysisResult carrying a given computed_levels
    — the only field these tests vary."""
    from src.models import TechAnalysisResult
    return TechAnalysisResult(
        symbol=symbol, rating="neutral", conviction="low",
        entry_price=100.0, stop_loss=95.0, reference_target=110.0,
        support_levels=[], resistance_levels=[],
        setup_type="range", expected_horizon_sessions=10,
        reasoning_chain=_tech_rc(), reasoning="test",
        computed_levels=levels,
    )


def _coverage_ctx(*, universe, bars_missing, run_id="run-test00000000"):
    ctx = RunContext.start("morning")
    ctx.run_id = run_id
    ctx.tech_bars_coverage = {
        "universe": universe,
        "bars_fetched": universe - bars_missing,
        "bars_missing": bars_missing,
        "bars_missing_symbols": [],
    }
    return ctx


def test_check_levels_coverage_persists_evidence_on_a_normal_run():
    """The `levels_coverage` evidence row is written even when nothing is
    wrong — observability cannot be conditional on severity, or the one
    normal day nobody looks at twice is exactly the day with no record."""
    from src.pipeline_stages import _check_levels_coverage

    ctx = _coverage_ctx(universe=12, bars_missing=0)
    analyses = [_levels_analysis(f"S{i}", [100.0]) for i in range(11)] + [
        _levels_analysis("EMPTY1", []),
    ]
    db = MagicMock()
    with patch("src.notifier.send_owner_alert") as alert:
        _check_levels_coverage(db, ctx, analyses)

    assert not alert.called
    db.insert_specialist_evidence.assert_called_once()
    kwargs = db.insert_specialist_evidence.call_args.kwargs
    assert kwargs["run_id"] == ctx.run_id
    assert kwargs["agent_name"] == "tech_analyst"
    assert kwargs["kind"] == "levels_coverage"
    assert kwargs["scope"] == "run"
    assert kwargs["symbol"] is None
    payload = json.loads(kwargs["evidence_json"])
    assert payload["universe"] == 12
    assert payload["bars_missing"] == 0
    assert payload["resolved"] == 12
    assert payload["levels_present"] == 11
    assert payload["levels_empty"] == 1
    assert payload["levels_empty_symbols"] == ["EMPTY1"]


def test_check_levels_coverage_matches_the_only_real_baseline_measured():
    """Regression pin for the one clean normal-run measurement that exists:
    2026-09-02's morning run had 64 resolved analyses, 63 with a computed
    level and 1 (MRVL) without — 1.6% empty. The threshold derivation in
    LEVELS_DEGRADED_RUN_EMPTY_SHARE's comment is only honest if that shape
    genuinely does not alert; pin it so a future change to the arithmetic
    can't silently start paging the owner every morning."""
    from src.pipeline_stages import _check_levels_coverage

    ctx = _coverage_ctx(universe=64, bars_missing=0)
    analyses = [_levels_analysis(f"S{i}", [100.0]) for i in range(63)] + [
        _levels_analysis("MRVL", []),
    ]
    db = MagicMock()
    with patch("src.notifier.send_owner_alert") as alert:
        _check_levels_coverage(db, ctx, analyses)

    assert not alert.called


def test_check_levels_coverage_alerts_red_on_total_levels_blackout():
    """Every resolved symbol comes back with zero structural levels — the
    unambiguous case the task calls out by name. A live feed does not put
    every chart in one batch into the identical empty state; this is a
    provider failure, not 12 coincidentally structureless charts."""
    from src.pipeline_stages import _check_levels_coverage

    ctx = _coverage_ctx(universe=12, bars_missing=0)
    analyses = [_levels_analysis(f"S{i}", []) for i in range(12)]
    db = MagicMock()
    with patch("src.notifier.send_owner_alert") as alert:
        _check_levels_coverage(db, ctx, analyses)

    alert.assert_called_once()
    body = alert.call_args.args[0]
    assert body.startswith("🔴 TECH DATA BLIND SPOT")
    assert "structural level" in body
    assert "12" in body


def test_check_levels_coverage_alerts_red_on_total_bars_blackout():
    """No symbol in the universe got bars back at all — tech_analyst never
    even ran (symbols_data was empty), so `analyses` is []. This is the
    sharper, earlier form of blindness: the pre-filter and tech_analyst
    both look identical to 'nothing worth trading' from downstream."""
    from src.pipeline_stages import _check_levels_coverage

    ctx = _coverage_ctx(universe=15, bars_missing=15)
    db = MagicMock()
    with patch("src.notifier.send_owner_alert") as alert:
        _check_levels_coverage(db, ctx, [])

    alert.assert_called_once()
    body = alert.call_args.args[0]
    assert body.startswith("🔴 TECH DATA BLIND SPOT")
    assert "data feed" in body
    assert "15" in body


def test_check_levels_coverage_alerts_orange_at_the_degraded_boundary():
    """Exactly half of resolved symbols come back empty: above the coarse
    50% tripwire but not total. Must alert, and must NOT use the RED/total
    header — the message should read as serious-but-partial, matching the
    existing 🔴 NO STOP AT ALL / 🟠 STOP PARTIALLY COVERS severity split."""
    from src.pipeline_stages import _check_levels_coverage

    ctx = _coverage_ctx(universe=10, bars_missing=0)
    analyses = [_levels_analysis(f"E{i}", []) for i in range(5)] + [
        _levels_analysis(f"F{i}", [100.0]) for i in range(5)
    ]
    db = MagicMock()
    with patch("src.notifier.send_owner_alert") as alert:
        _check_levels_coverage(db, ctx, analyses)

    alert.assert_called_once()
    body = alert.call_args.args[0]
    assert body.startswith("🟠 TECH DATA DEGRADED")
    assert "5/10" in body


def test_check_levels_coverage_no_alert_just_under_the_degraded_boundary():
    """49% empty must NOT alert — pins the boundary from the other side so
    `>=` vs `>` cannot silently flip without a test noticing."""
    from src.pipeline_stages import _check_levels_coverage

    ctx = _coverage_ctx(universe=100, bars_missing=0)
    analyses = [_levels_analysis(f"E{i}", []) for i in range(49)] + [
        _levels_analysis(f"F{i}", [100.0]) for i in range(51)
    ]
    db = MagicMock()
    with patch("src.notifier.send_owner_alert") as alert:
        _check_levels_coverage(db, ctx, analyses)

    assert not alert.called


def test_check_levels_coverage_small_universe_guarded_from_false_alarm():
    """3 resolved symbols, all empty, is 100% — but 3 is noise, not a
    universe. LEVELS_COVERAGE_MIN_SAMPLE must suppress it; without the
    guard this is exactly the small-N case that would page the owner off
    a 2-symbol rehearsal or test run."""
    from src.pipeline_stages import _check_levels_coverage

    ctx = _coverage_ctx(universe=3, bars_missing=0)
    analyses = [_levels_analysis(f"S{i}", []) for i in range(3)]
    db = MagicMock()
    with patch("src.notifier.send_owner_alert") as alert:
        _check_levels_coverage(db, ctx, analyses)

    assert not alert.called
    # Still recorded — the guard suppresses the ALERT, not the observability.
    db.insert_specialist_evidence.assert_called_once()


def test_check_levels_coverage_never_raises_when_bars_coverage_absent():
    """If `_run_tech` crashed before ever setting ctx.tech_bars_coverage
    (still its `{}` dataclass default), the bars axis must be skipped
    quietly rather than raising — and the levels axis, which does not
    depend on it, must still be evaluated correctly."""
    from src.pipeline_stages import _check_levels_coverage

    ctx = RunContext.start("morning")
    assert ctx.tech_bars_coverage == {}
    analyses = [_levels_analysis(f"S{i}", []) for i in range(12)]
    db = MagicMock()
    with patch("src.notifier.send_owner_alert") as alert:
        _check_levels_coverage(db, ctx, analyses)  # must not raise

    alert.assert_called_once()
    assert "structural level" in alert.call_args.args[0]
    assert "bar fetch" not in alert.call_args.args[0]  # bars axis had nothing to say


def test_check_levels_coverage_never_raises_on_malformed_coverage_dict():
    """A defensive check, not a real production shape: if
    ctx.tech_bars_coverage were ever malformed, this must degrade to
    'nothing to report' rather than crashing MorningResearchStage.run —
    matching _persist_evidence's own never-raises contract."""
    from src.pipeline_stages import _check_levels_coverage

    ctx = _coverage_ctx(universe=12, bars_missing=0)
    ctx.tech_bars_coverage = {"universe": "not-a-number"}
    db = MagicMock()
    with patch("src.notifier.send_owner_alert") as alert:
        _check_levels_coverage(db, ctx, [_levels_analysis("A", [100.0])])  # must not raise

    assert not alert.called


@patch("src.pipeline_stages.compute_indicators")
def test_morning_research_stage_alerts_owner_on_full_universe_levels_blackout(
    mock_compute_indicators,
):
    """End-to-end through MorningResearchStage.run(): every symbol gets
    bars, tech_analyst resolves all of them, and every one comes back with
    an empty computed_levels — the shape a dead structural-levels
    computation (or a feed silently serving unusable history to every
    request) would produce. Confirms the wiring from `_run_tech` (setting
    ctx.tech_bars_coverage) through to `_check_levels_coverage` actually
    fires the owner alert in the real stage, not just in the unit test
    calling it directly."""
    mock_compute_indicators.return_value = MagicMock()

    universe = [f"SYM{i}" for i in range(12)]
    mock_config = MagicMock()
    mock_config.trading.universe = universe
    mock_config.trading.lookback_days = 30

    market = MagicMock()
    market.get_ohlcv.return_value = [MagicMock()]

    from src.models import TechAnalysisResult
    analyses_map = {
        sym: TechAnalysisResult(
            symbol=sym, rating="neutral", conviction="low",
            entry_price=100.0, stop_loss=95.0, reference_target=110.0,
            support_levels=[], resistance_levels=[],
            setup_type="range", expected_horizon_sessions=10,
            reasoning_chain=_tech_rc(), reasoning="test",
            computed_levels=[],  # every symbol blind
        )
        for sym in universe
    }
    tech_agent = MagicMock()
    tech_agent.analyze_batch.return_value = (
        analyses_map,
        MagicMock(user_message="m", raw_text="{}", tokens_used=1,
                   input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"),
    )

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None
    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (None, MagicMock(
        user_message="m", raw_text="{}", tokens_used=1, model="t",
        input_tokens=1, output_tokens=1, cost_usd=0.0,
    ))
    tech_store = MagicMock()
    tech_store.load.return_value = {}
    tech_store.compute_ages.return_value = {}

    stage = MorningResearchStage(
        config=mock_config,
        db=MagicMock(),
        market=market,
        macro=MagicMock(),
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=tech_store,
        earnings_provider=MagicMock(),
        macro_analyst=macro_agent,
        news_analyst=MagicMock(),
        tech_analyst=tech_agent,
        earnings_analyst=MagicMock(),
        has_actionable_signal_fn=lambda *args, **kw: True,
        run_news_update_fn=lambda run_id, session: (None, None),
        load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
    )

    ctx = RunContext.start("morning")
    ctx.positions = []
    with patch("src.notifier.send_owner_alert") as alert:
        result_ctx = stage.run(ctx)

    assert len(result_ctx.analyses) == 12
    assert result_ctx.tech_bars_coverage["bars_missing"] == 0
    alert.assert_called_once()
    assert alert.call_args.args[0].startswith("🔴 TECH DATA BLIND SPOT")


@patch("src.pipeline_stages.compute_indicators")
def test_morning_research_stage_no_alert_when_bars_fetch_partly_fails_normally(
    mock_compute_indicators,
):
    """A couple of symbols missing bars out of a large universe is normal
    (a delisting, a temporary halt) and must not page the owner — only a
    majority-or-more failure should. Also confirms ctx.tech_bars_coverage
    counts the drop instead of only logging it (2026-09-02 fix)."""
    mock_compute_indicators.return_value = MagicMock()

    universe = [f"SYM{i}" for i in range(12)]
    missing = {"SYM0", "SYM1"}
    mock_config = MagicMock()
    mock_config.trading.universe = universe
    mock_config.trading.lookback_days = 30

    def _get_ohlcv(symbol, _lookback):
        return [] if symbol in missing else [MagicMock()]

    market = MagicMock()
    market.get_ohlcv.side_effect = _get_ohlcv

    from src.models import TechAnalysisResult
    resolved_symbols = [s for s in universe if s not in missing]
    analyses_map = {
        sym: TechAnalysisResult(
            symbol=sym, rating="neutral", conviction="low",
            entry_price=100.0, stop_loss=95.0, reference_target=110.0,
            support_levels=[], resistance_levels=[],
            setup_type="range", expected_horizon_sessions=10,
            reasoning_chain=_tech_rc(), reasoning="test",
            computed_levels=[100.0],  # every RESOLVED symbol has a level
        )
        for sym in resolved_symbols
    }
    tech_agent = MagicMock()
    tech_agent.analyze_batch.return_value = (
        analyses_map,
        MagicMock(user_message="m", raw_text="{}", tokens_used=1,
                   input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"),
    )

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None
    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (None, MagicMock(
        user_message="m", raw_text="{}", tokens_used=1, model="t",
        input_tokens=1, output_tokens=1, cost_usd=0.0,
    ))
    tech_store = MagicMock()
    tech_store.load.return_value = {}
    tech_store.compute_ages.return_value = {}

    stage = MorningResearchStage(
        config=mock_config,
        db=MagicMock(),
        market=market,
        macro=MagicMock(),
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=tech_store,
        earnings_provider=MagicMock(),
        macro_analyst=macro_agent,
        news_analyst=MagicMock(),
        tech_analyst=tech_agent,
        earnings_analyst=MagicMock(),
        has_actionable_signal_fn=lambda *args, **kw: True,
        run_news_update_fn=lambda run_id, session: (None, None),
        load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
    )

    ctx = RunContext.start("morning")
    ctx.positions = []
    with patch("src.notifier.send_owner_alert") as alert:
        result_ctx = stage.run(ctx)

    assert result_ctx.tech_bars_coverage["bars_missing"] == 2
    assert result_ctx.tech_bars_coverage["universe"] == 12
    assert set(result_ctx.tech_bars_coverage["bars_missing_symbols"]) == missing
    assert not alert.called


@patch("src.pipeline_stages.compute_indicators")
def test_morning_research_stage_records_bars_coverage_even_when_tech_analyst_crashes(
    mock_compute_indicators, tmp_path,
):
    """Bar fetch is the FIRST thing `_run_tech` does, before tech_analyst is
    ever called — so if analyze_batch itself raises (LLM/provider crash,
    not a bars problem), the coverage evidence row must still land with the
    correct bars numbers. This is the scenario the placement of
    `_check_levels_coverage` OUTSIDE the tech try/except in
    MorningResearchStage.run exists for: a tech-stage failure severe enough
    to crash the batch is exactly a case worth recording, not one to skip
    because the surrounding try/except already caught something. Uses a
    real on-disk DB (not a Mock) so the row is read back rather than
    asserted against a mock's call args."""
    import sqlite3

    from src.storage.db import Database

    mock_compute_indicators.return_value = MagicMock()

    universe = [f"SYM{i}" for i in range(12)]
    mock_config = MagicMock()
    mock_config.trading.universe = universe
    mock_config.trading.lookback_days = 30

    market = MagicMock()
    market.get_ohlcv.return_value = [MagicMock()]  # bars fine for everyone

    tech_agent = MagicMock()
    tech_agent.analyze_batch.side_effect = RuntimeError("provider outage")

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None
    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (None, MagicMock(
        user_message="m", raw_text="{}", tokens_used=1, model="t",
        input_tokens=1, output_tokens=1, cost_usd=0.0,
    ))
    tech_store = MagicMock()
    tech_store.load.return_value = {}

    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    try:
        stage = MorningResearchStage(
            config=mock_config,
            db=db,
            market=market,
            macro=MagicMock(),
            news_provider=MagicMock(),
            news_store=news_store,
            macro_store=macro_store,
            tech_store=tech_store,
            earnings_provider=MagicMock(),
            macro_analyst=macro_agent,
            news_analyst=MagicMock(),
            tech_analyst=tech_agent,
            earnings_analyst=MagicMock(),
            has_actionable_signal_fn=lambda *args, **kw: True,
            run_news_update_fn=lambda run_id, session: (None, None),
            load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
        )
        ctx = RunContext.start("morning")
        ctx.positions = []
        with patch("src.notifier.send_owner_alert") as alert:
            result_ctx = stage.run(ctx)

        assert result_ctx.data_status["tech"] == "failed"
        assert result_ctx.analyses == []
        # Bars were fine — the crash was in analyze_batch, after bars-fetch
        # already completed and recorded.
        assert result_ctx.tech_bars_coverage["bars_missing"] == 0
        assert result_ctx.tech_bars_coverage["universe"] == 12
        # 12 resolved (0) is below LEVELS_COVERAGE_MIN_SAMPLE-vs-0 either
        # way, so no owner alert fires from an empty analyses list — but
        # the coverage row must still exist.
        assert not alert.called

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT evidence_json FROM specialist_evidence "
            "WHERE agent_name='tech_analyst' AND kind='levels_coverage'"
        ).fetchone()
        conn.close()
        assert row is not None, "levels_coverage row must be written even when analyze_batch crashes"
        payload = json.loads(row["evidence_json"])
        assert payload["universe"] == 12
        assert payload["bars_missing"] == 0
        assert payload["resolved"] == 0
    finally:
        db.close()
