import pytest
import json
from unittest.mock import patch, MagicMock
from src.agents.risk_manager import RiskManagerAgent
from src.models import PortfolioDecision, ReasoningChain, TradeDecision, Position
from src.risk.rules import RiskViolation


def _pm_rc() -> ReasoningChain:
    """Minimal valid 7-step CoT for PortfolioDecision — all required
    fields populated with non-empty values per `Field(min_length=1)`.
    """
    return ReasoningChain(
        macro_filter="x", news_check="x", earnings_check="x",
        signal_conflicts="x", sizing_logic="x",
        portfolio_balance="x", cash_target="x",
    )


def _risk_rc_payload() -> dict:
    """RM JSON-shape reasoning_chain — every step a non-empty string."""
    return {
        "rr_audit": "every BUY at R/R ≥ 1.5",
        "signal_fidelity": "PM aligned with TA",
        "correlation_check": "no AI cluster breach",
        "event_risk": "no earnings within 3 days",
        "sizing_sanity": "all sizes proportional",
        "overall": "approve as-is",
    }


@pytest.fixture
def sample_portfolio_decision():
    return PortfolioDecision(
        reasoning_chain=_pm_rc(),
        decisions=[
            TradeDecision(
                action="BUY", symbol="SPY", allocation_pct=10.0,
                entry_price=507.0, stop_loss=490.0, take_profit=530.0,
                reasoning="Strong uptrend",
            ),
        ],
        portfolio_view="Bullish, 60% invested",
    )


@pytest.fixture
def mock_risk_response():
    return json.dumps({
        "approved": True,
        "reasoning_chain": _risk_rc_payload(),
        "modifications": [],
        "reasoning": "Plan looks sound. Risk-reward acceptable.",
    })


@patch("anthropic.Anthropic")
def test_risk_manager_approve(mock_cls, sample_portfolio_decision, mock_risk_response):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=mock_risk_response)]
    mock_response.usage.input_tokens = 800
    mock_response.usage.output_tokens = 200
    mock_client.messages.create.return_value = mock_response
    mock_cls.return_value = mock_client

    agent = RiskManagerAgent(api_key="test", model="claude-opus-4-6-20250725")
    verdict, agent_result = agent.review(
        portfolio_decision=sample_portfolio_decision,
        positions=[],
        macro_summary={"vix": {"current": 18.0}},
        rule_violations=[],
    )
    assert verdict is not None
    assert verdict.approved is True
    assert agent_result.tokens_used > 0


@patch("anthropic.Anthropic")
def test_risk_manager_with_violations(mock_cls, sample_portfolio_decision):
    rejection = json.dumps({
        "approved": False,
        "reasoning_chain": _risk_rc_payload(),
        "modifications": [],
        "reasoning": "Single-name position limit exceeded. No new trades.",
    })
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=rejection)]
    mock_response.usage.input_tokens = 800
    mock_response.usage.output_tokens = 200
    mock_client.messages.create.return_value = mock_response
    mock_cls.return_value = mock_client

    violations = [
        RiskViolation(rule="max_position_pct", message="Position 25.0% exceeds max 20%", value=25.0, limit=20.0),
    ]

    agent = RiskManagerAgent(api_key="test", model="claude-opus-4-6-20250725")
    verdict, agent_result = agent.review(
        portfolio_decision=sample_portfolio_decision,
        positions=[],
        macro_summary={"vix": {"current": 25.0}},
        rule_violations=violations,
    )
    assert verdict is not None
    assert verdict.approved is False
    assert agent_result is not None


# ---------------------------------------------------------------------------
# Per-entry isolation for modifications (mirrors PR #73/#74 pattern)
# ---------------------------------------------------------------------------

def _valid_risk_verdict_json() -> dict:
    return {
        "approved": True,
        "reasoning_chain": {
            "rr_audit": "All BUYs have R/R ≥ 1.5.",
            "signal_fidelity": "PM aligned with TA.",
            "correlation_check": "No clustering.",
            "event_risk": "No earnings within 3 days.",
            "sizing_sanity": "Sizes proportional to conviction.",
            "overall": "Approve as-is.",
        },
        "modifications": [],
        "scale_all_buys": 1.0,
        "reason_category": "clean",
        "reasoning": "Looks clean.",
    }


def _valid_modification(symbol: str = "NVDA") -> dict:
    return {
        "symbol": symbol,
        "field": "allocation_pct",
        "original_value": 12.0,
        "new_value": 8.0,
        "reason": "Trim concentration risk.",
    }


def test_drop_invalid_modifications_strips_non_numeric_value_keeps_rest():
    """A RiskModification with `original_value` as a non-coercible string
    must be dropped individually instead of failing the whole RiskVerdict
    (which would lose the reasoning_chain + scale_all_buys + the OTHER
    modifications)."""
    parsed = _valid_risk_verdict_json()
    parsed["modifications"] = [
        _valid_modification("NVDA"),
        {**_valid_modification("BAD"), "original_value": "not a number"},
        _valid_modification("AMZN"),
    ]
    out = RiskManagerAgent._drop_invalid_modifications(parsed)
    syms = [m["symbol"] for m in out["modifications"]]
    assert syms == ["NVDA", "AMZN"]


def test_drop_invalid_modifications_strips_missing_field():
    """A RiskModification missing the required `reason` field gets dropped."""
    parsed = _valid_risk_verdict_json()
    parsed["modifications"] = [
        _valid_modification("NVDA"),
        {"symbol": "BAD", "field": "x", "original_value": 1.0, "new_value": 0.5},
        _valid_modification("AMZN"),
    ]
    out = RiskManagerAgent._drop_invalid_modifications(parsed)
    syms = [m["symbol"] for m in out["modifications"]]
    assert syms == ["NVDA", "AMZN"]


def test_risk_verdict_constructs_after_dropping_bad_modification():
    """End-to-end: with the malformed modification stripped, RiskVerdict
    constructs and preserves reasoning_chain, scale_all_buys, approved."""
    from src.models import RiskVerdict

    parsed = _valid_risk_verdict_json()
    parsed["modifications"] = [
        _valid_modification("NVDA"),
        {"symbol": "BAD"},  # missing several required fields
    ]
    cleaned = RiskManagerAgent._drop_invalid_modifications(parsed)
    verdict = RiskVerdict(**cleaned)
    assert verdict.approved is True
    assert verdict.scale_all_buys == 1.0
    assert len(verdict.modifications) == 1
    assert verdict.modifications[0].symbol == "NVDA"


def test_drop_invalid_modifications_handles_non_list_shape():
    parsed = _valid_risk_verdict_json()
    parsed["modifications"] = "oops not a list"
    out = RiskManagerAgent._drop_invalid_modifications(parsed)
    assert out["modifications"] == []


@patch("anthropic.Anthropic")
def test_risk_review_survives_one_malformed_modification(mock_cls, sample_portfolio_decision):
    """End-to-end via review(): a bad modification in the LLM output no longer
    fails the whole verdict. Pre-fix this would leave execution stage with no
    RM guidance for the morning."""
    payload = _valid_risk_verdict_json()
    payload["approved"] = False
    payload["modifications"] = [
        _valid_modification("NVDA"),
        {"symbol": "BAD", "original_value": "garbage"},  # malformed
    ]
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=json.dumps(payload))]
    mock_resp.usage.input_tokens = 100
    mock_resp.usage.output_tokens = 50
    mock_client.messages.create.return_value = mock_resp
    mock_cls.return_value = mock_client

    agent = RiskManagerAgent(api_key="test", model="claude-opus-4-6-20250725")
    verdict, _ = agent.review(
        portfolio_decision=sample_portfolio_decision,
        positions=[],
        macro_summary={"vix": {"current": 18.0}},
        rule_violations=[],
    )
    assert verdict is not None
    assert verdict.approved is False
    assert len(verdict.modifications) == 1
    assert verdict.modifications[0].symbol == "NVDA"


def test_prompt_describes_correct_pipeline_order():
    """External review: the prompt claimed PortfolioConstructor runs
    AFTER the Risk Manager ('After you, PortfolioConstructor submits
    orders') — backwards. The constructor translates PM's targets into
    the orders RM reviews BEFORE RM ever runs; deterministic re-check +
    execution run after RM. Pin the corrected order so this can't
    silently drift back."""
    from src.agents.risk_manager import PROMPT_PATH
    text = PROMPT_PATH.read_text()
    assert "already ran, before you" in text
    assert "After you, `PortfolioConstructor` submits orders" not in text


@patch("anthropic.Anthropic")
def test_dropped_news_symbols_are_stated_as_unknown_not_silence(mock_cls, sample_portfolio_decision):
    """Incomplete news must reach Risk as UNKNOWN, matching the PM block."""
    from src.models import MacroNarrative, NewsIntelligenceReport
    news_intel = NewsIntelligenceReport(
        macro_narrative=MacroNarrative(
            last_updated="2026-09-15", era_themes=["test"],
            current_regime="risk-on",
        ),
        stock_news={"NVDA": [{
            "headline": "chip news", "sentiment": "bullish",
            "conviction": "medium", "impact_summary": "positive",
        }]},
        pm_briefing="NVDA bullish.",
        market_sentiment="bullish", confidence="medium",
    )
    news_intel.dropped_news_symbols = ["MSFT"]
    news_intel.stock_news["MSFT"] = []

    agent = RiskManagerAgent(api_key="test", model="test-model")
    msg = agent.build_user_message(
        portfolio_decision=sample_portfolio_decision,
        positions=[],
        macro_summary={"vix": {"current": 18.0}},
        rule_violations=[],
        news_intel=news_intel,
    )
    assert "News Answer Lost" in msg
    assert "MSFT" in msg
    assert "NOT an absence of news" in msg


# ── item 162: an advisory rendered as a VIOLATION ────────────────────────
#
# Owner ruling 2026-09-19: hard limits are enforced by CODE, and above a
# mere guideline the risk seat may RESIZE but never VETO. Every entry used to
# render with one prefix, `VIOLATION`, and 3 of 17 stored verdicts then
# vetoed a whole plan citing an advisory as "the hard risk rule".

def _findings(*rules):
    from src.agents.risk_manager import _format_engine_findings
    from src.risk.rules import RiskViolation
    return _format_engine_findings([
        RiskViolation(rule=r, message=f"{r} fired", value=1.0, limit=2.0)
        for r in rules
    ])


def test_an_advisory_does_not_render_as_a_violation():
    text = _findings("max_sector_pct")
    assert "VIOLATION" not in text
    assert "ADVISORY" in text
    assert "NO order was blocked" in text


def test_a_hard_limit_still_reads_as_a_breach_the_engine_enforced():
    text = _findings("cash_only")
    assert "HARD LIMIT BREACHED" in text
    assert "ALREADY REFUSED" in text


def test_the_class_is_read_from_the_rule_set_never_from_the_rule_name():
    """`max_sector_pct` and `max_sector_hard_pct` differ by one word and sit
    on opposite sides of the line. A name-based split gets this wrong."""
    from src.pipeline import HARD_BLOCK_RULES
    assert "max_sector_pct" not in HARD_BLOCK_RULES
    assert "max_sector_hard_pct" in HARD_BLOCK_RULES
    soft = _findings("max_sector_pct")
    hard = _findings("max_sector_hard_pct")
    assert "ADVISORY (nothing blocked) [max_sector_pct]" in soft
    assert "HARD LIMIT BREACHED [max_sector_hard_pct]" in hard


def test_a_hard_limit_can_never_rank_below_an_advisory():
    text = _findings(
        "max_sector_pct", "correlation_cluster", "data_degraded",
        "max_position_pct", "deployment_gap",
    )
    assert text.index("HARD LIMIT BREACHED") < text.index("ADVISORY (nothing blocked)")


def test_the_empty_block_is_not_a_false_all_clear_on_the_hard_limits():
    """`_filter_hard_risk_decisions` drops an order whose hard rule fired and
    `continue`s past this list, forwarding only `sector_unresolved_*`. So a
    hard entry cannot reach this block and an empty one proves nothing about
    hard limits — the old text, "No hard rule violations detected", claimed
    the opposite."""
    text = _findings()
    assert "No hard rule violations detected" not in text
    assert "NOT an all-clear" in text
    assert "BEFORE this block is built" in text


def test_every_advisory_the_pipeline_can_raise_renders_as_an_advisory():
    """Whatever new non-blocking rule a future change adds, it classifies
    correctly for free — membership of HARD_BLOCK_RULES is the only test."""
    for rule in (
        "max_sector_pct", "correlation_cluster", "deployment_gap",
        "data_degraded", "analysis_parse_loss", "analysis_field_nulled",
        "correlation_coverage_gap", "pm_audit_step_missing",
        "sector_unresolved_no_sector", "sector_unresolved_lookup_failed",
        "sector_unresolved",
    ):
        text = _findings(rule)
        assert f"ADVISORY (nothing blocked) [{rule}]" in text, rule
        assert "HARD LIMIT" not in text, rule


def test_every_hard_rule_renders_as_a_hard_limit():
    from src.pipeline import HARD_BLOCK_RULES
    for rule in sorted(HARD_BLOCK_RULES):
        text = _findings(rule)
        assert f"HARD LIMIT BREACHED [{rule}]" in text, rule
        assert "ADVISORY" not in text, rule
