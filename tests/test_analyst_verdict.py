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
    RATING_DIRECTION, RATING_MAGNITUDE, AnalystVerdict, TechAnalysisResult,
    TechReasoningChain, VerdictEvidence,
)
from src.risk.constants import REWARD_RISK_FLOOR
from src.verdicts import (
    CONVICTION_SCORE, RANKING_SIGNALS, SEAT_WEIGHT, rank_verdicts, score_verdict,
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
# 3. The ranking
# ==========================================================================

def test_ranking_reads_two_signals_at_equal_weight_and_seats_at_unit_weight():
    assert RANKING_SIGNALS == ("magnitude", "conviction_score")
    assert CONVICTION_SCORE == {"low": 0.0, "medium": 0.5, "high": 1.0}
    assert SEAT_WEIGHT == 1 and isinstance(SEAT_WEIGHT, int)
    assert not isinstance(SEAT_WEIGHT, dict)


def test_no_per_seat_weight_table_in_the_ranking_module():
    """Same guard `tests/test_signed_dissent.py` keeps over §9.4: a dict
    keyed by seat names holding numbers is a chosen weight, which §13.3
    rules out until a seat's own record justifies one."""
    seats = {"technical", "news", "earnings", "macro", "smart_money"}
    tree = ast.parse((REPO / "src" / "verdicts.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            assert not (keys & seats), f"seat-keyed dict at line {node.lineno}"


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


def test_two_seats_on_one_symbol_average_at_unit_weight():
    tech = _tech("XLE", "buy", "high").to_verdict()             # 0.5 + 1.0
    other = AnalystVerdict(
        seat="news", symbol="XLE", direction="bullish", magnitude=1.0,
        conviction="low", evidence=_evidence(), invalidation="x",  # 1.0 + 0.0
    )
    [c] = rank_verdicts([tech, other])
    assert c.components == {"magnitude": 0.75, "conviction_score": 0.5}
    assert c.score == 1.25
    assert c.seats == ["news", "technical"]


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
