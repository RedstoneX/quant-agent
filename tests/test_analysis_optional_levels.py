"""Owner rule 2026-10-09: a missing chart floor/ceiling or target never blocks a trade.

An actionable technical read with no analyst target, or with no AI-named
support/resistance, is kept and records "no target" / "no level named".
Neither number protects the stop, and the take-profit only ever comes from
measured levels. A dead price feed still fails under its own data-fault
reason. Prompts print the words, never "None".
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.agents.tech_analyst import TechAnalystAgent
from src.models import TargetPosition, TechAnalysisResult
from src.models.analysis import NO_LEVEL_NAMED_TEXT, NO_TARGET_TEXT, TechReasoningChain
from src.portfolio_constructor import PortfolioConstructor
from tests.test_tech_analyst import sample_bars, sample_indicators  # noqa: F401  (fixtures)


def _analysis(*, rating="buy", entry=100.0, stop=99.0, target=None, support=(90.0,), resistance=()):
    return TechAnalysisResult(
        symbol="NVDA",
        rating=rating,
        entry_price=entry,
        stop_loss=stop,
        reference_target=target,
        reasoning="test",
        support_levels=list(support),
        resistance_levels=list(resistance),
        computed_levels=[],
        atr_14=2.0,
        setup_type="range",
        expected_horizon_sessions=20,
        reasoning_chain=TechReasoningChain(trend="x", momentum="x", volatility="x", volume="x", support_resistance="x"),
        thesis_invalid_if="closes below support",
    )


def _evidence_texts(analysis):
    return {e.label: e.text for e in analysis.to_verdict().evidence if e.text}


@pytest.mark.parametrize("rating,entry,stop", [("buy", 100.0, 99.0), ("sell", 100.0, 101.0)])
def test_no_target_passes_and_records_no_target(rating, entry, stop):
    analysis = _analysis(rating=rating, entry=entry, stop=stop, target=None)
    assert analysis.reference_target is None
    assert analysis.reference_target_wrong_side is False
    assert analysis.target_text == NO_TARGET_TEXT == "no target"
    assert _evidence_texts(analysis)["reference_target"] == "no target"


def test_no_ai_named_levels_passes_and_records_no_level_named():
    analysis = _analysis(target=130.0, support=(), resistance=())
    assert analysis.levels_text == NO_LEVEL_NAMED_TEXT == "no level named"
    assert _evidence_texts(analysis)["analyst_levels"] == "no level named"


def _target():
    return TargetPosition(symbol="NVDA", direction="long", target_weight_pct=8.0, conviction="high", thesis="t")


def test_dead_feed_still_fails_with_its_data_fault_reason():
    analysis = _analysis(target=None, support=(), resistance=())
    # levels_coverage left at its "unknown" default: no usable history.
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_target()], positions=[], analyses=[analysis], total_value=100_000, price_map={"NVDA": 100.0}
    )
    assert not [d for d in decisions if d.action == "BUY"]
    assert "NVDA" in constructor.last_data_faults


def test_missing_measured_target_never_falls_back_to_the_ai_target():
    analysis = _analysis(target=130.0)
    constructor = PortfolioConstructor()
    seen = []
    real = constructor._reward_risk_at

    def spy(entry, stop, target_price, is_short):
        seen.append(target_price)
        return real(entry, stop, target_price, is_short)

    constructor._reward_risk_at = spy
    constructor._widen_stop_past_noise("NVDA", analysis, 100.0, 95.0, direction="long", target_price=None)
    assert 130.0 not in seen


def test_pm_prompt_says_no_target_never_none():
    agent = PortfolioManagerAgent(api_key="test", model="test-model")
    msg = agent.build_user_message(
        analyses=[_analysis(target=None)], positions=[], cash_balance=1000.0, total_value=1000.0
    )
    assert "Target: no target" in msg
    assert "Target: None" not in msg


def test_analyst_prior_rating_line_says_no_target_never_none(sample_bars, sample_indicators):  # noqa: F811
    from datetime import timedelta

    from src.util.time import et_today

    prior = {
        "SPY": {
            "rating": "buy",
            "conviction": "high",
            "first_seen_date": (et_today() - timedelta(days=4)).isoformat(),
            "entry_price": 500.0,
            "stop_loss": 490.0,
            "reference_target": None,
        }
    }
    with patch("anthropic.Anthropic"):
        agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
        msg = agent.build_user_message(
            symbols_data=[{"symbol": "SPY", "bars": sample_bars, "indicators": sample_indicators}],
            prior_ratings=prior,
        )
    assert "target no target" in msg
    assert "target None" not in msg
