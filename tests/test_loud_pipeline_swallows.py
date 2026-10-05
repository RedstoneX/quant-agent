"""A swallowed fault on the sizing-price and trading-day paths leaves a counted row."""
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.pipeline import TradingPipeline
from src.pipeline_stages import _today_sizing_price
from src.sentinel.reconciliation import ReconciliationLog


def _owner():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE reconciliation_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " ran_at TEXT DEFAULT CURRENT_TIMESTAMP, kind TEXT, agreed INTEGER,"
                 " detail TEXT, run_id TEXT)")
    return SimpleNamespace(db=SimpleNamespace(conn=conn), broker=MagicMock()), conn


def test_failed_latest_price_read_is_counted_and_still_returns_none():
    owner, conn = _owner()
    owner.broker = SimpleNamespace(get_latest_price=MagicMock(side_effect=RuntimeError("boom")))
    assert _today_sizing_price(owner, "AAA") is None
    assert ReconciliationLog(conn=conn).status(kind="guarded:stages.sizing_price.latest_read") == "disagreed"


def test_failed_trading_day_check_is_counted_and_fails_closed():
    owner, conn = _owner()
    owner.broker.is_trading_day.side_effect = RuntimeError("boom")
    assert TradingPipeline._is_trading_day(owner) is False
    assert ReconciliationLog(conn=conn).status(kind="guarded:stages.is_trading_day") == "disagreed"
