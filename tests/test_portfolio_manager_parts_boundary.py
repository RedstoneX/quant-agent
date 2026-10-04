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

from src.agents.portfolio_manager import PortfolioManagerAgent, grounding, prompt_evidence, ranking, rotation_section
from src.agents.portfolio_manager.candidate_ranking import CandidateRanking, _HostState
from src.agents.portfolio_manager.decision_grounding import DecisionGrounding
from src.agents.portfolio_manager.evidence_prompting import PromptEvidence
from src.agents.portfolio_manager.held_part import LiveMapping, is_delegate
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
        conflict_source_aliases=PortfolioManagerAgent._CONFLICT_SOURCE_ALIASES,
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


# --- Instalment 2: prompt evidence, rotation section, candidate ranking ----------

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


# --- The agent HOLDS its parts; it inherits none of them --------------------------

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


# --- Candidate ranking, rotation section, decision grounding: held, not inherited ---

#: (holder module, part class, held attribute, one body the part reads through `self.`, a no-arg-ish probe)
_HELD = [
    (ranking, CandidateRanking, "_candidate_ranking", "candidate_eligibility"),
    (rotation_section, RotationSection, "_rotation_section", "_rotation_constraint_line"),
    (grounding, DecisionGrounding, "_decision_grounding", "_conflict_is_named"),
]
_MIXINS = ("CandidateRankingMixin", "RotationSectionMixin", "DecisionGroundingMixin", "PromptEvidenceMixin")


def _hold_fn(module):
    return next(f for n, f in vars(module).items() if n.startswith("hold_"))


def test_agent_inherits_no_mixin_and_holds_every_part():
    assert not any(c.__name__ in _MIXINS for c in PortfolioManagerAgent.__mro__)
    assert [c.__name__ for c in PortfolioManagerAgent.__mro__][:3] == ["PortfolioManagerAgent", "LiveLimitPrompt", "BaseAgent"]
    for module, part, holder, _ in _HELD:
        assert type(getattr(PortfolioManagerAgent, holder)) is part
        for name in module.DELEGATED:
            assert is_delegate(PortfolioManagerAgent, name), name


@pytest.mark.parametrize("module, part, holder, body", _HELD)
def test_delegates_cover_every_body_and_live_bodies_are_delegated(module, part, holder, body):
    bodies = {n for n, f in vars(part).items() if inspect.isfunction(f) and n != "__init__"}
    assert bodies == set(module.DELEGATED)
    assert body in module.LIVE_BODIES
    assert set(module.LIVE_BODIES) <= bodies


@pytest.mark.parametrize("module, part, holder, body", _HELD)
def test_part_is_built_held_and_run_on_a_bare_class_with_no_agent(module, part, holder, body):
    """Constructed from a bare class, exercised through the delegates, and the held
    part's own body runs when nothing is swapped: no agent object is built."""
    class Bare:
        build_evidence_registry = staticmethod(lambda *a, **k: {})
        _macro_sectors = staticmethod(lambda *a, **k: [])

    _hold_fn(module)(Bare)
    held = getattr(Bare, holder)
    assert type(held) is part
    if module is grounding:  # the mixin's class attributes now ride in with the holder
        assert Bare._CONFLICT_SOURCE_ALIASES == PortfolioManagerAgent._CONFLICT_SOURCE_ALIASES
        assert Bare._DECISION_FIELDS == PortfolioManagerAgent._DECISION_FIELDS == ("targets",)
    assert set(module.DELEGATED) <= set(vars(Bare))
    # A live body on the part forwards to the part's OWN body while Bare is pristine
    # (the recursion guard): mark the class body and see the mark come back through
    # both the delegate and the part's `self.` read.
    with patch.object(part, body, lambda self, *a, **k: ("OWN", self)):
        assert getattr(Bare, body)() == ("OWN", held)
        assert getattr(held, body)() == ("OWN", held)
    # Swapped on the bare host after construction: the next call through the part sees it.
    with patch.object(Bare, body, classmethod(lambda cls, *a, **k: "SWAPPED")):
        assert not is_delegate(Bare, body)
        assert getattr(held, body)() == "SWAPPED"
    assert is_delegate(Bare, body)
    # Re-holding with a caller-built part swaps the whole part.
    assert getattr(_hold_fn(module)(Bare, held), holder) is held


def test_aliases_are_a_live_view_of_the_agent_class():
    view = PortfolioManagerAgent._decision_grounding._CONFLICT_SOURCE_ALIASES
    assert isinstance(view, LiveMapping)
    assert dict(view) == PortfolioManagerAgent._CONFLICT_SOURCE_ALIASES
    with patch.object(PortfolioManagerAgent, "_CONFLICT_SOURCE_ALIASES", {"news": ("press",)}):
        assert PortfolioManagerAgent._conflict_is_named("ABC press says", "ABC", "news")
        assert not PortfolioManagerAgent._conflict_is_named("ABC news says", "ABC", "news")
    assert PortfolioManagerAgent._conflict_is_named("ABC smart money", "ABC", "smart_money")


def test_held_ranking_part_reads_the_agent_class_live():
    """The held part's getter/setter hit `PortfolioManagerAgent` at call time, with the
    body's None default, so a test that assigns `_macro_parse_failures` on the agent
    class is what the body sees."""
    agent = PortfolioManagerAgent
    before = getattr(agent, "_macro_parse_failures", None)
    try:
        agent._macro_parse_failures = ["live"]
        assert agent._candidate_ranking._host._macro_parse_failures == ["live"]
        agent._candidate_ranking._host._macro_parse_failures = ["written"]
        assert agent._macro_parse_failures == ["written"]
    finally:
        if before is None:
            if "_macro_parse_failures" in vars(agent):
                del agent._macro_parse_failures
        else:
            agent._macro_parse_failures = before
