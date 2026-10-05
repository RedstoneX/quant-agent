"""Boundary witnesses: the lifted prompt-facts signal helpers build and run with no pipeline behind them.

Every collaborator is an explicit keyword-only constructor argument, so the class is
built from stubs alone (clause 5 of tests/boundary_harness.py).
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.prompt_facts.missed_ops_signals import MissedOpsSignals
from tests.boundary_harness import check_boundary


def _build(cls, **overrides):
    params = inspect.signature(cls).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return cls(**kwargs)


LIFTED = [MissedOpsSignals]


@pytest.mark.parametrize("cls", LIFTED)
def test_every_lifted_piece_is_constructible_from_stubs(cls):
    _build(cls)
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@pytest.mark.parametrize("module", ["src.prompt_facts.missed_ops_signals"])
def test_every_lifted_module_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_held_set_reads_only_the_db_collaborator():
    db = MagicMock(name="db")
    db.get_positions_snapshot = MagicMock(return_value=[])
    sig = _build(MissedOpsSignals, db=db)
    assert sig.db is db and sig._parse_logged_agent_response is not None


def test_macro_sector_map_tolerates_an_empty_macro_store():
    macro_store = MagicMock(name="macro_store")
    macro_store.latest = MagicMock(return_value=None)
    macro_store.get_latest = MagicMock(return_value=None)
    sig = _build(MissedOpsSignals, macro_store=macro_store)
    out = sig._missed_ops_macro_sector_map()
    assert isinstance(out, dict)



# --- The trade-review parts (src/prompt_facts/review/): built and run with no pipeline behind them.

from src.prompt_facts.review.blocked import ReviewBlocked  # noqa: E402
from src.prompt_facts.review.calibration import ReviewCalibration  # noqa: E402
from src.prompt_facts.review.exits import ReviewExits  # noqa: E402
from src.prompt_facts.review.grading import ReviewGrading  # noqa: E402
from src.prompt_facts.review.replay import ReviewReplay  # noqa: E402

REVIEW_PARTS = [ReviewGrading, ReviewExits, ReviewCalibration, ReviewBlocked, ReviewReplay]
REVIEW_MODULES = [f"src.prompt_facts.review.{m}" for m in ("grading", "exits", "calibration", "blocked", "replay")]


@pytest.mark.parametrize("cls", REVIEW_PARTS)
def test_every_review_part_is_constructible_from_stubs(cls):
    _build(cls)
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@pytest.mark.parametrize("module", REVIEW_MODULES)
def test_every_review_module_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_recent_performance_runs_against_a_stub_db():
    """Exercised, not just built: an empty daily-pnl history yields a dict and reads only the db."""
    db = MagicMock(name="db")
    db.get_daily_pnl = MagicMock(return_value=[])
    part = _build(ReviewCalibration, db=db)
    out = part._compute_recent_performance(current_equity=100_000.0)
    assert isinstance(out, dict)
    db.get_daily_pnl.assert_called()


def test_exits_part_reads_the_sweeper_live():
    """The sweeper is a callable read per use, never snapshotted at construction."""
    calls = []
    part = _build(ReviewExits, sweeper=lambda: calls.append("read") or None)
    assert part._sweeper() is None and calls == ["read"]


def test_grading_part_is_handed_post_exit_reality_not_owning_it():
    """`_build_trade_grade_summary` reads `_build_post_exit_reality`; the grading part never defines it."""
    assert not hasattr(ReviewGrading, "_build_post_exit_reality") and hasattr(ReviewExits, "_build_post_exit_reality")
    part = _build(ReviewGrading, build_post_exit_reality=lambda *a, **k: "HANDED IN")
    assert part._build_post_exit_reality() == "HANDED IN"



# --- The parent prompt-facts parts (src/prompt_facts/*.py): built and run with no pipeline behind them.

from src.prompt_facts.decisions import PromptDecisions  # noqa: E402
from src.pipeline_prompt_facts import PromptExposure  # noqa: E402
from src.prompt_facts.heat import PromptHeat  # noqa: E402
from src.pipeline_prompt_facts import PromptHistory  # noqa: E402
from src.prompt_facts.pm_facts import PromptPMFacts  # noqa: E402
from src.pipeline_prompt_facts import PromptPositionFacts  # noqa: E402
from src.prompt_facts.projected import PromptProjected  # noqa: E402
from src.prompt_facts.watchlist import PromptWatchlist  # noqa: E402

FACT_PARTS = [PromptHistory, PromptDecisions, PromptProjected, PromptWatchlist, PromptExposure, PromptHeat, PromptPMFacts, PromptPositionFacts]
FACT_MODULES = [f"src.prompt_facts.{m}" for m in ("decisions", "projected", "watchlist", "heat", "pm_facts")]


@pytest.mark.parametrize("cls", FACT_PARTS)
def test_every_fact_part_is_constructible_from_stubs(cls):
    _build(cls)
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@pytest.mark.parametrize("module", FACT_MODULES)
def test_every_fact_module_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_heat_part_is_handed_the_stop_map_not_owning_it():
    """Portfolio heat reads the stop map through the host's shim; the heat part never defines it, and the sweeper is read per use."""
    assert not hasattr(PromptHeat, "_build_stop_map") and hasattr(PromptExposure, "_build_stop_map")
    reads = []
    part = _build(PromptHeat, sweeper=lambda: reads.append("sweeper") or None,
                  build_stop_map=lambda positions: reads.append("stop_map") or ({}, {}, set()))
    part._build_portfolio_heat([MagicMock(name="pos", symbol="AAA")], 100_000.0)
    assert reads[:2] == ["sweeper", "stop_map"]


def test_pm_facts_part_is_handed_heat_and_history_not_owning_them():
    assert not hasattr(PromptPMFacts, "_build_portfolio_heat") and not hasattr(PromptPMFacts, "_build_position_history")
    part = _build(PromptPMFacts, build_portfolio_heat=lambda *a, **k: "HEAT", build_position_history=lambda *a, **k: {})
    assert part._build_portfolio_heat() == "HEAT" and part._build_position_history() == {}
    assert "config" in inspect.signature(PromptPMFacts).parameters, "the body reads getattr(self, 'config') by string; it must be handed in"


def test_projected_part_reads_and_writes_the_sector_cache_through_its_owner():
    """The sector cache belongs to the host: a bare part has none (so the body's getattr default applies), and a write lands on the owner."""
    assert not hasattr(PromptProjected(), "_last_symbol_sectors")
    owner = SimpleNamespace(_last_symbol_sectors={"AAA": "Tech"})
    part = _build(PromptProjected, sector_cache_owner=owner)
    assert part._last_symbol_sectors == {"AAA": "Tech"}
    part._last_symbol_sectors = {"BBB": "Energy"}
    assert owner._last_symbol_sectors == {"BBB": "Energy"}


def test_decisions_part_runs_against_a_stub_db():
    """Exercised, not just built: a db that returns nothing yields a string and reads only the db."""
    db = MagicMock(name="db")
    part = _build(PromptDecisions, db=db)
    assert isinstance(part._build_rm_recent_verdicts(), str)
    assert db.method_calls, "the body never read the db collaborator"


# --- The trade-review SEAT (src/prompt_facts/review/held.py): built alone with a host handed in,
# every collaborator read off the host at each call. The pipeline holds one and inherits nothing.

from src.prompt_facts.review.held import HOST_COLLABORATORS, PromptFactsReview  # noqa: E402


def test_review_seat_is_constructible_alone_and_passes_the_boundary_check():
    part = PromptFactsReview(host=SimpleNamespace())
    params = inspect.signature(PromptFactsReview).parameters.values()
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params)
    # a bare host: every read is None, nothing raises
    assert all(getattr(part, name) is None for name in HOST_COLLABORATORS)
    verdict = check_boundary("src.prompt_facts.review.held")
    assert verdict.passed, verdict.failures


def test_review_seat_runs_a_body_and_reads_a_collaborator_swapped_after_construction():
    """Exercised, not just built; and the db swapped on the host AFTER the part was built is the one that runs."""
    first, second = MagicMock(name="first_db"), MagicMock(name="second_db")
    first.get_daily_pnl.return_value = []
    second.get_daily_pnl.return_value = []
    host = SimpleNamespace(db=first)
    part = PromptFactsReview(host=host)
    assert isinstance(part._compute_recent_performance(current_equity=100_000.0), dict)
    first.get_daily_pnl.assert_called()
    host.db = second
    part._compute_recent_performance(current_equity=100_000.0)
    second.get_daily_pnl.assert_called()
    assert first.get_daily_pnl.call_count == 1


def test_review_seat_hands_the_grading_part_the_hosts_post_exit_reality_live():
    """`_build_post_exit_reality` is a collaborator: the grading part gets whatever the host carries NOW."""
    host = SimpleNamespace()
    part = PromptFactsReview(host=host)
    assert part._review_grading()._build_post_exit_reality is None
    host._build_post_exit_reality = lambda *a, **k: "SWAPPED AFTER CONSTRUCTION"
    assert part._review_grading()._build_post_exit_reality() == "SWAPPED AFTER CONSTRUCTION"
    host._sweeper = lambda: "SWEPT"
    assert part._review_exits()._sweeper() == "SWEPT"
