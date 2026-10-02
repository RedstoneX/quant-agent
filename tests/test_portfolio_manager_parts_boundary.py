"""Boundary witnesses: the lifted portfolio-manager decision-grounding piece builds
and runs with no agent (and no pipeline) behind it.

Every collaborator is an explicit keyword-only constructor argument, so the class
is built from stubs alone (clause 5 of tests/boundary_harness.py). Follows
tests/test_cost_circuit_parts_boundary.py.
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest

from src.agents.portfolio_manager.decision_grounding import DecisionGrounding
from src.agents.portfolio_manager.grounding import (
    DecisionGroundingMixin, _OWN_BODIES, _is_own_shim,
)
from src.trading_calendar import et_today
from tests.boundary_harness import check_boundary


def _build(cls, **overrides):
    params = inspect.signature(cls).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return cls(**kwargs)


def _bare():
    """A part with its own bodies (no swapped collaborators)."""
    return DecisionGrounding(
        build_evidence_registry=MagicMock(name="build_evidence_registry"),
        conflict_source_aliases=DecisionGroundingMixin._CONFLICT_SOURCE_ALIASES,
    )


@pytest.mark.parametrize("cls", [DecisionGrounding])
def test_every_lifted_piece_is_constructible_from_stubs(cls):
    _build(cls)
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@pytest.mark.parametrize("module", ["src.agents.portfolio_manager.decision_grounding"])
def test_every_lifted_module_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_grounding_statics_run_with_nothing_behind_them():
    part = _bare()
    day = et_today().isoformat()
    rows = f"- [{day}] something happened → ABC(bullish), XYZ(bearish)\n"
    by_date = part._state_change_symbols_by_date(rows)
    assert by_date == {day: {"ABC": {"bullish"}, "XYZ": {"bearish"}}}, by_date
    assert part._catalyst_cites_state_change(f"per {day} row", "ABC", "bullish", by_date)
    assert not part._catalyst_cites_state_change(f"per {day} row", "ABC", "bearish", by_date)
    assert part._conflict_is_named("ABC vs smart money", "ABC", "smart_money")
    assert not part._conflict_is_named("ABC vs news", "ABC", "smart_money")
    assert part._canonical_targets([]) == []


def test_part_uses_its_own_bodies_unless_swapped():
    part = _bare()
    assert part._conflict_is_named.__func__ is DecisionGrounding._conflict_is_named
    assert part._target_intent.__func__ is DecisionGrounding._target_intent
    swapped = MagicMock(name="swapped")
    assert _build(DecisionGrounding, conflict_is_named=swapped)._conflict_is_named is swapped


def test_aliases_are_read_live_off_the_host_not_the_part():
    part = DecisionGrounding(build_evidence_registry=None, conflict_source_aliases={"news": ("press",)})
    assert part._conflict_is_named("ABC press says", "ABC", "news")
    assert not part._conflict_is_named("ABC news says", "ABC", "news")


def test_shims_build_the_part_per_call_and_see_a_swapped_body():
    class Host(DecisionGroundingMixin):
        build_evidence_registry = staticmethod(lambda *a, **k: {})

    # Pristine: the mixin's own shims are never handed back to the part.
    assert all(_is_own_shim(Host, attr) for attr in _OWN_BODIES)
    assert Host._conflict_is_named("ABC smart-money", "ABC", "smart_money") is True
    # Swapped after construction: the next call sees the swap.
    with patch.object(Host, "_conflict_is_named", classmethod(lambda cls, *a: "SWAPPED")):
        assert not _is_own_shim(Host, "_conflict_is_named")
        assert Host._grounding()._conflict_is_named("x", "y", "z") == "SWAPPED"
    assert _is_own_shim(Host, "_conflict_is_named")
    assert _is_own_shim(Host, "_target_intent")
    assert not _is_own_shim(Host, "missing_attr")


def test_shim_signatures_cover_every_body():
    bodies = {n for n, f in vars(DecisionGrounding).items()
              if inspect.isfunction(f) and n != "__init__"}
    shims = {n for n, f in vars(DecisionGroundingMixin).items()
             if isinstance(f, classmethod) and n != "_grounding"}
    assert bodies == shims
