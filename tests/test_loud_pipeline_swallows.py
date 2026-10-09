"""Swallowed faults on two evening pipeline paths leave a counted row."""

from unittest.mock import MagicMock

from src.sentinel.reconciliation import ReconciliationLog
from tests.pipeline_factory import build_pipeline


def _status(pipeline, where):
    return ReconciliationLog(conn=pipeline.db.conn).status(kind=f"guarded:stages.{where}")


def test_failed_quarter_end_check_is_counted_and_returns_none():
    broker = MagicMock()
    broker.is_last_trading_day_of_quarter.side_effect = RuntimeError("boom")
    pipeline = build_pipeline(broker=broker)
    assert pipeline._maybe_run_quarterly_meta() is None
    assert _status(pipeline, "meta_quarter_end_check") == "disagreed"


def test_failed_earnings_sweep_is_counted_and_returns_empty(monkeypatch):
    pipeline = build_pipeline(_news_held_symbols=lambda positions: ["AAA"])
    monkeypatch.setattr("src.data.event_calendar.fetch_earnings_proximity", MagicMock(side_effect=RuntimeError("boom")))
    assert pipeline._evening_earnings_proximity([{"symbol": "AAA"}]) == []
    assert _status(pipeline, "evening_earnings_proximity") == "disagreed"
