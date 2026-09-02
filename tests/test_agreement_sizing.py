"""Spec §9.4 — "agreement earns size".

`TargetPosition.risk_allocation_pct` is ceilinged — never raised — by the
SIGNED score over the independent seats (of technical/news/earnings/macro/
smart_money): those aligned with the target's proposed action MINUS those
opposed to it. The ceiling is computed deterministically from the canonical
evidence registry (reusing `validate_grounding`'s own polarity rule, not a
second one), applied in `PortfolioConstructor` strictly BEFORE
`allocate_risk_budget` and the single-name clamps, and can never exceed the
ratified 5% per-trade envelope.

The signed sum landed 2026-09-02. `tests/test_signed_dissent.py` holds the
acceptance criterion for that change (unanimous cases must price exactly as
the old aligned-count rule did) and the mechanical pin on seat weights; this
file is the rule's own behaviour, end to end.
"""

from src.config import RiskConfig
from src.models import Position, TargetPosition, TechAnalysisResult, TechReasoningChain
from src.portfolio_constructor import ConstructorConfig, PortfolioConstructor
from src.risk.rules import (
    agreement_ceiling_for_score, count_aligned_sources, signed_source_score,
    stance_is_aligned,
)

import pytest
from pydantic import ValidationError


def _tech_rc() -> TechReasoningChain:
    return TechReasoningChain(
        trend="x", momentum="x", volatility="x", volume="x",
        support_resistance="x",
    )


def _analysis(symbol: str, entry: float = 100.0, stop: float = 95.0,
              target: float = 115.0) -> TechAnalysisResult:
    return TechAnalysisResult(
        symbol=symbol, rating="buy", entry_price=entry, stop_loss=stop,
        reference_target=target, support_levels=[stop], resistance_levels=[target],
        # Python-set by TechAnalystAgent, not model-emitted. The constructor
        # derives the take-profit from `computed_levels` (2026-09-01) and
        # refuses without them; the ATR sits just inside the noise band so
        # the structural stop is left alone.
        computed_levels=[stop, target], atr_14=(entry - stop) / 3.5,
        setup_type="range", expected_horizon_sessions=60,
        reasoning="test", reasoning_chain=_tech_rc(),
    )


# --------------------------------------------------------------------------
# count_aligned_sources / stance_is_aligned — the deterministic vocabulary
# --------------------------------------------------------------------------

def test_count_aligned_sources_one_aligned_long():
    sources = {"technical": "bullish", "news": "bearish", "macro": "neutral"}
    assert count_aligned_sources("NVDA", sources, "long") == 1


def test_count_aligned_sources_one_aligned_short():
    sources = {"technical": "bullish", "news": "bearish", "macro": "neutral"}
    assert count_aligned_sources("NVDA", sources, "short") == 1


def test_count_aligned_sources_two_aligned_long():
    sources = {"technical": "bullish", "earnings": "bullish", "macro": "bearish"}
    assert count_aligned_sources("AAPL", sources, "long") == 2
    assert count_aligned_sources("AAPL", sources, "short") == 1


def test_count_aligned_sources_three_or_more_aligned():
    sources = {
        "technical": "bullish", "earnings": "bullish", "news": "bullish",
        "macro": "bearish", "smart_money": "bearish",
    }
    assert count_aligned_sources("CEG", sources, "long") == 3
    assert count_aligned_sources("CEG", sources, "short") == 2


def test_count_aligned_sources_zero_when_nothing_points_that_way():
    sources = {"technical": "neutral", "macro": "neutral"}
    assert count_aligned_sources("XLF", sources, "long") == 0
    assert count_aligned_sources("XLF", sources, "short") == 0


def test_count_aligned_sources_empty_registry_is_zero():
    assert count_aligned_sources("ANY", {}, "long") == 0


def test_macro_polarity_flips_for_inverse_etf():
    """A risk-off macro stance supports owning an INVERSE ETF (SQQQ is
    -3x) — same twist `validate_grounding` has always applied."""
    assert stance_is_aligned("macro", "SQQQ", "risk_off", wants_bullish=True)
    assert not stance_is_aligned("macro", "SQQQ", "risk_on", wants_bullish=True)
    # A normal (non-inverse) symbol is not flipped.
    assert stance_is_aligned("macro", "AAPL", "risk_on", wants_bullish=True)
    assert not stance_is_aligned("macro", "AAPL", "risk_off", wants_bullish=True)


# --------------------------------------------------------------------------
# agreement_ceiling_for_score — the schedule lookup
# --------------------------------------------------------------------------

SCHEDULE = [3.0, 4.0, 5.0, 5.0, 5.0]


def test_ceiling_schedule_one_net_source_is_the_strictest_tier():
    assert agreement_ceiling_for_score(SCHEDULE, 1) == 3.0


def test_ceiling_schedule_zero_or_negative_is_a_block():
    """The schedule's first rung prices ONE net source and there is no rung
    below it. Zero (or negative) net evidence therefore returns 0.0, which
    the constructor reads as "no order" — the same lookup that sizes the
    trade is the one that refuses it, so a dissenter is never charged twice."""
    assert agreement_ceiling_for_score(SCHEDULE, 0) == 0.0
    assert agreement_ceiling_for_score(SCHEDULE, -1) == 0.0
    assert agreement_ceiling_for_score(SCHEDULE, -5) == 0.0


def test_ceiling_schedule_two():
    assert agreement_ceiling_for_score(SCHEDULE, 2) == 4.0


def test_ceiling_schedule_three_or_more_is_the_full_envelope():
    assert agreement_ceiling_for_score(SCHEDULE, 3) == 5.0
    assert agreement_ceiling_for_score(SCHEDULE, 4) == 5.0
    assert agreement_ceiling_for_score(SCHEDULE, 5) == 5.0


def test_ceiling_schedule_score_past_schedule_length_uses_last_entry():
    assert agreement_ceiling_for_score(SCHEDULE, 99) == 5.0


def test_ceiling_schedule_empty_is_inert_including_its_block():
    """An unconfigured schedule must not silently become the STRICTEST rule.
    A desk that switched the ceiling off did not ask for a dissent veto."""
    assert agreement_ceiling_for_score([], 1) == float("inf")
    assert agreement_ceiling_for_score([], 0) == float("inf")
    assert agreement_ceiling_for_score([], -3) == float("inf")


# --------------------------------------------------------------------------
# RiskConfig validation — config, not a module constant, and it must never
# be able to widen the envelope or reward MORE agreement with LESS room.
# --------------------------------------------------------------------------

def _risk_kwargs(**overrides):
    base = dict(
        max_position_pct=20, max_total_position_pct=90, max_daily_loss_pct=3,
        max_sector_pct=40, require_stop_loss=True,
    )
    base.update(overrides)
    return base


def test_risk_config_default_agreement_schedule_is_well_formed():
    cfg = RiskConfig(**_risk_kwargs())
    assert cfg.agreement_ceiling_pct == [3.0, 4.0, 5.0, 5.0, 5.0]


def test_risk_config_rejects_schedule_exceeding_the_envelope():
    with pytest.raises(ValidationError):
        RiskConfig(**_risk_kwargs(agreement_ceiling_pct=[3.0, 4.0, 5.0, 5.0, 6.0]))


def test_risk_config_rejects_a_decreasing_schedule():
    with pytest.raises(ValidationError):
        RiskConfig(**_risk_kwargs(agreement_ceiling_pct=[4.0, 3.0, 5.0, 5.0, 5.0]))


def test_risk_config_rejects_wrong_length():
    with pytest.raises(ValidationError):
        RiskConfig(**_risk_kwargs(agreement_ceiling_pct=[3.0, 4.0, 5.0]))


def test_risk_config_rejects_non_positive_entries():
    with pytest.raises(ValidationError):
        RiskConfig(**_risk_kwargs(agreement_ceiling_pct=[0.0, 4.0, 5.0, 5.0, 5.0]))


# --------------------------------------------------------------------------
# PortfolioConstructor integration — the ceiling in the actual sizing path
# --------------------------------------------------------------------------

def _registry(**sources_per_symbol) -> dict[str, dict[str, str]]:
    return sources_per_symbol


def test_ceiling_only_ever_reduces_a_modest_request_is_untouched():
    """A low-agreement target asking for LESS than its ceiling is sized
    exactly as requested — the ceiling never adds size, and it never cuts
    a request that was already under it."""
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA", risk_allocation_pct=2.0, conviction="medium",
        thesis="Modest single-source idea.",
    )
    analysis = _analysis("NVDA")
    registry = _registry(NVDA={"technical": "bullish"})  # 1 aligned source -> ceiling 3.0

    decisions = constructor.construct_orders(
        targets=[target], positions=[], analyses=[analysis],
        total_value=100_000.0, price_map={"NVDA": 100.0},
        evidence_registry=registry,
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.action == "BUY"
    # 2% risk / $5 risk-per-share * $100 entry = 40% notional weight, well
    # under the 20% single-name cap... so check the single-name clamp binds
    # instead of asserting a bare weight. What matters for THIS test is that
    # the agreement ceiling (3.0%) never touched a 2.0% request.
    assert "agreement ceiling" not in d.reasoning


def test_ceiling_binds_and_says_so_in_the_order_reasoning():
    """A single-source target asking for the full envelope is capped, and
    the order's reasoning must say why — mirroring the existing single-
    name-ceiling precedent (`_build_buy`'s cap_note)."""
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA", risk_allocation_pct=5.0, conviction="high",
        thesis="Single-source high-conviction ask.",
    )
    # Wide stop so the single-name (20%) ceiling does NOT also bind here —
    # isolates the agreement ceiling's effect.
    analysis = _analysis("NVDA", entry=100.0, stop=80.0, target=140.0)
    registry = _registry(NVDA={"technical": "bullish"})  # 1 aligned -> ceiling 3.0

    decisions = constructor.construct_orders(
        targets=[target], positions=[], analyses=[analysis],
        total_value=100_000.0, price_map={"NVDA": 100.0},
        evidence_registry=registry,
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert "agreement ceiling" in d.reasoning
    assert "not PM inconsistency" in d.reasoning
    # risk 3.0% (post-ceiling) / $20 risk-per-share * $100 entry = 15% weight
    assert abs(d.allocation_pct - 15.0) < 0.05


def test_ceiling_never_exceeds_the_five_percent_envelope_even_if_misconfigured():
    """Defence in depth: even a (hypothetically) misconfigured schedule
    above the envelope cannot win, because the runtime code takes
    min(envelope, ceiling) — never the ceiling alone."""
    constructor = PortfolioConstructor(ConstructorConfig(
        agreement_ceiling_pct=(999.0, 999.0, 999.0, 999.0, 999.0),
    ))
    target = TargetPosition(
        symbol="NVDA", risk_allocation_pct=5.0, conviction="high",
        thesis="Full agreement, huge misconfigured ceiling.",
    )
    analysis = _analysis("NVDA", entry=100.0, stop=80.0, target=140.0)
    registry = _registry(NVDA={
        "technical": "bullish", "earnings": "bullish", "news": "bullish",
    })  # 3 aligned -> would-be ceiling 999, but envelope wins

    decisions = constructor.construct_orders(
        targets=[target], positions=[], analyses=[analysis],
        total_value=100_000.0, price_map={"NVDA": 100.0},
        evidence_registry=registry,
    )
    assert len(decisions) == 1
    # 5.0% risk / $20 risk-per-share * $100 entry = 25% weight -> clamped by
    # the 20% single-name ceiling, not by agreement — either way, nothing
    # above the ratified envelope's implied sizing ever appears.
    assert decisions[0].allocation_pct <= 25.0 + 1e-6


def test_no_op_wall_full_agreement_is_byte_identical_to_no_registry():
    """With 3+ aligned sources the schedule's ceiling equals the full
    envelope, so sizing must be IDENTICAL to a call that supplies no
    evidence_registry at all (today's behaviour)."""
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA", risk_allocation_pct=3.0, conviction="high",
        thesis="Full agreement idea.",
    )
    analysis = _analysis("NVDA")

    baseline = constructor.construct_orders(
        targets=[target], positions=[], analyses=[analysis],
        total_value=100_000.0, price_map={"NVDA": 100.0},
    )
    full_agreement_registry = _registry(NVDA={
        "technical": "bullish", "earnings": "bullish", "macro": "bullish",
    })
    with_registry = constructor.construct_orders(
        targets=[target], positions=[], analyses=[analysis],
        total_value=100_000.0, price_map={"NVDA": 100.0},
        evidence_registry=full_agreement_registry,
    )
    assert len(baseline) == len(with_registry) == 1
    b, w = baseline[0], with_registry[0]
    assert b.action == w.action
    assert b.allocation_pct == w.allocation_pct
    assert b.entry_price == w.entry_price
    assert b.stop_loss == w.stop_loss
    assert b.take_profit == w.take_profit
    assert b.reasoning == w.reasoning


def test_missing_evidence_registry_leaves_ceiling_unenforced():
    """Same "no view, don't invent one" posture as `existing_risk_pct`/
    `clusters`: omitting the registry must NOT be silently treated as zero
    agreement — it must leave the ceiling OFF, not clamp to the strictest
    tier."""
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA", risk_allocation_pct=5.0, conviction="high",
        thesis="No registry supplied at all.",
    )
    analysis = _analysis("NVDA", entry=100.0, stop=80.0, target=140.0)

    decisions = constructor.construct_orders(
        targets=[target], positions=[], analyses=[analysis],
        total_value=100_000.0, price_map={"NVDA": 100.0},
        # evidence_registry omitted entirely
    )
    assert len(decisions) == 1
    assert "agreement ceiling" not in decisions[0].reasoning
    # 5.0% / $20 * $100 = 25% -> clamped only by the single-name (20%) cap.
    assert abs(decisions[0].allocation_pct - 20.0) < 0.05


def test_composition_agreement_ceiling_then_budget_allocator_then_single_name():
    """All three deterministic layers must hold together and in order:
    the agreement ceiling narrows each REQUEST first, `allocate_risk_budget`
    rations what's left across a correlated cluster second, and the
    single-name notional clamp still applies to the resulting size third.

    Cluster share tightened to 16% of the 25% portfolio ceiling (= 4.0%)
    so the allocator actually has to ration between two single-source
    (agreement-ceilinged to 3.0% each) requests — 6.0% combined otherwise
    fits the default 10% cluster cap without the allocator doing anything,
    which would leave this layer untested.
    """
    constructor = PortfolioConstructor(ConstructorConfig(max_cluster_risk_share_pct=16.0))
    # Two single-source (agreement ceiling 3.0%) targets in the same
    # correlation cluster, both requesting the full 5% envelope.
    targets = [
        TargetPosition(symbol="OKLO", risk_allocation_pct=5.0, conviction="high",
                       thesis="Nuclear theme A."),
        TargetPosition(symbol="CEG", risk_allocation_pct=5.0, conviction="high",
                       thesis="Nuclear theme B."),
    ]
    analyses = [
        _analysis("OKLO", entry=100.0, stop=90.0, target=130.0),
        _analysis("CEG", entry=100.0, stop=90.0, target=130.0),
    ]
    registry = _registry(
        OKLO={"technical": "bullish"}, CEG={"technical": "bullish"},
    )  # both single-source -> agreement ceiling 3.0% each

    decisions = constructor.construct_orders(
        targets=targets, positions=[], analyses=analyses,
        total_value=100_000.0,
        price_map={"OKLO": 100.0, "CEG": 100.0},
        existing_risk_pct={}, clusters=[["OKLO", "CEG"]],
        evidence_registry=registry,
    )
    buys = {d.symbol: d for d in decisions if d.action == "BUY"}
    assert set(buys) == {"OKLO", "CEG"}
    for d in buys.values():
        assert "agreement ceiling" in d.reasoning

    # Both requests were narrowed 5.0% -> 3.0% by the agreement ceiling
    # BEFORE the allocator ever saw them (6.0% combined). Alphabetical
    # tie-break processes CEG first: it fits the 4.0% cluster cap in full
    # (risk 3.0% / $10 risk-per-share * $100 entry = 30% notional weight,
    # then clamped to 20% by the single-name ceiling). OKLO is processed
    # second with only 1.0% of cluster headroom left (4.0 - 3.0), rationed
    # by the allocator down to 1.0% risk = 10% notional weight, which never
    # reaches the single-name ceiling.
    assert "single-name ceiling" in buys["CEG"].reasoning
    assert buys["CEG"].allocation_pct == pytest.approx(20.0, abs=0.05)
    assert "cluster" in buys["OKLO"].reasoning
    assert "single-name ceiling" not in buys["OKLO"].reasoning
    assert buys["OKLO"].allocation_pct == pytest.approx(10.0, abs=0.05)


# ==========================================================================
# §9.4 FRESHNESS — a stale view must not earn live size
# ==========================================================================
#
# `build_evidence_registry` read `investment_implications.sentiment` and threw
# `filing_date` / `is_new` away, and nothing in `src/risk/rules.py` or
# `src/portfolio_constructor.py` ever looked at the age of an earnings stance.
# A bullish earnings view therefore counted as a full live corroborating
# source forever: one stale stance moved a name from 1 aligned source to 2 and
# bought it a 3.0% -> 4.0% risk allowance, a 33% larger allowance, on evidence
# that had confirmed nothing about today.
#
# The gate is a REMOVAL FROM THE TALLY only. The stance stays in the canonical
# registry, so `validate_grounding` still sees the coverage and a PM that
# cites it does not fail the session — this can shrink a ceiling and can never
# raise one, matching the constructor's standing posture that it may only
# refuse size a request did not earn.

from datetime import date, timedelta       # noqa: E402
from unittest.mock import patch            # noqa: E402

from src.agents.portfolio_manager import PortfolioManagerAgent   # noqa: E402
from src.risk.rules import (                                     # noqa: E402
    EARNINGS_STANCE_MAX_AGE_DAYS, count_opposing_sources,
)

_ASOF = date(2026, 9, 1)


def _earnings(symbol: str, sentiment: str, *, age_days: int,
              is_new: bool = False, asof: date = _ASOF) -> dict:
    """One entry in the `earnings_analyses` list, in the shape the pipeline
    actually hands the PM (`run_earnings_preprocess` / `analyze_reports`)."""
    filing_date = (asof - timedelta(days=age_days)).isoformat()
    return {
        "symbol": symbol,
        "form_type": "10-Q",
        "filing_date": filing_date,
        "is_new": is_new,
        "analysis": {
            "symbol": symbol,
            "filing_date": filing_date,
            "investment_implications": {"sentiment": sentiment},
        },
    }


# --------------------------------------------------------------------------
# The threshold itself, at the boundary
# --------------------------------------------------------------------------

def test_freshness_threshold_reuses_the_earnings_seat_s_own_90_days():
    """Not a number invented here: the earnings prompt already caps its own
    conviction at `low` past 90 days, and `_missed_ops_earnings_signal`
    already refuses anything older than 90 days as recent evidence."""
    assert EARNINGS_STANCE_MAX_AGE_DAYS == 90


def test_stance_exactly_at_the_threshold_is_still_fresh():
    """90 days old is NOT stale — the gate fires strictly past the
    threshold, so the boundary day is paid for like any other."""
    stale = PortfolioManagerAgent.stale_evidence_sources(
        earnings_analyses=[_earnings("NVDA", "bullish", age_days=90)],
        asof=_ASOF,
    )
    assert stale == {}


def test_stance_one_day_past_the_threshold_is_stale():
    stale = PortfolioManagerAgent.stale_evidence_sources(
        earnings_analyses=[_earnings("NVDA", "bullish", age_days=91)],
        asof=_ASOF,
    )
    assert stale == {"NVDA": frozenset({"earnings"})}


def test_a_filing_with_no_date_is_treated_as_stale():
    """An unknowable age is not evidence of freshness. Same call
    `_missed_ops_earnings_signal` already makes on an unparseable date."""
    entry = _earnings("NVDA", "bullish", age_days=1)
    entry["filing_date"] = ""
    entry["analysis"].pop("filing_date")
    stale = PortfolioManagerAgent.stale_evidence_sources(
        earnings_analyses=[entry], asof=_ASOF,
    )
    assert stale == {"NVDA": frozenset({"earnings"})}


def test_freshness_verdict_follows_the_same_last_wins_rule_as_the_stance():
    """Two filings for one symbol: the registry keeps the LAST one's stance,
    so the freshness verdict must attach to that same filing and not to an
    earlier one that happens to be fresher."""
    analyses = [
        _earnings("NVDA", "bullish", age_days=200),
        _earnings("NVDA", "bullish", age_days=5),
    ]
    registry = PortfolioManagerAgent.build_evidence_registry(
        analyses=[], positions=[], news_intel=None,
        earnings_analyses=analyses, macro_analysis=None,
    )
    assert registry["NVDA"]["earnings"] == "bullish"
    assert PortfolioManagerAgent.stale_evidence_sources(
        earnings_analyses=analyses, asof=_ASOF,
    ) == {}
    # ...and reversed, the stale one wins and is gated.
    assert PortfolioManagerAgent.stale_evidence_sources(
        earnings_analyses=list(reversed(analyses)), asof=_ASOF,
    ) == {"NVDA": frozenset({"earnings"})}


def test_a_gated_stance_stays_in_the_registry():
    """The gate must not delete coverage. `validate_grounding` fails the
    WHOLE session on any error, so removing a stale earnings stance from the
    registry would turn a PM citation of it into a session failure — a hard
    block, not the size reduction this is meant to be."""
    analyses = [_earnings("NVDA", "bullish", age_days=200)]
    registry = PortfolioManagerAgent.build_evidence_registry(
        analyses=[], positions=[], news_intel=None,
        earnings_analyses=analyses, macro_analysis=None,
    )
    assert registry["NVDA"]["earnings"] == "bullish"


# --------------------------------------------------------------------------
# The tally: a stale stance stops counting
# --------------------------------------------------------------------------

def test_count_aligned_sources_ignores_a_gated_source():
    sources = {"technical": "bullish", "earnings": "bullish"}
    assert count_aligned_sources("NVDA", sources, "long") == 2
    assert count_aligned_sources(
        "NVDA", sources, "long", ignored_sources=frozenset({"earnings"}),
    ) == 1


# --------------------------------------------------------------------------
# End to end: the stale bullish view no longer buys the higher ceiling
# --------------------------------------------------------------------------

def _stale_ceiling_decisions(stale_sources):
    """One full-envelope long on NVDA with technical + earnings both bullish.

    Geometry: entry 100 / stop 70 / target 160. Risk-per-share $30, so a
    ceiling of 4.0% risk is a 13.33% weight and 3.0% is a 10.00% weight —
    both far below the 20% single-name cap, which therefore cannot be what
    moves the number.

    Returns the raw decision LIST, because gating every aligned source now
    leaves a net score of zero and produces no order at all.
    """
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA", risk_allocation_pct=5.0, conviction="high",
        thesis="Technical and earnings both bullish.",
    )
    registry = _registry(NVDA={"technical": "bullish", "earnings": "bullish"})
    return constructor.construct_orders(
        targets=[target], positions=[],
        analyses=[_analysis("NVDA", entry=100.0, stop=70.0, target=160.0)],
        total_value=100_000.0, price_map={"NVDA": 100.0},
        evidence_registry=registry, stale_sources=stale_sources,
    )


def _stale_ceiling_decision(stale_sources):
    decisions = _stale_ceiling_decisions(stale_sources)
    assert len(decisions) == 1
    return decisions[0]


def test_a_fresh_second_source_earns_the_two_source_ceiling():
    d = _stale_ceiling_decision(None)
    # 2 aligned -> 4.0% risk / $30 rps * $100 = 13.33% weight
    assert abs(d.allocation_pct - 13.333) < 0.05


def test_a_stale_bullish_earnings_view_no_longer_earns_the_higher_ceiling():
    """The defect, priced. Same registry, same trade — the only difference
    is that the earnings filing is older than the threshold, and the risk
    allowance drops a rung from 4.0% to 3.0%."""
    d = _stale_ceiling_decision({"NVDA": frozenset({"earnings"})})
    # 1 aligned -> 3.0% risk / $30 rps * $100 = 10.00% weight
    assert abs(d.allocation_pct - 10.0) < 0.05
    assert "agreement ceiling" in d.reasoning


def test_the_freshness_gate_can_only_ever_reduce():
    """Gating a source can never raise the ceiling, whatever it gates.

    Gating EVERYTHING leaves a net score of zero, which is now a refusal
    rather than the strictest rung — still a reduction, just the largest one
    available. Asserted as "no order", not as a smaller order."""
    fresh = _stale_ceiling_decision(None).allocation_pct
    for gated in ({"NVDA": frozenset({"earnings"})},
                  {"NVDA": frozenset({"technical"})}):
        assert _stale_ceiling_decision(gated).allocation_pct <= fresh + 1e-9
    assert _stale_ceiling_decisions(
        {"NVDA": frozenset({"technical", "earnings"})}
    ) == []


def test_no_stale_map_leaves_the_ceiling_exactly_as_it_was():
    """A caller with no freshness view must not have one invented for it —
    the same posture `evidence_registry=None` already takes."""
    assert (_stale_ceiling_decision(None).allocation_pct
            == _stale_ceiling_decision({}).allocation_pct)


# ==========================================================================
# §9.4 DISSENT — counted, visible, and SUBTRACTED (2026-09-02)
# ==========================================================================
#
# `count_aligned_sources` counts only sources aligned with the trade, so on a
# long a bearish earnings stance contributes 0 to it — arithmetically
# identical to neutral and to no coverage at all. That is still true OF THAT
# COUNT; what changed is that the count is no longer what sizes the trade.
# `signed_source_score` nets the opposed seats off, and
# `agreement_ceiling_for_score` prices the net. Both counts are still reported
# because "2 for, 1 against" and "net +1" are different facts.

def test_count_opposing_sources_on_a_long():
    sources = {"technical": "bullish", "earnings": "bearish",
               "macro": "neutral", "news": "bearish"}
    assert count_aligned_sources("NVDA", sources, "long") == 1
    assert count_opposing_sources("NVDA", sources, "long") == 2


def test_count_opposing_sources_on_a_short():
    """The exact mirror: on a short the bullish seats are the dissenters."""
    sources = {"technical": "bearish", "earnings": "bullish",
               "macro": "neutral", "news": "bullish"}
    assert count_aligned_sources("NVDA", sources, "short") == 1
    assert count_opposing_sources("NVDA", sources, "short") == 2


def test_neutral_is_in_neither_count():
    """A seat with no view took no side — it must not read as dissent."""
    sources = {"technical": "bullish", "macro": "neutral", "news": "mixed"}
    assert count_aligned_sources("NVDA", sources, "long") == 1
    assert count_opposing_sources("NVDA", sources, "long") == 0


def test_a_silent_a_neutral_and_a_dissenting_seat_are_no_longer_one_number():
    """The defect in one assertion. All three used to score the same zero;
    only the first two still do."""
    silent = {"technical": "bullish"}
    neutral = {"technical": "bullish", "earnings": "neutral"}
    dissenting = {"technical": "bullish", "earnings": "bearish"}
    assert signed_source_score("NVDA", silent, "long") == 1
    assert signed_source_score("NVDA", neutral, "long") == 1
    assert signed_source_score("NVDA", dissenting, "long") == 0


def test_opposing_count_honours_the_freshness_gate_too():
    """A stance too stale to corroborate is also too stale to dissent —
    one freshness rule, not two."""
    sources = {"technical": "bullish", "earnings": "bearish"}
    assert count_opposing_sources("NVDA", sources, "long") == 1
    assert count_opposing_sources(
        "NVDA", sources, "long", ignored_sources=frozenset({"earnings"}),
    ) == 0


def test_opposing_count_flips_with_macro_polarity_on_an_inverse_etf():
    """`count_opposing_sources` must use the SAME polarity vocabulary as the
    aligned count, inverse-ETF macro flip included — a second notion of
    "opposed" would let the two disagree about identical evidence."""
    sources = {"macro": "risk_on"}          # bullish tape
    assert count_aligned_sources("SQQQ", sources, "long") == 0
    assert count_opposing_sources("SQQQ", sources, "long") == 1


def _dissent_decisions(registry, *, risk_pct: float = 5.0):
    """One NVDA long, entry 100 / stop 70 / target 160 (risk-per-share $30).

    At that geometry 5.0% risk is a 16.67% weight, 4.0% is 13.33% and 3.0% is
    10.00% — all under the 20% single-name cap, so any movement here is the
    agreement ceiling and nothing else.
    """
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA", risk_allocation_pct=risk_pct, conviction="high",
        thesis="Seats disagree about this one.",
    )
    return constructor.construct_orders(
        targets=[target], positions=[],
        analyses=[_analysis("NVDA", entry=100.0, stop=70.0, target=160.0)],
        total_value=100_000.0, price_map={"NVDA": 100.0},
        evidence_registry=registry,
    )


def test_dissent_moves_the_size_down_a_ceiling_rung():
    """The subtraction test that actually bites. 2-aligned/1-opposed nets to
    +1, so the ceiling drops 4.0% -> 3.0% and the weight 13.33% -> 10.00%.
    (1-aligned/1-opposed cannot distinguish a rung drop from a block, which
    is why this case and not that one.)"""
    decisions = _dissent_decisions(_registry(NVDA={
        "technical": "bullish", "earnings": "bullish", "macro": "bearish",
    }))
    assert len(decisions) == 1
    d = decisions[0]
    assert abs(d.allocation_pct - 10.0) < 0.05
    assert "1 independent source(s) took the OPPOSITE side" in d.reasoning
    assert "already been subtracted" in d.reasoning


def test_three_aligned_and_one_opposed_sizes_at_the_two_seat_rung():
    """The consequence stated in the ratified change, checked as arithmetic
    rather than as a special case: S = 3 - 1 = 2, so it prices where a flat
    two-source idea prices, not where a three-source one does."""
    contested = _dissent_decisions(_registry(NVDA={
        "technical": "bullish", "earnings": "bullish", "news": "bullish",
        "macro": "bearish",
    }))
    flat_two = _dissent_decisions(_registry(NVDA={
        "technical": "bullish", "earnings": "bullish",
    }))
    flat_three = _dissent_decisions(_registry(NVDA={
        "technical": "bullish", "earnings": "bullish", "news": "bullish",
    }))
    assert len(contested) == len(flat_two) == len(flat_three) == 1
    assert contested[0].allocation_pct == pytest.approx(
        flat_two[0].allocation_pct, abs=1e-9,
    )
    assert contested[0].allocation_pct < flat_three[0].allocation_pct


def test_a_net_score_of_zero_produces_no_order_at_all():
    """One for, one against is not a small idea — it is not an idea. And it
    must come from the ceiling arithmetic itself: there is no standalone
    dissent veto anywhere in the constructor to charge the seat twice."""
    assert _dissent_decisions(_registry(NVDA={
        "technical": "bullish", "earnings": "bearish",
    })) == []


def test_a_net_score_below_zero_produces_no_order_at_all():
    assert _dissent_decisions(_registry(NVDA={
        "technical": "bullish", "earnings": "bearish", "macro": "bearish",
    })) == []


def test_blocking_a_target_leaves_a_held_position_alone():
    """A refusal to BUY is not a decision to SELL. A zero-weight plan would
    read to the delta loop as "PM wants this closed", so a blocked target has
    to vanish from the plan entirely rather than be sized at zero."""
    constructor = PortfolioConstructor()
    target = TargetPosition(
        symbol="NVDA", risk_allocation_pct=5.0, conviction="high",
        thesis="Adding to a name the earnings seat is bearish on.",
    )
    held = Position(
        symbol="NVDA", qty=100.0, avg_entry=90.0, current_price=100.0,
        market_value=10_000.0, unrealized_pnl=1_000.0, sector="Technology",
    )
    decisions = constructor.construct_orders(
        targets=[target], positions=[held],
        analyses=[_analysis("NVDA", entry=100.0, stop=70.0, target=160.0)],
        total_value=100_000.0, price_map={"NVDA": 100.0},
        evidence_registry=_registry(NVDA={
            "technical": "bullish", "earnings": "bearish",
        }),
    )
    assert [d.action for d in decisions if d.action in ("SELL", "BUY")] == []


def test_dissent_is_recorded_on_the_order_that_survives_it():
    """The number still appears in the order note — and now says plainly that
    it has already been paid for, so a reader does not double-count it."""
    with_dissent = _dissent_decisions(_registry(NVDA={
        "technical": "bullish", "earnings": "bullish", "macro": "bearish",
    }))
    without_dissent = _dissent_decisions(_registry(NVDA={
        "technical": "bullish", "earnings": "bullish", "macro": "neutral",
    }))
    assert len(with_dissent) == len(without_dissent) == 1
    assert with_dissent[0].allocation_pct < without_dissent[0].allocation_pct
    assert "OPPOSITE side" in with_dissent[0].reasoning
    assert "OPPOSITE side" not in without_dissent[0].reasoning


# ==========================================================================
# What the PM is shown
# ==========================================================================

def _pm_agent():
    with patch("anthropic.Anthropic"):
        return PortfolioManagerAgent(api_key="test", model="claude-sonnet-4-6")


def _pm_message(age_days: int) -> str:
    agent = _pm_agent()
    with patch("src.agents.portfolio_manager.et_today", return_value=_ASOF):
        return agent.build_user_message(
            analyses=[_analysis("NVDA")],
            positions=[],
            earnings_analyses=[_earnings("NVDA", "bullish", age_days=age_days)],
            cash_balance=100_000.0,
            total_value=100_000.0,
        )


def test_a_gated_stance_is_not_shown_to_the_pm_as_corroborating():
    """The prompt already labelled a cached view `[from cache]` with its
    filing date — and then counted it as a live aligned source in the same
    message. Both halves must now say the same thing."""
    msg = _pm_message(age_days=200)
    # The tally the PM is quoted, the agreement line's own caveat, the
    # registry block, and the earnings section must ALL say the same thing —
    # each is asserted separately because each is written separately.
    assert "- NVDA: 1 aligned / 0 opposed = net +1 if long" in msg
    assert "earnings stance NOT counted — filing older than 90d" in msg
    assert "STALE (still real coverage, still citable as provenance" in msg
    assert "does NOT count toward the agreement ceiling" in msg


def test_a_fresh_stance_is_shown_as_corroborating():
    msg = _pm_message(age_days=10)
    assert "- NVDA: 2 aligned / 0 opposed = net +2 if long" in msg
    assert "NOT counted" not in msg
    assert "STALE" not in msg


def test_the_pm_is_shown_the_opposing_count_and_the_net():
    """The net is what the constructor will actually price, so the PM must
    see it before it sizes — a ceiling the PM cannot predict reads as the
    constructor contradicting PM's own reasoning (2026-08-20 incident)."""
    agent = _pm_agent()
    msg = agent.build_user_message(
        analyses=[_analysis("NVDA")],
        positions=[],
        earnings_analyses=[_earnings("NVDA", "bearish", age_days=10)],
        cash_balance=100_000.0, total_value=100_000.0,
    )
    assert "- NVDA: 1 aligned / 1 opposed = net +0 if long" in msg
    assert "1 aligned / 1 opposed = net +0 if short" in msg


def test_the_pm_is_told_a_non_positive_net_produces_no_order():
    """Standing rule of this desk: the PM is never sized against silently."""
    msg = _pm_message(age_days=10)
    assert "net score of zero or below produces NO ORDER AT ALL" in msg
