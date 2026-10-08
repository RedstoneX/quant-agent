"""A sizing price that could not be READ refuses the entry, under its own name.

Before: every read failure inside `_today_sizing_price` returned the same
`None` as a measured "no today print", so a broker error was recorded as a
fact about the stock. Now a failed read raises `SizingPriceUnavailable`, and
every caller refuses the entry as `sizing_price_unreadable`. The measured
absence keeps its old reason and wording. A short is treated exactly like a
long. A failed exchange-calendar read refuses the session rather than
reporting "no session today".
"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

import src.stage_execution as stage_execution
from src.execution.broker import LivePrice
from src.execution.broker_parts.account_reads import AccountReads
from src.models import PortfolioDecision, ReasoningChain, TradeDecision
from src.pipeline_context import RunContext
from src.pipeline_stages import (
    ExecutionStage, _rotation_buy_leg_projected_refusal, _today_sizing_price,
)
from src.refusal_errors import SizingPriceUnavailable
from src.sizing_refusal import (
    NO_SIZING_PRINT, SIZING_PRICE_UNREADABLE, sizing_price_or_refusal,
)

ET = ZoneInfo("America/New_York")


def _boom(*_a, **_k):
    raise ConnectionError("broker read timed out")


# --- the reader: a FAILED read raises; a MEASURED absence stays None --------

@pytest.mark.parametrize("broker", [
    SimpleNamespace(get_latest_price_stamped=_boom),
    SimpleNamespace(get_intraday_snapshots=_boom),
    SimpleNamespace(get_latest_price=_boom),
], ids=["stamped", "snapshot", "bare"])
def test_every_failed_read_raises_rather_than_returning_none(broker):
    with pytest.raises(SizingPriceUnavailable) as err:
        _today_sizing_price(SimpleNamespace(broker=broker), "NVDA")
    assert isinstance(err.value.__cause__, ConnectionError)


def test_measured_absence_still_returns_none():
    stale = LivePrice(price=161.79, source="last_trade",
                      trade_at=datetime(2026, 9, 16, 15, 59, tzinfo=ET),
                      is_today=False, is_today_print=False)
    broker = SimpleNamespace(get_latest_price_stamped=lambda s: stale,
                             get_latest_price=lambda s: stale.price)
    assert _today_sizing_price(SimpleNamespace(broker=broker), "NVDA") is None


# --- the helper: two different names for two different states --------------

def test_unreadable_is_refused_with_its_own_reason_and_logs_traceback(caplog):
    def reader(_p, _s):
        raise SizingPriceUnavailable("NVDA: stamped read failed")

    with caplog.at_level("ERROR", logger="src.sizing_refusal"):
        price, why, detail = sizing_price_or_refusal(reader, None, "NVDA", "buy")
    assert price is None and why == SIZING_PRICE_UNREADABLE
    assert detail.startswith(SIZING_PRICE_UNREADABLE)
    assert any(r.exc_info for r in caplog.records)


@pytest.mark.parametrize("measured", [None, 0, -1.0, True])
def test_measured_absence_keeps_todays_reason_and_wording(measured):
    price, why, detail = sizing_price_or_refusal(
        lambda _p, _s: measured, None, "NVDA", "buy")
    assert price is None and why == NO_SIZING_PRINT
    assert detail.startswith("no today trade print to size the buy against")


def test_a_real_price_passes_through():
    assert sizing_price_or_refusal(lambda _p, _s: 101, None, "X", "buy") == (101.0, "", "")


# --- the rotation buy leg ---------------------------------------------------

def test_rotation_buy_leg_refuses_an_unreadable_sizing_price(monkeypatch):
    def _raise(_p, s):
        raise SizingPriceUnavailable(f"{s}: bare price read failed")

    monkeypatch.setattr("src.pipeline_rotation_exec._today_sizing_price", _raise)
    broker = SimpleNamespace(
        get_latest_price_stamped=lambda s: LivePrice(
            price=110.0, source="last_trade", trade_at=datetime.now(ET),
            is_today=True, is_today_print=True),
        get_latest_price=lambda s: 110.0,
    )
    clearance, gate, detail = _rotation_buy_leg_projected_refusal(
        SimpleNamespace(broker=broker), SimpleNamespace(),
        rotation=SimpleNamespace(),
        buy_decision=SimpleNamespace(symbol="NVDA", action="BUY",
                                     entry_price=110.0, stop_loss=104.0,
                                     allocation_pct=5.0),
        positions=[], total_value=100_000.0, rotation_sell=None, cash=50_000.0,
    )
    assert clearance is None and gate == "no_price"
    assert detail.startswith(SIZING_PRICE_UNREADABLE)


# --- the REAL ExecutionStage submit loop, long and short --------------------

def _exec_pipeline(price: float = 100.0):
    pipeline = MagicMock()
    pipeline.broker.get_latest_price_stamped.return_value = LivePrice(
        price=price, source="last_trade", trade_at=datetime.now(ET),
        is_today=True, is_today_print=True,
    )
    pipeline.broker.get_latest_price.return_value = price
    pipeline.broker.get_latest_quote.return_value = {
        "bid_price": price - 1.0, "ask_price": price + 1.0,
    }
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0}, [], {},
    )
    return pipeline


def _run(decision, monkeypatch, reader):
    skips = []
    monkeypatch.setattr(stage_execution, "_today_sizing_price", reader)
    monkeypatch.setattr(
        stage_execution, "_record_execution_skip",
        lambda _p, _c, sym, reason, *_a, **_k: skips.append((sym, reason)),
    )
    pipeline = _exec_pipeline()
    ctx = RunContext.start("morning")
    ctx.cash, ctx.total_value, ctx.last_equity = 50_000.0, 100_000.0, 100_000.0
    ctx.positions, ctx.symbols_bars = [], {}
    rc = ReasoningChain(macro_filter="x", news_check="x", earnings_check="x",
                        signal_conflicts="x", sizing_logic="x",
                        portfolio_balance="x", cash_target="x")
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=rc, decisions=[decision], portfolio_view="t")
    ExecutionStage(pipeline=pipeline).run(ctx)
    return pipeline, skips


def _decision(action: str) -> TradeDecision:
    long = action == "BUY"
    return TradeDecision(
        action=action, symbol="TSLA", allocation_pct=10, entry_price=100.0,
        stop_loss=94.0 if long else 106.0, take_profit=118.0 if long else 88.0,
        reasoning="new name",
    )


def _unreadable(_p, s):
    raise SizingPriceUnavailable(f"{s}: stamped read failed")


@pytest.mark.parametrize("action", ["BUY", "SHORT"])
def test_unreadable_sizing_price_refuses_the_entry(action, monkeypatch):
    pipeline, skips = _run(_decision(action), monkeypatch, _unreadable)
    assert ("TSLA", SIZING_PRICE_UNREADABLE) in skips
    assert ("TSLA", NO_SIZING_PRINT) not in skips
    pipeline.broker.submit_order.assert_not_called()


@pytest.mark.parametrize("action", ["BUY", "SHORT"])
def test_measured_absence_refuses_exactly_as_before(action, monkeypatch):
    pipeline, skips = _run(_decision(action), monkeypatch, lambda _p, _s: None)
    assert ("TSLA", NO_SIZING_PRINT) in skips
    assert ("TSLA", SIZING_PRICE_UNREADABLE) not in skips
    pipeline.broker.submit_order.assert_not_called()


# --- a failed exchange-calendar read refuses; it is never "no session" ------

def test_failed_calendar_read_raises_and_is_not_cached():
    reads = AccountReads.__new__(AccountReads)
    reads._trading_day_cache = {}
    reads.client = SimpleNamespace(get_calendar=_boom)
    with pytest.raises(ConnectionError):
        reads.is_trading_day(date(2026, 10, 8))
    assert reads._trading_day_cache == {}


def test_pipeline_trading_day_check_propagates_the_failure():
    from tests.pipeline_factory import build_pipeline

    broker = MagicMock(name="broker")
    broker.is_trading_day.side_effect = _boom
    pipe = build_pipeline(broker=broker)
    with pytest.raises(ConnectionError):
        pipe._is_trading_day()
