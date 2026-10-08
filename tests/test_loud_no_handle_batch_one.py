"""Handlers that bound no exception name now leave a counted row."""
from unittest.mock import MagicMock

from src.data.technical import atr_for_symbol
from src.market_session import _calendar_edge


def test_atr_failure_is_recorded_and_returns_none(monkeypatch):
    rec = MagicMock()
    monkeypatch.setattr("src.data.technical.record_swallowed", rec)
    market = MagicMock()
    market.get_bars.side_effect = RuntimeError("boom")
    market.get_ohlcv.side_effect = RuntimeError("boom")
    assert atr_for_symbol(market, "AAA") is None
    rec.assert_called_once()
    assert rec.call_args.args[0] == "data.technical.atr_for_symbol"


def test_calendar_edge_failure_is_recorded(monkeypatch):
    rec = MagicMock()
    monkeypatch.setattr("src.market_session.record_swallowed", rec)
    broker = MagicMock()
    broker.get_open.side_effect = RuntimeError("boom")
    problems: list[str] = []
    assert _calendar_edge(broker, "get_open", None, problems) is None
    rec.assert_called_once()
    assert problems
