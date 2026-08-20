"""Regression tests for the 2026-08-18 15:03 verdict-validation incident.

The Risk Manager APPROVED the day's plan with three halving modifications
but omitted `sizing_sanity` and `overall` from its reasoning_chain. The
RiskVerdict validation failure turned the approval into verdict=None,
RiskStage logged "Risk manager REJECTED trades: parse error", the run
exited with status `rejected` (exit 0, marker written), and the trading
day was over — an approving verdict destroyed by a prose omission.

The fix is one bounded repair reprompt naming the exact validation
errors. A second failure preserves the fail-closed None → reject path.
"""
import json

import pytest

from src.agents.base import AgentResult
from src.agents.portfolio_manager import PortfolioManagerAgent
from src.agents.risk_manager import RiskManagerAgent
from src.models import PortfolioDecision, ReasoningChain

# Abbreviated but structurally identical to the recorded 2026-08-18 verdict:
# approved=true, 3 modifications, reasoning_chain MISSING sizing_sanity+overall.
BROKEN_VERDICT = json.dumps({
    "approved": True,
    "reasoning_chain": {
        "rr_audit": "XLE R/R 0.58 invalid; XLF 1.31 below 1.5 without catalyst.",
        "signal_fidelity": "BUYs align with Tech ratings; entry deviations noted.",
        "correlation_check": "JPM + XLF correlated but within limits.",
        "event_risk": "No imminent earnings or FOMC for proposed names.",
    },
    "modifications": [
        {"symbol": "XLE", "field": "allocation_pct", "original_value": 5.0,
         "new_value": 2.5, "reason": "R/R < 1.0 violates minimum discipline."},
        {"symbol": "XLF", "field": "allocation_pct", "original_value": 5.0,
         "new_value": 2.5, "reason": "R/R < 1.5 without catalyst."},
        {"symbol": "JPM", "field": "allocation_pct", "original_value": 10.0,
         "new_value": 7.5, "reason": "Stop too tight; noise stop-out risk."},
    ],
    "scale_all_buys": 1.0,
    "reason_category": "rr_fail",
    "reasoning": "Modifications applied; no veto — PM directionally sound.",
})

FIXED_VERDICT = json.loads(BROKEN_VERDICT)
FIXED_VERDICT["reasoning_chain"]["sizing_sanity"] = (
    "Post-modification sizes proportional to conviction and R/R."
)
FIXED_VERDICT["reasoning_chain"]["overall"] = (
    "Plan approved with three size reductions reflecting degraded R/R."
)
FIXED_VERDICT = json.dumps(FIXED_VERDICT)


def _result(raw: str, user_message: str = "original input") -> AgentResult:
    return AgentResult(
        raw_text=raw, tokens_used=0, model="test", user_message=user_message,
    )


def _rm() -> RiskManagerAgent:
    return RiskManagerAgent.__new__(RiskManagerAgent)


def _review(agent):
    chain = ReasoningChain(
        macro_filter="m", news_check="n", earnings_check="e",
        signal_conflicts="s", sizing_logic="z", portfolio_balance="b",
        cash_target="c",
    )
    return agent.review(
        portfolio_decision=PortfolioDecision(
            reasoning_chain=chain, targets=[], portfolio_view="v",
        ),
        positions=[], macro_summary={}, rule_violations=[],
    )


def test_incomplete_approving_verdict_is_repaired(monkeypatch):
    agent = _rm()
    calls: list[str] = []
    monkeypatch.setattr(
        RiskManagerAgent, "run",
        lambda self, **kw: _result(BROKEN_VERDICT), raising=False,
    )

    def fake_execute(self, user_message):
        calls.append(user_message)
        return _result(FIXED_VERDICT, user_message=user_message)

    monkeypatch.setattr(RiskManagerAgent, "_execute", fake_execute, raising=False)

    verdict, result = _review(agent)

    assert verdict is not None, "repair must recover the approving verdict"
    assert verdict.approved is True
    assert len(verdict.modifications) == 3
    assert len(calls) == 1, "exactly one repair call"
    assert "SCHEMA REPAIR REQUIRED" in calls[0]
    assert "sizing_sanity" in calls[0], "repair coda must name the missing field"
    assert "original input" in calls[0], "repair must replay the original context"


def test_repair_failure_stays_fail_closed(monkeypatch):
    agent = _rm()
    monkeypatch.setattr(
        RiskManagerAgent, "run",
        lambda self, **kw: _result(BROKEN_VERDICT), raising=False,
    )
    # Second attempt is broken too (still missing the fields).
    monkeypatch.setattr(
        RiskManagerAgent, "_execute",
        lambda self, user_message: _result(BROKEN_VERDICT), raising=False,
    )

    verdict, _ = _review(agent)
    assert verdict is None, "double validation failure must stay fail-closed"


def test_repair_returning_garbage_stays_fail_closed(monkeypatch):
    agent = _rm()
    monkeypatch.setattr(
        RiskManagerAgent, "run",
        lambda self, **kw: _result(BROKEN_VERDICT), raising=False,
    )
    monkeypatch.setattr(
        RiskManagerAgent, "_execute",
        lambda self, user_message: _result("I cannot comply."), raising=False,
    )

    verdict, _ = _review(agent)
    assert verdict is None


def test_pm_decision_missing_chain_field_is_repaired(monkeypatch):
    broken = json.dumps({
        "reasoning_chain": {
            "macro_filter": "m", "news_check": "n", "earnings_check": "e",
            "signal_conflicts": "s", "sizing_logic": "z",
            "portfolio_balance": "b",
            # cash_target (mandatory, min_length=1) omitted
        },
        "targets": [{
            "symbol": "XLE", "target_weight_pct": 5.0, "conviction": "medium",
            "thesis": "energy tailwind", "thesis_invalid_if": "", "catalyst": "",
        }],
        "portfolio_view": "small energy book",
    })
    fixed_obj = json.loads(broken)
    fixed_obj["reasoning_chain"]["cash_target"] = "cash 95% by design"
    fixed = json.dumps(fixed_obj)

    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    monkeypatch.setattr(
        PortfolioManagerAgent, "run",
        lambda self, **kw: _result(broken), raising=False,
    )
    monkeypatch.setattr(
        PortfolioManagerAgent, "_execute",
        lambda self, user_message: _result(fixed, user_message=user_message),
        raising=False,
    )

    decision, _ = agent.decide(analyses=[], positions=[])
    assert decision is not None
    assert len(decision.targets) == 1
    assert decision.reasoning_chain.cash_target == "cash 95% by design"
