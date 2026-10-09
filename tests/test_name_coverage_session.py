"""Boundary witness: NameCoverageRecordSession runs with no pipeline behind it.

Every collaborator is an explicit constructor argument, so the step can be
built from stubs alone and exercised directly.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from src.sessions.name_coverage_session import NameCoverageRecordSession


def _build(**overrides) -> NameCoverageRecordSession:
    params = inspect.signature(NameCoverageRecordSession).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return NameCoverageRecordSession(**kwargs)


def test_constructible_without_a_pipeline_and_exercisable():
    ctx = MagicMock(analyses=[], specialist_results={})
    assert _build().run(ctx, MagicMock()) is None
