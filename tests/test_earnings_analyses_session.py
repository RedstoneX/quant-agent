"""Boundary witness: EarningsAnalysesLoadSession runs with no pipeline behind it.

Every collaborator is an explicit constructor argument, so the step can be
built from stubs alone and exercised directly.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from src.sessions.earnings_analyses_session import EarningsAnalysesLoadSession


def _build(**overrides) -> EarningsAnalysesLoadSession:
    params = inspect.signature(EarningsAnalysesLoadSession).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return EarningsAnalysesLoadSession(**kwargs)


def test_constructible_without_a_pipeline_and_exercisable():
    result = _build().run("run-1", universe=[])
    assert isinstance(result, tuple) and len(result) == 2
