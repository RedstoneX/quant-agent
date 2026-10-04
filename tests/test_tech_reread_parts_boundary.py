"""Boundary witness: the technical seat's re-read cache builds and runs with no agent behind it.

`TechReread` (src/agents/tech_reread.py) is a HELD part, built per call by the
thin `TechAnalystAgent.analyze_batch` shim. Here it is constructed from stubs
alone and exercised without `TechAnalystAgent` ever being imported.
"""
from __future__ import annotations

import inspect
import pathlib
from types import SimpleNamespace

import pytest

import src.research_throttle as throttle
from src.agents.tech_reread import TechReread
from src.evidence_gate import READ_CARRIED
from src.models import TechAnalysisResult
from tests.boundary_harness import check_boundary


def test_module_passes_the_boundary_check():
    verdict = check_boundary("src.agents.tech_reread")
    assert verdict.passed, verdict.failures


def test_part_takes_keyword_only_collaborators():
    params = inspect.signature(TechReread).parameters
    assert params and all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


def _verdict(symbol):
    return TechAnalysisResult(
        symbol=symbol, rating="buy", conviction="high",
        thesis_invalid_if="loses 99", reasoning="because",
        entry_price=100.0, stop_loss=95.0, reference_target=120.0,
        setup_type="range", expected_horizon_sessions=10,
        support_levels=[95.0], resistance_levels=[120.0],
        reasoning_chain={"trend": "up", "momentum": "firm",
                         "volatility": "normal", "support_resistance": "mid", "volume": "average"},
    )


class _Spy:
    def __init__(self):
        self.calls = []

    def __call__(self, symbols, **kw):
        self.calls.append([s["symbol"] for s in symbols])
        return {s["symbol"]: _verdict(s["symbol"]) for s in symbols}, "agent-result"


def test_nothing_carried_asks_the_seat_about_everything_and_stamps_fingerprints():
    spy, state = _Spy(), SimpleNamespace()
    part = TechReread(ask=lambda: spy, state=state)
    out, agent_result = part.analyze_batch([{"symbol": "AAA"}, {"symbol": "BBB"}])
    assert spy.calls == [["AAA", "BBB"]]
    assert agent_result == "agent-result"
    assert set(out) == {"AAA", "BBB"}
    assert state.last_carried == {}


def test_unchanged_symbol_is_carried_forward_and_only_the_rest_asked(monkeypatch):
    stored = _verdict("AAA").model_dump()
    monkeypatch.setattr(throttle, "carry_unchanged_tech_reads", lambda fps, prior: {"AAA": stored})
    spy, state = _Spy(), SimpleNamespace()
    part = TechReread(ask=lambda: spy, state=state)
    out, _ = part.analyze_batch([{"symbol": "AAA"}, {"symbol": "BBB"}], prior_ratings={"AAA": {}})
    assert spy.calls == [["BBB"]]
    assert out["AAA"].read_state == READ_CARRIED
    assert set(state.last_carried) == {"AAA"}


def test_everything_carried_makes_no_call_and_clears_the_sink(monkeypatch):
    stored = _verdict("AAA").model_dump()
    monkeypatch.setattr(throttle, "carry_unchanged_tech_reads", lambda fps, prior: {"AAA": stored})
    spy, state = _Spy(), SimpleNamespace()
    part = TechReread(ask=lambda: spy, state=state)
    out, agent_result = part.analyze_batch([{"symbol": "AAA"}], prior_ratings={"AAA": {}})
    assert spy.calls == [] and agent_result is None
    assert set(out) == {"AAA"}
    assert state.last_unreadable == {} and state.last_unanswered == set()


def test_unreadable_stored_verdict_asks_the_seat(monkeypatch):
    monkeypatch.setattr(throttle, "carry_unchanged_tech_reads", lambda fps, prior: {"AAA": {"symbol": "AAA"}})
    spy, state = _Spy(), SimpleNamespace()
    TechReread(ask=lambda: spy, state=state).analyze_batch([{"symbol": "AAA"}], prior_ratings={"AAA": {}})
    assert spy.calls == [["AAA"]]                            # fails open: never reuse what it cannot rebuild


def test_ask_is_read_live_per_call_not_snapshotted():
    spies = [_Spy()]
    part = TechReread(ask=lambda: spies[-1], state=SimpleNamespace())
    part.analyze_batch([{"symbol": "AAA"}])
    spies.append(_Spy())                                     # rebound after construction
    part.analyze_batch([{"symbol": "BBB"}])
    assert spies[0].calls == [["AAA"]] and spies[1].calls == [["BBB"]]


def test_witness_never_imports_the_owner_agent():
    import ast
    tree = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert "src.agents.tech_analyst" not in imported
