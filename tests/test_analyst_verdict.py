"""Phase 13 — the shared analyst verdict shape, Technical's mapping onto it,
and the Portfolio Manager's deterministic ranking over it.

Three things are pinned here:

1. `AnalystVerdict` refuses an incomplete verdict — a directional call with
   no invalidation condition or no evidence is not a verdict.
2. `TechAnalysisResult.to_verdict` is a RESTATEMENT of a real technical
   read (including the production-shaped response `tests/test_tech_analyst`
   uses), not a second opinion.
3. `PortfolioManagerAgent.rank_candidates` produces a deterministic order
   among equally-eligible candidates and never orders a name a gate refused.
   The eligibility port is cross-checked against the item-18 audit script on
   the real run-64290730 fixture, so the two cannot drift apart silently.
"""

from __future__ import annotations

import ast
import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from ops.model_policy import scenarios as S
from ops.model_policy.deterministic_selection import evaluate
from src.agents.portfolio_manager import PortfolioManagerAgent
from src.models import (
    RATING_DIRECTION, RATING_MAGNITUDE, AnalystVerdict, EarningsAnalysis,
    NewsIntelligenceReport, StockNewsItem, TechAnalysisResult,
    TechReasoningChain, VerdictEvidence,
)
from src.risk.constants import REWARD_RISK_FLOOR
from src.verdicts import (
    CONVICTION_SCORE, RANKING_SIGNALS, SEAT_WEIGHT, rank_verdicts, score_verdict,
    seat_weight,
)

REPO = Path(__file__).parent.parent
SESSION = date(2026, 9, 1)


def _chain() -> TechReasoningChain:
    return TechReasoningChain(
        trend="above MA20/50/200", momentum="RSI 58, MACD positive",
        volatility="mid-band", volume="+15% confirms",
        support_resistance="support 95, resistance 112",
    )


def _tech(symbol: str, rating: str = "buy", conviction: str = "medium",
          invalid_if: str = "closes below MA50 on volume",
          target: float | None = None) -> TechAnalysisResult:
    if rating == "neutral":
        return TechAnalysisResult(
            symbol=symbol, rating="neutral", conviction=conviction,
            reasoning="no setup", reasoning_chain=_chain(),
            thesis_invalid_if=invalid_if,
        )
    long = rating in ("buy", "strong_buy")
    # 100 / 95 / 112 is R/R 2.40 long; 100 / 105 / 88 is 2.40 short.
    return TechAnalysisResult(
        symbol=symbol, rating=rating, conviction=conviction, entry_price=100,
        stop_loss=95 if long else 105,
        reference_target=(target if target is not None else (112 if long else 88)),
        support_levels=[95] if long else [88],
        resistance_levels=[112] if long else [105],
        setup_type="range", expected_horizon_sessions=10,
        reasoning="one-line why", reasoning_chain=_chain(),
        thesis_invalid_if=invalid_if,
    )


# ==========================================================================
# 1. The shape
# ==========================================================================

def _evidence() -> list[VerdictEvidence]:
    return [VerdictEvidence(label="stop_loss", value=95.0)]


def test_a_complete_directional_verdict_validates():
    v = AnalystVerdict(
        seat="technical", symbol="aapl", direction="bullish", magnitude=0.5,
        conviction="medium", evidence=_evidence(), invalidation="closes below 95",
    )
    assert v.symbol == "AAPL"
    assert v.signed_magnitude == 0.5


def test_a_bearish_verdict_has_a_negative_signed_magnitude():
    v = AnalystVerdict(
        seat="technical", symbol="AAPL", direction="bearish", magnitude=1.0,
        conviction="high", evidence=_evidence(), invalidation="closes above 105",
    )
    assert v.signed_magnitude == -1.0


@pytest.mark.parametrize("invalidation", ["", "   ", None])
def test_a_directional_verdict_without_an_invalidation_is_refused(invalidation):
    with pytest.raises(ValidationError, match="invalidation"):
        AnalystVerdict(
            seat="technical", symbol="AAPL", direction="bullish", magnitude=0.5,
            conviction="medium", evidence=_evidence(), invalidation=invalidation,
        )


def test_a_directional_verdict_without_evidence_is_refused():
    with pytest.raises(ValidationError, match="evidence"):
        AnalystVerdict(
            seat="technical", symbol="AAPL", direction="bullish", magnitude=0.5,
            conviction="medium", evidence=[], invalidation="closes below 95",
        )


def test_a_neutral_verdict_may_be_blank_but_may_not_lean():
    neutral = AnalystVerdict(
        seat="technical", symbol="AAPL", direction="neutral", magnitude=0.0,
        conviction="low",
    )
    assert neutral.signed_magnitude == 0.0
    with pytest.raises(ValidationError, match="neutral"):
        AnalystVerdict(
            seat="technical", symbol="AAPL", direction="neutral", magnitude=0.3,
            conviction="low",
        )


def test_magnitude_is_bounded_to_the_unit_interval():
    for bad in (-0.1, 1.1):
        with pytest.raises(ValidationError):
            AnalystVerdict(
                seat="technical", symbol="AAPL", direction="bullish", magnitude=bad,
                conviction="medium", evidence=_evidence(), invalidation="x",
            )


def test_evidence_must_carry_something_checkable():
    with pytest.raises(ValidationError, match="checkable"):
        VerdictEvidence(label="trend")
    VerdictEvidence(label="trend", text="above MA20")
    VerdictEvidence(label="event", as_of=date(2026, 8, 31))
    VerdictEvidence(label="stop", value=95.0)


def test_the_shape_has_exactly_the_four_judgement_fields_plus_identity():
    """The spec names four things. Identity (seat, symbol) is not judgement.
    A fifth judgement field is a design change and should fail here first."""
    assert set(AnalystVerdict.model_fields) == {
        "seat", "symbol", "direction", "magnitude", "conviction",
        "evidence", "invalidation",
    }


# ==========================================================================
# 2. Technical's mapping
# ==========================================================================

def test_rating_encodings_are_equal_spaced_and_symmetric():
    assert RATING_MAGNITUDE == {
        "strong_buy": 1.0, "buy": 0.5, "neutral": 0.0, "sell": 0.5, "strong_sell": 1.0,
    }
    assert RATING_DIRECTION["buy"] == RATING_DIRECTION["strong_buy"] == "bullish"
    assert RATING_DIRECTION["sell"] == RATING_DIRECTION["strong_sell"] == "bearish"
    assert RATING_DIRECTION["neutral"] == "neutral"


def test_tech_read_restates_onto_the_verdict_without_inventing_anything():
    a = _tech("NVDA", "strong_buy", "high", invalid_if="closes below MA50 on volume")
    v = a.to_verdict()
    assert v.seat == "technical"
    assert v.symbol == "NVDA"
    assert (v.direction, v.magnitude, v.conviction) == ("bullish", 1.0, "high")
    assert v.invalidation == "closes below MA50 on volume"
    by_label = {}
    for e in v.evidence:
        by_label.setdefault(e.label, []).append(e)
    assert by_label["entry_price"][0].value == 100.0
    assert by_label["stop_loss"][0].value == 95.0
    assert by_label["reference_target"][0].value == 112.0
    assert by_label["risk_reward"][0].value == a.risk_reward == 2.4
    assert [e.value for e in by_label["support_level"]] == [95.0]
    assert [e.value for e in by_label["resistance_level"]] == [112.0]
    for step in ("trend", "momentum", "volatility", "volume", "support_resistance"):
        assert by_label[step][0].text == getattr(a.reasoning_chain, step)


def test_a_short_read_maps_to_a_bearish_verdict():
    v = _tech("NKE", "sell", "medium").to_verdict()
    assert (v.direction, v.magnitude, v.signed_magnitude) == ("bearish", 0.5, -0.5)


def test_a_neutral_read_maps_to_a_neutral_verdict_with_no_lean():
    v = _tech("QQQ", "neutral", invalid_if="").to_verdict()
    assert (v.direction, v.magnitude, v.invalidation) == ("neutral", 0.0, "")


def test_a_missing_soft_invalidation_falls_back_to_the_analysts_own_stop():
    """~2% of actionable reads arrive with `thesis_invalid_if` blank (see
    the field's own comment). The hard stop is the analyst's own stated
    falsifier, so the verdict uses it and SAYS so — nothing is invented and
    the verdict still validates."""
    long = _tech("AAPL", "buy", invalid_if="").to_verdict()
    assert "below stop 95" in long.invalidation
    assert "hard stop" in long.invalidation
    short = _tech("AAPL", "sell", invalid_if="").to_verdict()
    assert "above stop 105" in short.invalidation


def test_the_production_shaped_tech_response_populates_the_verdict():
    """The exact response body `tests/test_tech_analyst.py` feeds the
    agent, parsed the way `_analyze_chunk` parses it."""
    body = json.loads(json.dumps([{
        "symbol": "SPY", "rating": "buy", "conviction": "high",
        "entry_price": 507.0, "reference_target": 530.0, "stop_loss": 494.0,
        "support_levels": [494.0], "resistance_levels": [530.0],
        "setup_type": "range", "expected_horizon_sessions": 10,
        "thesis_invalid_if": "Price closes below MA50 (492) on above-average volume",
        "reasoning_chain": {
            "trend": "Above MA20/50/200 stacked bullish.",
            "momentum": "RSI 58 neutral-bullish, MACD hist positive.",
            "volatility": "Mid-band, ATR steady.",
            "volume": "+15% confirms uptrend.",
            "support_resistance": "Support MA50 498, resistance upper band 520.",
        },
        "reasoning": "Clean bullish alignment.",
    }]))
    v = TechAnalysisResult(**body[0]).to_verdict()
    assert (v.direction, v.magnitude, v.conviction) == ("bullish", 0.5, "high")
    assert v.invalidation.startswith("Price closes below MA50")
    assert {e.label for e in v.evidence} >= {
        "entry_price", "stop_loss", "reference_target", "risk_reward",
        "support_level", "resistance_level", "trend", "momentum",
    }


def test_every_real_technical_read_on_the_fixture_day_maps_to_a_valid_verdict():
    """59 production reads from run-64290730 — none may fail the shape."""
    verdicts = [a.to_verdict() for a in S._SELECTION_ANALYSES]
    assert len(verdicts) == len(S._SELECTION_ANALYSES)
    for a, v in zip(S._SELECTION_ANALYSES, verdicts):
        assert v.direction == RATING_DIRECTION[a.rating]
        assert v.conviction == a.conviction


# ==========================================================================
# 2b. Earnings' mapping
# ==========================================================================

def _earnings(sentiment: str = "bullish", conviction: str = "medium",
              bull_case: str = "services mix reaccelerates",
              bear_case: str = "China demand craters",
              data_quality: str = "complete filing, no estimates") -> EarningsAnalysis:
    return EarningsAnalysis(
        symbol="AAPL", form_type="10-Q", filing_date="2026-03-15",
        revenue={"total": "$95.4B"}, profitability={}, cash_flow={},
        balance_sheet={}, guidance="flat", data_quality=data_quality,
        investment_implications={
            "sentiment": sentiment, "conviction": conviction,
            "key_thesis": "services mix offsets hardware softness",
            "bull_case": bull_case, "bear_case": bear_case,
            "reasoning_chain": {
                "fundamental_quality": "gross margin expanding 40bps",
                "growth_trajectory": "services +12% YoY, hardware flat",
                "strategic_risks": "vision pro adoption unproven",
                "management_execution": "buyback pace matches prior guide",
                "valuation_context": "premium holds only if services mix keeps rising",
            },
        },
    )


def test_a_bullish_earnings_read_maps_onto_the_verdict():
    v = _earnings("bullish", "high").to_verdict()
    assert v.seat == "earnings"
    assert v.symbol == "AAPL"
    assert (v.direction, v.magnitude, v.conviction) == ("bullish", 0.5, "high")
    assert v.invalidation == "China demand craters"
    by_label = {e.label: e for e in v.evidence}
    assert by_label["key_thesis"].text == "services mix offsets hardware softness"
    assert by_label["fundamental_quality"].text == "gross margin expanding 40bps"
    assert by_label["growth_trajectory"].text == "services +12% YoY, hardware flat"
    assert by_label["strategic_risks"].text == "vision pro adoption unproven"
    assert by_label["management_execution"].text == "buyback pace matches prior guide"
    assert by_label["valuation_context"].text == "premium holds only if services mix keeps rising"
    assert by_label["data_quality"].text == "complete filing, no estimates"


def test_a_bearish_earnings_read_uses_the_bull_case_as_its_invalidation():
    v = _earnings("bearish", "low").to_verdict()
    assert (v.direction, v.magnitude, v.conviction) == ("bearish", 0.5, "low")
    assert v.invalidation == "services mix reaccelerates"
    assert v.signed_magnitude == -0.5


def test_a_neutral_earnings_read_maps_to_a_neutral_verdict_with_no_lean():
    v = _earnings("neutral", "medium").to_verdict()
    assert (v.direction, v.magnitude, v.invalidation) == ("neutral", 0.0, "")


def test_an_undisclosed_falsifier_is_treated_as_blank_not_as_content():
    """`bull_case`/`bear_case` default to the literal 'not disclosed' — that
    placeholder must not be passed through as if it were a real falsifier.
    With no numeric stop to fall back to (unlike Technical), a directional
    call with nothing disclosed against it has a blank invalidation, and
    `AnalystVerdict` itself refuses to construct with a directional call and
    no invalidation — the seat cannot invent a falsifier from nothing."""
    a = _earnings("bullish", "medium", bear_case="not disclosed")
    with pytest.raises(ValidationError, match="invalidation"):
        a.to_verdict()
    # Case-insensitive / whitespace-padded placeholder is caught too.
    a2 = _earnings("bearish", "medium", bull_case="  Not Disclosed  ")
    with pytest.raises(ValidationError, match="invalidation"):
        a2.to_verdict()


def test_data_quality_evidence_is_omitted_when_it_is_the_bare_default():
    v = _earnings("bullish", "medium", data_quality="not disclosed").to_verdict()
    assert "data_quality" not in {e.label for e in v.evidence}


# ==========================================================================
# 3. The ranking
# ==========================================================================

def test_ranking_reads_two_signals_at_equal_weight():
    assert RANKING_SIGNALS == ("magnitude", "conviction_score")
    assert CONVICTION_SCORE == {"low": 0.0, "medium": 0.5, "high": 1.0}


def test_seat_weight_is_the_ratified_research_informed_prior():
    """§13.3 AMENDED 2026-09-03, this module only. `src/risk/rules.py`'s
    sizing-path SEAT_WEIGHT is untouched and stays pinned at 1 — see
    `tests/test_signed_dissent.py`. This one may hold a per-seat prior
    because it only reorders already-eligible, already-agreeing candidates;
    it never changes how much money a trade risks."""
    assert SEAT_WEIGHT == {
        "technical": 1.2, "earnings": 1.2, "news": 1.0,
        "smart_money": 0.8, "macro": 0.8,
    }
    assert seat_weight("technical") == 1.2
    assert seat_weight("some_future_seat_not_yet_reviewed") == 1.0, (
        "an unreviewed seat must default to unweighted, never zero or missing"
    )


def test_only_one_seat_weight_table_exists_in_the_ranking_module():
    """A SECOND, undocumented seat-keyed numeric dict sneaking in alongside
    the ratified `SEAT_WEIGHT` would be an uncited, chosen weight — the
    exact thing §13.3 still forbids anywhere except the one reviewed table.

    Catches both a `{}` literal AND a `dict(seat=weight, ...)` call — a
    prior version of this guard only walked `ast.Dict` nodes and an
    adversarial review (2026-09-03) found `dict(technical=1.1, ...)` sailed
    straight through it undetected."""
    seats = {"technical", "news", "earnings", "macro", "smart_money"}
    tree = ast.parse((REPO / "src" / "verdicts.py").read_text())
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            if keys & seats:
                hits.append(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "dict"
        ):
            kw_keys = {kw.arg for kw in node.keywords if kw.arg is not None}
            if kw_keys & seats:
                hits.append(node.lineno)
    assert len(hits) == 1, (
        f"expected exactly one seat-keyed table (SEAT_WEIGHT), found at "
        f"lines {hits}"
    )


def test_score_is_magnitude_plus_conviction():
    v = _tech("A", "strong_buy", "low").to_verdict()
    assert score_verdict(v) == 1.0
    v = _tech("A", "buy", "high").to_verdict()
    assert score_verdict(v) == 1.5
    v = _tech("A", "buy", "medium").to_verdict()
    assert score_verdict(v) == 1.0


def test_rank_verdicts_orders_by_score_then_symbol_and_skips_neutral():
    verdicts = [
        _tech("CCC", "buy", "medium").to_verdict(),          # 1.0
        _tech("AAA", "buy", "medium").to_verdict(),          # 1.0 — ties on symbol
        _tech("BBB", "strong_sell", "high").to_verdict(),    # 2.0
        _tech("DDD", "buy", "low").to_verdict(),             # 0.5
        _tech("EEE", "neutral", invalid_if="").to_verdict(), # not a candidate
    ]
    ranked = rank_verdicts(verdicts)
    assert [c.symbol for c in ranked] == ["BBB", "AAA", "CCC", "DDD"]
    assert [c.score for c in ranked] == [2.0, 1.0, 1.0, 0.5]
    assert ranked[0].direction == "bearish"
    assert ranked[0].seats == ["technical"]
    # Pure: same input, same order.
    assert [c.symbol for c in rank_verdicts(verdicts)] == ["BBB", "AAA", "CCC", "DDD"]


def test_two_seats_on_one_symbol_average_at_the_research_informed_weight():
    """Same fixture as the old unit-weight test, recomputed for §13.3's
    2026-09-03 amendment: technical enters at 1.2x, news at 1.0x, so
    technical's numbers pull the weighted average further than a plain
    mean would — the whole point of giving it a higher prior."""
    tech = _tech("XLE", "buy", "high").to_verdict()             # 0.5 + 1.0, weight 1.2
    other = AnalystVerdict(
        seat="news", symbol="XLE", direction="bullish", magnitude=1.0,
        conviction="low", evidence=_evidence(), invalidation="x",  # 1.0 + 0.0, weight 1.0
    )
    [c] = rank_verdicts([tech, other])
    # magnitude: (0.5*1.2 + 1.0*1.0) / 2.2 = 0.7273
    # conviction: (1.0*1.2 + 0.0*1.0) / 2.2 = 0.5455
    assert c.components == {"magnitude": 0.7273, "conviction_score": 0.5455}
    assert c.score == 1.2728
    assert c.seats == ["news", "technical"]


def test_a_single_seat_verdict_is_unaffected_by_its_own_weight():
    """The weighting only changes how MULTIPLE seats are averaged together.
    A symbol only one seat has a view on ranks by its own raw score either
    way, regardless of which seat that is or what its weight is."""
    tech = _tech("XLE", "buy", "high").to_verdict()
    [c] = rank_verdicts([tech])
    assert c.score == score_verdict(tech)


def test_two_verdicts_from_the_same_seat_do_not_double_that_seats_weight():
    """Found by adversarial review, 2026-09-03: two earnings filings for one
    symbol in the same run (nothing upstream enforces one filing per symbol
    per run) used to push earnings' 1.2 weight from 50% of a two-seat group
    to 66.7%, silently. `rank_verdicts` must count at most ONE verdict per
    (symbol, seat) — last one wins, matching the same convention already
    used for the evidence registry."""
    tech = _tech("XLE", "buy", "high").to_verdict()          # weight 1.2
    first_earnings = AnalystVerdict(
        seat="earnings", symbol="XLE", direction="bullish", magnitude=0.2,
        conviction="low", evidence=_evidence(), invalidation="x",
    )
    second_earnings = AnalystVerdict(
        seat="earnings", symbol="XLE", direction="bullish", magnitude=0.9,
        conviction="high", evidence=_evidence(), invalidation="y",
    )
    [with_one] = rank_verdicts([tech, first_earnings])
    [with_duplicate] = rank_verdicts([tech, first_earnings, second_earnings])
    # The duplicate must be treated as a REPLACEMENT (last wins), not an
    # ADDITION — group size, and therefore each seat's share of the total
    # weight, must be identical whether one or two earnings verdicts arrive.
    assert with_one.seats == with_duplicate.seats == ["earnings", "technical"]
    [with_second_only] = rank_verdicts([tech, second_earnings])
    assert with_duplicate.score == with_second_only.score
    assert with_duplicate.score != score_verdict(tech)  # earnings still counts, just once


def test_seats_disagreeing_on_direction_are_not_ranked():
    tech = _tech("XLE", "buy", "high").to_verdict()
    other = AnalystVerdict(
        seat="news", symbol="XLE", direction="bearish", magnitude=1.0,
        conviction="high", evidence=_evidence(), invalidation="x",
    )
    assert rank_verdicts([tech, other]) == []


# --- eligibility + ranking through the PM -----------------------------------

STATE_CHANGES = (
    "- [2026-08-31] Anthropic signs $35 billion cloud deal with "
    "Nvidia-backed Lambda → NVDA\n"
)


def _registry(analyses, extra=None):
    reg = PortfolioManagerAgent.build_evidence_registry(
        analyses=analyses, positions=[], news_intel=None, earnings_analyses=[],
        macro_analysis=None, smart_money_findings=[], symbol_sectors={},
    )
    for symbol, sources in (extra or {}).items():
        reg.setdefault(symbol, {}).update(sources)
    return reg


def test_pm_ranks_equally_eligible_candidates_deterministically():
    analyses = [
        _tech("CCC", "buy", "medium"),
        _tech("AAA", "strong_buy", "high"),
        _tech("BBB", "sell", "high"),
        _tech("DDD", "buy", "low"),
    ]
    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=analyses, evidence_registry=_registry(analyses),
        allowed_buy_symbols={"AAA", "BBB", "CCC", "DDD"},
        active_state_changes="", asof=SESSION,
    )
    assert blocked == {}
    assert [c.symbol for c in ranked] == ["AAA", "BBB", "CCC", "DDD"]
    assert [c.score for c in ranked] == [2.0, 1.5, 1.0, 0.5]


def test_eligibility_agrees_with_the_constructors_real_gate_once_wired():
    """2026-09-04 fix, case (a). Without `real_reward_risk_by_symbol`, the
    PM's eligibility gate and `PortfolioConstructor`'s real gate can pass
    DISJOINT sets on the same candidates — that was the audit finding.
    With it wired, `candidate_eligibility`'s admitted set matches the
    constructor's real one on the SAME candidate data: NVDA overstates
    (self-reported R/R 10.0, real 0.60 — the constructor would refuse it)
    and GEV understates (self-reported 0.8, real 1.60 — the constructor
    would take it). Both directions of the disagreement are covered in one
    place, on real production geometry, not a hand-picked ratio."""
    from src.portfolio_constructor import PortfolioConstructor

    def _structured(symbol, *, model_target, computed_levels):
        return TechAnalysisResult(
            symbol=symbol, rating="buy", conviction="medium",
            entry_price=100.0, stop_loss=95.0, reference_target=model_target,
            support_levels=[95.0], resistance_levels=[model_target],
            computed_levels=computed_levels, atr_14=(100.0 - 95.0) / 3.5,
            setup_type="range", expected_horizon_sessions=60,
            reasoning="test", reasoning_chain=_chain(),
        )

    overstated = _structured("NVDA", model_target=150.0, computed_levels=[95.0, 103.0])
    understated = _structured("GEV", model_target=104.0, computed_levels=[95.0, 108.0])
    analyses = [overstated, understated]
    allowed = {"NVDA", "GEV"}

    constructor = PortfolioConstructor()
    real_map = {
        a.symbol: constructor.real_reward_risk_preview(a, "long")
        for a in analyses
    }
    real_eligible = {sym for sym, rr in real_map.items() if (rr or 0.0) >= REWARD_RISK_FLOOR}
    assert real_eligible == {"GEV"}  # the constructor's own real answer

    # OLD path (no real map): eligibility keys off the self-reported ratio
    # and DISAGREES with the constructor on BOTH names.
    old = PortfolioManagerAgent.candidate_eligibility(
        analyses=analyses, evidence_registry=_registry(analyses),
        allowed_buy_symbols=allowed, active_state_changes="", asof=SESSION,
    )
    old_eligible = {sym for sym, why in old.items() if not why}
    assert old_eligible == {"NVDA"}
    assert old_eligible != real_eligible

    # NEW path: eligibility matches the constructor's real gate exactly.
    new = PortfolioManagerAgent.candidate_eligibility(
        analyses=analyses, evidence_registry=_registry(analyses),
        allowed_buy_symbols=allowed, active_state_changes="", asof=SESSION,
        real_reward_risk_by_symbol=real_map,
    )
    new_eligible = {sym for sym, why in new.items() if not why}
    assert new_eligible == real_eligible == {"GEV"}


# ==========================================================================
# 2c. All five seats through the real integration path (2026-09-03)
# ==========================================================================

def _news_intel(stock_news: dict) -> "NewsIntelligenceReport":
    return NewsIntelligenceReport(
        macro_narrative={
            "last_updated": "2026-09-03", "era_themes": ["rates"],
            "current_regime": "mid-cycle, rates on hold",
        },
        stock_news=stock_news, pm_briefing="ok",
        market_sentiment="neutral", confidence="medium",
    )


def test_rank_candidates_combines_all_five_seats_at_their_researched_weight():
    """End-to-end through the real PM entry point, not `rank_verdicts`
    directly: one symbol where technical, news, and earnings all cover it
    and agree bullish, must outrank a symbol only technical covers at the
    same tech reading — proving the extra seats actually reach the ranking,
    not just exist as unused methods."""
    solo = _tech("SOLO", "buy", "medium")     # 0.5 + 0.5 = 1.0, alone
    covered = _tech("MULTI", "buy", "medium")  # same tech reading as SOLO
    analyses = [solo, covered]

    news_intel = _news_intel({
        "MULTI": [StockNewsItem(
            headline="guidance raised", sentiment="bullish", conviction="high",
            impact_summary="raised full-year guide",
        )],
    })
    multi_earnings = _earnings("bullish", "high")
    multi_earnings.symbol = "MULTI"
    earnings_analyses = [{
        "symbol": "MULTI",
        "analysis": multi_earnings.model_dump(),
    }]

    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=analyses, evidence_registry=_registry(analyses),
        allowed_buy_symbols={"SOLO", "MULTI"},
        active_state_changes="", asof=SESSION,
        news_intel=news_intel, earnings_analyses=earnings_analyses,
    )
    assert blocked == {}
    assert [c.symbol for c in ranked] == ["MULTI", "SOLO"]
    assert ranked[0].seats == ["earnings", "news", "technical"]
    # SOLO is untouched by the new seats: still exactly its own raw score.
    solo_candidate = ranked[1]
    assert solo_candidate.score == score_verdict(solo.to_verdict())


def test_rank_candidates_survives_a_malformed_earnings_entry():
    """A single bad earnings dict must drop only earnings' contribution for
    that symbol — never crash the whole ranking call."""
    analyses = [_tech("AAA", "buy", "medium")]
    bad_earnings = [{"symbol": "AAA", "analysis": {"not_a_real_shape": True}}]
    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=analyses, evidence_registry=_registry(analyses),
        allowed_buy_symbols={"AAA"}, active_state_changes="", asof=SESSION,
        earnings_analyses=bad_earnings,
    )
    assert blocked == {}
    assert [c.symbol for c in ranked] == ["AAA"]
    assert ranked[0].seats == ["technical"]  # earnings silently dropped, not crashed


def test_rank_candidates_drops_earnings_with_no_wrapper_symbol_at_all():
    """A blank/missing wrapper symbol means there's no ground truth to check
    the LLM's own claimed symbol against — must drop, not trust it anyway.
    Matches `_earnings_stance_rows`'s identical handling of this case."""
    analyses = [_tech("AAA", "buy", "medium")]
    no_symbol_wrapper = [{
        "symbol": "", "analysis": _earnings("bullish", "high").model_dump(),
    }]
    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=analyses, evidence_registry=_registry(analyses),
        allowed_buy_symbols={"AAA"}, active_state_changes="", asof=SESSION,
        earnings_analyses=no_symbol_wrapper,
    )
    assert blocked == {}
    assert [c.symbol for c in ranked] == ["AAA"]
    assert ranked[0].seats == ["technical"]


def test_rank_candidates_drops_earnings_on_a_wrapper_vs_analysis_symbol_mismatch():
    """The pipeline wrapper's `symbol` is ground truth; `EarningsAnalysis.
    symbol` is part of the LLM's own JSON and could disagree (a hallucinated
    ticker). A mismatch must drop earnings' contribution for that entry, not
    trust either side silently."""
    analyses = [_tech("AAA", "buy", "medium")]
    mismatched = _earnings("bullish", "high")  # symbol="AAPL" by fixture default
    earnings_analyses = [{"symbol": "AAA", "analysis": mismatched.model_dump()}]
    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=analyses, evidence_registry=_registry(analyses),
        allowed_buy_symbols={"AAA"}, active_state_changes="", asof=SESSION,
        earnings_analyses=earnings_analyses,
    )
    assert blocked == {}
    assert [c.symbol for c in ranked] == ["AAA"]
    assert ranked[0].seats == ["technical"]  # earnings dropped on mismatch


def test_rank_candidates_survives_a_malformed_macro_dict():
    """Same contract for macro: garbage input must not take down the run."""
    analyses = [_tech("AAA", "buy", "medium")]
    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=analyses, evidence_registry=_registry(analyses),
        allowed_buy_symbols={"AAA"}, active_state_changes="", asof=SESSION,
        macro_analysis={"not_a_real_shape": True},
    )
    assert blocked == {}
    assert [c.symbol for c in ranked] == ["AAA"]
    assert ranked[0].seats == ["technical"]


def test_rank_candidates_macro_verdict_applies_uniformly_to_every_symbol():
    """Documented simplification: macro's verdict is the same broad read for
    every candidate, not sector-adjusted. Two unrelated symbols both get the
    same macro contribution."""
    analyses = [_tech("AAA", "buy", "medium"), _tech("BBB", "buy", "medium")]
    macro = {
        "reasoning_chain": {
            "volatility_analysis": "VIX mid-range, flat trend",
            "yield_curve_analysis": "2s10s modestly positive",
            "monetary_policy_analysis": "Fed on hold, no forward guidance shift",
            "inflation_labor_credit": "CPI cooling, unemployment stable, spreads tight",
            "cross_signal_synthesis": "consistent risk-on read across signals",
            "sector_implications": "broad, no strong sector tilt",
        },
        "regime": "risk-on", "confidence": "high", "equity_outlook": "bullish",
        "position_guidance": {
            "target_invested_pct": 90, "cash_recommendation_pct": 10,
            "reasoning": "risk-on regime supports full deployment",
        },
        "summary": "broadly constructive",
        "bear_triggers": ["CPI surprises hot"],
    }
    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=analyses, evidence_registry=_registry(analyses),
        allowed_buy_symbols={"AAA", "BBB"}, active_state_changes="",
        asof=SESSION, macro_analysis=macro,
    )
    assert blocked == {}
    assert {c.symbol for c in ranked} == {"AAA", "BBB"}
    for c in ranked:
        assert c.seats == ["macro", "technical"]


def test_pm_never_orders_a_name_a_gate_refused():
    analyses = [
        _tech("GOOD", "buy", "medium"),
        _tech("NEUT", "neutral", "high", invalid_if=""),          # R2
        _tech("NOTU", "strong_buy", "high"),                      # R3 — not BUY-eligible
        _tech("SUBF", "strong_buy", "high", target=104),          # R4 — R/R 0.8, no row
        _tech("NVDA", "strong_buy", "high", target=104),          # R4 door — row names it
        _tech("OPPO", "strong_buy", "high"),                      # R5 — net 0
    ]
    registry = _registry(analyses, extra={"OPPO": {"macro": "bearish"}})
    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=analyses, evidence_registry=registry,
        allowed_buy_symbols={"GOOD", "NEUT", "SUBF", "NVDA", "OPPO"},
        active_state_changes=STATE_CHANGES, asof=SESSION,
    )
    # Every refused name would have OUTSCORED GOOD (2.0 vs 1.0) — none is ranked.
    assert [c.symbol for c in ranked] == ["NVDA", "GOOD"]
    assert set(blocked) == {"NEUT", "NOTU", "SUBF", "OPPO"}
    assert blocked["NEUT"] == ["R2 neutral rating"]
    assert blocked["NOTU"] == ["R3 not BUY-eligible"]
    assert blocked["SUBF"][0].startswith("R4 R/R 0.80 under the 1.50 floor")
    assert blocked["OPPO"] == ["R5 net evidence +0 if long — no rung"]


def test_the_rr_floor_used_for_ranking_is_the_one_threaded_in():
    analyses = [_tech("AAA", "buy", "high", target=108)]  # R/R 1.6
    kwargs = dict(
        analyses=analyses, evidence_registry=_registry(analyses),
        allowed_buy_symbols={"AAA"}, active_state_changes="", asof=SESSION,
    )
    ranked, _ = PortfolioManagerAgent.rank_candidates(rr_floor=REWARD_RISK_FLOOR, **kwargs)
    assert [c.symbol for c in ranked] == ["AAA"]
    ranked, blocked = PortfolioManagerAgent.rank_candidates(rr_floor=2.0, **kwargs)
    assert ranked == [] and "AAA" in blocked


def test_shorts_are_not_subject_to_the_buy_eligibility_gate():
    analyses = [_tech("NKE", "sell", "medium")]
    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=analyses, evidence_registry=_registry(analyses),
        allowed_buy_symbols=set(), active_state_changes="", asof=SESSION,
    )
    assert [c.symbol for c in ranked] == ["NKE"] and blocked == {}


def test_production_eligibility_matches_the_item_18_audit_on_the_real_day():
    """The audit script (`ops/model_policy/deterministic_selection.py`) and
    the production port must admit the SAME twelve names on run-64290730.
    If one moves and the other does not, this fails."""
    sel = S._SELECTION
    rows = evaluate(sel, S._SELECTION_ANALYSES, S._SELECTION_POSITIONS, S._SELECTION_NEWS)
    audit = sorted(r["symbol"] for r in rows if r["eligible"])
    registry = PortfolioManagerAgent.build_evidence_registry(
        analyses=S._SELECTION_ANALYSES, positions=S._SELECTION_POSITIONS,
        news_intel=S._SELECTION_NEWS, earnings_analyses=sel["earnings_analyses"],
        macro_analysis=sel["macro_analysis"], smart_money_findings=[],
        symbol_sectors={},
    )
    stale = PortfolioManagerAgent.stale_evidence_sources(
        earnings_analyses=sel["earnings_analyses"],
    )
    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=S._SELECTION_ANALYSES, evidence_registry=registry,
        stale_sources=stale,
        allowed_buy_symbols=set(sel["allowed_buy_symbols"]) | set(sel["transient_admitted_symbols"]),
        active_state_changes=sel["memory"]["active_state_changes"], asof=SESSION,
    )
    assert sorted(c.symbol for c in ranked) == audit
    assert len(audit) == 12
    assert not (set(blocked) & set(audit))
    # The pinned order at equal weight over Technical's verdicts alone. Nine
    # of the twelve tie at 1.00 (all `buy`/`sell` at `medium`), and ties
    # break on symbol — see docs/WORK.md item 18 for why that is reported
    # rather than "fixed" with a weight nobody has measured.
    assert [c.symbol for c in ranked] == [
        "XLE", "COP", "CVX", "FLNC", "MSFT", "NKE", "NVDA", "PATH", "PFE",
        "TSM", "CHPX", "CRM",
    ]


def test_the_ranking_is_rendered_into_the_pm_prompt(monkeypatch):
    import src.agents.portfolio_manager as pm_module
    monkeypatch.setattr(pm_module, "et_today", lambda: SESSION)
    analyses = [
        _tech("CCC", "buy", "medium"),
        _tech("AAA", "strong_buy", "high"),
        _tech("ZZZ", "neutral", invalid_if=""),
    ]
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    msg = agent.build_user_message(
        analyses=analyses, positions=[], macro_analysis=None, cash_balance=10_000,
        total_value=100_000, news_intel=None, earnings_analyses=[],
        smart_money_findings=[], allowed_buy_symbols={"AAA", "CCC"},
        active_state_changes="", rr_floor=REWARD_RISK_FLOOR,
    )
    section = msg.split("## Candidate Ranking")[1].split("\n## ")[0]
    lines = [ln for ln in section.splitlines() if ln[:2] in ("1.", "2.", "3.")]
    assert lines[0].startswith("1. AAA — bullish | score 2.00")
    assert lines[1].startswith("2. CCC — bullish | score 1.00")
    assert "invalid if — technical: closes below MA50 on volume" in lines[0]
    assert "- ZZZ: R2 neutral rating" in section
    # The ranking sits AFTER the reports it orders.
    assert msg.index("## Technical Analysis Reports") < msg.index("## Candidate Ranking")


def test_the_prompt_says_nothing_is_ranked_when_nothing_is_eligible(monkeypatch):
    import src.agents.portfolio_manager as pm_module
    monkeypatch.setattr(pm_module, "et_today", lambda: SESSION)
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    msg = agent.build_user_message(
        analyses=[_tech("AAA", "buy", "high")], positions=[], macro_analysis=None,
        cash_balance=10_000, total_value=100_000, news_intel=None,
        earnings_analyses=[], smart_money_findings=[], allowed_buy_symbols=set(),
        active_state_changes="",
    )
    assert "(no name passes every pre-decision rule today)" in msg
    assert "- AAA: R3 not BUY-eligible" in msg
