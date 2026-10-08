"""Boundary witness: QuarterlyMetaReflectionSession runs with no pipeline behind it.

Every collaborator is an explicit constructor argument, so the step can be
built from stubs alone and exercised directly.
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from src.sessions.quarterly_meta_session import QuarterlyMetaReflectionSession


def _build(**overrides) -> QuarterlyMetaReflectionSession:
    params = inspect.signature(QuarterlyMetaReflectionSession).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return QuarterlyMetaReflectionSession(**kwargs)


def test_constructible_without_a_pipeline_and_exercisable():
    broker = MagicMock()
    broker.is_last_trading_day_of_quarter.return_value = False
    result = _build(broker=broker).run(force=False)
    assert isinstance(result, dict)
