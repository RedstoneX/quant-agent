"""Witness: the trade-review prompt facts are ONE standalone part the pipeline HOLDS.

`ReviewFacts` (src/prompt_facts/review/facts.py) inherits from nothing and is built here
from stubs with no pipeline behind it; `hold_review_facts` installs its eleven names on a
bare class and the collaborators are read off the host at each call, never snapshotted.
This file imports nothing from `src.pipeline`, so it is the pipeline-free importer the
boundary harness's clause 5 looks for.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from src.pipeline_prompt_facts_review import build_review_facts, hold_review_facts
from src.prompt_facts.review.facts import REVIEW_FACT_NAMES, ReviewFacts
from tests.boundary_harness import check_boundary


def _build(cls, **overrides):
    params = inspect.signature(cls).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return cls(**kwargs)


def test_review_facts_is_built_from_stubs_with_no_pipeline_and_inherits_nothing():
    part = _build(ReviewFacts)
    assert type(part).__mro__ == (ReviewFacts, object)
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in inspect.signature(ReviewFacts).parameters.values())
    assert all(callable(getattr(part, n)) for n in REVIEW_FACT_NAMES) and len(REVIEW_FACT_NAMES) == 11
    verdict = check_boundary("src.prompt_facts.review.facts")
    assert verdict.passed, verdict.failures


def test_review_facts_runs_a_body_against_a_stub_db():
    db = MagicMock(name="db")
    db.get_daily_pnl = MagicMock(return_value=[])
    out = _build(ReviewFacts, db=db)._compute_recent_performance(current_equity=100_000.0)
    assert isinstance(out, dict) and db.get_daily_pnl.called


def test_cross_family_read_defaults_to_its_own_exits_body_or_the_handed_one():
    own = _build(ReviewFacts, build_post_exit_reality=None)
    assert own.grading()._build_post_exit_reality == own._build_post_exit_reality
    handed = _build(ReviewFacts, build_post_exit_reality=lambda *a, **k: "HANDED IN")
    assert handed.grading()._build_post_exit_reality() == "HANDED IN"


def test_bare_class_holds_the_review_facts_and_reads_collaborators_live():
    """Delegates install on a class with no pipeline; a db swapped AFTER holding is what runs."""
    class Bare:
        pass
    hold_review_facts(Bare)
    assert not any(b.__name__.endswith("Mixin") for b in Bare.__mro__)
    host = Bare()
    host.db = MagicMock(name="first")
    host.db.get_daily_pnl = MagicMock(return_value=[])
    host._compute_recent_performance(current_equity=1.0)
    swapped = MagicMock(name="second")
    swapped.get_daily_pnl = MagicMock(return_value=[])
    host.db = swapped
    host._compute_recent_performance(current_equity=1.0)
    assert swapped.get_daily_pnl.called
    assert isinstance(build_review_facts(host), ReviewFacts)


def test_prompt_facts_mixin_holds_the_review_facts_and_inherits_no_review_mixin():
    import src.pipeline_prompt_facts_review as review_module
    from src.pipeline_prompt_facts import PromptFactsMixin
    assert not hasattr(review_module, "PromptFactsReviewMixin")
    assert PromptFactsMixin.__bases__ == (object,)
    assert all(getattr(getattr(PromptFactsMixin, n), "_held_delegate", None) == n for n in REVIEW_FACT_NAMES)
    assert PromptFactsMixin._build_post_exit_reality.__doc__.endswith("src/prompt_facts/review/facts.py.")
