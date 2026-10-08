"""Boundary witness: ExpectedSessionsMissingSession runs with no pipeline behind it.

Every collaborator is an explicit constructor argument, so the step can be
built from stubs alone and exercised directly.
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from src.sessions.expected_sessions_session import ExpectedSessionsMissingSession


def _build(**overrides) -> ExpectedSessionsMissingSession:
    params = inspect.signature(ExpectedSessionsMissingSession).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return ExpectedSessionsMissingSession(**kwargs)


def test_constructible_without_a_pipeline_and_exercisable():
    db = MagicMock()
    db.session_prefixes_logged_on.side_effect = RuntimeError("boom")
    assert _build(db=db).run() == []
