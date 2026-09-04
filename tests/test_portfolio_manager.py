import pytest
import json
from unittest.mock import patch, MagicMock
from src.agents.portfolio_manager import PortfolioManagerAgent
from src.models import TechAnalysisResult, TechReasoningChain, Position


def _tech_rc() -> TechReasoningChain:
    return TechReasoningChain(
        trend="x", momentum="x", volatility="x", volume="x",
        support_resistance="x",
    )


@pytest.fixture
def sample_analyses():
    return [
        # 507 / 490 / 540 is reward:risk 1.94. The old 530 target made it
        # 1.35 — accidentally below the floor, chosen before the PM layer
        # enforced one. Since 2026-09-02 a sub-floor target without a
        # catalyst that resolves to an Active News State Change row is
        # dropped (`_apply_subfloor_catalyst_rule`), which silently turned
        # every test in this file into a test of THAT rule instead of the
        # one it was written for. See tests/test_subfloor_catalyst_gate.py
        # for the rule's own coverage.
        TechAnalysisResult(
            symbol="SPY", rating="buy", entry_price=507.0,
            reference_target=540.0, stop_loss=490.0,
            support_levels=[490.0], resistance_levels=[540.0],
            setup_type="range", expected_horizon_sessions=10,
            reasoning="Strong uptrend", reasoning_chain=_tech_rc(),
        ),
        TechAnalysisResult(
            symbol="QQQ", rating="neutral", entry_price=None,
            reference_target=None, stop_loss=None,
            reasoning="Mixed signals", reasoning_chain=_tech_rc(),
        ),
    ]


@pytest.fixture
def sample_positions():
    return [
        Position(
            symbol="AAPL", qty=5, avg_entry=180.0, current_price=190.0,
            market_value=950.0, unrealized_pnl=50.0, sector="Technology",
        ),
    ]


@pytest.fixture
def sample_macro():
    return {
        "vix": {"current": 18.0, "mean_5d": 17.5, "trend": "falling"},
        "treasury": {"us2y": 4.5, "us10y": 4.3, "spread_2_10": -0.2, "inverted": True},
        "fed_funds_rate": 5.25,
    }


@pytest.fixture
def mock_pm_response():
    return json.dumps({
        "reasoning_chain": {
            "macro_filter": "risk-on regime, tech overweight",
            "news_check": "no fresh HIGH bearish state changes",
            "earnings_check": "SPY n/a, AAPL filing intact",
            "signal_conflicts": "SPY: technical signal supports the target",
            "sizing_logic": "high conviction → 10%",
            "portfolio_balance": "Tech 60% well under 40% cap",
            "cash_target": "current 50% → after ~40%, fine for risk-on",
        },
        "targets": [
            {
                "symbol": "SPY",
                "target_weight_pct": 10.0,
                "conviction": "high",
                "thesis": "Technical setup supports a target.",
                "provenance": [{
                    "source": "technical", "observed_stance": "buy",
                    "relationship": "supports", "evidence": "validated buy rating",
                }],
            }
        ],
        "portfolio_view": "Cautiously bullish, 60% invested",
    })


@patch("anthropic.Anthropic")
def test_portfolio_manager_decide(mock_cls, sample_analyses, sample_positions, sample_macro, mock_pm_response):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=mock_pm_response)]
    mock_response.usage.input_tokens = 1000
    mock_response.usage.output_tokens = 300
    mock_client.messages.create.return_value = mock_response
    mock_cls.return_value = mock_client

    agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6-20250725")
    result, agent_result = agent.decide(
        analyses=sample_analyses,
        positions=sample_positions,
        macro_analysis=sample_macro,
        cash_balance=5000.0,
        total_value=10000.0,
    )

    assert result is not None
    assert len(result.targets) == 1
    assert result.targets[0].symbol == "SPY"
    assert agent_result.tokens_used > 0
    assert agent_result.user_message != ""


@patch("anthropic.Anthropic")
def test_portfolio_manager_bad_response(mock_cls, sample_analyses, sample_positions, sample_macro):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="Let me think about this...")]
    mock_response.usage.input_tokens = 1000
    mock_response.usage.output_tokens = 100
    mock_client.messages.create.return_value = mock_response
    mock_cls.return_value = mock_client

    agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6-20250725")
    result, agent_result = agent.decide(
        analyses=sample_analyses,
        positions=sample_positions,
        macro_analysis=sample_macro,
        cash_balance=5000.0,
        total_value=10000.0,
    )
    assert result is None
    assert agent_result is not None


# ---------------------------------------------------------------------------
# Per-entry isolation for targets (mirrors PR #73/#74 pattern)
# Highest blast radius of any agent's per-entry isolation gap: a bad target
# wipes the whole PortfolioDecision → reasoning_chain + portfolio_view + every
# other target lost → entire morning session executes 0 trades.
# ---------------------------------------------------------------------------

def _valid_pm_targets_json() -> dict:
    return {
        "reasoning_chain": {
            "macro_filter": "Risk-on regime, VIX falling.",
            "news_check": "AI capex narrative intact.",
            "earnings_check": "AAPL strong, NVDA truncated.",
            "signal_conflicts": "NVDA: 3/4 aligned. AAPL: thesis weakening.",
            "sizing_logic": "JPM 10%, NVDA 8%.",
            "portfolio_balance": "Tech 32%, no sector > 40%.",
            "cash_target": "After targets ~15% cash.",
            "continuity_check": "5-day risk-on arc intact.",
        },
        "targets": [],
        "portfolio_view": "Moderately bullish.",
    }


def _valid_target(symbol: str = "NVDA", weight: float = 8.0) -> dict:
    return {
        "symbol": symbol,
        "target_weight_pct": weight,
        "conviction": "high",
        "thesis": "Technical setup supports the target.",
        "thesis_invalid_if": "Price closes below MA50.",
        "provenance": [{
            "source": "technical", "observed_stance": "buy",
            "relationship": "supports", "evidence": "validated buy rating",
        }],
    }


def test_drop_invalid_targets_strips_overweight_keeps_rest():
    """A TargetPosition with target_weight_pct > 25 fails the schema's
    Field(le=25.0) constraint. Must be dropped individually so the rest
    of the morning's decisions still execute."""
    parsed = _valid_pm_targets_json()
    parsed["targets"] = [
        _valid_target("NVDA", 8.0),
        _valid_target("AMZN", 12.0),
        {**_valid_target("BAD", 30.0)},  # over 25% cap
        _valid_target("JPM", 6.0),
    ]
    out = PortfolioManagerAgent._drop_invalid_targets(parsed)
    syms = [t["symbol"] for t in out["targets"]]
    assert syms == ["NVDA", "AMZN", "JPM"]


def test_drop_invalid_targets_strips_negative_weight():
    parsed = _valid_pm_targets_json()
    parsed["targets"] = [
        _valid_target("NVDA", 8.0),
        {**_valid_target("BAD"), "target_weight_pct": -5.0},  # below ge=0
        _valid_target("JPM", 6.0),
    ]
    out = PortfolioManagerAgent._drop_invalid_targets(parsed)
    syms = [t["symbol"] for t in out["targets"]]
    assert syms == ["NVDA", "JPM"]


def test_drop_invalid_targets_strips_missing_required_field():
    """thesis is required (no default) — a target without it must be dropped."""
    parsed = _valid_pm_targets_json()
    parsed["targets"] = [
        _valid_target("NVDA", 8.0),
        {"symbol": "BAD", "target_weight_pct": 5.0, "conviction": "low"},  # no thesis
        _valid_target("JPM", 6.0),
    ]
    out = PortfolioManagerAgent._drop_invalid_targets(parsed)
    syms = [t["symbol"] for t in out["targets"]]
    assert syms == ["NVDA", "JPM"]


def test_portfolio_decision_constructs_after_dropping_bad_target():
    """End-to-end: with the malformed target stripped, PortfolioDecision
    constructs and preserves reasoning_chain + portfolio_view + the OTHER
    targets so morning still executes the valid trades."""
    from src.models import PortfolioDecision

    parsed = _valid_pm_targets_json()
    parsed["targets"] = [
        _valid_target("NVDA", 8.0),
        {**_valid_target("BAD"), "target_weight_pct": 99.0},  # invalid
        _valid_target("JPM", 6.0),
    ]
    cleaned = PortfolioManagerAgent._drop_invalid_targets(parsed)
    decision = PortfolioDecision(**cleaned)
    assert decision.portfolio_view == "Moderately bullish."
    assert len(decision.targets) == 2
    assert {t.symbol for t in decision.targets} == {"NVDA", "JPM"}


def test_drop_invalid_targets_handles_non_list_shape():
    parsed = _valid_pm_targets_json()
    parsed["targets"] = "oops not a list"
    out = PortfolioManagerAgent._drop_invalid_targets(parsed)
    assert out["targets"] == []


def test_drop_invalid_targets_drops_non_dict_items():
    parsed = _valid_pm_targets_json()
    parsed["targets"] = [
        _valid_target("NVDA"),
        "stray string",
        None,
        _valid_target("JPM"),
    ]
    out = PortfolioManagerAgent._drop_invalid_targets(parsed)
    syms = [t["symbol"] for t in out["targets"]]
    assert syms == ["NVDA", "JPM"]


@patch("anthropic.Anthropic")
def test_pm_decide_survives_one_malformed_target(mock_cls, sample_analyses, sample_positions, sample_macro):
    """End-to-end: morning's PM survives one bad target row. Pre-fix this
    would silence the entire morning — 0 trades executed even though 4 of 5
    targets were valid."""
    payload = _valid_pm_targets_json()
    payload["targets"] = [
        _valid_target("SPY", 10.0),
        {**_valid_target("BAD"), "target_weight_pct": 50.0},  # invalid
    ]
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=json.dumps(payload))]
    mock_response.usage.input_tokens = 100
    mock_response.usage.output_tokens = 50
    mock_client.messages.create.return_value = mock_response
    mock_cls.return_value = mock_client

    agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6-20250725")
    decision, _ = agent.decide(
        analyses=sample_analyses,
        positions=sample_positions,
        macro_analysis=sample_macro,
        cash_balance=5000.0,
        total_value=10000.0,
    )

    assert decision is not None, "decision must survive one bad target"
    assert decision.portfolio_view == "Moderately bullish."
    assert len(decision.targets) == 1
    assert decision.targets[0].symbol == "SPY"


# ===========================================================================
# Item 18 (2026-09-04) — "THE ANALYSTS DO NOT CONCLUDE, THEY TRANSCRIBE".
# The earnings seat used to courier its whole eight-field extraction form
# into PM's prompt (~1,400 chars/filing, 70% of a 200k-char prompt across
# ~35 filings/day). PM's rendered prompt text must now carry only a short
# verdict — call, conviction, thesis, and a pointer to the full record —
# while the full extraction stays computed/stored for audit elsewhere
# (proven in tests/test_earnings_analyst.py, not here).
# ===========================================================================

def _full_earnings_analysis(
    symbol="AAPL", sentiment="bullish", conviction="medium",
    bull_case="not disclosed", bear_case="Competition erodes margins.",
):
    return {
        "symbol": symbol, "form_type": "10-Q", "filing_date": "2026-08-01",
        "revenue": {"total": "$10.0 billion", "yoy_growth": "+5%"},
        "profitability": {"gross_margin": "45%", "operating_margin": "20%", "eps": "$1.00"},
        "cash_flow": {"operating_cf": "$3.0 billion"},
        "balance_sheet": {"cash_and_equivalents": "$4.0 billion"},
        "guidance": "Management did not provide numeric guidance",
        "strategic_direction": {
            "key_initiatives": ["Expanding into cloud services"],
            "competitive_positioning": "Market leader with 35% share",
        },
        "risk_flags": {
            "strategic_risks": ["Cloud expansion faces entrenched competitors"],
            "operational_risks": ["FX volatility remains a headwind"],
        },
        "strategy_consistency": "Consistent with prior quarter",
        "investment_implications": {
            "sentiment": sentiment, "conviction": conviction,
            "reasoning_chain": {
                "fundamental_quality": "Revenue +5% with margin expansion",
                "growth_trajectory": "Operating leverage building QoQ",
                "strategic_risks": "Cloud competition is real but execution on track",
                "management_execution": "Guidance hit, capex on plan",
                "valuation_context": "Trades at a reasonable forward multiple",
            },
            "key_thesis": (
                "Margins expanded to 45% while demand stayed resilient across "
                "core products. This holds as long as cloud investment keeps "
                "paying back in gross margin, not just top-line growth."
            ),
            "bull_case": bull_case, "bear_case": bear_case,
        },
        "data_quality": "Filing text complete through MD&A.",
    }


@patch("anthropic.Anthropic")
def test_earnings_section_renders_short_verdict_not_the_eight_field_form(mock_cls):
    """The PM prompt's earnings section must show the short verdict shape
    (call / thesis / invalidation / pointer) and must NOT reproduce the old
    form-filling labels (`Filing metrics:`, `Competitive positioning:`,
    `Strategy consistency:`, `Analyst synthesis:`) that used to make this
    section ~1,400 chars per filing regardless of how much judgement it
    actually contained.
    """
    agent = PortfolioManagerAgent(api_key="test", model="test-model")
    ea = {
        "symbol": "AAPL", "analysis": _full_earnings_analysis(),
        "is_new": True, "form_type": "10-Q", "filing_date": "2026-08-01",
        "analysis_path": "/data/earnings/AAPL/analysis_10-Q_2026-08-01.md",
    }

    msg = agent.build_user_message(
        analyses=[], positions=[], cash_balance=1000.0, total_value=1000.0,
        earnings_analyses=[ea],
    )

    start = msg.find("## Earnings Analysis")
    end = msg.find("\n## ", start + 3)
    section = msg[start: end if end != -1 else len(msg)]

    # The short verdict IS there.
    assert "Call: bullish (medium)" in section
    assert "Margins expanded to 45%" in section
    assert "Invalidated if: Competition erodes margins." in section
    assert "/data/earnings/AAPL/analysis_10-Q_2026-08-01.md" in section

    # The old eight-field form is NOT there.
    for old_label in (
        "Filing metrics:", "Filing guidance:", "Competitive positioning:",
        "Strategy consistency:", "Analyst synthesis:", "Data quality:",
        "Strategic risks:", "Operational risks:",
    ):
        assert old_label not in section, f"old form label {old_label!r} leaked into PM prompt"

    # And the section is a small fraction of its old ~1,400 chars/filing.
    assert len(section) < 700


@patch("anthropic.Anthropic")
def test_earnings_section_falls_back_when_falsifier_undisclosed(mock_cls):
    """`AnalystVerdict` (Phase 13 ranking shape) REFUSES to construct a
    directional call with no stated invalidation — correct for ranking,
    where an unfalsifiable call should never win a slot. But dropping the
    seat's view from PM's prompt entirely over the same gap would leave PM
    blind on that name rather than just under-informed, which is a worse
    outcome for a PROMPT than for a ranking score. `_render_earnings_verdict`
    must fall back to the raw sentiment/conviction/key_thesis fields instead
    of vanishing the filing from the prompt.
    """
    agent = PortfolioManagerAgent(api_key="test", model="test-model")
    ea = {
        "symbol": "ORCL",
        "analysis": _full_earnings_analysis(
            symbol="ORCL", sentiment="bullish",
            bull_case="not disclosed", bear_case="not disclosed",
        ),
        "is_new": False, "form_type": "10-Q", "filing_date": "2026-08-01",
    }

    msg = agent.build_user_message(
        analyses=[], positions=[], cash_balance=1000.0, total_value=1000.0,
        earnings_analyses=[ea],
    )

    start = msg.find("## Earnings Analysis")
    end = msg.find("\n## ", start + 3)
    section = msg[start: end if end != -1 else len(msg)]

    assert "Call: bullish (medium)" in section
    assert "Invalidated if: not disclosed by the analyst" in section
    # No pointer was supplied on the wrapper — a locatable reference is
    # still shown rather than nothing at all.
    assert "ORCL" in section
