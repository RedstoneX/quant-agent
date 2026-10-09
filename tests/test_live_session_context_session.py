"""Boundary witness: LiveSessionContextSession runs with no pipeline behind it.

Every collaborator is an explicit constructor argument, so the step can be
built from stubs alone and exercised directly.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from src.sessions.live_session_context_session import LiveSessionContextSession


def _build(**overrides) -> LiveSessionContextSession:
    params = inspect.signature(LiveSessionContextSession).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return LiveSessionContextSession(**kwargs)


def test_constructible_without_a_pipeline_and_exercisable():
    assert isinstance(_build().run([]), dict)
