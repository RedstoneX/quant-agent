"""Boundary witness: EveningStopProximitySession runs with no pipeline behind it.

Every collaborator is an explicit constructor argument, so the step can be
built from stubs alone and exercised directly.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from src.sessions.evening_stop_proximity_session import EveningStopProximitySession


def _build(**overrides) -> EveningStopProximitySession:
    params = inspect.signature(EveningStopProximitySession).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return EveningStopProximitySession(**kwargs)


def test_constructible_without_a_pipeline_and_exercisable():
    assert _build().run([]) == []
