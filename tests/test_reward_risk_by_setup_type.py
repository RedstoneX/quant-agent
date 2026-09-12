"""The reward:risk gate, by setup type — docs/WORK.md item 1 part (d).

Owner decision, 2026-09-11. The desk applied ONE fixed minimum reward:risk
(1.5) to every trade it considered, identically, whatever kind of trade it
was. That was the single largest measured cause of trades not happening
(funnel item 1: 17 of 68 blocked proposals). Two different things were wrong
with it, and they need two different fixes because this desk already
distinguishes two kinds of trade (`src/risk/trailing.py`):

**Type B / trend (breakout).** There is no overhead level anyone is
defending, and the desk manages the position accordingly — trailing the stop
from entry, with no fixed profit target at all. Any "reward" put in the
numerator of a ratio for such a trade is a number invented to make the ratio
computable, disconnected from how the trade is actually exited. So there is
NO reward:risk test of any kind: not at PM eligibility, not at the PM's
sub-floor gate, not in the constructor, not at the execution belt, not in the
Risk Manager's prompt. Approval rests on the risk side — a real stop — plus
the conviction and evidence checks that run regardless of setup type.

**Type A / range.** The reward:risk is REAL: measured from this specific
trade's own support (where the stop sits) and its own resistance (where
`_derive_target` says price travels). It is still computed. What stopped is
comparing it to one fixed number applied to every range trade the same way —
that is the arbitrary-universal-threshold pattern `docs/OUTCOME.md` already
rejects. The real ratio now feeds the already-ratified weighted ranking
(`src/verdicts.py::rank_verdicts`, spec §13.3) as an ordering signal.

**What "trend" means is ONE definition**, `src.risk.constants.is_trend_trade`,
and it is deliberately shared with `derive_structural_target`'s measured-move
projection (funnel item 6) so the two cannot drift: either the analyst
labelled it `breakout`, or the desk's own level computation found no level in
the trade's direction on a chart that yielded levels elsewhere.

**No new fixed number is introduced anywhere by any of this.**
"""

from __future__ import annotations

import pytest

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.models import (
    PortfolioDecision, TargetPosition, TechAnalysisResult, TechReasoningChain,
    TradeDecision,
)
from src.pipeline_stages import EXECUTION_REWARD_RISK_BELT, _execution_rr_floor
from src.portfolio_constructor import PortfolioConstructor
from src.risk.constants import REWARD_RISK_FLOOR, STARTER_POSITION_RISK_PCT

EQUITY = 100_000.0
ENTRY = 100.0
STOP = 95.0                     # a real, computed support level
ATR = (ENTRY - STOP) / 3.5      # keeps the stop outside the noise band
THIN_LEVEL = 102.0              # reward $2 against risk $5 -> R/R 0.40
FAT_LEVEL = 115.0               # reward $15 against risk $5 -> R/R 3.00


def _rc() -> TechReasoningChain:
    return TechReasoningChain(
        trend="x", momentum="x", volatility="x", volume="x",
        support_resistance="x",
    )


def _analysis(
    symbol: str, *, setup_type: str, upper_level: float = THIN_LEVEL,
) -> TechAnalysisResult:
    """One long candidate. Every price here is a level the system computed,
    so nothing is ever refused for missing structure — the only thing that
    varies between the fixtures below is the setup type and how far the
    ceiling is."""
    return TechAnalysisResult(
        symbol=symbol, rating="buy", conviction="medium", entry_price=ENTRY,
        stop_loss=STOP, reference_target=upper_level,
        support_levels=[STOP], resistance_levels=[upper_level],
        computed_levels=[STOP, upper_level], atr_14=ATR,
        setup_type=setup_type, expected_horizon_sessions=60,
        reasoning="validated production-like trend and momentum evidence",
        reasoning_chain=_rc(),
    )


def _target(symbol: str, *, risk: float = 3.0) -> TargetPosition:
    return TargetPosition(
        symbol=symbol, conviction="medium", direction="long",
        thesis=f"{symbol} setup", risk_allocation_pct=risk,
    )


def _decision(targets: list[dict]) -> PortfolioDecision:
    return PortfolioDecision.model_validate({
        "reasoning_chain": {
            "macro_filter": "Macro checked.", "news_check": "News checked.",
            "earnings_check": "Earnings checked.",
            "signal_conflicts": "None material.",
            "sizing_logic": "Sizing checked.",
            "portfolio_balance": "Book checked.",
            "cash_target": "Cash checked.",
        },
        "targets": targets, "portfolio_view": "Test decision.",
    })


def _raw_target(symbol: str, *, risk: float = 3.0) -> dict:
    return {
        "symbol": symbol, "conviction": "medium", "direction": "long",
        "thesis": f"{symbol} setup.", "catalyst": "",
        "risk_allocation_pct": risk,
        "provenance": [{
            "source": "technical", "observed_stance": "buy",
            "relationship": "supports", "evidence": "current-run rating",
        }],
    }


def _build(analysis: TechAnalysisResult, *, risk: float = 3.0):
    return PortfolioConstructor().construct_orders(
        targets=[_target(analysis.symbol, risk=risk)], analyses=[analysis],
        positions=[], total_value=EQUITY,
        price_map={analysis.symbol: ENTRY},
    )


# ==========================================================================
# Type B / trend — no reward:risk computation may block or size it
# ==========================================================================

def test_a_breakout_with_an_awful_traditional_ratio_is_still_built():
    """THE case. Reward $2 against risk $5 is R/R 0.40 — under every floor
    this desk has ever had, and under the execution belt too. It trades,
    because the $102 "reward" is a level nothing says this trade stops at:
    a breakout is exited by a trailing stop, not at a target."""
    orders = _build(_analysis("AAA", setup_type="breakout"))
    assert len(orders) == 1
    assert orders[0].action == "BUY"
    assert orders[0].reward_risk is not None and orders[0].reward_risk < 1.0
    # And the RISK side is untouched: the level-backed stop ships as placed.
    assert orders[0].stop_loss == STOP


def test_a_breakout_is_never_resized_by_any_reward_risk_computation():
    """Not blocked AND not shrunk. The floor's measured cost was both — the
    Risk Manager halved allocations "per R/R enforcement policy" on names it
    did not outright refuse. Two identical breakouts whose ONLY difference is
    how far the overhead level sits must size identically."""
    thin = _build(_analysis("AAA", setup_type="breakout", upper_level=THIN_LEVEL))
    fat = _build(_analysis("AAA", setup_type="breakout", upper_level=FAT_LEVEL))
    assert len(thin) == len(fat) == 1
    assert thin[0].allocation_pct == fat[0].allocation_pct
    assert thin[0].stop_loss == fat[0].stop_loss == STOP


def test_a_breakout_is_exempt_from_the_pm_subfloor_gate_entirely():
    """No drop, no catalyst requirement, and — unlike a range trade — no
    starter-size cap either. None of those may key off a reward:risk figure
    for a trade with no ceiling to measure one against."""
    analysis = _analysis("AAA", setup_type="breakout")
    result = PortfolioManagerAgent._apply_subfloor_catalyst_rule(
        _decision([_raw_target("AAA", risk=3.0)]), analyses=[analysis],
        positions=[], total_value=EQUITY, active_state_changes="",
        rr_floor=REWARD_RISK_FLOOR, starter_risk_pct=STARTER_POSITION_RISK_PCT,
        real_reward_risk_by_symbol={"AAA": 0.4},
    )
    assert [t.symbol for t in result.targets] == ["AAA"]
    assert result.targets[0].risk_allocation_pct == 3.0
    assert result.targets[0].subfloor_catalyst_verified is False


def test_a_range_trade_on_the_same_numbers_is_capped_where_a_breakout_is_not():
    """The control for the test above: identical geometry, identical thin
    ratio, only the setup type differs. The range trade keeps the
    starter-size cap (a risk-reducing protection, deliberately retained);
    the breakout keeps its full size."""
    analysis = _analysis("AAA", setup_type="range")
    result = PortfolioManagerAgent._apply_subfloor_catalyst_rule(
        _decision([_raw_target("AAA", risk=3.0)]), analyses=[analysis],
        positions=[], total_value=EQUITY, active_state_changes="",
        rr_floor=REWARD_RISK_FLOOR, starter_risk_pct=STARTER_POSITION_RISK_PCT,
        real_reward_risk_by_symbol={"AAA": 0.4},
    )
    assert [t.symbol for t in result.targets] == ["AAA"]
    assert result.targets[0].risk_allocation_pct == STARTER_POSITION_RISK_PCT


def test_the_built_breakout_order_carries_its_setup_type_to_execution():
    """The execution stage cannot re-derive the setup type without building a
    second copy of the classification, so the constructor pins it onto the
    order — the same mechanism `stop_rule` already uses."""
    orders = _build(_analysis("AAA", setup_type="breakout"))
    assert orders[0].setup_type == "breakout"
    orders = _build(_analysis("AAA", setup_type="range", upper_level=FAT_LEVEL))
    assert orders[0].setup_type == "range"


def test_the_execution_belt_does_not_apply_to_a_breakout_order():
    """The last place the removed floor could have reappeared. A flat 1.2 at
    execution would have killed exactly the trades this change exists to
    allow, the moment execution moved anything."""
    breakout = TradeDecision(
        action="BUY", symbol="AAA", allocation_pct=5.0, entry_price=ENTRY,
        stop_loss=STOP, take_profit=THIN_LEVEL, reasoning="t",
        setup_type="breakout",
    )
    assert _execution_rr_floor(breakout) is None


def test_the_execution_belt_on_a_range_order_asks_only_about_degradation():
    """For a range order the belt survives, but as the question it says it is
    asking: did EXECUTION make this worse than what the Risk Manager
    approved? `min` means it can only ever be lower than the flat belt, never
    higher, so no ordinary order gets a looser bar."""
    thin = TradeDecision(
        action="BUY", symbol="AAA", allocation_pct=5.0, entry_price=ENTRY,
        stop_loss=STOP, take_profit=THIN_LEVEL, reasoning="t",
        setup_type="range",
    )
    assert _execution_rr_floor(thin) == pytest.approx(thin.reward_risk)
    assert _execution_rr_floor(thin) < EXECUTION_REWARD_RISK_BELT

    fat = TradeDecision(
        action="BUY", symbol="AAA", allocation_pct=5.0, entry_price=ENTRY,
        stop_loss=STOP, take_profit=FAT_LEVEL, reasoning="t",
        setup_type="range",
    )
    assert _execution_rr_floor(fat) == EXECUTION_REWARD_RISK_BELT


def test_a_breakout_with_no_measurable_reward_is_still_not_blocked_on_it():
    """"Unmeasurable" is still a reward-side refusal, and the owner decision
    is that no reward-side computation blocks a trend trade. A range trade
    still fails closed here — that contrast is the assertion."""
    constructor = PortfolioConstructor()
    for setup, expected in (("breakout", STOP), ("range", None)):
        assert constructor._widen_stop_past_noise(
            "AAA", _analysis("AAA", setup_type=setup), ENTRY, STOP,
            direction="long", target_price=float("nan"),
        ) == expected, setup


# ==========================================================================
# Type A / range — a real ratio, used as a signal rather than a cutoff
# ==========================================================================

def test_a_range_trades_ratio_is_computed_from_its_own_real_levels():
    """Both sides measured, neither guessed: risk is entry to the computed
    support the stop sits on, reward is entry to the computed resistance
    `_derive_target` picks. 15 / 5 = 3.00 and 2 / 5 = 0.40."""
    constructor = PortfolioConstructor()
    assert constructor.real_reward_risk_preview(
        _analysis("AAA", setup_type="range", upper_level=FAT_LEVEL), "long",
    ) == 3.0
    assert constructor.real_reward_risk_preview(
        _analysis("AAA", setup_type="range", upper_level=THIN_LEVEL), "long",
    ) == 0.4


def test_a_weak_range_ratio_is_not_rejected_and_ranks_below_a_strong_one():
    """The replacement for the hard floor, end to end through the real PM
    entry point. Two range candidates identical in rating and conviction —
    so they tie on the composite score — differing only in where their own
    real resistance sits. Neither is refused; the honest ratio decides which
    the desk is pointed at first, and the weak one's is on the record."""
    strong = _analysis("STRG", setup_type="range", upper_level=FAT_LEVEL)
    weak = _analysis("WEAK", setup_type="range", upper_level=THIN_LEVEL)
    analyses = [weak, strong]
    constructor = PortfolioConstructor()
    real_map = {
        a.symbol: constructor.real_reward_risk_preview(a, "long")
        for a in analyses
    }
    assert real_map == {"STRG": 3.0, "WEAK": 0.4}

    registry = PortfolioManagerAgent.build_evidence_registry(
        analyses=analyses, positions=[], news_intel=None,
        earnings_analyses=[], macro_analysis=None, smart_money_findings=[],
        symbol_sectors={},
    )
    ranked, blocked = PortfolioManagerAgent.rank_candidates(
        analyses=analyses, evidence_registry=registry,
        allowed_buy_symbols={"STRG", "WEAK"}, active_state_changes="",
        real_reward_risk_by_symbol=real_map,
    )
    assert blocked == {}, "a thin-but-real payoff is not a refusal any more"
    assert [c.symbol for c in ranked] == ["STRG", "WEAK"]
    assert ranked[0].score == ranked[1].score, (
        "they must genuinely tie on the composite, or this is not testing "
        "the reward:risk signal at all"
    )
    assert ranked[0].components["risk_reward_tiebreak"] == 3.0
    assert ranked[1].components["risk_reward_tiebreak"] == 0.4


def test_the_weak_range_trade_still_ships_an_order_at_starter_size():
    """The other half of "not a hard rejection": it actually trades. The
    starter-size cap applied by the PM gate is what bounds it, not a
    refusal."""
    orders = _build(
        _analysis("AAA", setup_type="range"), risk=STARTER_POSITION_RISK_PCT,
    )
    assert len(orders) == 1
    assert orders[0].stop_loss == STOP
    assert orders[0].reward_risk == 0.4
