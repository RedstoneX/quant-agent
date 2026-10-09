"""Calendar availability stays distinct from a confirmed closed market."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from src.execution.broker import AlpacaBroker
from tests.pipeline_factory import build_pipeline


@patch("src.execution.broker.TradingClient")
def test_successful_empty_calendar_is_a_cached_holiday(mock_tc_cls):
    client = MagicMock()
    client.get_calendar.return_value = []
    mock_tc_cls.return_value = client
    broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)
    target = date(2026, 12, 25)

    assert broker.is_trading_day(target) is False
    assert broker.is_trading_day(target) is False
    client.get_calendar.assert_called_once()


@pytest.mark.parametrize(
    "entrypoint",
    [
        "run_earnings_preprocess",
        "run_morning",
        "run_intra_check",
        "run_intra_safety",
        "run_midday",
        "run_close",
        "run_evening",
    ],
)
def test_calendar_failure_propagates_before_public_session_work(entrypoint):
    """An unavailable calendar is not a successful market-holiday session."""
    pipeline = build_pipeline(broker=MagicMock())
    pipeline.broker.is_trading_day.side_effect = RuntimeError("calendar 503")
    pipeline._activate_cost_session = MagicMock()
    pipeline._kill_switch_halt_result = MagicMock()

    with pytest.raises(RuntimeError, match="calendar 503"):
        getattr(pipeline, entrypoint)()

    pipeline._activate_cost_session.assert_not_called()
    pipeline._kill_switch_halt_result.assert_not_called()
    pipeline.broker.submit_order.assert_not_called()
