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
        morning_trades=[], total_value=100_000.0, avg_hold_days=5.0,
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
        morning_trades=[], total_value=100_000.0, avg_hold_days=5.0,
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
