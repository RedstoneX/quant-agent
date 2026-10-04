"""Boundary witnesses: the lifted review-shaped prompt-facts parts (trade grading,
outcome review, outlook review, blocked proposals, evening replay inputs) build and
run with no pipeline behind them.

Every collaborator is an explicit keyword-only constructor argument, so each class
is built from stubs alone (clause 5 of tests/boundary_harness.py). Follows
tests/test_portfolio_manager_parts_boundary.py.
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from src.pipeline_prompt_facts_review import PromptFactsReviewMixin
from src.prompt_facts.blocked_proposals import BlockedProposals
from src.prompt_facts.evening_replay_inputs import EveningReplayInputs
from src.prompt_facts.outcome_review import OutcomeReview
from src.prompt_facts.outlook_review import OutlookReview
from src.prompt_facts.trade_grading import TradeGrading
from tests.boundary_harness import check_boundary

PARTS = [TradeGrading, OutcomeReview, OutlookReview, BlockedProposals, EveningReplayInputs]


def _build(cls, **overrides):
    params = inspect.signature(cls).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return cls(**kwargs)


def _empty_db():
    """A store with nothing in it: every reader answers empty."""
    db = MagicMock(name="db")
    for reader in ("get_trades", "get_recent_insights", "get_daily_pnl", "get_proposal_funnel_rows"):
        getattr(db, reader).return_value = []
    db.compute_trade_calibration.return_value = {"n": 0}
    db.get_proposal_funnel_rows.return_value = {}
    return db


@pytest.mark.parametrize("cls", PARTS)
def test_every_lifted_piece_is_constructible_from_stubs(cls):
    _build(cls)
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@pytest.mark.parametrize("module", [
    "src.prompt_facts.trade_grading",
    "src.prompt_facts.outcome_review",
    "src.prompt_facts.outlook_review",
    "src.prompt_facts.blocked_proposals",
    "src.prompt_facts.evening_replay_inputs",
])
def test_every_lifted_module_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_trade_grading_reads_only_its_collaborators():
    db = _empty_db()
    part = _build(TradeGrading, db=db, build_post_exit_reality=MagicMock(return_value=None))
    assert part._build_recent_sells_for_grading(lookback_days=5) == []
    assert part._build_recent_buys_for_grading(lookback_days=5) == []
    assert db.get_trades.called
    part._build_trade_grade_summary(lookback_days=14)
    assert db.get_recent_insights.called


def test_outcome_review_runs_on_an_empty_store():
    db = _empty_db()
    operator_log = MagicMock(name="log_conviction_outcome_for_operator")
    part = _build(OutcomeReview, db=db, log_conviction_outcome_for_operator=operator_log)
    perf = part._compute_recent_performance(current_equity=1000.0)
    assert isinstance(perf, dict) and db.get_daily_pnl.called
    assert part._build_recent_loss_pits(lookback_days=14) == ""
    assert part._build_calibration_note(lookback_days=45) == ""
    operator_log.assert_called_once_with({"n": 0})
    assert part._build_post_exit_reality() is None


def test_outlook_review_and_blocked_proposals_run_on_an_empty_store():
    db = _empty_db()
    assert _build(OutlookReview, db=db)._build_recent_missed_lessons(lookback_days=14) == ""
    assert isinstance(_build(OutlookReview, db=db)._build_recent_outlook_calibration(lookback=10), dict)
    assert isinstance(_build(BlockedProposals, db=db)._build_blocked_proposals(), str)
    assert db.get_proposal_funnel_rows.called


def test_evening_replay_inputs_writes_under_the_given_root(tmp_path):
    part = EveningReplayInputs()
    part._persist_evening_replay_inputs(
        date_iso="2000-01-03", run_id="run-boundary", positions=[], macro_summary="",
        total_value=0.0, daily_pnl=0.0, daily_return_pct=0.0, today_trades=[], prior_outlook=None,
        recent_sells=[], recent_buys=[], news_intel=[], earnings_analyses=[], weekly_narrative="",
        active_state_changes=[], outlook_calibration={}, missed_ops_snapshots=[],
        thesis_health_context="", root_dir=tmp_path,
    )
    assert any(p.is_file() for p in tmp_path.rglob("*")), "nothing was persisted"


def test_the_shim_reads_collaborators_live_per_call():
    """The pipeline-side mixin is a seam: swapping `db` after construction is seen
    by the next call, with no pipeline object anywhere."""
    class Host(PromptFactsReviewMixin):
        pass
    host = Host()
    first, second = _empty_db(), _empty_db()
    host.db = first
    host._build_recent_loss_pits(14)
    host.db = second
    host._build_recent_loss_pits(14)
    assert first.get_recent_insights.call_count == 1
    assert second.get_recent_insights.call_count == 1
