"""Boundary witnesses: the lifted portfolio-manager pieces (decision grounding,
prompt evidence, rotation section, candidate ranking) build and run with no agent
(and no pipeline) behind them.

Every collaborator is an explicit keyword-only constructor argument, so the class
is built from stubs alone (clause 5 of tests/boundary_harness.py). Follows
tests/test_cost_circuit_parts_boundary.py.
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest

from src.agents.portfolio_manager import PortfolioManagerAgent, prompt_evidence, ranking, rotation_section
from src.agents.portfolio_manager.candidate_ranking import CandidateRanking, _HostState
from src.agents.portfolio_manager.decision_grounding import DecisionGrounding
from src.agents.portfolio_manager.evidence_prompting import PromptEvidence
from src.agents.portfolio_manager.grounding import (
    DecisionGroundingMixin, _OWN_BODIES, _is_own_shim,
)
from src.agents.portfolio_manager.rotation_rendering import RotationSection
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


@pytest.mark.parametrize("cls", [DecisionGrounding, PromptEvidence, RotationSection, CandidateRanking, _HostState])
def test_every_lifted_piece_is_constructible_from_stubs(cls):
    _build(cls)
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@pytest.mark.parametrize("module", [
    "src.agents.portfolio_manager.decision_grounding",
    "src.agents.portfolio_manager.evidence_prompting",
    "src.agents.portfolio_manager.rotation_rendering",
    "src.agents.portfolio_manager.candidate_ranking",
])
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


# --- Instalment 2: prompt evidence, rotation section, candidate ranking ----------

#: (shim module, mixin, part, one body the part reads through `self.`)
_INSTALMENT_2 = [
    (ranking, ranking.CandidateRankingMixin, CandidateRanking, "candidate_eligibility"),
]


def _ranking_part(**overrides):
    kwargs = dict(
        get_macro_parse_failures=lambda: None,
        set_macro_parse_failures=lambda value: None,
        macro_sectors=MagicMock(name="macro_sectors"),
    )
    kwargs.update(overrides)
    return CandidateRanking(**kwargs)


def test_prompt_evidence_statics_run_with_nothing_behind_them():
    part = PromptEvidence()
    assert part._collapse_stances(["bullish", "bullish"]) == "bullish"
    assert part._sector_guidance_rows(None) == []
    assert part._collapse_stances.__func__ is PromptEvidence._collapse_stances
    assert _build(PromptEvidence, collapse_stances=lambda v: "SWAPPED")._collapse_stances([]) == "SWAPPED"


def test_prompt_text_is_the_same_through_the_shim_and_the_part():
    """Prompt text is code that can rot: the mixin's shim and the bare part must
    render byte-identical text (the lift moved the bodies verbatim)."""
    rows: list[dict] = []  # the empty roll-up; richer rows need a fixture the proof script covers
    assert (PortfolioManagerAgent._render_earnings_no_call_rollup(rows)
            == PromptEvidence()._render_earnings_no_call_rollup(rows))


def test_rotation_part_runs_with_nothing_behind_it():
    part = RotationSection()
    assert part._rotation_constraint_line.__func__ is RotationSection._rotation_constraint_line
    swapped = MagicMock(name="swapped")
    assert _build(RotationSection, rotation_constraint_line=swapped)._rotation_constraint_line is swapped


def test_host_state_is_read_and_assigned_live_never_copied():
    class Host:  # starts WITHOUT the attribute, as the agent class does
        pass

    state = _HostState(
        get_macro_parse_failures=lambda: getattr(Host, "_macro_parse_failures", None),
        set_macro_parse_failures=lambda value: setattr(Host, "_macro_parse_failures", value),
        macro_sectors=lambda *a, **k: ("sectors", a, k),
    )
    # The body's exact read: a None default when the host lacks the attribute.
    assert getattr(state, "_macro_parse_failures", None) is None
    # The body's exact assignment flows through to the host, and the next read sees it.
    state._macro_parse_failures = []
    assert Host._macro_parse_failures == []
    state._macro_parse_failures.append("reason")
    assert Host._macro_parse_failures == ["reason"]
    Host._macro_parse_failures = ["replaced on the host"]
    assert state._macro_parse_failures == ["replaced on the host"]
    assert state._macro_sectors([], {}) == ("sectors", ([], {}), {})


def test_ranking_part_wires_the_host_state_from_its_arguments():
    seen = {}
    part = _ranking_part(set_macro_parse_failures=lambda value: seen.setdefault("v", value))
    part._host._macro_parse_failures = ["x"]
    assert seen == {"v": ["x"]}
    assert part.candidate_eligibility.__func__ is CandidateRanking.candidate_eligibility
    swapped = MagicMock(name="swapped")
    assert _ranking_part(candidate_eligibility=swapped).candidate_eligibility is swapped


def test_ranking_shim_reads_the_agent_class_live():
    """The shim's getter/setter hit `PortfolioManagerAgent` at call time, with the
    body's None default, so a test that assigns `_macro_parse_failures` on the agent
    class is what the body sees."""
    agent = ranking.PortfolioManagerAgent
    before = getattr(agent, "_macro_parse_failures", None)
    try:
        agent._macro_parse_failures = ["live"]
        assert ranking.CandidateRankingMixin._candidate_ranking()._host._macro_parse_failures == ["live"]
        ranking.CandidateRankingMixin._candidate_ranking()._host._macro_parse_failures = ["written"]
        assert agent._macro_parse_failures == ["written"]
    finally:
        if before is None:
            if "_macro_parse_failures" in vars(agent):
                del agent._macro_parse_failures
        else:
            agent._macro_parse_failures = before


@pytest.mark.parametrize("module, mixin, part, body", _INSTALMENT_2)
def test_instalment_2_shims_build_the_part_per_call_and_see_a_swapped_body(module, mixin, part, body):
    class Host(mixin):
        pass

    builder = next(n for n, f in vars(mixin).items()
                   if isinstance(f, classmethod) and n not in vars(part))
    # Pristine: the mixin's own shims are never handed back to the part.
    assert all(module._is_own_shim(Host, attr) for attr in module._OWN_BODIES)
    assert body in module._OWN_BODIES
    built = getattr(Host, builder)()
    assert getattr(built, body).__func__ is getattr(part, body)
    with patch.object(Host, body, classmethod(lambda cls, *a, **k: "SWAPPED")):
        assert not module._is_own_shim(Host, body)
        assert getattr(getattr(Host, builder)(), body)() == "SWAPPED"
    assert module._is_own_shim(Host, body)
    assert not module._is_own_shim(Host, "missing_attr")


@pytest.mark.parametrize("module, mixin, part, body", _INSTALMENT_2)
def test_instalment_2_shim_signatures_cover_every_body(module, mixin, part, body):
    bodies = {n for n, f in vars(part).items() if inspect.isfunction(f) and n != "__init__"}
    builder = next(n for n, f in vars(mixin).items()
                   if isinstance(f, classmethod) and n not in vars(part))
    shims = {n for n, f in vars(mixin).items() if isinstance(f, classmethod) and n != builder}
    assert bodies == shims
    assert set(module._OWN_BODIES) <= bodies


# --- The agent HOLDS the prompt-evidence part; it no longer inherits it -----------

def test_agent_holds_prompt_evidence_instead_of_inheriting():
    assert not any(c.__name__ == "PromptEvidenceMixin" for c in PortfolioManagerAgent.__mro__)
    assert type(PortfolioManagerAgent._prompt_evidence) is PromptEvidence
    for name in prompt_evidence.DELEGATED:
        assert isinstance(inspect.getattr_static(PortfolioManagerAgent, name), classmethod), name
        assert not hasattr(PromptEvidence, name) or callable(getattr(PromptEvidence, name))
    assert PortfolioManagerAgent._collapse_stances(["bullish", "bullish"]) == "bullish"


def test_prompt_evidence_part_is_built_and_exercised_without_the_agent():
    """Constructed from nothing, exercised, and held on a bare class: no agent built."""
    class Bare:
        pass

    prompt_evidence.hold_prompt_evidence(Bare)
    assert set(prompt_evidence.DELEGATED) <= set(vars(Bare))
    assert Bare._collapse_stances(["bullish", "bullish"]) == "bullish"
    assert Bare._sector_guidance_rows(None) == []
    swapped = PromptEvidence(collapse_stances=lambda v: "SWAPPED")
    assert prompt_evidence.hold_prompt_evidence(Bare, swapped)._collapse_stances([]) == "SWAPPED"


def test_delegates_cover_every_prompt_evidence_body():
    bodies = {n for n, f in vars(PromptEvidence).items()
              if n != "__init__" and (inspect.isfunction(f) or isinstance(f, (classmethod, staticmethod)))}
    assert bodies == set(prompt_evidence.DELEGATED)


# --- The agent HOLDS the rotation-section part; it no longer inherits it ----------

def test_agent_holds_rotation_section_instead_of_inheriting():
    assert not any(c.__name__ == "RotationSectionMixin" for c in PortfolioManagerAgent.__mro__)
    assert type(PortfolioManagerAgent._rotation_section) is RotationSection
    for name in rotation_section.DELEGATED:
        assert isinstance(inspect.getattr_static(PortfolioManagerAgent, name), classmethod), name
        assert callable(getattr(RotationSection, name)), name
    precheck = PortfolioManagerAgent.rotation_precheck(
        ranked=[], blocked={}, held_symbols=set(), existing_risk_pct=None, ceiling_pct=5.0)
    assert precheck.telemetry_available is False


def test_rotation_section_part_is_built_and_exercised_without_the_agent():
    """Constructed from nothing, exercised, and held on a bare class: no agent built."""
    class Bare:
        pass

    rotation_section.hold_rotation_section(Bare)
    assert set(rotation_section.DELEGATED) <= set(vars(Bare))
    precheck = Bare.rotation_precheck(ranked=[], blocked={}, held_symbols=set(),
                                      existing_risk_pct=None, ceiling_pct=5.0)
    assert precheck.telemetry_available is False
    swapped = RotationSection(rotation_precheck=lambda **kw: "SWAPPED")
    assert rotation_section.hold_rotation_section(Bare, swapped).rotation_precheck() == "SWAPPED"


def test_delegates_cover_every_rotation_section_body():
    bodies = {n for n, f in vars(RotationSection).items()
              if n != "__init__" and (inspect.isfunction(f) or isinstance(f, (classmethod, staticmethod)))}
    assert bodies == set(rotation_section.DELEGATED)
