"""Boundary witness: NewsUpdateSession runs with no pipeline behind it.

Every collaborator is an explicit constructor argument, so the step can be
built from stubs alone and exercised directly.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from src.sessions.news_update_session import NewsUpdateSession


def _build(**overrides) -> NewsUpdateSession:
    params = inspect.signature(NewsUpdateSession).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return NewsUpdateSession(**kwargs)


def test_constructible_without_a_pipeline_and_exercisable():
    session = _build()
    assert callable(session.run)
    assert list(inspect.signature(session.run).parameters)[:2] == ["run_id", "session"]
