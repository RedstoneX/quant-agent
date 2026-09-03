"""Phase 13 — `MacroAnalysis.to_verdict()`, the macro seat's restatement
onto the shared `AnalystVerdict` shape (mirrors `TechAnalysisResult.
to_verdict`, pinned in `tests/test_analyst_verdict.py`).

MacroAnalysis carries no numeric magnitude field (unlike Technical's rating
rungs), so `to_verdict` DERIVES one from `confidence` plus a `regime_shift`
bonus — see the method's docstring in `src/models.py` for the exact mapping
and why it is flagged as unmeasured. These tests pin concrete, hand-computed
expected values for that mapping so a change to it is caught here first.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.models import (
    MacroAnalysis, MacroObservation, MacroPositionGuidance, MacroReasoningChain,
)


def _chain() -> MacroReasoningChain:
    return MacroReasoningChain(
        volatility_analysis="VIX compressing.",
        yield_curve_analysis="Curve steepening.",
        monetary_policy_analysis="Fed on hold.",
        inflation_labor_credit="Core CPI sticky, labor soft, credit tight.",
        cross_signal_synthesis="Risk-on lean with an inflation caveat.",
        sector_implications="Tech, financials overweight.",
    )


def _guidance() -> MacroPositionGuidance:
    return MacroPositionGuidance(
        target_invested_pct=75.0, cash_recommendation_pct=25.0, reasoning="Hold buffer.",
    )


def _macro(
    equity_outlook: str, confidence: str = "medium", regime_shift: bool = False,
    shift_reason: str = "", bull_triggers: list[str] | None = None,
    bear_triggers: list[str] | None = None, key_observations=None,
    sector_guidance=None, risk_factors=None,
) -> MacroAnalysis:
    return MacroAnalysis(
        reasoning_chain=_chain(),
        regime="risk-on" if equity_outlook == "bullish" else "risk-off",
        confidence=confidence,
        equity_outlook=equity_outlook,
        regime_shift=regime_shift,
        shift_reason=shift_reason,
        key_observations=key_observations or [],
        sector_guidance=sector_guidance or [],
        risk_factors=risk_factors or [],
        position_guidance=_guidance(),
        bull_triggers=bull_triggers or [],
        bear_triggers=bear_triggers or [],
        summary="Moderately supportive.",
    )


# ==========================================================================
# Magnitude mapping
# ==========================================================================

def test_bullish_with_regime_shift_gets_confidence_base_plus_bonus():
    a = _macro(
        "bullish", confidence="high", regime_shift=True,
        shift_reason="Fed pivots dovish and credit spreads snap tighter",
    )
    v = a.to_verdict("SPY")
    assert v.seat == "macro"
    assert v.symbol == "SPY"
    assert v.direction == "bullish"
    # base 0.75 (high) + 0.25 (regime_shift bonus) = 1.0, clamped
    assert v.magnitude == 1.0
    assert v.conviction == "high"
    assert v.invalidation == "Fed pivots dovish and credit spreads snap tighter"


def test_bearish_without_regime_shift_uses_confidence_base_only():
    a = _macro(
        "bearish", confidence="medium", regime_shift=False,
        bull_triggers=["Core CPI MoM < 0.2% for 2 months"],
    )
    v = a.to_verdict("XLE")
    assert v.direction == "bearish"
    # base 0.5 (medium), no regime_shift bonus
    assert v.magnitude == 0.5
    assert v.conviction == "medium"
    assert v.invalidation == "Core CPI MoM < 0.2% for 2 months"


def test_low_confidence_bullish_maps_to_the_low_base_rate():
    a = _macro("bullish", confidence="low", bear_triggers=["HY OAS > 450bps"])
    v = a.to_verdict("QQQ")
    assert v.magnitude == 0.25
    assert v.invalidation == "HY OAS > 450bps"


def test_regime_shift_bonus_clamps_at_one_rather_than_overshooting():
    a = _macro(
        "bearish", confidence="high", regime_shift=True,
        shift_reason="Credit seizes up",
    )
    v = a.to_verdict("IWM")
    assert v.magnitude == 1.0  # 0.75 + 0.25 = 1.0 exactly, not > 1.0


def test_neutral_outlook_always_maps_to_zero_magnitude_even_with_regime_shift():
    """A neutral verdict with nonzero magnitude is refused by `AnalystVerdict`
    as self-contradictory — `to_verdict` must never construct one, regardless
    of confidence or `regime_shift`."""
    a = _macro(
        "neutral", confidence="high", regime_shift=True, shift_reason="Something moved",
    )
    v = a.to_verdict("SPY")
    assert v.direction == "neutral"
    assert v.magnitude == 0.0
    assert v.invalidation == ""


# ==========================================================================
# Invalidation fallback chain
# ==========================================================================

def test_regime_shift_reason_wins_over_triggers_when_both_present():
    a = _macro(
        "bullish", regime_shift=True, shift_reason="Curve un-inverts on Fed cuts",
        bear_triggers=["Some trigger that should be ignored"],
    )
    v = a.to_verdict("NVDA")
    assert v.invalidation == "Curve un-inverts on Fed cuts"


def test_bullish_falls_back_to_first_bear_trigger_when_no_shift_reason():
    a = _macro("bullish", bear_triggers=["HY OAS > 450bps", "VIX > 30"])
    v = a.to_verdict("NVDA")
    assert v.invalidation == "HY OAS > 450bps"


def test_bearish_falls_back_to_first_bull_trigger_when_no_shift_reason():
    a = _macro("bearish", bull_triggers=["Core CPI cools", "Fed cuts twice"])
    v = a.to_verdict("XLE")
    assert v.invalidation == "Core CPI cools"


def test_directional_call_with_no_stated_falsifier_gets_a_generic_fallback_not_a_blank():
    """Unlike Technical (which always has its own hard stop to fall back to),
    macro has no analogous always-present number. `AnalystVerdict` REQUIRES
    non-empty invalidation for any non-neutral call, so `to_verdict` must
    never leave this blank for a directional read."""
    a = _macro("bullish")  # no shift_reason, no bear_triggers
    v = a.to_verdict("MSFT")
    assert v.invalidation != ""
    assert "bullish" in v.invalidation


# ==========================================================================
# Evidence
# ==========================================================================

def test_evidence_is_built_from_observations_sectors_and_risk_factors():
    a = _macro(
        "bullish",
        bear_triggers=["HY OAS > 450bps"],
        key_observations=[
            MacroObservation(indicator="VIX", reading="19.5", interpretation="Compressed, calm tape"),
        ],
        sector_guidance=[
            {"sector": "Technology", "stance": "overweight", "reason": "AI capex cycle"},
        ],
        risk_factors=["Core CPI sticky"],
    )
    v = a.to_verdict("NVDA")
    by_label = {e.label: e for e in v.evidence}
    assert by_label["VIX"].text == "19.5 — Compressed, calm tape"
    assert by_label["sector:Technology"].text == "overweight — AI capex cycle"
    assert by_label["risk_factor_0"].text == "Core CPI sticky"
    for e in v.evidence:
        assert e.value is None  # no numeric evidence is invented


def test_evidence_falls_back_to_reasoning_chain_when_nothing_else_is_populated():
    a = _macro("bullish", bear_triggers=["HY OAS > 450bps"])  # no observations/sectors/risks
    v = a.to_verdict("MSFT")
    assert len(v.evidence) == 1
    assert v.evidence[0].label == "cross_signal_synthesis"
    assert v.evidence[0].text == "Risk-on lean with an inflation caveat."


def test_neutral_verdict_may_carry_no_evidence():
    a = _macro("neutral")
    v = a.to_verdict("SPY")
    assert v.evidence == []


# ==========================================================================
# Full round trip validates
# ==========================================================================

def test_a_full_directional_macro_read_produces_a_valid_verdict():
    a = _macro(
        "bearish", confidence="high", regime_shift=True,
        shift_reason="Credit spreads blow out past 500bps",
        key_observations=[
            MacroObservation(indicator="HY_OAS", reading="480bps", interpretation="Widening fast"),
        ],
    )
    v = a.to_verdict("XLF")  # must not raise
    assert v.seat == "macro"
    assert v.symbol == "XLF"
    assert (v.direction, v.magnitude, v.conviction) == ("bearish", 1.0, "high")
    assert v.invalidation == "Credit spreads blow out past 500bps"
    assert len(v.evidence) == 1
