"""Boundary witnesses: the lifted prompt-facts signal helpers build and run with no pipeline behind them.

Every collaborator is an explicit keyword-only constructor argument, so the class is
built from stubs alone (clause 5 of tests/boundary_harness.py).
"""
from __future__ import annotations

import inspect
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
