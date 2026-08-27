"""Pipeline stages — Phase 4 #1 infrastructure.

These tests cover the stage pattern itself: stages take explicit
dependencies at construction, accept a RunContext, populate ctx fields,
and return the context. Exhaustive integration testing of the
MorningResearchStage's parallel fan-out is covered indirectly by the
existing pipeline integration tests in test_pipeline.py.
"""

import json
from unittest.mock import MagicMock, patch

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


def test_risk_stage_persists_hard_risk_block_when_post_rm_modifications_block_everything():
    """Second early-return site: RM approves with modifications, the
    re-filter after applying them blocks everything. risk_manager.review
    IS reached and logged normally here — the forensic risk_gate row is
    additive on top of (not instead of) the real risk_manager agent_logs
    row RiskStage.run() already writes for a reached RM call."""
    from src.models import PortfolioDecision, RiskVerdict

    first_pass_decisions = [_buy("AAPL", 10)]
    pipeline = _risk_stage_pipeline(first_pass_decisions)
    pipeline._apply_risk_modifications = MagicMock(return_value=first_pass_decisions)

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
        run_news_update_fn=lambda *a, **kw: None,
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
        run_news_update_fn=lambda run_id, session: None,
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
        run_news_update_fn=lambda *args, **kwargs: None,
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
        run_news_update_fn=lambda run_id, session: None,
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
            run_news_update_fn=lambda run_id, session: news_intel,
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
        run_news_update_fn=lambda run_id, session: None,
        load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
    )

    ctx = RunContext.start("morning")
    ctx.positions = []
    stage.run(ctx)

    assert macro_store.load_last_state.call_count == 1
    tech_kwargs = tech_agent.analyze_batch.call_args.kwargs
    assert tech_kwargs["prior_macro_regime"] == "risk-off"
    assert tech_kwargs["prior_macro_outlook"] == "bearish"
