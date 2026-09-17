"""RC3 deployment-convergence facts (2026-07-16 forensics), re-based on the
owner's fully-invested mandate (2026-09-17).

Macro demanded 72-75% invested for three months; realized invested%
averaged 39% and declined monotonically while every layer shaved sizes
independently. Nothing reconciled the compound. Two fixes, still in force:

  1. PMFacts carries invested_target_pct + deployment_gap_pp and renders a
     ⚠️ DEPLOYMENT GAP section (>15pp under) that the PM prompt requires be
     answered in the cash_target step.
  2. The `deployment_gap` advisory must NOT tell RM to scale_all_buys down.

Since 2026-09-17 the target is the fixed 100% mandate
(`DESK_INVESTED_TARGET_PCT`), not macro's `target_invested_pct`, and there is
no longer ANY branch that asks for a scale-down for being above target.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.models import Position, TradeDecision
from src.pipeline import TradingPipeline
from src.pipeline_context import PMFacts
from src.risk.rules import DESK_INVESTED_TARGET_PCT, RiskRuleEngine
from src.config import RiskConfig


def test_mandate_is_fully_invested():
    assert DESK_INVESTED_TARGET_PCT == 100.0


def test_pm_facts_render_deployment_gap_when_under():
    f = PMFacts()
    f.invested_pct = 39.0
    f.invested_target_pct = 100.0
    f.deployment_gap_pp = -61.0
    out = f.render()
    assert "DEPLOYMENT GAP" in out
    assert "61pp UNDER" in out
    assert "cash_target" in out
    assert "shorts" in out


def test_pm_facts_render_near_mandate_still_asks_for_residual_cash_reason():
    f = PMFacts()
    f.invested_pct = 96.0
    f.invested_target_pct = 100.0
    f.deployment_gap_pp = -4.0
    out = f.render()
    assert "Deployment vs Fully-Invested Mandate" in out
    assert "cash_target" in out
    assert "DEPLOYMENT GAP" not in out


def test_pm_facts_render_over_mandate_never_suggests_trimming():
    f = PMFacts()
    f.invested_pct = 140.0
    f.invested_target_pct = 100.0
    f.deployment_gap_pp = 40.0
    out = f.render()
    assert "OVER" not in out
    assert "trim" not in out.lower()


def test_pm_facts_render_no_target_no_section():
    out = PMFacts().render()
    assert "Deployment vs" not in out
    assert "DEPLOYMENT GAP" not in out


def test_build_pm_facts_gap_is_against_mandate_not_macro():
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.db = MagicMock()
    pipeline.db.compute_trade_calibration.return_value = {}
    pipeline.db.get_recent_agent_outputs.return_value = []
    pipeline._build_position_history = MagicMock(return_value={})
    # A legacy macro object still carrying a target must not move the target.
    macro = SimpleNamespace(
        position_guidance=SimpleNamespace(target_invested_pct=75.0),
    )
    pos = Position(symbol="GE", qty=26, avg_entry=316, current_price=360,
                   market_value=9_360, unrealized_pnl=1_144,
                   unrealized_intraday_pnl=0.0, sector="Industrials")
    f = pipeline._build_pm_facts(
        positions=[pos], analyses=[], total_value=100_000.0,
        cash=90_640.0, recent_performance={}, macro_analysis=macro,
    )
    assert f.invested_target_pct == 100.0
    # invested ≈ 9.4% → gap ≈ -90.6pp
    assert f.deployment_gap_pp is not None and f.deployment_gap_pp < -90


def _engine_pipeline():
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.risk_engine = RiskRuleEngine(RiskConfig(
        max_position_pct=50, max_total_position_pct=200,
        max_daily_loss_pct=10, max_sector_pct=100,
        require_stop_loss=True, allow_margin=False,
    ))
    return pipeline


def test_under_deployment_advisory_does_not_ask_for_scale_down():
    pipeline = _engine_pipeline()
    decision = TradeDecision(action="BUY", symbol="NVDA", allocation_pct=5.0,
                             entry_price=100.0, stop_loss=90.0,
                             take_profit=130.0, reasoning="x")
    _, violations, _ = pipeline._filter_hard_risk_decisions(
        [decision], [], total_value=100_000.0, daily_pnl=0.0,
        baseline=100_000.0, invested_target_pct=DESK_INVESTED_TARGET_PCT,
        cash=95_000.0,
    )
    dev = [v for v in violations if v.rule == "deployment_gap"]
    assert dev, "5% projected vs the 100% mandate must fire the advisory"
    assert "UNDER" in dev[0].message
    assert "do NOT scale" in dev[0].message
    assert dev[0].limit == 100.0
    assert not any(v.rule == "macro_exposure_deviation" for v in violations)


def test_advisory_never_scales_down_for_being_above_target():
    """(a) Owner mandate: a book at or above the target — here 90% held plus
    a 20% BUY, i.e. 110% projected against 100%, and 110% against a legacy
    50% macro-style target — must never produce an advisory asking RM to
    scale buys or shorts down."""
    pipeline = _engine_pipeline()
    positions = [Position(symbol="NVDA", qty=100, avg_entry=800,
                          current_price=900, market_value=90_000,
                          unrealized_pnl=10_000, unrealized_intraday_pnl=0.0,
                          sector="Technology")]
    decisions = [
        TradeDecision(action="BUY", symbol="AAPL", allocation_pct=20.0,
                      entry_price=100.0, stop_loss=90.0,
                      take_profit=130.0, reasoning="x"),
    ]
    for target in (DESK_INVESTED_TARGET_PCT, 50.0):
        _, violations, _ = pipeline._filter_hard_risk_decisions(
            list(decisions), positions, total_value=100_000.0, daily_pnl=0.0,
            baseline=100_000.0, invested_target_pct=target, cash=100_000.0,
        )
        assert not [v for v in violations if v.rule == "deployment_gap"], target
        assert not any("scale_all_buys" in v.message for v in violations), target


def test_within_band_below_mandate_is_quiet():
    pipeline = _engine_pipeline()
    positions = [Position(symbol="NVDA", qty=100, avg_entry=800,
                          current_price=900, market_value=90_000,
                          unrealized_pnl=10_000, unrealized_intraday_pnl=0.0,
                          sector="Technology")]
    _, violations, _ = pipeline._filter_hard_risk_decisions(
        [], positions, total_value=100_000.0, daily_pnl=0.0,
        baseline=100_000.0, invested_target_pct=DESK_INVESTED_TARGET_PCT,
        cash=10_000.0,
    )
    assert not [v for v in violations if v.rule == "deployment_gap"]
