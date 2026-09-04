"""docs/WORK.md item 32 — drawdown-brake rescaling to the real per-trade
risk unit.

The 5-day/-3%, 20-day/-8% rolling-return "drawdown brake" (`in_drawdown`
in `src/pipeline.py::_compute_recent_performance`) and the 3% daily-loss
circuit breaker (`RiskConfig.max_daily_loss_pct`, enforced in
`src/risk/rules.py`) were flat, hardcoded/independently-configured
percentages sized for this desk's real per-trade risk BEFORE PRs #258/#259
fixed the notional-cap bug that had silently suppressed it to ~1%
regardless of stated conviction. Once that fix lands, `max_position_risk_pct`
(the ratified 5% envelope) becomes the real per-trade risk unit, and these
three thresholds must move with it rather than staying pinned to the old,
now-stale ~1% assumption.

These tests pin the DERIVATION (thresholds move proportionally with
`max_position_risk_pct`) and a concrete regression case showing behaviour
that would have been WRONG under the old hardcoded literals is now correct
under the derived ones. They do not, and cannot, validate the inherited
multipliers (3, 8, 3) themselves against real drawdown history — that
history was wiped by the 2026-09-02 clean-slate reset (see
docs/INCIDENT_HISTORY.md) and is explicitly flagged PROVISIONAL in
docs/WORK.md pending real post-fix trade data.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.config import RiskConfig
from src.risk.rules import RiskRuleEngine
from src.pipeline import TradingPipeline
from src.agents.portfolio_manager import PortfolioManagerAgent
from src.models import Position, TechAnalysisResult


# ---------------------------------------------------------------------------
# RiskConfig derivation
# ---------------------------------------------------------------------------

def test_drawdown_thresholds_derive_from_the_real_risk_unit():
    """At the ratified 5% per-trade unit with the inherited 3x/8x
    multipliers, the 5-day/20-day thresholds are -15%/-40%, not the old
    flat -3%/-8%."""
    cfg = RiskConfig(
        max_position_pct=100, max_total_position_pct=200,
        max_sector_pct=75, require_stop_loss=True,
        max_position_risk_pct=5.0,
        drawdown_5d_risk_multiple=3.0, drawdown_20d_risk_multiple=8.0,
    )
    assert cfg.drawdown_5d_threshold_pct == pytest.approx(-15.0)
    assert cfg.drawdown_20d_threshold_pct == pytest.approx(-40.0)


def test_drawdown_thresholds_rescale_when_the_risk_unit_changes():
    """The whole point of item 32: the thresholds must move WITH
    max_position_risk_pct, not sit as independently-chosen constants that
    go stale the next time the real risk unit changes."""
    low_risk = RiskConfig(
        max_position_pct=100, max_total_position_pct=200,
        max_sector_pct=75, require_stop_loss=True,
        max_position_risk_pct=1.0,
        # Schedule capped to the 1% envelope — unrelated to this test,
        # just satisfies the "ceiling can only narrow, never widen"
        # invariant `agreement_ceiling_pct` is checked against.
        agreement_ceiling_pct=[0.6, 0.8, 1.0, 1.0, 1.0],
        drawdown_5d_risk_multiple=3.0, drawdown_20d_risk_multiple=8.0,
    )
    high_risk = RiskConfig(
        max_position_pct=100, max_total_position_pct=200,
        max_sector_pct=75, require_stop_loss=True,
        max_position_risk_pct=5.0,
        drawdown_5d_risk_multiple=3.0, drawdown_20d_risk_multiple=8.0,
    )
    assert low_risk.drawdown_5d_threshold_pct == pytest.approx(-3.0)
    assert low_risk.drawdown_20d_threshold_pct == pytest.approx(-8.0)
    # 5x the real per-trade risk unit -> 5x deeper thresholds.
    assert high_risk.drawdown_5d_threshold_pct == pytest.approx(
        low_risk.drawdown_5d_threshold_pct * 5,
    )
    assert high_risk.drawdown_20d_threshold_pct == pytest.approx(
        low_risk.drawdown_20d_threshold_pct * 5,
    )


def test_effective_max_daily_loss_pct_explicit_value_wins():
    """Matches `sector_hard_ceiling_pct`'s override pattern: an explicit
    max_daily_loss_pct always wins over the derived one, so the ~50
    existing fixtures across this repo that set it to an arbitrary literal
    for unrelated reasons keep working unchanged."""
    cfg = RiskConfig(
        max_position_pct=20, max_total_position_pct=90,
        max_daily_loss_pct=3, max_sector_pct=40, require_stop_loss=True,
        max_position_risk_pct=5.0, daily_loss_risk_multiple=3.0,
    )
    assert cfg.effective_max_daily_loss_pct == 3


def test_effective_max_daily_loss_pct_derives_when_unset():
    cfg = RiskConfig(
        max_position_pct=100, max_total_position_pct=200,
        max_sector_pct=75, require_stop_loss=True,
        max_position_risk_pct=5.0, daily_loss_risk_multiple=3.0,
    )
    assert cfg.max_daily_loss_pct is None
    assert cfg.effective_max_daily_loss_pct == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# RiskRuleEngine — the daily-loss circuit breaker actually enforced
# ---------------------------------------------------------------------------

@pytest.fixture
def rescaled_engine():
    cfg = RiskConfig(
        max_position_pct=100, max_total_position_pct=200,
        max_sector_pct=75, require_stop_loss=True,
        max_position_risk_pct=5.0, daily_loss_risk_multiple=3.0,
        # max_daily_loss_pct left unset -> derives to 15.0
    )
    return RiskRuleEngine(cfg)


def test_daily_loss_circuit_breaker_does_not_trip_under_the_old_flat_3pct(
    rescaled_engine,
):
    """A 10% daily loss would have tripped the OLD flat 3% breaker.
    Under the real 5%-risk-unit-derived 15% breaker it must not — this is
    the concrete case item 32 says was going to misfire once #258/#259
    make 5% real."""
    violation = rescaled_engine.check_daily_loss(baseline=100_000, daily_pnl=-10_000)
    assert violation is None


def test_daily_loss_circuit_breaker_trips_past_the_new_threshold(rescaled_engine):
    violation = rescaled_engine.check_daily_loss(baseline=100_000, daily_pnl=-16_000)
    assert violation is not None
    assert violation.rule == "max_daily_loss_pct"
    assert violation.limit == pytest.approx(15.0)


def test_daily_loss_circuit_breaker_still_honours_an_explicit_override():
    """If an operator explicitly sets max_daily_loss_pct, that value is
    enforced even though it disagrees with the derived one — same override
    semantics as `max_sector_hard_pct`."""
    cfg = RiskConfig(
        max_position_pct=100, max_total_position_pct=200,
        max_sector_pct=75, require_stop_loss=True,
        max_position_risk_pct=5.0, max_daily_loss_pct=3.0,
    )
    engine = RiskRuleEngine(cfg)
    violation = engine.check_daily_loss(baseline=100_000, daily_pnl=-4_000)
    assert violation is not None
    assert violation.limit == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# TradingPipeline._compute_recent_performance — the rolling-return brake
# ---------------------------------------------------------------------------

def _rows(values: list[float]) -> list[dict]:
    """`values[0]` is today, `values[N]` is N trading days ago — matches
    `get_daily_pnl`'s newest-first ordering."""
    return [{"total_value": v} for v in values]


def _pipeline_with_risk(risk: RiskConfig, rows: list[dict]) -> TradingPipeline:
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.config = SimpleNamespace(risk=risk)
    pipeline.db = MagicMock()
    pipeline.db.get_daily_pnl.return_value = rows
    return pipeline


def test_recent_performance_uses_the_derived_5d_threshold_not_the_old_flat_one():
    """rolling_5d of -10% sits BELOW the old hardcoded -3.0 (would have
    tripped `in_drawdown`) but ABOVE the real-risk-unit-derived -15%
    threshold, so it must NOT trip once the desk's real per-trade risk is
    the ratified 5%."""
    risk = RiskConfig(
        max_position_pct=100, max_total_position_pct=200,
        max_sector_pct=75, require_stop_loss=True,
        max_position_risk_pct=5.0,
        drawdown_5d_risk_multiple=3.0, drawdown_20d_risk_multiple=8.0,
    )
    # Equity fell 10% over 5 trading days ago, flat otherwise (no 20-day
    # window worth of rows -> rolling_20d is None and can't independently
    # trip the flag).
    rows = _rows([90_000, 91_000, 92_000, 93_000, 94_000, 100_000])
    pipeline = _pipeline_with_risk(risk, rows)
    result = pipeline._compute_recent_performance(current_equity=90_000)
    assert result["rolling_5d_pct"] == pytest.approx(-10.0)
    assert result["in_drawdown"] is False


def test_recent_performance_still_trips_past_the_derived_5d_threshold():
    risk = RiskConfig(
        max_position_pct=100, max_total_position_pct=200,
        max_sector_pct=75, require_stop_loss=True,
        max_position_risk_pct=5.0,
        drawdown_5d_risk_multiple=3.0, drawdown_20d_risk_multiple=8.0,
    )
    rows = _rows([83_000, 91_000, 92_000, 93_000, 94_000, 100_000])
    pipeline = _pipeline_with_risk(risk, rows)
    result = pipeline._compute_recent_performance(current_equity=83_000)
    assert result["rolling_5d_pct"] == pytest.approx(-17.0)
    assert result["in_drawdown"] is True


def test_pm_prompt_renders_the_real_configured_threshold_not_a_hardcoded_one():
    """`PortfolioManagerAgent.build_user_message` used to hand-type
    "5d < -3% OR 20d < -8%" into the prompt. Once those numbers are
    config-derived, a hand-typed copy would silently go stale again the
    next time they rescale — the prompt must render whatever
    `_compute_recent_performance` actually computed."""
    from src.models import TechReasoningChain

    agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6-20250725")
    analyses = [
        TechAnalysisResult(
            symbol="SPY", rating="buy", entry_price=500.0,
            reference_target=530.0, stop_loss=485.0,
            support_levels=[485.0], resistance_levels=[530.0],
            setup_type="range", expected_horizon_sessions=10,
            reasoning="x",
            reasoning_chain=TechReasoningChain(
                trend="x", momentum="x", volatility="x", volume="x",
                support_resistance="x",
            ),
        ),
    ]
    positions = [
        Position(symbol="AAPL", qty=5, avg_entry=180.0, current_price=190.0,
                 market_value=950.0, unrealized_pnl=50.0, sector="Technology"),
    ]
    message = agent.build_user_message(
        analyses=analyses, positions=positions, macro_analysis=None,
        cash_balance=5000.0, total_value=10000.0,
        recent_performance={
            "rolling_5d_pct": -4.0, "rolling_20d_pct": -10.0,
            "in_drawdown": False, "trailing_days": 25,
            "drawdown_5d_threshold_pct": -15.0,
            "drawdown_20d_threshold_pct": -40.0,
        },
    )
    assert "5d < -15.0% OR 20d < -40.0%" in message
    assert "5d < −3% OR 20d < −8%" not in message


def test_recent_performance_20d_threshold_also_derives_from_the_risk_unit():
    risk = RiskConfig(
        max_position_pct=100, max_total_position_pct=200,
        max_sector_pct=75, require_stop_loss=True,
        max_position_risk_pct=5.0,
        drawdown_5d_risk_multiple=3.0, drawdown_20d_risk_multiple=8.0,
    )
    # 25 rows so rows[20] (20 trading days ago) resolves. rows[5] is held
    # equal to current_equity throughout so the 5-day check never
    # independently trips — this isolates the 20-day threshold. A -20% move
    # over the 20-day window would have tripped the old hardcoded -8.0 but
    # sits above the real-risk-unit-derived -40%.
    values = [100_000] * 25
    values[0] = 80_000  # today
    values[5] = 80_000  # 5 trading days ago -> rolling_5d = 0%
    values[20] = 100_000  # 20 trading days ago
    rows = _rows(values)
    pipeline = _pipeline_with_risk(risk, rows)
    result = pipeline._compute_recent_performance(current_equity=80_000)
    assert result["rolling_5d_pct"] == pytest.approx(0.0)
    assert result["rolling_20d_pct"] == pytest.approx(-20.0)
    assert result["in_drawdown"] is False

    # Push past -40% and it must trip.
    values[0] = 55_000
    values[5] = 55_000
    rows = _rows(values)
    pipeline = _pipeline_with_risk(risk, rows)
    result = pipeline._compute_recent_performance(current_equity=55_000)
    assert result["rolling_5d_pct"] == pytest.approx(0.0)
    assert result["rolling_20d_pct"] == pytest.approx(-45.0)
    assert result["in_drawdown"] is True
