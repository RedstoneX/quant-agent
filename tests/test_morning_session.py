"""Boundary witness: MorningSession runs with no pipeline behind it.

Every collaborator is an explicit constructor argument, so the session can be
built from stubs alone. This exercises the earliest return path.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from src.sessions.morning_session import MorningSession


def _build(**overrides) -> MorningSession:
    params = inspect.signature(MorningSession).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return MorningSession(**kwargs)


def test_constructible_without_a_pipeline_and_skips_on_holiday():
    session = _build(is_trading_day=lambda: False)
    result = session.run()
    assert result["status"] == "market_holiday"
    assert result["run_id"]
