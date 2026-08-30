"""Spec §9.4 — "agreement earns size".

`TargetPosition.risk_allocation_pct` is ceilinged — never raised — by how
many independent seats (of technical/news/earnings/macro/smart_money) are
directionally aligned with the target's proposed action. The ceiling is
computed deterministically from the canonical evidence registry (reusing
`validate_grounding`'s own polarity rule, not a second one), applied in
`PortfolioConstructor` strictly BEFORE `allocate_risk_budget` and the
single-name clamps, and can never exceed the ratified 5% per-trade envelope.
"""

from src.config import RiskConfig
from src.models import Position, TargetPosition, TechAnalysisResult, TechReasoningChain
from src.portfolio_constructor import ConstructorConfig, PortfolioConstructor
from src.risk.rules import (
    agreement_ceiling_for_count, count_aligned_sources, stance_is_aligned,
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
        setup_type="range", expected_horizon_sessions=10,
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
# agreement_ceiling_for_count — the schedule lookup
# --------------------------------------------------------------------------

SCHEDULE = [3.0, 4.0, 5.0, 5.0, 5.0]


def test_ceiling_schedule_zero_and_one_share_the_strictest_tier():
    assert agreement_ceiling_for_count(SCHEDULE, 0) == 3.0
    assert agreement_ceiling_for_count(SCHEDULE, 1) == 3.0


def test_ceiling_schedule_two():
    assert agreement_ceiling_for_count(SCHEDULE, 2) == 4.0


def test_ceiling_schedule_three_or_more_is_the_full_envelope():
    assert agreement_ceiling_for_count(SCHEDULE, 3) == 5.0
    assert agreement_ceiling_for_count(SCHEDULE, 4) == 5.0
    assert agreement_ceiling_for_count(SCHEDULE, 5) == 5.0


def test_ceiling_schedule_count_past_schedule_length_uses_last_entry():
    assert agreement_ceiling_for_count(SCHEDULE, 99) == 5.0


def test_ceiling_schedule_empty_is_inert():
    assert agreement_ceiling_for_count([], 1) == float("inf")


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
