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


# ===========================================================================
# External review: schema repair must never become a re-decision.
# ===========================================================================

def test_repair_that_flips_approved_fails_closed(monkeypatch):
    """If the repaired response changes `approved`, that is a re-decision,
    not a schema completion — must fail closed even though the repair
    otherwise parses cleanly."""
    agent = _rm()
    monkeypatch.setattr(
        RiskManagerAgent, "run",
        lambda self, **kw: _result(BROKEN_VERDICT), raising=False,
    )
    flipped = json.loads(FIXED_VERDICT)
    flipped["approved"] = False
    monkeypatch.setattr(
        RiskManagerAgent, "_execute",
        lambda self, user_message: _result(json.dumps(flipped)),
        raising=False,
    )

    verdict, _ = _review(agent)
    assert verdict is None, "changed `approved` must fail closed, not be accepted"


def test_repair_that_changes_a_modification_value_fails_closed(monkeypatch):
    agent = _rm()
    monkeypatch.setattr(
        RiskManagerAgent, "run",
        lambda self, **kw: _result(BROKEN_VERDICT), raising=False,
    )
    tampered = json.loads(FIXED_VERDICT)
    tampered["modifications"][0]["new_value"] = 9.9  # was 2.5
    monkeypatch.setattr(
        RiskManagerAgent, "_execute",
        lambda self, user_message: _result(json.dumps(tampered)),
        raising=False,
    )

    verdict, _ = _review(agent)
    assert verdict is None


def test_repair_that_changes_reason_category_fails_closed(monkeypatch):
    agent = _rm()
    monkeypatch.setattr(
        RiskManagerAgent, "run",
        lambda self, **kw: _result(BROKEN_VERDICT), raising=False,
    )
    tampered = json.loads(FIXED_VERDICT)
    tampered["reason_category"] = "clean"  # was rr_fail
    monkeypatch.setattr(
        RiskManagerAgent, "_execute",
        lambda self, user_message: _result(json.dumps(tampered)),
        raising=False,
    )

    verdict, _ = _review(agent)
    assert verdict is None


def test_repair_that_changes_scale_all_buys_fails_closed(monkeypatch):
    agent = _rm()
    monkeypatch.setattr(
        RiskManagerAgent, "run",
        lambda self, **kw: _result(BROKEN_VERDICT), raising=False,
    )
    tampered = json.loads(FIXED_VERDICT)
    tampered["scale_all_buys"] = 0.5  # was 1.0
    monkeypatch.setattr(
        RiskManagerAgent, "_execute",
        lambda self, user_message: _result(json.dumps(tampered)),
        raising=False,
    )

    verdict, _ = _review(agent)
    assert verdict is None


def test_repair_reordering_modifications_is_still_accepted(monkeypatch):
    """Order-insensitive comparison: reordering the SAME modifications is
    not a re-decision and must not spuriously fail closed."""
    agent = _rm()
    monkeypatch.setattr(
        RiskManagerAgent, "run",
        lambda self, **kw: _result(BROKEN_VERDICT), raising=False,
    )
    reordered = json.loads(FIXED_VERDICT)
    reordered["modifications"] = list(reversed(reordered["modifications"]))
    monkeypatch.setattr(
        RiskManagerAgent, "_execute",
        lambda self, user_message: _result(json.dumps(reordered)),
        raising=False,
    )

    verdict, _ = _review(agent)
    assert verdict is not None
    assert verdict.approved is True


def test_decision_bearing_validation_error_skips_repair_entirely(monkeypatch):
    """`approved` missing entirely is a decision-bearing validation
    failure — must fail closed WITHOUT attempting a repair call."""
    agent = _rm()
    missing_approved = json.dumps({
        "reasoning_chain": {
            "rr_audit": "a", "signal_fidelity": "b", "correlation_check": "c",
            "event_risk": "d", "sizing_sanity": "e", "overall": "f",
        },
        "modifications": [], "scale_all_buys": 1.0, "reason_category": "clean",
        "reasoning": "no approved field",
    })
    monkeypatch.setattr(
        RiskManagerAgent, "run",
        lambda self, **kw: _result(missing_approved), raising=False,
    )
    execute_calls: list[str] = []
    monkeypatch.setattr(
        RiskManagerAgent, "_execute",
        lambda self, user_message: execute_calls.append(user_message) or _result("{}"),
        raising=False,
    )

    verdict, _ = _review(agent)
    assert verdict is None
    assert execute_calls == [], "repair must not be attempted for a decision-bearing failure"


def test_pm_repair_that_changes_target_weight_fails_closed(monkeypatch):
    broken = json.dumps({
        "reasoning_chain": {
            "macro_filter": "m", "news_check": "n", "earnings_check": "e",
            "signal_conflicts": "s", "sizing_logic": "z",
            "portfolio_balance": "b",
        },
        "targets": [{
            "symbol": "XLE", "target_weight_pct": 5.0, "conviction": "medium",
            "thesis": "energy tailwind", "thesis_invalid_if": "", "catalyst": "",
        }],
        "portfolio_view": "small energy book",
    })
    tampered = json.loads(broken)
    tampered["reasoning_chain"]["cash_target"] = "cash 95%"
    tampered["targets"][0]["target_weight_pct"] = 15.0  # was 5.0 — re-decided

    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    monkeypatch.setattr(
        PortfolioManagerAgent, "run",
        lambda self, **kw: _result(broken), raising=False,
    )
    monkeypatch.setattr(
        PortfolioManagerAgent, "_execute",
        lambda self, user_message: _result(json.dumps(tampered)),
        raising=False,
    )

    decision, _ = agent.decide(analyses=[], positions=[])
    assert decision is None, "a changed target weight must fail closed"


def test_pm_repair_that_adds_a_target_fails_closed(monkeypatch):
    broken = json.dumps({
        "reasoning_chain": {
            "macro_filter": "m", "news_check": "n", "earnings_check": "e",
            "signal_conflicts": "s", "sizing_logic": "z",
            "portfolio_balance": "b",
        },
        "targets": [{
            "symbol": "XLE", "target_weight_pct": 5.0, "conviction": "medium",
            "thesis": "energy tailwind", "thesis_invalid_if": "", "catalyst": "",
        }],
        "portfolio_view": "small energy book",
    })
    tampered = json.loads(broken)
    tampered["reasoning_chain"]["cash_target"] = "cash 90%"
    tampered["targets"].append({
        "symbol": "NVDA", "target_weight_pct": 8.0, "conviction": "high",
        "thesis": "smuggled in during repair", "thesis_invalid_if": "", "catalyst": "",
    })

    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    monkeypatch.setattr(
        PortfolioManagerAgent, "run",
        lambda self, **kw: _result(broken), raising=False,
    )
    monkeypatch.setattr(
        PortfolioManagerAgent, "_execute",
        lambda self, user_message: _result(json.dumps(tampered)),
        raising=False,
    )

    decision, _ = agent.decide(analyses=[], positions=[])
    assert decision is None


def test_pm_decision_bearing_validation_error_skips_repair_entirely(monkeypatch):
    """`targets` is not a list at all (e.g. a stray string) — decision-
    bearing failure; must fail closed without a repair call. Note:
    `_drop_invalid_targets` normalizes non-list `targets` to `[]` BEFORE
    the pydantic construction, so a `targets`-rooted failure here means
    another decision-bearing constraint (e.g. duplicate symbols) — this
    test exercises the guard path via a monkeypatched validation_error
    check rather than hunting a specific schema constraint."""
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    broken = json.dumps({
        "reasoning_chain": {
            "macro_filter": "m", "news_check": "n", "earnings_check": "e",
            "signal_conflicts": "s", "sizing_logic": "z",
            "portfolio_balance": "b", "cash_target": "c",
        },
        "targets": [{
            "symbol": "XLE", "target_weight_pct": 5.0, "conviction": "medium",
            "thesis": "t", "thesis_invalid_if": "", "catalyst": "",
        }],
        # portfolio_view omitted — required str field, but NOT decision-bearing
        # for PM (only "targets" is in _DECISION_FIELDS), so this alone should
        # still attempt repair. This test instead forces the guard directly.
    })
    monkeypatch.setattr(
        PortfolioManagerAgent, "run",
        lambda self, **kw: _result(broken), raising=False,
    )
    monkeypatch.setattr(
        PortfolioManagerAgent, "validation_error_touches",
        staticmethod(lambda error, fields: True), raising=False,
    )
    execute_calls: list[str] = []
    monkeypatch.setattr(
        PortfolioManagerAgent, "_execute",
        lambda self, user_message: execute_calls.append(user_message) or _result("{}"),
        raising=False,
    )

    decision, _ = agent.decide(analyses=[], positions=[])
    assert decision is None
    assert execute_calls == [], "repair must not be attempted when the guard fires"


# ===========================================================================
# External re-review: comparison must be strict and type-safe, and PM's
# comparison must cover the full TargetPosition payload.
# ===========================================================================

def test_repair_changing_approved_to_string_false_fails_closed(monkeypatch):
    """`bool("false")` is True in Python — a naive bool() coercion would
    have let a repair silently flip an approval into a STRING "false"
    and still compare as unchanged. Must fail closed on the type change
    alone, independent of the truthy/falsy value."""
    agent = _rm()
    monkeypatch.setattr(
        RiskManagerAgent, "run",
        lambda self, **kw: _result(BROKEN_VERDICT), raising=False,
    )
    tampered = json.loads(FIXED_VERDICT)
    tampered["approved"] = "false"  # JSON string, not boolean false
    monkeypatch.setattr(
        RiskManagerAgent, "_execute",
        lambda self, user_message: _result(json.dumps(tampered)),
        raising=False,
    )

    verdict, _ = _review(agent)
    assert verdict is None


def test_repair_changing_scale_from_zero_fails_closed(monkeypatch):
    """0.0 is RM's explicit 'kill all BUYs' veto, not an absent value —
    `or 1.0` would have silently reinstated every BUY the original
    verdict killed. Must fail closed on 0.0 -> 1.0."""
    agent = _rm()
    broken_zero_scale = json.loads(BROKEN_VERDICT)
    broken_zero_scale["scale_all_buys"] = 0.0
    monkeypatch.setattr(
        RiskManagerAgent, "run",
        lambda self, **kw: _result(json.dumps(broken_zero_scale)), raising=False,
    )
    tampered = json.loads(FIXED_VERDICT)
    tampered["scale_all_buys"] = 1.0  # was 0.0 — reinstates every BUY
    monkeypatch.setattr(
        RiskManagerAgent, "_execute",
        lambda self, user_message: _result(json.dumps(tampered)),
        raising=False,
    )

    verdict, _ = _review(agent)
    assert verdict is None


def test_repair_preserving_scale_zero_is_still_accepted(monkeypatch):
    """Sanity check for the fix above: 0.0 -> 0.0 (correctly preserved)
    must NOT be rejected by the strict comparison."""
    agent = _rm()
    broken_zero_scale = json.loads(BROKEN_VERDICT)
    broken_zero_scale["scale_all_buys"] = 0.0
    monkeypatch.setattr(
        RiskManagerAgent, "run",
        lambda self, **kw: _result(json.dumps(broken_zero_scale)), raising=False,
    )
    fixed_zero_scale = json.loads(FIXED_VERDICT)
    fixed_zero_scale["scale_all_buys"] = 0.0
    monkeypatch.setattr(
        RiskManagerAgent, "_execute",
        lambda self, user_message: _result(json.dumps(fixed_zero_scale)),
        raising=False,
    )

    verdict, _ = _review(agent)
    assert verdict is not None
    assert verdict.scale_all_buys == 0.0


def test_repair_changing_modification_reason_fails_closed(monkeypatch):
    """`reason` is part of a modification's own decision content (unlike
    the top-level narrative reasoning_chain) and must be preserved too."""
    agent = _rm()
    monkeypatch.setattr(
        RiskManagerAgent, "run",
        lambda self, **kw: _result(BROKEN_VERDICT), raising=False,
    )
    tampered = json.loads(FIXED_VERDICT)
    tampered["modifications"][0]["reason"] = "a completely different rationale"
    monkeypatch.setattr(
        RiskManagerAgent, "_execute",
        lambda self, user_message: _result(json.dumps(tampered)),
        raising=False,
    )

    verdict, _ = _review(agent)
    assert verdict is None


def test_pm_repair_changing_conviction_fails_closed(monkeypatch):
    broken = json.dumps({
        "reasoning_chain": {
            "macro_filter": "m", "news_check": "n", "earnings_check": "e",
            "signal_conflicts": "s", "sizing_logic": "z", "portfolio_balance": "b",
        },
        "targets": [{
            "symbol": "XLE", "target_weight_pct": 5.0, "conviction": "low",
            "thesis": "energy tailwind", "thesis_invalid_if": "", "catalyst": "",
        }],
        "portfolio_view": "small energy book",
    })
    tampered = json.loads(broken)
    tampered["reasoning_chain"]["cash_target"] = "cash 95%"
    tampered["targets"][0]["conviction"] = "high"  # was low — same weight, re-decided conviction

    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    monkeypatch.setattr(
        PortfolioManagerAgent, "run",
        lambda self, **kw: _result(broken), raising=False,
    )
    monkeypatch.setattr(
        PortfolioManagerAgent, "_execute",
        lambda self, user_message: _result(json.dumps(tampered)),
        raising=False,
    )

    decision, _ = agent.decide(analyses=[], positions=[])
    assert decision is None


def test_pm_repair_changing_thesis_fails_closed(monkeypatch):
    broken = json.dumps({
        "reasoning_chain": {
            "macro_filter": "m", "news_check": "n", "earnings_check": "e",
            "signal_conflicts": "s", "sizing_logic": "z", "portfolio_balance": "b",
        },
        "targets": [{
            "symbol": "XLE", "target_weight_pct": 5.0, "conviction": "medium",
            "thesis": "energy tailwind from geopolitical risk", "thesis_invalid_if": "",
            "catalyst": "",
        }],
        "portfolio_view": "small energy book",
    })
    tampered = json.loads(broken)
    tampered["reasoning_chain"]["cash_target"] = "cash 95%"
    tampered["targets"][0]["thesis"] = "unrelated momentum breakout thesis"

    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    monkeypatch.setattr(
        PortfolioManagerAgent, "run",
        lambda self, **kw: _result(broken), raising=False,
    )
    monkeypatch.setattr(
        PortfolioManagerAgent, "_execute",
        lambda self, user_message: _result(json.dumps(tampered)),
        raising=False,
    )

    decision, _ = agent.decide(analyses=[], positions=[])
    assert decision is None


def test_pm_repair_changing_suggested_stop_price_fails_closed(monkeypatch):
    broken = json.dumps({
        "reasoning_chain": {
            "macro_filter": "m", "news_check": "n", "earnings_check": "e",
            "signal_conflicts": "s", "sizing_logic": "z", "portfolio_balance": "b",
        },
        "targets": [{
            "symbol": "XLE", "target_weight_pct": 5.0, "conviction": "medium",
            "thesis": "t", "thesis_invalid_if": "", "catalyst": "",
            "suggested_stop_price": 59.0,
        }],
        "portfolio_view": "small energy book",
    })
    tampered = json.loads(broken)
    tampered["reasoning_chain"]["cash_target"] = "cash 95%"
    tampered["targets"][0]["suggested_stop_price"] = 45.0  # was 59.0 — widens risk

    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    monkeypatch.setattr(
        PortfolioManagerAgent, "run",
        lambda self, **kw: _result(broken), raising=False,
    )
    monkeypatch.setattr(
        PortfolioManagerAgent, "_execute",
        lambda self, user_message: _result(json.dumps(tampered)),
        raising=False,
    )

    decision, _ = agent.decide(analyses=[], positions=[])
    assert decision is None


def test_pm_repair_changing_catalyst_fails_closed(monkeypatch):
    broken = json.dumps({
        "reasoning_chain": {
            "macro_filter": "m", "news_check": "n", "earnings_check": "e",
            "signal_conflicts": "s", "sizing_logic": "z", "portfolio_balance": "b",
        },
        "targets": [{
            "symbol": "XLE", "target_weight_pct": 5.0, "conviction": "medium",
            "thesis": "t", "thesis_invalid_if": "", "catalyst": "",
        }],
        "portfolio_view": "small energy book",
    })
    tampered = json.loads(broken)
    tampered["reasoning_chain"]["cash_target"] = "cash 95%"
    tampered["targets"][0]["catalyst"] = "fabricated earnings beat"

    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    monkeypatch.setattr(
        PortfolioManagerAgent, "run",
        lambda self, **kw: _result(broken), raising=False,
    )
    monkeypatch.setattr(
        PortfolioManagerAgent, "_execute",
        lambda self, user_message: _result(json.dumps(tampered)),
        raising=False,
    )

    decision, _ = agent.decide(analyses=[], positions=[])
    assert decision is None


def test_pm_repair_preserving_full_payload_is_still_accepted(monkeypatch):
    """Sanity check: a repair that changes ONLY the missing schema field
    and reproduces every target field exactly must still succeed."""
    broken = json.dumps({
        "reasoning_chain": {
            "macro_filter": "m", "news_check": "n", "earnings_check": "e",
            "signal_conflicts": "s", "sizing_logic": "z", "portfolio_balance": "b",
        },
        "targets": [{
            "symbol": "xle", "target_weight_pct": 5.0, "conviction": "MEDIUM",
            "thesis": "t", "thesis_invalid_if": "inv", "catalyst": "cat",
            "suggested_stop_price": 59.0,
        }],
        "portfolio_view": "small energy book",
    })
    fixed = json.loads(broken)
    fixed["reasoning_chain"]["cash_target"] = "cash 95%"

    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    monkeypatch.setattr(
        PortfolioManagerAgent, "run",
        lambda self, **kw: _result(broken), raising=False,
    )
    monkeypatch.setattr(
        PortfolioManagerAgent, "_execute",
        lambda self, user_message: _result(json.dumps(fixed)),
        raising=False,
    )

    decision, _ = agent.decide(analyses=[], positions=[])
    assert decision is not None
    assert decision.targets[0].suggested_stop_price == 59.0
