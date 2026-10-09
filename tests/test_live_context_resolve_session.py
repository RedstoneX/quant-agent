"""Boundary witness: LiveContextResolveSession runs with no pipeline behind it.

Every collaborator is an explicit constructor argument, so the step can be
built from stubs alone and exercised directly.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from src.sessions.live_context_resolve_session import LiveContextResolveSession


def _build(**overrides) -> LiveContextResolveSession:
    params = inspect.signature(LiveContextResolveSession).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return LiveContextResolveSession(**kwargs)


def test_constructible_without_a_pipeline_and_exercisable():
    result = LiveContextResolveSession().run({}, [])
    assert isinstance(result, tuple)
