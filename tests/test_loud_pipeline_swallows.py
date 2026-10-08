"""Swallowed faults on two evening pipeline paths leave a counted row."""
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.pipeline import TradingPipeline
from src.sentinel.reconciliation import ReconciliationLog


def _owner():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE reconciliation_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " ran_at TEXT DEFAULT CURRENT_TIMESTAMP, kind TEXT, agreed INTEGER,"
                 " detail TEXT, run_id TEXT)")
    return SimpleNamespace(db=SimpleNamespace(conn=conn), broker=MagicMock()), conn


def test_failed_quarter_end_check_is_counted_and_returns_none():
    owner, conn = _owner()
    owner.broker.is_last_trading_day_of_quarter.side_effect = RuntimeError("boom")
    assert TradingPipeline._maybe_run_quarterly_meta(owner) is None
    assert ReconciliationLog(conn=conn).status(kind="guarded:stages.meta_quarter_end_check") == "disagreed"


def test_failed_earnings_sweep_is_counted_and_returns_empty(monkeypatch):
    owner, conn = _owner()
    owner.market = object()
    owner._news_held_symbols = lambda positions: ["AAA"]
    owner.config = None
    monkeypatch.setattr("src.data.event_calendar.fetch_earnings_proximity",
                        MagicMock(side_effect=RuntimeError("boom")))
    assert TradingPipeline._evening_earnings_proximity(owner, [{"symbol": "AAA"}]) == []
    assert ReconciliationLog(conn=conn).status(
        kind="guarded:stages.evening_earnings_proximity") == "disagreed"
