"""Deterministic PM provenance/holding boundary and production-scale context."""

import json
from datetime import date

from unittest.mock import patch

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.agents.base import AgentResult
from src.models import (
    NewsIntelligenceReport, PortfolioDecision, Position, TechAnalysisResult,
    TechReasoningChain,
)


def _analysis(symbol: str, rating: str = "buy") -> TechAnalysisResult:
    buy = rating in {"buy", "strong_buy"}
    return TechAnalysisResult(
        symbol=symbol, rating=rating, conviction="medium", entry_price=100,
        stop_loss=95 if buy else 105, reference_target=112 if buy else 88,
        reasoning="validated production-like trend and momentum evidence",
        reasoning_chain=TechReasoningChain(
            trend="daily trend", momentum="momentum", volatility="ATR",
            volume="volume", support_resistance="levels",
        ),
    )


def _decision(target: dict, conflicts: str = "Explicit source audit.") -> PortfolioDecision:
    return PortfolioDecision.model_validate({
        "reasoning_chain": {
            "macro_filter": "Macro checked.", "news_check": "News checked.",
            "earnings_check": "Earnings checked.", "signal_conflicts": conflicts,
            "sizing_logic": "Sizing checked.", "portfolio_balance": "Book checked.",
            "cash_target": "Cash checked.",
        },
        "targets": [target], "portfolio_view": "Grounded target only.",
    })


def test_pm_allows_explicit_disagreement_but_rejects_false_alignment():
    target = {
        "symbol": "AAPL", "target_weight_pct": 5, "conviction": "medium",
        "thesis": "PM disagrees with the bearish technical signal.",
        "provenance": [{
            "source": "technical", "observed_stance": "sell",
            "relationship": "conflicts", "evidence": "Catalyst outweighs trend",
        }],
    }
    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target), analyses=[_analysis("AAPL", "sell")], positions=[],
        news_intel=None, earnings_analyses=[], macro_analysis=None,
        total_value=100_000,
    )
    assert errors == []

    target["provenance"][0]["relationship"] = "supports"
    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target), analyses=[_analysis("AAPL", "sell")], positions=[],
        news_intel=None, earnings_analyses=[], macro_analysis=None,
        total_value=100_000,
    )
    assert any("does not support" in error for error in errors)


def test_pm_grounding_allows_transient_symbol_only_when_allowlisted_and_analyzed():
    target = {
        "symbol": "VST", "target_weight_pct": 5, "conviction": "medium",
        "thesis": "Temporary SEC admission still has validated Technical support.",
        "provenance": [{
            "source": "technical", "observed_stance": "buy",
            "relationship": "supports", "evidence": "current-run trend",
        }],
    }
    kwargs = dict(
        analyses=[_analysis("VST")], positions=[], news_intel=None,
        earnings_analyses=[], macro_analysis=None, total_value=100_000,
    )
    assert PortfolioManagerAgent.validate_grounding(
        _decision(target), allowed_buy_symbols={"AAPL", "VST"}, **kwargs,
    ) == []

    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target), allowed_buy_symbols={"AAPL"}, **kwargs,
    )
    assert any("temporary-admission allowlist" in error for error in errors)


def test_pm_grounding_rejects_allowlisted_increase_without_current_technical():
    target = {
        "symbol": "VST", "target_weight_pct": 5, "conviction": "medium",
        "thesis": "Smart Money alone cannot open the position.",
        "provenance": [{
            "source": "smart_money", "observed_stance": "bullish",
            "relationship": "supports", "evidence": "material purchase",
        }],
    }
    from src.models import SmartMoneyFinding, SmartMoneyObservation
    observation = SmartMoneyObservation(
        symbol="VST", actor="Example Insider", direction="buy",
        transaction_date=date.today(), disclosure_date=date.today(),
        source_url="https://www.sec.gov/example", lag_days=0,
        disclosure_age_days=0, freshness="fresh",
        economic_role="confirmatory",
    )
    finding = SmartMoneyFinding(
        symbol="VST", stance="bullish", economic_role="confirmatory",
        summary="material purchase", why_now="new SEC filing",
        observations=[observation],
    )
    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target), analyses=[], positions=[], news_intel=None,
        earnings_analyses=[], macro_analysis=None,
        smart_money_findings=[finding], total_value=100_000,
        allowed_buy_symbols={"VST"},
    )
    assert any("lacks a current-run Technical analysis" in error for error in errors)


def test_pm_rejects_invented_coverage_phantom_exit_and_unproved_ratio():
    target = {
        "symbol": "MSFT", "target_weight_pct": 0, "conviction": "low",
        "thesis": "Close because 4/4 signals aligned.",
        "provenance": [{
            "source": "news", "observed_stance": "bearish",
            "relationship": "supports", "evidence": "claimed headline",
        }],
    }
    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target, "MSFT: 4/4 aligned."),
        analyses=[_analysis("MSFT")], positions=[], news_intel=None,
        earnings_analyses=[], macro_analysis=None, total_value=100_000,
    )
    assert any("not an actual holding" in error for error in errors)
    assert any("coverage that does not exist" in error for error in errors)
    assert any("claims denominator 4" in error for error in errors)


def test_pm_cannot_bypass_grounding_with_legacy_concrete_decisions():
    decision = PortfolioDecision.model_validate({
        "reasoning_chain": {
            "macro_filter": "m", "news_check": "n", "earnings_check": "e",
            "signal_conflicts": "s", "sizing_logic": "z",
            "portfolio_balance": "b", "cash_target": "c",
        },
        "decisions": [{
            "action": "BUY", "symbol": "AAPL", "allocation_pct": 5,
            "entry_price": 100, "stop_loss": 95, "take_profit": 110,
            "reasoning": "bypass targets",
        }],
        "portfolio_view": "legacy bypass",
    })
    errors = PortfolioManagerAgent.validate_grounding(
        decision, analyses=[_analysis("AAPL")], positions=[], news_intel=None,
        earnings_analyses=[], macro_analysis=None, total_value=100_000,
    )
    assert any("only grounded targets" in error for error in errors)


def test_pm_free_text_does_not_override_structured_provenance():
    target = {
        "symbol": "AAPL", "target_weight_pct": 5, "conviction": "medium",
        "thesis": "Technical setup supports the target.",
        "provenance": [{
            "source": "technical", "observed_stance": "buy",
            "relationship": "supports", "evidence": "validated buy rating",
        }],
    }
    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target, "AAPL: technical buy and news bullish."),
        analyses=[_analysis("AAPL")], positions=[], news_intel=None,
        earnings_analyses=[], macro_analysis=None, total_value=100_000,
    )
    # Free-form prose is not machine interpreted.  The exact structured
    # provenance is the enforceable boundary and remains grounded.
    assert errors == []


def test_pm_may_truthfully_state_that_symbol_coverage_is_absent():
    target = {
        "symbol": "AAPL", "target_weight_pct": 5, "conviction": "medium",
        "thesis": "Technical setup supports the target; no news/earnings available.",
        "provenance": [{
            "source": "technical", "observed_stance": "buy",
            "relationship": "supports", "evidence": "validated buy rating",
        }],
    }
    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target, "AAPL: tech=buy, news=n/a, earnings=unavailable."),
        analyses=[_analysis("AAPL")], positions=[], news_intel=None,
        earnings_analyses=[], macro_analysis=None, total_value=100_000,
    )
    assert errors == []


def test_queued_earnings_is_absent_evidence_not_directional_none():
    """Production run-76bd4e83: ``None`` must not become stance ``'none'``.

    A just-filed placeholder has no completed earnings analysis. It stays in
    the human-readable prompt as unavailable context, but must not enter the
    authoritative provenance registry or force a false directional label.
    """
    analyses = [_analysis("AMR", "strong_buy")]
    queued_earnings = [{
        "symbol": "AMR",
        "queued": True,
        "form_type": "10-Q",
        "filing_date": "2026-08-07",
        "analysis": None,
    }]

    assert PortfolioManagerAgent._collapse_stances([None]) is None
    assert PortfolioManagerAgent._collapse_stances(["unavailable"]) is None

    registry = PortfolioManagerAgent.build_evidence_registry(
        analyses=analyses,
        positions=[],
        news_intel=None,
        earnings_analyses=queued_earnings,
        macro_analysis=None,
    )
    assert registry == {"AMR": {"technical": "strong_buy"}}

    target = {
        "symbol": "AMR", "target_weight_pct": 5, "conviction": "medium",
        "thesis": "Current Technical strength supports a bounded starter.",
        "provenance": [{
            "source": "technical", "observed_stance": "strong_buy",
            "relationship": "supports", "evidence": "current-run trend",
        }],
    }
    assert PortfolioManagerAgent.validate_grounding(
        _decision(target),
        analyses=analyses,
        positions=[],
        news_intel=None,
        earnings_analyses=queued_earnings,
        macro_analysis=None,
        total_value=100_000,
        allowed_buy_symbols={"AMR"},
    ) == []

    target["provenance"].append({
        "source": "earnings", "observed_stance": "none",
        "relationship": "context", "evidence": "not analyzed yet",
    })
    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target),
        analyses=analyses,
        positions=[],
        news_intel=None,
        earnings_analyses=queued_earnings,
        macro_analysis=None,
        total_value=100_000,
        allowed_buy_symbols={"AMR"},
    )
    assert any("earnings coverage that does not exist" in error for error in errors)


def test_grounding_failure_returns_original_result_without_another_llm_call(monkeypatch):
    base = {
        "reasoning_chain": {
            "macro_filter": "m", "news_check": "n", "earnings_check": "e",
            "signal_conflicts": "AAPL: technical buy and news bullish.",
            "sizing_logic": "z", "portfolio_balance": "b", "cash_target": "c",
        },
        "targets": [{
            "symbol": "AAPL", "target_weight_pct": 5, "conviction": "medium",
            "thesis": "Technical support is claimed, with a phantom news claim.",
            "provenance": [{
                "source": "news", "observed_stance": "bullish",
                "relationship": "supports", "evidence": "phantom news",
            }],
        }],
        "portfolio_view": "Selective long.",
    }
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    first = AgentResult(raw_text=json.dumps(base), tokens_used=1, model="test", user_message="input")
    monkeypatch.setattr(PortfolioManagerAgent, "run", lambda self, **kwargs: first)
    execute_calls = []
    monkeypatch.setattr(
        PortfolioManagerAgent, "_execute",
        lambda self, message: execute_calls.append(message),
    )
    decision, result = agent.decide(analyses=[_analysis("AAPL")], positions=[])
    assert decision is None
    assert result is first
    assert execute_calls == []


def test_production_scale_pm_prompt_and_grounding_contract():
    """Observed production scale: 30 candidates, 15 holdings, memory layers."""
    symbols = [
        "SPY", "QQQ", "IWM", "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
        "META", "AVGO", "AMD", "ORCL", "MU", "JPM", "GS", "V", "MA",
        "UNH", "LLY", "XOM", "CVX", "COST", "WMT", "CAT", "GE", "BA",
        "NEE", "VST", "CEG", "BRK-B",
    ]
    analyses = [_analysis(symbol) for symbol in symbols]
    positions = [
        Position(
            symbol=symbol, qty=10, avg_entry=90, current_price=100,
            market_value=1000, unrealized_pnl=100, sector="Diversified",
        )
        for symbol in symbols[:15]
    ]
    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="test")
        message = agent.build_user_message(
            analyses=analyses, positions=positions,
            macro_analysis={"regime": "risk_on", "equity_outlook": "bullish"},
            cash_balance=50_000, total_value=100_000,
            weekly_narrative="Seven-day portfolio narrative. " * 80,
            macro_trajectory="Regime trajectory evidence. " * 80,
            active_state_changes="Current state change. " * 80,
            pm_recent_decisions="Prior grounded target. " * 80,
            rm_recent_verdicts="Prior risk verdict. " * 80,
        )
    assert len(message) > 18_000
    assert all(f"- {symbol}:" in message for symbol in symbols)

    target = {
        "symbol": "BRK-B", "target_weight_pct": 5, "conviction": "medium",
        "thesis": "Technical buy supports a bounded starter.",
        "provenance": [{
            "source": "technical", "observed_stance": "buy",
            "relationship": "supports", "evidence": "validated buy rating",
        }],
    }
    assert PortfolioManagerAgent.validate_grounding(
        _decision(target), analyses=analyses, positions=positions,
        news_intel=None, earnings_analyses=[],
        macro_analysis={"regime": "risk_on", "equity_outlook": "bullish"},
        total_value=100_000,
    ) == []


def test_pm_rejects_historical_smart_money_as_support():
    from src.models import SmartMoneyFinding, SmartMoneyObservation
    finding = SmartMoneyFinding(
        symbol="AAPL", stance="bullish", economic_role="historical",
        summary="one disclosed purchase", why_now="new disclosure",
        observations=[SmartMoneyObservation(
            symbol="AAPL", actor="Example Member", direction="buy",
            transaction_date=date(2026, 6, 1), disclosure_date=date(2026, 7, 11),
            source_url="https://example.test/filing", lag_days=40,
            disclosure_age_days=20,
            freshness="stale", economic_role="historical",
        )],
    )
    target = {
        "symbol": "AAPL", "target_weight_pct": 5, "conviction": "medium",
        "thesis": "Technical trend plus congressional context.",
        "provenance": [
            {"source": "technical", "observed_stance": "buy", "relationship": "supports", "evidence": "trend"},
            {"source": "smart_money", "observed_stance": "bullish", "relationship": "supports", "evidence": "disclosure"},
        ],
    }
    errors = PortfolioManagerAgent.validate_grounding(
        _decision(target), analyses=[_analysis("AAPL", "buy")], positions=[],
        news_intel=None, earnings_analyses=[], macro_analysis=None,
        smart_money_findings=[finding], total_value=100_000,
    )
    assert any("historical smart-money evidence cannot support" in error for error in errors)
