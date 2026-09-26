"""Board item 109 — the macro double-count, and the owner's 2026-09-25 ruling.

Macro is the only seat that back-fills a stance onto EVERY name: when a read
states nothing about a symbol's sector, `build_evidence_registry` writes the
market-wide `equity_outlook` into that symbol's slot. The §9.4 agreement
tally then counted it as one more INDEPENDENT per-name seat, so one market
opinion was counted once per name, as though several analysts had each looked
at each name.

The ruling this file pins, in three parts:

  * Macro STAYS as a per-name input, weighted by the strength the reading
    itself states. The seat's only strength expression is `confidence`, which
    `MacroAnalysis.to_verdict` already reports as `conviction` and
    `src/verdicts.py::score_verdict` already weights; magnitude stays
    `NO_STATED_STRENGTH`, because a second scale keyed on the same field is
    the double count retired item 31 deleted. Nothing new is invented here.
  * The weighting is SIGN-SYMMETRIC — the same reading at the same stated
    strength moves a long and a short by the same magnitude in opposite
    directions, with no asymmetry between the two sides.
  * No single seat, macro included, may admit a name on its own.
"""

from __future__ import annotations

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.models import (
    AnalystVerdict, MacroAnalysis, MacroPositionGuidance, MacroReasoningChain,
    NO_STATED_STRENGTH, Position, TechAnalysisResult,
    TechReasoningChain, VerdictEvidence,
)
from src.risk.rules import (
    OWN_BAR_REASON_PREFIX,
    count_aligned_sources,
    count_opposing_sources,
    own_bar_block_reason,
    signed_source_score,
)
from src.verdicts import score_verdict, seat_weight


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _chain() -> MacroReasoningChain:
    return MacroReasoningChain(
        volatility_analysis="VIX compressing.",
        yield_curve_analysis="Curve steepening.",
        monetary_policy_analysis="Fed on hold.",
        inflation_labor_credit="Core CPI sticky, labor soft, credit tight.",
        cross_signal_synthesis="Risk-on lean with an inflation caveat.",
        sector_implications="Energy overweight.",
    )


def _energy_row(reason: str = "crude backwardation") -> dict:
    """`MacroAnalysis._sanitize_sector_guidance` keeps DICT rows and silently
    drops model instances, so the seat's real LLM-JSON shape is what is
    exercised here."""
    return {"sector": "Energy", "stance": "overweight", "reason": reason}


def _macro_analysis(
    *, equity_outlook: str = "bullish", confidence: str = "medium",
    sector_guidance: list[dict] | None = None,
) -> MacroAnalysis:
    return MacroAnalysis(
        reasoning_chain=_chain(),
        regime="risk-on" if equity_outlook == "bullish" else "risk-off",
        confidence=confidence,
        equity_outlook=equity_outlook,
        key_observations=[],
        sector_guidance=sector_guidance or [],
        risk_factors=[],
        position_guidance=MacroPositionGuidance(
            target_invested_pct=75.0, cash_recommendation_pct=25.0,
            reasoning="Hold buffer.",
        ),
        bull_triggers=["credit spreads snap tighter"],
        bear_triggers=["credit spreads blow out past the March wides"],
        summary="Moderately supportive.",
    )


def _analysis(symbol: str) -> TechAnalysisResult:
    return TechAnalysisResult(
        symbol=symbol, rating="buy", entry_price=100.0, stop_loss=95.0,
        reference_target=115.0, support_levels=[95.0], resistance_levels=[115.0],
        computed_levels=[95.0, 115.0], atr_14=(100.0 - 95.0) / 3.5,
        setup_type="range", expected_horizon_sessions=60,
        reasoning="test",
        reasoning_chain=TechReasoningChain(
            trend="x", momentum="x", volatility="x", volume="x",
            support_resistance="x",
        ),
        thesis_invalid_if="closes below support",
    )


def _registry(macro: MacroAnalysis, *, sector: str | None) -> dict:
    return PortfolioManagerAgent.build_evidence_registry(
        analyses=[_analysis("XOM")],
        positions=[],
        news_intel=None,
        earnings_analyses=[],
        macro_analysis=macro.model_dump(),
        symbol_sectors={"XOM": sector} if sector else {},
    )


def _uncounted(macro: MacroAnalysis, registry: dict, *, sector: str | None) -> dict:
    return PortfolioManagerAgent.uncounted_evidence_sources(
        earnings_analyses=[],
        registry=registry,
        positions=[],
        macro_analysis=macro.model_dump(),
        symbol_sectors={"XOM": sector} if sector else {},
    )


def _v(seat, *, direction="bullish", conviction="medium"):
    return AnalystVerdict(
        seat=seat, symbol="XOM", direction=direction, magnitude=0.0,
        conviction=conviction,
        evidence=[VerdictEvidence(label="ev", text="a checkable observed fact")],
        invalidation="closes back below the breakout level",
    )


def _macro_verdict(*, direction="bullish", conviction="medium", sector_specific):
    label = "sector_stance:Energy" if sector_specific else "equity_outlook"
    return AnalystVerdict(
        seat="macro", symbol="XOM", direction=direction,
        magnitude=NO_STATED_STRENGTH, conviction=conviction,
        evidence=[VerdictEvidence(label=label, text="a checkable observed fact")],
        invalidation="the broad regime call reverses",
    )


# ---------------------------------------------------------------------------
# A broadcast stance is still COVERAGE — it is simply not per-name AGREEMENT
# ---------------------------------------------------------------------------

def test_a_broadcast_macro_stance_stays_in_the_registry():
    """It is real coverage the PM may still cite and `validate_grounding`
    must still recognise. Removing it from the registry would turn every
    honest macro citation into a grounding failure."""
    macro = _macro_analysis(equity_outlook="bullish")
    registry = _registry(macro, sector=None)
    assert registry["XOM"]["macro"] == "bullish"


def test_a_broadcast_macro_stance_does_not_count_toward_agreement():
    macro = _macro_analysis(equity_outlook="bullish")
    registry = _registry(macro, sector=None)
    uncounted = _uncounted(macro, registry, sector=None)
    assert uncounted["XOM"] == frozenset({"macro"})
    sources = registry["XOM"]
    # Technical is the only seat left counting.
    assert count_aligned_sources(
        "XOM", sources, "long", ignored_sources=uncounted["XOM"],
    ) == 1


def test_a_sector_specific_macro_stance_still_counts():
    """The whole point of the ruling: macro is weighted, not removed. A read
    that actually looked at this name's sector is per-name evidence and is
    counted exactly as before."""
    macro = _macro_analysis(
        equity_outlook="bearish",
        sector_guidance=[_energy_row("crude backwardation and capex discipline")],
    )
    registry = _registry(macro, sector="Energy")
    uncounted = _uncounted(macro, registry, sector="Energy")
    assert registry["XOM"]["macro"] == "bullish"
    assert "XOM" not in uncounted
    assert count_aligned_sources("XOM", registry["XOM"], "long") == 2


def test_broadcast_and_sector_stances_do_not_count_the_same():
    """Same macro read, same symbol, same stated confidence — the only
    difference is whether the seat looked at this name's own sector."""
    broad = _macro_analysis(equity_outlook="bullish")
    sector = _macro_analysis(
        equity_outlook="bullish",
        sector_guidance=[_energy_row()],
    )
    broad_reg, sector_reg = _registry(broad, sector=None), _registry(sector, sector="Energy")
    broad_net = signed_source_score(
        "XOM", broad_reg["XOM"], "long",
        ignored_sources=_uncounted(broad, broad_reg, sector=None).get("XOM"),
    )
    sector_net = signed_source_score(
        "XOM", sector_reg["XOM"], "long",
        ignored_sources=_uncounted(sector, sector_reg, sector="Energy").get("XOM"),
    )
    assert broad_net == 1 and sector_net == 2


# ---------------------------------------------------------------------------
# SIGN SYMMETRY — the owner's second ruling, pinned
# ---------------------------------------------------------------------------

def test_a_sector_macro_reading_moves_a_long_and_a_short_equally_and_oppositely():
    """"It can be measured and weighted depending on if it's positive or
    negative. Would help on a long or a short." One reading, one stated
    strength: equal magnitude, opposite sign."""
    macro = _macro_analysis(
        equity_outlook="neutral",
        sector_guidance=[_energy_row()],
    )
    sources = {"macro": "bullish"}
    long_net = signed_source_score("XOM", sources, "long")
    short_net = signed_source_score("XOM", sources, "short")
    assert long_net == -short_net != 0
    assert count_aligned_sources("XOM", sources, "long") == count_opposing_sources(
        "XOM", sources, "short",
    )
    # And the mirrored reading mirrors the result exactly.
    bearish = {"macro": "bearish"}
    assert signed_source_score("XOM", bearish, "long") == -long_net
    assert signed_source_score("XOM", bearish, "short") == -short_net
    # The sector row, not the broad outlook, is what set that direction.
    assert macro.to_verdict("XOM", sector="Energy").direction == "bullish"


def test_the_broadcast_exclusion_is_symmetric_across_the_two_sides():
    """A broadcast stance stops corroborating a long by exactly as much as it
    stops dissenting against it — the exclusion is one removal consulted
    identically by both counts, so it cannot favour either direction."""
    for outlook in ("bullish", "bearish"):
        macro = _macro_analysis(equity_outlook=outlook)
        registry = _registry(macro, sector=None)
        ignored = _uncounted(macro, registry, sector=None)["XOM"]
        sources = registry["XOM"]
        for direction in ("long", "short"):
            with_macro = signed_source_score("XOM", sources, direction)
            without = signed_source_score(
                "XOM", sources, direction, ignored_sources=ignored,
            )
            # The macro contribution removed is +1 or -1, never anything else,
            # and it is removed whichever way it pointed.
            assert abs(with_macro - without) == 1


def test_the_stated_strength_weights_a_long_and_a_short_identically():
    """The weight is READ from the seat's own `confidence`, the only strength
    this reading states, and it is the same number on either side."""
    for conviction, expected in (("low", 0.0), ("medium", 0.5), ("high", 1.0)):
        bull = _macro_analysis(equity_outlook="bullish", confidence=conviction)
        bear = _macro_analysis(equity_outlook="bearish", confidence=conviction)
        bull_v, bear_v = bull.to_verdict("XOM"), bear.to_verdict("XOM")
        assert bull_v.conviction == bear_v.conviction == conviction
        assert score_verdict(bull_v) == score_verdict(bear_v) == expected
        # Magnitude stays flat — a second scale keyed on `confidence` would
        # be the retired item 31 double count returning.
        assert bull_v.magnitude == bear_v.magnitude == NO_STATED_STRENGTH


def test_a_stronger_macro_reading_weighs_more_than_a_weaker_one():
    """"Depending on how strong the data point is ... the data point should be
    weighted." Measured from the reading, not chosen here."""
    scores = [
        score_verdict(_macro_analysis(confidence=c).to_verdict("XOM"))
        for c in ("low", "medium", "high")
    ]
    assert scores == sorted(scores) and scores[0] < scores[-1]
    assert seat_weight("macro") > 0  # never muted


# ---------------------------------------------------------------------------
# NO-SOLO — enforced at the entry bar itself
# ---------------------------------------------------------------------------

def test_macro_alone_cannot_admit_a_name():
    """"Nothing can green light a name on its own." Technical confirms timing
    but carries no positive weight, so macro would otherwise be the only
    supporter and the name would pass."""
    for sector_specific in (True, False):
        verdicts = [
            _v("technical"),
            _macro_verdict(direction="bullish", sector_specific=sector_specific),
        ]
        reason = own_bar_block_reason(verdicts, direction="bullish")
        assert reason is not None
        assert reason.startswith(OWN_BAR_REASON_PREFIX)
        assert "macro is the only seat supporting" in reason


def test_macro_alone_cannot_admit_a_short_either():
    verdicts = [
        _v("technical", direction="bearish"),
        _macro_verdict(direction="bearish", sector_specific=True),
    ]
    reason = own_bar_block_reason(verdicts, direction="bearish")
    assert reason is not None and "macro is the only seat supporting" in reason


def test_macro_plus_another_seat_still_clears_the_bar():
    """The rule is no-SOLO, not a macro penalty: macro corroborating a real
    second seat is untouched."""
    verdicts = [_v("technical"), _v("news"), _macro_verdict(sector_specific=True)]
    assert own_bar_block_reason(verdicts, direction="bullish") is None


def test_the_highest_possible_macro_strength_is_still_not_enough_alone():
    """Unconditional on the weight: no strength a macro reading can state
    makes one market view sufficient by itself."""
    verdicts = [
        _v("technical"),
        _macro_verdict(conviction="high", sector_specific=True),
    ]
    assert own_bar_block_reason(verdicts, direction="bullish") is not None


# ---------------------------------------------------------------------------
# The gate can only ever shrink the tally
# ---------------------------------------------------------------------------

def test_the_broadcast_gate_never_manufactures_agreement():
    macro = _macro_analysis(equity_outlook="bearish")
    registry = _registry(macro, sector=None)
    ignored = _uncounted(macro, registry, sector=None)["XOM"]
    sources = registry["XOM"]
    for direction in ("long", "short"):
        assert count_aligned_sources(
            "XOM", sources, direction, ignored_sources=ignored,
        ) <= count_aligned_sources("XOM", sources, direction)


def test_a_held_name_with_no_registry_coverage_is_still_reached():
    """Positions, not just analysed candidates, are the population macro was
    broadcasting onto — the gate has to see them or the double count survives
    on exactly the names it hurt most."""
    macro = _macro_analysis(equity_outlook="bullish")
    positions = [Position(
        symbol="KO", qty=10, avg_entry=60.0, current_price=62.0,
        market_value=620.0, unrealized_pnl=20.0, sector="Consumer Defensive",
    )]
    registry = PortfolioManagerAgent.build_evidence_registry(
        analyses=[], positions=positions, news_intel=None,
        earnings_analyses=[], macro_analysis=macro.model_dump(),
    )
    uncounted = PortfolioManagerAgent.uncounted_evidence_sources(
        earnings_analyses=[], registry=registry, positions=positions,
        macro_analysis=macro.model_dump(),
    )
    assert registry["KO"]["macro"] == "bullish"
    assert uncounted["KO"] == frozenset({"macro"})
