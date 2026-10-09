"""Boundary witness: EarningsPreprocessSession runs with no pipeline behind it.

Every collaborator is an explicit constructor argument, so the session can be
built from stubs alone. This exercises the earliest return path.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from src.sessions.earnings_preprocess_session import EarningsPreprocessSession


def _build(**overrides) -> EarningsPreprocessSession:
    params = inspect.signature(EarningsPreprocessSession).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return EarningsPreprocessSession(**kwargs)


def test_constructible_without_a_pipeline_and_skips_on_holiday():
    session = _build(is_trading_day=lambda: False)
    result = session.run()
    assert result == {"status": "market_holiday", "run_id": result["run_id"]}
    assert result["run_id"]
