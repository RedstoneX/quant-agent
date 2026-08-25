"""Regression tests for the 2026-08-17/20 PM parse-annihilation incident.

Production forensics (trading-utility recovery): `AgentResult.parse_json`
scored the PM plan's inner `targets` array (5 pts/symbol) above the full
PortfolioDecision object, because `_EXPECTED_AGENT_KEY_WEIGHTS` still
anchored on the pre-constructor-refactor key `decisions` while PM emits
`targets`. Any plan with >= 8 targets therefore lost the candidate
selection to a fragment of itself, `PortfolioDecision(**list)` raised, and
the session collapsed to "no trades" — 10 of the 13 decision runs between
2026-08-14 and 2026-08-20 died this way, each retried at full research
cost on the next 30-min tick.

Fixtures are the VERBATIM recorded `full_response` payloads from the two
killed production runs (run-cbf2adbd 17 targets, run-5dc7a354 11 targets).
"""
from pathlib import Path

import pytest

from src.agents.base import AgentResult
from src.agents.portfolio_manager import PortfolioManagerAgent
from src.models import PortfolioDecision

FIXTURES = Path(__file__).parent / "fixtures"


def _result(raw: str) -> AgentResult:
    return AgentResult(raw_text=raw, tokens_used=0, model="test")


@pytest.mark.parametrize(
    "fixture_name,expected_targets",
    [
        ("pm_response_17_targets_20260820.txt", 17),
        ("pm_response_11_targets_20260817.txt", 11),
    ],
)
def test_recorded_production_payload_parses_to_full_decision(
    fixture_name, expected_targets,
):
    raw = (FIXTURES / fixture_name).read_text()
    parsed = _result(raw).parse_json()
    assert isinstance(parsed, dict), (
        f"parse_json picked a {type(parsed).__name__} fragment over the "
        f"decision object — the 2026-08-17/20 incident has regressed"
    )
    assert len(parsed["targets"]) == expected_targets
    # And the full decision must survive validation end-to-end.
    cleaned = PortfolioManagerAgent._drop_invalid_targets(dict(parsed))
    decision = PortfolioDecision(**cleaned)
    assert len(decision.targets) == expected_targets
    assert decision.portfolio_view


def test_large_target_count_never_outranks_containing_object():
    """Synthetic worst case: far more targets than either weight table
    entry can offset. The nested-fragment filter (not just the `targets`
    key weight) must keep the container winning."""
    targets = ",".join(
        '{"symbol": "SY%02d", "target_weight_pct": 1.0, "conviction": "low",'
        ' "thesis": "t", "thesis_invalid_if": "", "catalyst": ""}' % i
        for i in range(40)  # 40 * 5 = 200 pts as a bare array
    )
    raw = (
        "```json\n"
        '{"reasoning_chain": {"macro_filter": "m"}, "targets": [' + targets + "],"
        ' "portfolio_view": "wide book"}\n'
        "```"
    )
    parsed = _result(raw).parse_json()
    assert isinstance(parsed, dict)
    assert len(parsed["targets"]) == 40


def test_tech_analyst_array_in_prose_still_returns_array():
    """2026-07-16 fix regression: an analyses ARRAY wrapped in prose must
    return the whole array, not its last element — the nested-fragment
    filter must treat the array as the container of its elements."""
    raw = (
        "Here are my analyses:\n"
        '[{"symbol": "AAPL", "rating": "buy"},'
        ' {"symbol": "MSFT", "rating": "neutral"},'
        ' {"symbol": "NVDA", "rating": "buy"}]\n'
        "Done."
    )
    parsed = _result(raw).parse_json()
    assert isinstance(parsed, list)
    assert len(parsed) == 3


def test_disjoint_draft_then_correction_still_prefers_correction():
    raw = (
        "First attempt:\n"
        '{"approved": true, "reasoning_chain": {"a": "draft"}}\n'
        "Actually, correcting:\n"
        '{"approved": false, "reasoning_chain": {"a": "corrected"}}'
    )
    parsed = _result(raw).parse_json()
    assert parsed["reasoning_chain"] == {"a": "corrected"}


def test_scoreless_prose_wrapper_still_yields_inner_payload():
    raw = (
        "note:\n"
        '{"thinking": "hmm", "answer": {"approved": true,'
        ' "reasoning_chain": {"a": "x"}}}\n'
        "trailing prose so the clean full-text load fails"
    )
    parsed = _result(raw).parse_json()
    assert isinstance(parsed, dict)
    assert "approved" in parsed


def test_pm_decide_treats_non_dict_parse_as_failure(monkeypatch):
    """A list reaching decide() must return (None, result) — a loud parse
    failure the pipeline retries — never a silent 'no trades' hold."""
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    fake = _result('[{"symbol": "XOM", "target_weight_pct": 5.0}]')
    monkeypatch.setattr(
        PortfolioManagerAgent, "run", lambda self, **kw: fake, raising=False,
    )
    decision, result = agent.decide(analyses=[], positions=[])
    assert decision is None
    assert result is fake


def test_pm_decide_treats_all_malformed_targets_as_failure(monkeypatch):
    raw = '''{
      "reasoning_chain": {
        "macro_filter":"m", "news_check":"n", "earnings_check":"e",
        "signal_conflicts":"s", "sizing_logic":"z",
        "portfolio_balance":"b", "cash_target":"c"
      },
      "targets":[{"symbol":"AAPL","target_weight_pct":99,"thesis":"bad"}],
      "portfolio_view":"actionable target was attempted"
    }'''
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    fake = _result(raw)
    monkeypatch.setattr(
        PortfolioManagerAgent, "run", lambda self, **kw: fake, raising=False,
    )
    decision, result = agent.decide(analyses=[], positions=[])
    assert decision is None
    assert result is fake
