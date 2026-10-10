"""Two former silent swallows in decision grounding now fail loudly or say why."""

from __future__ import annotations

import pytest

from src.agents.portfolio_manager import decision_grounding as dg


def _grounding():
    return dg.DecisionGrounding(build_evidence_registry=lambda *a, **k: None, conflict_source_aliases={})


def test_malformed_target_is_refused_with_its_real_reason(monkeypatch):
    seen = []
    monkeypatch.setattr(dg.parse_telemetry, "record_dropped_item", lambda *a, **k: seen.append((a, k)))
    out = _grounding()._canonical_targets([{"symbol": "AAPL", "target_weight_pct": "not-a-number"}])
    assert out is None
    assert len(seen) == 1
    (model, sym), kw = seen[0]
    assert (model, sym) == ("TargetPosition", "AAPL")
    assert "malformed target" in kw["reason"] and "target_weight_pct" in kw["reason"]


def test_unrelated_exception_in_target_validation_is_not_swallowed(monkeypatch):
    def boom(**_):
        raise RuntimeError("unrelated bug")

    monkeypatch.setattr(dg, "TargetPosition", boom)
    with pytest.raises(RuntimeError, match="unrelated bug"):
        _grounding()._canonical_targets([{"symbol": "AAPL"}])


def test_unreadable_date_raises_instead_of_switching_off_checks(monkeypatch):
    def broken():
        raise OSError("clock unreadable")

    monkeypatch.setattr(dg, "et_today", broken)
    with pytest.raises(OSError, match="clock unreadable"):
        _grounding()._state_change_symbols_by_date("- [2026-10-09] AAPL up")
