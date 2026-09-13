"""Phase 13 — `MacroAnalysis.to_verdict()`, the macro seat's restatement
onto the shared `AnalystVerdict` shape (mirrors `TechAnalysisResult.
to_verdict`, pinned in `tests/test_analyst_verdict.py`).

MacroAnalysis carries no numeric magnitude field (unlike Technical's rating
rungs). `to_verdict` used to DERIVE one from `confidence` plus a
`regime_shift` bonus; both were deleted 2026-09-13 on review (docs/WORK.md
retired item 31) because `confidence` is also what the verdict reports as
`conviction`, and `score_verdict` adds magnitude to conviction — so macro's
confidence was entering the composite twice, at a spacing (0.25/0.5/0.75,
+0.25) nothing stood behind. Magnitude is now flat
(`SINGLE_RUNG_MAGNITUDE`) for any directional read, 0.0 for neutral, and
the tests below pin that it stays flat.

Also pinned here, from the same review: the SECTOR-ADJUSTED direction. A
symbol whose sector has its own `sector_guidance` row takes that row's
stance, not the broad `equity_outlook` — the same resolution
`build_evidence_registry` already performed for the prompt's evidence
section, so one macro read can no longer give two answers about one symbol.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.models import (
    SINGLE_RUNG_MAGNITUDE, MacroAnalysis, MacroObservation, MacroPositionGuidance,
    MacroReasoningChain,
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

def test_a_directional_read_carries_the_flat_single_rung_magnitude():
    a = _macro(
        "bullish", confidence="high", regime_shift=True,
        shift_reason="Fed pivots dovish and credit spreads snap tighter",
    )
    v = a.to_verdict("SPY")
    assert v.seat == "macro"
    assert v.symbol == "SPY"
    assert v.direction == "bullish"
    assert v.magnitude == SINGLE_RUNG_MAGNITUDE
    assert v.conviction == "high"
    assert v.invalidation == "Fed pivots dovish and credit spreads snap tighter"


def test_bearish_without_regime_shift_carries_the_same_flat_magnitude():
    a = _macro(
        "bearish", confidence="medium", regime_shift=False,
        bull_triggers=["Core CPI MoM < 0.2% for 2 months"],
    )
    v = a.to_verdict("XLE")
    assert v.direction == "bearish"
    assert v.magnitude == SINGLE_RUNG_MAGNITUDE
    assert v.conviction == "medium"
    assert v.invalidation == "Core CPI MoM < 0.2% for 2 months"


@pytest.mark.parametrize("confidence", ["low", "medium", "high"])
@pytest.mark.parametrize("regime_shift", [False, True])
def test_magnitude_tracks_neither_confidence_nor_regime_shift(confidence, regime_shift):
    """The mechanical guard against the deleted tables coming back.
    `score_verdict` is magnitude + conviction; confidence must reach the
    score exactly once, through conviction, and `regime_shift` must not
    silently price anything at all — it reaches the reader as the stated
    falsifier instead."""
    a = _macro(
        "bullish", confidence=confidence, regime_shift=regime_shift,
        shift_reason="Fed pivots dovish",
        bear_triggers=["HY OAS > 450bps"],
    )
    v = a.to_verdict("QQQ")
    assert v.conviction == confidence
    assert v.magnitude == SINGLE_RUNG_MAGNITUDE


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
    assert (v.direction, v.magnitude, v.conviction) == (
        "bearish", SINGLE_RUNG_MAGNITUDE, "high",
    )
    assert v.invalidation == "Credit spreads blow out past 500bps"
    assert len(v.evidence) == 1


# ==========================================================================
# Sector-adjusted direction — item 31, closed 2026-09-13.
#
# `build_evidence_registry` already resolved a per-symbol macro stance from
# `sector_guidance`, falling back to `equity_outlook` only when the sector
# had no row. `to_verdict` applied the broad outlook to every symbol, so the
# same macro read could tell the PM two different things about one name in
# one prompt. These pin that they now agree.
# ==========================================================================

def _sector(sector: str, stance: str, reason: str = "policy tailwind") -> dict:
    # A dict, not a MacroSectorGuidance: `_sanitize_sector_guidance` is a
    # mode="before" validator that reads rows as mappings, which is also the
    # shape they arrive in from the LLM and from MacroStore.
    return {"sector": sector, "stance": stance, "reason": reason}


def test_sector_stance_overrides_the_broad_outlook_for_that_sector():
    a = _macro(
        "bullish", confidence="high",
        sector_guidance=[_sector("Energy", "underweight", "crude rolling over")],
        bear_triggers=["HY OAS > 450bps"],
    )
    v = a.to_verdict("XOM", sector="Energy")
    assert v.direction == "bearish"          # NOT the bullish broad read
    assert v.conviction == "high"            # the only confidence the seat states
    assert v.magnitude == SINGLE_RUNG_MAGNITUDE


def test_a_symbol_in_an_unmentioned_sector_still_gets_the_broad_read():
    a = _macro(
        "bullish", confidence="medium",
        sector_guidance=[_sector("Energy", "underweight", "crude rolling over")],
        bear_triggers=["HY OAS > 450bps"],
    )
    v = a.to_verdict("NVDA", sector="Technology")
    assert v.direction == "bullish"


def test_no_sector_supplied_behaves_exactly_as_before():
    a = _macro(
        "bearish", confidence="low",
        sector_guidance=[_sector("Energy", "overweight")],
        bull_triggers=["CPI cools"],
    )
    assert a.to_verdict("XOM").direction == "bearish"


def test_sector_matching_is_case_and_whitespace_insensitive():
    a = _macro(
        "bearish", confidence="medium",
        sector_guidance=[_sector("Technology", "overweight", "AI capex")],
        bull_triggers=["CPI cools"],
    )
    assert a.to_verdict("NVDA", sector="  technology ").direction == "bullish"


def test_a_neutral_sector_row_neutralises_a_directional_broad_read():
    a = _macro(
        "bullish", confidence="high",
        sector_guidance=[_sector("Utilities", "neutral", "rate-sensitive, no edge")],
        bear_triggers=["HY OAS > 450bps"],
    )
    v = a.to_verdict("NEE", sector="Utilities")
    assert v.direction == "neutral"
    assert v.magnitude == 0.0
    assert v.invalidation == ""


def test_disagreeing_sector_rows_resolve_to_neutral_not_to_the_broad_read():
    """`collapse_stances` returns "mixed" for an unresolved split, which the
    evidence registry treats as supporting nothing. Falling back to the broad
    read here would resurrect exactly the stance the sector rows contradict."""
    a = _macro(
        "bullish", confidence="high",
        sector_guidance=[
            _sector("Energy", "overweight", "refining margins"),
            _sector("Energy", "underweight", "crude rolling over"),
        ],
        bear_triggers=["HY OAS > 450bps"],
    )
    v = a.to_verdict("XOM", sector="Energy")
    assert v.direction == "neutral"
    assert v.magnitude == 0.0


def test_the_deciding_sector_row_is_cited_first_in_the_evidence():
    a = _macro(
        "bullish", confidence="high",
        sector_guidance=[_sector("Energy", "underweight", "crude rolling over")],
        bear_triggers=["HY OAS > 450bps"],
    )
    v = a.to_verdict("XOM", sector="Energy")
    assert v.evidence[0].label == "sector_stance:Energy"
    assert "crude rolling over" in v.evidence[0].text
    # The reader must be able to see WHAT it overrode.
    assert "bullish" in v.evidence[0].text


def test_the_verdict_direction_matches_the_evidence_registry_for_the_same_read():
    """The actual defect item 31 named: one macro read, one answer. Compare
    `to_verdict` against `build_evidence_registry`'s own per-symbol stance,
    computed from the identical MacroAnalysis and sector mapping."""
    from src.agents.portfolio_manager import PortfolioManagerAgent
    from src.models import (
        NewsIntelligenceReport, StockNewsItem, normalize_sector_stance,
    )

    a = _macro(
        "bullish", confidence="high",
        sector_guidance=[_sector("Energy", "underweight", "crude rolling over")],
        bear_triggers=["HY OAS > 450bps"],
    )
    # The registry only reaches symbols something else already put in it; a
    # news item is the cheapest way to get XOM onto the surface.
    news = NewsIntelligenceReport(
        macro_narrative={
            "last_updated": "2026-09-13",
            "era_themes": ["AI capex"],
            "current_regime": "risk-on",
        },
        pm_briefing="nothing actionable",
        market_sentiment="neutral",
        confidence="low",
        stock_news={"XOM": [StockNewsItem(
            headline="XOM refinery update", sentiment="neutral",
            conviction="low", impact_summary="no directional read",
        )]},
    )
    registry = PortfolioManagerAgent.build_evidence_registry(
        analyses=[], positions=[], news_intel=news, earnings_analyses=[],
        macro_analysis=a.model_dump(), smart_money_findings=None,
        symbol_sectors={"XOM": "Energy"},
    )
    registry_stance = normalize_sector_stance(registry["XOM"]["macro"])
    assert registry_stance == a.to_verdict("XOM", sector="Energy").direction
