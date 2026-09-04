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
under the derived ones.

2026-09-04 — TWO FURTHER BUGS in the multiples themselves, both fixed here:

  BUG 1: the daily breaker used the SAME multiple as the 5-day window (3),
  so at the 5% risk unit it sat at -15% in a single session — reachable
  only on a single-name gap event, i.e. decorative. Rescaled by
  square-root-of-time from the 5-day anchor (Van Hemert/Ganz/Harvey et al.,
  "Drawdowns", JPM 2020): 3 x sqrt(1/5) = 1.3416... -> 1.34, a -6.7%
  breaker, and a 1 : sqrt(5) ratio between the windows.

  BUG 2: the 20-day brake at 8x (-40%) contradicted this desk's OTHER,
  older drawdown-response system — the §11.2 gross-exposure de-levering
  ladder, which alerts the owner at -20%. Moved to 4x (-20%) so the newer
  brake cannot stay silent past the point the ladder escalates.

The ANCHOR multiple (N_5d = 3) is still not validated against real drawdown
history — that history was wiped by the 2026-09-02 clean-slate reset (see
docs/INCIDENT_HISTORY.md) and stays flagged PROVISIONAL in docs/WORK.md.
These fixes correct the RELATIVE scaling between windows and the
contradiction with the ladder; they do not calibrate the anchor.
"""
import math
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.config import RiskConfig
from src.risk.rules import (
    GROSS_LADDER_ALERT_PCT,
    RiskRuleEngine,
    resolve_gross_ceiling,
)
from src.pipeline import TradingPipeline
from src.agents.portfolio_manager import PortfolioManagerAgent
from src.models import Position, TechAnalysisResult


# ---------------------------------------------------------------------------
# RiskConfig derivation
# ---------------------------------------------------------------------------

def test_drawdown_thresholds_derive_from_the_real_risk_unit():
    """At the ratified 5% per-trade unit with the SHIPPED multiples, the
    5-day/20-day thresholds are -15%/-20%, not the old flat -3%/-8%.

    The 20-day multiple was 8 (-40%) until the bug-2 fix below; it is 4
    now. This asserts the defaults, so a silent change to either shipped
    multiple fails here rather than in production.
    """
    cfg = RiskConfig(
        max_position_pct=100, max_total_position_pct=200,
        max_sector_pct=75, require_stop_loss=True,
        max_position_risk_pct=5.0,
    )
    assert cfg.drawdown_5d_risk_multiple == pytest.approx(3.0)
    assert cfg.drawdown_20d_risk_multiple == pytest.approx(4.0)
    assert cfg.drawdown_5d_threshold_pct == pytest.approx(-15.0)
    assert cfg.drawdown_20d_threshold_pct == pytest.approx(-20.0)


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
    """Shipped default multiple is 1.34 (bug-1 sqrt-time fix), so at the
    ratified 5% risk unit the derived breaker is 6.7%, not the old 15%."""
    cfg = RiskConfig(
        max_position_pct=100, max_total_position_pct=200,
        max_sector_pct=75, require_stop_loss=True,
        max_position_risk_pct=5.0,
    )
    assert cfg.max_daily_loss_pct is None
    assert cfg.daily_loss_risk_multiple == pytest.approx(1.34)
    assert cfg.effective_max_daily_loss_pct == pytest.approx(6.7)


# ---------------------------------------------------------------------------
# BUG 1 — the daily circuit breaker shared the 5-day window's multiple, which
# made it decorative. Fixed by square-root-of-time scaling.
# ---------------------------------------------------------------------------

def test_daily_multiple_is_sqrt_time_consistent_with_the_5_day_anchor():
    """The load-bearing arithmetic of bug 1, spelled out.

    Drawdown magnitude over a window scales with sqrt(time) (Van Hemert,
    Ganz, Harvey et al., "Drawdowns", JPM 2020). Anchoring on the 5-day
    window, the 1-day multiple must be N_5d x sqrt(1/5):

        3.0 x sqrt(1/5) = 3.0 x 0.4472135955 = 1.3416407865...

    shipped as 1.34 (2dp — the anchor itself is provisional to roughly the
    nearest half, so more digits would be false precision).
    """
    cfg = RiskConfig(
        max_position_pct=100, max_total_position_pct=200,
        max_sector_pct=75, require_stop_loss=True,
        max_position_risk_pct=5.0,
    )
    exact = cfg.drawdown_5d_risk_multiple * math.sqrt(1 / 5)
    assert exact == pytest.approx(1.3416407865)
    assert cfg.daily_loss_risk_multiple == pytest.approx(round(exact, 2))

    # The ratio between the two windows is now 1 : sqrt(5) = 1 : 2.236...,
    # not the old, incoherent 1 : 1 that both being 3.0 produced. The
    # shipped 1.34 gives 2.2388 — 0.12% off exact sqrt(5), the residual of
    # rounding to 2dp, which is orders of magnitude smaller than the
    # uncertainty in the provisional anchor itself.
    ratio = cfg.drawdown_5d_risk_multiple / cfg.daily_loss_risk_multiple
    assert ratio == pytest.approx(math.sqrt(5), rel=2e-3)


def test_the_old_daily_multiple_was_not_sqrt_time_consistent():
    """Regression guard on the DEFECT itself: an operator who sets the
    daily multiple back to the 5-day one reproduces bug 1 — a breaker at
    -15% of equity in one session, reachable only on a gap event."""
    broken = RiskConfig(
        max_position_pct=100, max_total_position_pct=200,
        max_sector_pct=75, require_stop_loss=True,
        max_position_risk_pct=5.0, daily_loss_risk_multiple=3.0,
    )
    assert broken.effective_max_daily_loss_pct == pytest.approx(15.0)
    # A brutal but entirely realistic -8% day would NOT have tripped it.
    assert RiskRuleEngine(broken).check_daily_loss(
        baseline=100_000, daily_pnl=-8_000,
    ) is None
    # It does under the shipped 6.7% breaker.
    fixed = RiskConfig(
        max_position_pct=100, max_total_position_pct=200,
        max_sector_pct=75, require_stop_loss=True,
        max_position_risk_pct=5.0,
    )
    assert RiskRuleEngine(fixed).check_daily_loss(
        baseline=100_000, daily_pnl=-8_000,
    ) is not None


def test_daily_multiple_still_rescales_with_the_risk_unit():
    """Bug 1 changes the multiple; it must not break item 32's original
    property that the breaker tracks `max_position_risk_pct`."""
    cfg = RiskConfig(
        max_position_pct=100, max_total_position_pct=200,
        max_sector_pct=75, require_stop_loss=True,
        max_position_risk_pct=2.0,
        agreement_ceiling_pct=[0.6, 0.8, 1.0, 1.5, 2.0],
    )
    assert cfg.effective_max_daily_loss_pct == pytest.approx(2.68)  # 1.34 x 2


# ---------------------------------------------------------------------------
# BUG 2 — the 20-day brake stayed silent long past the point the older
# gross-exposure de-levering ladder had already alerted the owner.
# ---------------------------------------------------------------------------

def test_20d_brake_fires_no_later_than_the_ladder_alerts_the_owner():
    """The load-bearing constraint of bug 2.

    The §11.2 ladder drops to 0.5x AND alerts the owner at
    `GROSS_LADDER_ALERT_PCT` (-20%). The 20-day brake used to sit at -40%,
    so the ladder had halved the book and woken the owner while the newer
    system was still completely silent — for another twenty points.
    """
    cfg = RiskConfig(
        max_position_pct=100, max_total_position_pct=200,
        max_sector_pct=75, require_stop_loss=True,
        max_position_risk_pct=5.0,
    )
    assert GROSS_LADDER_ALERT_PCT == -20.0
    assert cfg.drawdown_20d_threshold_pct <= GROSS_LADDER_ALERT_PCT
    # And exactly at it, from `20 / max_position_risk_pct = 4`.
    assert cfg.drawdown_20d_risk_multiple == pytest.approx(
        abs(GROSS_LADDER_ALERT_PCT) / cfg.max_position_risk_pct,
    )
    assert cfg.drawdown_20d_threshold_pct == pytest.approx(-20.0)

    # The state that motivated the fix: at -20% peak-to-trough the ladder
    # is on its deepest rung and alerting.
    ceiling = resolve_gross_ceiling(-20.0, base_x=2.0)
    assert ceiling.ceiling_x == pytest.approx(0.5)
    assert ceiling.alert_owner is True


def test_5d_brake_already_agreed_with_the_ladder_and_was_left_alone():
    """The 5-day brake fires at -15%, which is exactly the ladder's
    -15% -> 1.0x rung. The two systems already agreed at this window, so
    bug 2 deliberately changed nothing here."""
    cfg = RiskConfig(
        max_position_pct=100, max_total_position_pct=200,
        max_sector_pct=75, require_stop_loss=True,
        max_position_risk_pct=5.0,
    )
    assert cfg.drawdown_5d_threshold_pct == pytest.approx(-15.0)
    assert resolve_gross_ceiling(
        cfg.drawdown_5d_threshold_pct, base_x=2.0,
    ).ceiling_x == pytest.approx(1.0)


def test_the_old_20d_multiple_reproduces_the_contradiction():
    """Regression guard on the defect: at the old 8x multiple the 20-day
    brake was silent at -30% peak-to-trough, ten points past the owner
    alert."""
    broken = RiskConfig(
        max_position_pct=100, max_total_position_pct=200,
        max_sector_pct=75, require_stop_loss=True,
        max_position_risk_pct=5.0, drawdown_20d_risk_multiple=8.0,
    )
    assert broken.drawdown_20d_threshold_pct == pytest.approx(-40.0)
    assert broken.drawdown_20d_threshold_pct < GROSS_LADDER_ALERT_PCT
    # -30%: ladder alerting on its floor rung, old brake still silent.
    assert resolve_gross_ceiling(-30.0, base_x=2.0).alert_owner is True
    assert -30.0 > broken.drawdown_20d_threshold_pct  # not yet tripped


# ---------------------------------------------------------------------------
# RiskRuleEngine — the daily-loss circuit breaker actually enforced
# ---------------------------------------------------------------------------

@pytest.fixture
def rescaled_engine():
    cfg = RiskConfig(
        max_position_pct=100, max_total_position_pct=200,
        max_sector_pct=75, require_stop_loss=True,
        max_position_risk_pct=5.0,
        # max_daily_loss_pct and daily_loss_risk_multiple left unset ->
        # derives to 1.34 x 5 = 6.7
    )
    return RiskRuleEngine(cfg)


def test_daily_loss_circuit_breaker_does_not_trip_under_the_old_flat_3pct(
    rescaled_engine,
):
    """A 5% daily loss would have tripped the OLD flat 3% breaker (the
    pre-item-32 literal). Under the real 5%-risk-unit-derived, sqrt-time
    scaled 6.7% breaker it must not — one full losing max-size trade is not
    a circuit-breaker event."""
    violation = rescaled_engine.check_daily_loss(baseline=100_000, daily_pnl=-5_000)
    assert violation is None


def test_daily_loss_circuit_breaker_trips_past_the_new_threshold(rescaled_engine):
    violation = rescaled_engine.check_daily_loss(baseline=100_000, daily_pnl=-7_000)
    assert violation is not None
    assert violation.rule == "max_daily_loss_pct"
    assert violation.limit == pytest.approx(6.7)


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
    )
    # 25 rows so rows[20] (20 trading days ago) resolves. rows[5] is held
    # equal to current_equity throughout so the 5-day check never
    # independently trips — this isolates the 20-day threshold.
    #
    # -15% over the 20-day window: past the old flat -8.0 literal, but
    # still inside the real-risk-unit-derived -20% threshold.
    values = [100_000] * 25
    values[0] = 85_000  # today
    values[5] = 85_000  # 5 trading days ago -> rolling_5d = 0%
    values[20] = 100_000  # 20 trading days ago
    rows = _rows(values)
    pipeline = _pipeline_with_risk(risk, rows)
    result = pipeline._compute_recent_performance(current_equity=85_000)
    assert result["rolling_5d_pct"] == pytest.approx(0.0)
    assert result["rolling_20d_pct"] == pytest.approx(-15.0)
    assert result["in_drawdown"] is False

    # BUG 2, end to end: -25% over the window. The OLD -40% threshold left
    # this silent while the de-levering ladder was already on its 0.5x
    # floor rung and alerting the owner. It must trip now.
    assert resolve_gross_ceiling(-25.0, base_x=2.0).alert_owner is True
    values[0] = 75_000
    values[5] = 75_000
    rows = _rows(values)
    pipeline = _pipeline_with_risk(risk, rows)
    result = pipeline._compute_recent_performance(current_equity=75_000)
    assert result["rolling_5d_pct"] == pytest.approx(0.0)
    assert result["rolling_20d_pct"] == pytest.approx(-25.0)
    assert result["in_drawdown"] is True
