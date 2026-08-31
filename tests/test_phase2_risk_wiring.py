"""Phase 2a — the deterministic risk primitives, wired into the real pipeline.

Covers the four audit findings this tranche closes:

  §1.1  the drawdown-halve rule is code, not a prompt instruction
  §1.2  the Portfolio Manager receives the correlation matrix BEFORE it chooses
  §1.3  portfolio heat is computed and shown to PM and the Risk Manager
  §1.4  R-multiple reaches the Position Reviewer

Each test names the finding it pins so a later editor sees what breaks.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.config import RiskConfig
from src.models import Position, TradeDecision
from src.pipeline import HARD_BLOCK_RULES, TradingPipeline
from src.pipeline_context import PMFacts
from src.risk.rules import DRAWDOWN_BUY_SCALE, RiskRuleEngine, apply_drawdown_scale


def _buy(symbol="NVDA", alloc=10.0) -> TradeDecision:
    return TradeDecision(
        action="BUY", symbol=symbol, allocation_pct=alloc,
        entry_price=100.0, stop_loss=95.0, take_profit=115.0,
        reasoning="high conviction breakout",
    )


def _position(symbol="NVDA", qty=10, avg_entry=100.0, current_price=110.0,
              sector="Technology") -> Position:
    return Position(
        symbol=symbol, qty=qty, avg_entry=avg_entry, current_price=current_price,
        market_value=qty * current_price, unrealized_pnl=qty * (current_price - avg_entry),
        sector=sector,
    )


def _risk_config(**overrides) -> RiskConfig:
    fields = dict(
        max_position_pct=20, max_total_position_pct=90, max_daily_loss_pct=3,
        max_sector_pct=40, require_stop_loss=True,
    )
    fields.update(overrides)
    return RiskConfig(**fields)


# ===========================================================================
# §1.1 — the drawdown-halve is deterministic code now
# ===========================================================================

def test_drawdown_scale_halves_every_buy():
    decisions = [_buy("NVDA", 12.0), _buy("AMD", 5.0)]
    scaled, notes = apply_drawdown_scale(decisions, in_drawdown=True)
    assert [d.allocation_pct for d in scaled] == [6.0, 2.5]
    assert len(notes) == 2


def test_drawdown_scale_is_a_no_op_when_not_in_drawdown():
    decisions = [_buy("NVDA", 12.0)]
    scaled, notes = apply_drawdown_scale(decisions, in_drawdown=False)
    assert scaled[0].allocation_pct == 12.0
    assert notes == []


def test_drawdown_scale_leaves_sells_and_holds_alone():
    """De-risking during a drawdown is the point — never shrink an exit."""
    sell = TradeDecision(
        action="SELL", symbol="NVDA", allocation_pct=100.0, entry_price=0.0,
        stop_loss=0.0, take_profit=0.0, reasoning="thesis invalid",
    )
    hold = TradeDecision(
        action="HOLD", symbol="AMD", allocation_pct=0.0, entry_price=0.0,
        stop_loss=0.0, take_profit=0.0, reasoning="keep",
    )
    scaled, _ = apply_drawdown_scale([sell, hold], in_drawdown=True)
    assert scaled[0].allocation_pct == 100.0
    assert scaled[1].allocation_pct == 0.0


def test_drawdown_scale_records_provenance_in_the_reasoning():
    """The AI Risk Manager audits constructed orders against PM's prose. An
    unexplained size difference reads to it as PM contradicting itself — on
    2026-08-20 exactly that mismatch drew a full-plan veto over deterministic
    math."""
    scaled, _ = apply_drawdown_scale([_buy("NVDA", 12.0)], in_drawdown=True)
    assert "in_drawdown=true" in scaled[0].reasoning
    assert "not PM inconsistency" in scaled[0].reasoning


def test_drawdown_scale_zeroes_a_buy_that_halves_below_tradable_size():
    scaled, notes = apply_drawdown_scale([_buy("NVDA", 0.004)], in_drawdown=True)
    assert scaled[0].allocation_pct == 0.0
    assert "0%" in notes[0]


def test_drawdown_buy_cap_is_a_hard_block_rule():
    """A violation here means a BUY reached the engine unscaled. Fail closed."""
    assert "drawdown_buy_cap" in HARD_BLOCK_RULES


def test_engine_blocks_an_unscaled_buy_during_drawdown():
    engine = RiskRuleEngine(_risk_config())
    violations = engine.check(
        decision=_buy("NVDA", 15.0),   # over 20% x 0.5 = 10%
        positions=[], total_value=100_000.0, daily_pnl=0.0,
        baseline=100_000.0, in_drawdown=True,
    )
    rules = {v.rule for v in violations}
    assert "drawdown_buy_cap" in rules


def test_engine_allows_the_same_buy_once_it_has_been_halved():
    engine = RiskRuleEngine(_risk_config())
    halved, _ = apply_drawdown_scale([_buy("NVDA", 15.0)], in_drawdown=True)
    violations = engine.check(
        decision=halved[0], positions=[], total_value=100_000.0, daily_pnl=0.0,
        baseline=100_000.0, in_drawdown=True,
    )
    assert "drawdown_buy_cap" not in {v.rule for v in violations}


def test_drawdown_cap_bounds_new_money_not_existing_winners():
    """The rule the prompts have always stated is "halve every new BUY", not
    "force-trim existing positions during a drawdown". A small add on top of a
    large held position must not trip the drawdown cap."""
    engine = RiskRuleEngine(_risk_config())
    violations = engine.check(
        decision=_buy("NVDA", 2.0),
        positions=[_position("NVDA", qty=150, current_price=100.0)],  # 15% held
        total_value=100_000.0, daily_pnl=0.0, baseline=100_000.0,
        in_drawdown=True,
    )
    assert "drawdown_buy_cap" not in {v.rule for v in violations}


def test_engine_does_not_apply_the_cap_outside_a_drawdown():
    engine = RiskRuleEngine(_risk_config())
    violations = engine.check(
        decision=_buy("NVDA", 15.0), positions=[], total_value=100_000.0,
        daily_pnl=0.0, baseline=100_000.0, in_drawdown=False,
    )
    assert "drawdown_buy_cap" not in {v.rule for v in violations}


def test_drawdown_scale_constant_matches_the_documented_rule():
    assert DRAWDOWN_BUY_SCALE == 0.5


# ===========================================================================
# §1.2 — correlation reaches PM before it decides
# ===========================================================================

def test_correlation_clusters_group_transitively():
    """A theme is one bet even when its outer pair falls under the threshold."""
    from src.data.correlation import correlation_clusters

    matrix = {
        "OKLO": {"CEG": 0.82, "VST": 0.60},
        "CEG": {"OKLO": 0.82, "VST": 0.79},
        "VST": {"CEG": 0.79, "OKLO": 0.60},
        "KO": {},
    }
    assert correlation_clusters(["OKLO", "CEG", "VST", "KO"], matrix) == [
        ["CEG", "OKLO", "VST"],
    ]


def test_correlation_clusters_omit_singletons():
    from src.data.correlation import correlation_clusters

    assert correlation_clusters(["KO", "PEP"], {"KO": {"PEP": 0.2}}) == []


def test_correlation_clusters_are_stable_and_largest_first():
    from src.data.correlation import correlation_clusters

    matrix = {
        "A": {"B": 0.9}, "B": {"A": 0.9},
        "C": {"D": 0.8, "E": 0.8}, "D": {"C": 0.8}, "E": {"C": 0.8},
    }
    assert correlation_clusters(["E", "A", "D", "C", "B"], matrix) == [
        ["C", "D", "E"], ["A", "B"],
    ]


def test_correlation_clusters_use_absolute_correlation():
    """An inverse ETF moving −0.9 against the book is the same single bet."""
    from src.data.correlation import correlation_clusters

    matrix = {"SPY": {"SH": -0.95}, "SH": {"SPY": -0.95}}
    assert correlation_clusters(["SPY", "SH"], matrix) == [["SH", "SPY"]]


def test_correlation_clusters_empty_without_a_matrix():
    from src.data.correlation import correlation_clusters

    assert correlation_clusters(["A", "B"], {}) == []


def test_ensure_correlation_matrix_is_memoized_on_the_context():
    """RiskStage must score PM against the numbers PM was actually shown, and
    must not pay to rebuild them."""
    from src.pipeline_context import RunContext

    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.market = MagicMock()
    pipeline.config = SimpleNamespace(trading=SimpleNamespace(lookback_days=1800))
    ctx = RunContext.start("morning")
    ctx.symbols_bars = {}

    with patch("src.data.correlation.build_correlation_matrix",
               return_value={"NVDA": {"AMD": 0.9}}) as build:
        first = pipeline._ensure_correlation_matrix(ctx, [])
        second = pipeline._ensure_correlation_matrix(ctx, [])

    assert first == second == {"NVDA": {"AMD": 0.9}}
    assert build.call_count == 1, "the matrix must be built once per run"
    assert ctx.correlation_matrix == {"NVDA": {"AMD": 0.9}}


def test_ensure_correlation_matrix_degrades_to_empty_on_failure():
    from src.pipeline_context import RunContext

    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.market = MagicMock()
    pipeline.config = SimpleNamespace(trading=SimpleNamespace(lookback_days=1800))
    ctx = RunContext.start("morning")
    ctx.symbols_bars = {}

    with patch("src.data.correlation.build_correlation_matrix",
               side_effect=RuntimeError("boom")):
        assert pipeline._ensure_correlation_matrix(ctx, []) == {}


# ===========================================================================
# §1.3 / §1.4 — the facts block PM actually reads
# ===========================================================================

def test_pm_facts_render_the_risk_block_with_headroom():
    from src.risk.metrics import portfolio_heat

    facts = PMFacts()
    facts.heat = portfolio_heat(
        positions=[_position("NVDA", qty=100, avg_entry=100.0, current_price=110.0)],
        equity=100_000.0, stops={"NVDA": 95.0},
    )
    facts.risk_ceiling_pct = 25.0
    rendered = facts.render()
    assert "Portfolio Risk" in rendered
    assert "headroom" in rendered
    assert "NVDA" in rendered


def test_pm_facts_say_unknown_when_heat_could_not_be_computed():
    """A failed heat build must never render as a risk-free book."""
    rendered = PMFacts().render()
    assert "not computed this run" in rendered
    assert "UNSOURCED:no_risk_data" in rendered


def test_pm_facts_render_correlation_clusters():
    facts = PMFacts()
    facts.correlation_clusters = [["AMD", "NVDA", "SMH"]]
    rendered = facts.render()
    assert "Correlation Clusters" in rendered
    assert "AMD / NVDA / SMH" in rendered
    assert "ONE bet" in rendered


def test_pm_facts_flag_missing_correlation_coverage_as_a_disabled_gate():
    facts = PMFacts()
    facts.correlation_coverage = False
    rendered = facts.render()
    assert "coverage MISSING" in rendered
    assert "disabled" in rendered


def test_pm_facts_tell_pm_not_to_pre_apply_the_drawdown_halving():
    """Two halvings quarter the position. Exactly one layer may own it."""
    facts = PMFacts()
    facts.in_drawdown = True
    rendered = facts.render()
    assert "Do NOT pre-halve" in rendered

    facts.in_drawdown = False
    assert "pre-halve" not in facts.render()


def test_pm_prompt_no_longer_carries_a_drawdown_multiplier():
    """The sizing formula and the engine must not both apply the haircut."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1]
            / "config" / "prompts" / "portfolio_manager.md").read_text()
    assert "drawdown   = 0.5" not in text
    assert "× stale × drawdown" not in text
    assert "no `drawdown` term" in text


# ===========================================================================
# §1.3 — the Risk Manager sees the same heat PM sized against
# ===========================================================================

def _rm_message(**overrides) -> str:
    from src.agents.risk_manager import RiskManagerAgent
    from src.models import PortfolioDecision, ReasoningChain

    decision = PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter="risk-on", news_check="quiet", earnings_check="none",
            signal_conflicts="none", sizing_logic="per conviction",
            portfolio_balance="within caps", cash_target="10%",
        ),
        decisions=[_buy()], portfolio_view="constructive",
    )
    kwargs = dict(
        portfolio_decision=decision, positions=[_position()],
        macro_summary={}, rule_violations=[],
    )
    kwargs.update(overrides)
    with patch("anthropic.Anthropic"):
        agent = RiskManagerAgent(api_key="test", model="claude-sonnet-4-6")
    return agent.build_user_message(**kwargs)


def test_rm_sees_the_portfolio_risk_block():
    from src.risk.metrics import portfolio_heat

    heat = portfolio_heat(
        positions=[_position("NVDA", qty=100, avg_entry=100.0, current_price=110.0)],
        equity=100_000.0, stops={"NVDA": 95.0},
    )
    msg = _rm_message(heat=heat, risk_ceiling_pct=25.0)
    assert "Portfolio Risk" in msg
    assert "headroom" in msg


def test_rm_is_told_when_risk_could_not_be_computed():
    msg = _rm_message(heat=None)
    assert "not computed this run" in msg
    assert "UNKNOWN" in msg


def test_rm_is_told_the_engine_already_halved_the_buys():
    """RM must not demand a halving that already happened, nor read the
    smaller size as PM inconsistency."""
    msg = _rm_message(recent_performance={
        "rolling_5d_pct": -4.1, "rolling_20d_pct": -2.0,
        "in_drawdown": True, "trailing_days": 22,
    })
    assert "ALREADY halved" in msg
    assert "not read the smaller size as PM inconsistency" in msg


# ===========================================================================
# §1.4 — R-multiple reaches the Position Reviewer
# ===========================================================================

def test_position_facts_carry_the_r_multiple_against_the_entry_stop():
    """The denominator is the bet that was made, not the trailed level."""
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.db = MagicMock()
    pipeline.db.get_symbol_last_buy.return_value = {
        "stop_loss": 90.0, "take_profit": 130.0, "timestamp": None,
    }
    pipeline.broker = MagicMock()
    pipeline.broker.get_current_stop_price.return_value = 105.0   # trailed up
    pipeline._atr_for_symbol = MagicMock(return_value=2.0)

    facts = pipeline._build_position_facts(
        positions=[_position("NVDA", qty=10, avg_entry=100.0, current_price=120.0)],
        morning_trades=[], total_value=100_000.0,
    )
    # Risked $10/share at entry, now +$20 → 2R, NOT (120-100)/(100-105).
    assert facts["NVDA"]["r_multiple"] == 2.0
    assert facts["NVDA"]["initial_stop"] == 90.0
    # Live stop 105 is above entry 100 → budget risk released.
    assert facts["NVDA"]["risk_released"] is True


def test_position_facts_omit_r_when_no_risk_was_defined_at_entry():
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.db = MagicMock()
    pipeline.db.get_symbol_last_buy.return_value = {
        "stop_loss": 0.0, "take_profit": 130.0, "timestamp": None,
    }
    pipeline.broker = MagicMock()
    pipeline.broker.get_current_stop_price.return_value = None
    pipeline._atr_for_symbol = MagicMock(return_value=2.0)

    facts = pipeline._build_position_facts(
        positions=[_position("NVDA", qty=10, avg_entry=100.0, current_price=120.0)],
        morning_trades=[], total_value=100_000.0,
    )
    assert facts["NVDA"]["r_multiple"] is None
    assert facts["NVDA"]["initial_stop"] is None


def test_reviewer_renders_r_multiple_in_the_metrics_line():
    from src.agents.position_reviewer import PositionReviewerAgent

    with patch("anthropic.Anthropic"):
        agent = PositionReviewerAgent(api_key="test", model="claude-sonnet-4-6")
    msg = agent.build_user_message(
        positions=[_position("NVDA", qty=10, avg_entry=100.0, current_price=120.0)],
        macro_summary={}, cash_balance=10_000.0, total_value=100_000.0,
        position_facts={"NVDA": {
            "days_held": 6, "r_multiple": 2.0, "risk_released": True,
            "thesis_progress_pct": 40.0, "distance_to_stop_pct": 12.5,
        }},
        session_type="midday",
    )
    assert "R=+2.00" in msg
    assert "risk_released" in msg


def test_reviewer_prompt_documents_r_before_thesis_progress():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1]
            / "config" / "prompts" / "position_reviewer.md").read_text()
    assert "R-multiple" in text
    assert text.index("`R` = the **R-multiple**") < text.index("`thesis_progress_pct` =")


# ===========================================================================
# The Risk Manager must be GIVEN reward:risk, never left to divide it itself
#
# 2026-08-31 incident. The RM received the constructed order as bare
# Entry/Stop/Target text with no ratio, did the arithmetic inside the model,
# and produced TWO DIFFERENT ANSWERS IN ONE RESPONSE for a BUY on RSG
# (entry 221.14, stop 207.90, target 242.96): `rr_audit` said "R/R = 1.65 ...
# above 1.5, so compliant", `reasoning` said "R/R = 1.31, which is below the
# 1.5 floor". The pipeline acts on `reasoning`, so a compliant trade was
# rejected and the desk took no position that session. 1.65 is correct; 1.31
# matches no combination of the inputs.
#
# The seam these cover — build a real constructed order, render it into the
# RM prompt, assert the ratio is present and correct — had NO test at all.
# ===========================================================================

def _decision(action="BUY", entry=221.14, stop=207.90, target=242.96):
    from src.models import TradeDecision
    return TradeDecision(
        action=action, symbol="RSG", allocation_pct=5.0,
        entry_price=entry, stop_loss=stop, take_profit=target,
        reasoning="constructed order under test",
    )


def test_trade_decision_computes_reward_risk_in_python():
    """The exact 2026-08-31 order. 1.65, deterministically, every time."""
    assert _decision().reward_risk == 1.65


def test_reward_risk_mirrors_the_short_side():
    d = _decision(action="SHORT", entry=100.0, stop=110.0, target=80.0)
    assert d.reward_risk == 2.0


def test_reward_risk_is_none_when_there_is_no_entry_geometry():
    """SELL/COVER reduce an existing position; HOLD opens nothing. A ratio
    rendered for these would be fiction."""
    for action in ("SELL", "COVER", "HOLD"):
        assert _decision(action=action).reward_risk is None


def test_malformed_buy_geometry_is_rejected_before_a_ratio_can_exist():
    """A BUY whose stop sits above entry never becomes a TradeDecision at all
    — `validate_buy_prices` refuses it at construction. Asserted here so the
    reward_risk guard's None-branch is understood as defence in depth behind
    an existing validator, not as the only thing standing between the desk and
    a fake ratio."""
    import pytest as _pytest
    from pydantic import ValidationError
    with _pytest.raises(ValidationError):
        _decision(stop=230.0)


def test_rm_prompt_carries_the_computed_reward_risk_for_each_trade():
    """The regression that matters: the ratio must reach the RM's prompt, so
    the model never has to derive the number its own floor is judged against."""
    from src.models import PortfolioDecision, ReasoningChain

    decision = PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter="risk-on", news_check="quiet", earnings_check="none",
            signal_conflicts="none", sizing_logic="per conviction",
            portfolio_balance="within caps", cash_target="10%",
        ),
        decisions=[_decision()], portfolio_view="constructive",
    )
    msg = _rm_message(portfolio_decision=decision)
    assert "R/R 1.65:1" in msg, (
        "the constructed order's computed reward:risk must be rendered into "
        "the Risk Manager prompt — without it the model divides the prices "
        "itself, which is exactly what failed on 2026-08-31"
    )


def test_rm_prompt_omits_reward_risk_where_none_exists():
    """A SELL must not carry a ratio — rendering 'R/R None:1' would be worse
    than rendering nothing."""
    from src.models import PortfolioDecision, ReasoningChain

    decision = PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter="risk-on", news_check="quiet", earnings_check="none",
            signal_conflicts="none", sizing_logic="per conviction",
            portfolio_balance="within caps", cash_target="10%",
        ),
        decisions=[_decision(action="SELL")], portfolio_view="constructive",
    )
    msg = _rm_message(portfolio_decision=decision)
    assert "R/R None" not in msg


# ===========================================================================
# The constructor must SAY what it removed, or the RM vetoes the survivors
#
# 2026-08-31, first forced session of the day. The constructor struck a BUY on
# NVDA (reward:risk 1.42 against the 1.50 floor). PM's reasoning_chain, written
# BEFORE the constructor ran, still argued for it. The RM saw a narrative about
# a trade absent from the order list and rejected the ENTIRE plan:
#
#   "PM constructs a detailed narrative around a BUY NVDA trade that does not
#    exist in the proposed orders. This undermines trust in the decision logic.
#    While COP and V are valid, the plan as presented is not internally
#    consistent and cannot be approved."
#
# Two trades it had just called valid died for a bookkeeping mismatch. The RM
# was reasoning correctly from what it was shown; it was shown the wrong thing.
# ===========================================================================

def _pd_with(dropped, decisions=None):
    from src.models import PortfolioDecision, ReasoningChain
    return PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter="risk-on", news_check="quiet", earnings_check="none",
            signal_conflicts="none", sizing_logic="per conviction",
            portfolio_balance="within caps", cash_target="10%",
        ),
        decisions=decisions if decisions is not None else [_decision()],
        constructor_dropped=dropped,
        portfolio_view="constructive",
    )


def test_rm_is_told_which_symbols_the_constructor_removed():
    msg = _rm_message(portfolio_decision=_pd_with(["NVDA", "VLO"]))
    assert "Removed Before You Saw This" in msg
    assert "NVDA" in msg and "VLO" in msg


def test_rm_is_told_removal_was_deterministic_and_not_incoherence():
    """The block must not merely list symbols — it must tell the RM that a
    narrative mentioning them is EXPECTED, which is the part that stops the
    full-plan veto."""
    msg = _rm_message(portfolio_decision=_pd_with(["NVDA"]))
    assert "EXPECTED" in msg
    assert "do not veto the surviving trades" in msg


def test_no_removal_block_when_the_constructor_dropped_nothing():
    """A clean plan must not carry an empty scary heading."""
    msg = _rm_message(portfolio_decision=_pd_with([]))
    assert "Removed Before You Saw This" not in msg


def test_constructor_dropped_defaults_empty_so_old_call_sites_are_unaffected():
    from src.models import PortfolioDecision, ReasoningChain
    pd = PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter="a", news_check="b", earnings_check="c",
            signal_conflicts="d", sizing_logic="e", portfolio_balance="f",
            cash_target="g",
        ),
        portfolio_view="x",
    )
    assert pd.constructor_dropped == []
