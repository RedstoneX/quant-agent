"""A FAILED price read is never "no price".

Owner ruling 2026-10-08: "if price can't be read, that's a fundamental
absolute deal breaker". The broker read is retried; when it still fails it
raises `PriceReadFailed`. On the entry path the failure is classified by one
read of the session's reference symbol: reference reads -> that one name is
refused (`price_read_failed`), others trade; reference fails too -> the feed
is down, no new entries for the rest of the session (`price_feed_unreadable`),
recorded durably. Absence stays a per-name `no_sizing_print`. Existing stops
are never touched by any of this.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

import src.sizing_refusal as sizing_refusal
from tests.test_broker_market_data import _broker, _price_client
from src.execution import price_read
from src.execution.broker import LivePrice
from src.models import PortfolioDecision, ReasoningChain, TradeDecision
from src.pipeline_context import RunContext
from src.price_feed_preflight import preflight_price_feed, price_feed_session_start
from src.refusal_errors import PriceReadFailed
from src.sizing_refusal import (
    NO_PRINT_AT_OPEN_DEFERRED, NO_SIZING_PRINT, PRICE_FEED_UNREADABLE, PRICE_READ_FAILED,
)
from src.stage_execution import ExecutionStage

ET = ZoneInfo("America/New_York")


def _print(price: float) -> LivePrice:
    return LivePrice(price=price, source="last_trade", trade_at=datetime.now(ET),
                     is_today=True, is_today_print=True)


# --- the read layer: retry, then FAILURE is typed and ABSENCE stays None ----

def test_error_then_success_on_retry_returns_the_price(monkeypatch):
    waits: list[float] = []
    calls = iter([ConnectionError("blip"), _print(101.0)])

    def read_once(_symbol):
        step = next(calls)
        if isinstance(step, Exception):
            raise step
        return step

    got = price_read.read_price_with_retry(read_once, "NVDA", sleep=waits.append)
    assert got.price == 101.0
    assert len(waits) == 1 and waits[0] == price_read.BACKOFF_BASE_S


def test_persistent_error_raises_typed_failure_with_its_cause():
    def read_once(_symbol):
        raise TimeoutError("feed down")

    with pytest.raises(PriceReadFailed) as err:
        price_read.read_price_with_retry(read_once, "NVDA", sleep=lambda _s: None)
    assert isinstance(err.value.__cause__, TimeoutError)
    assert "NVDA" in str(err.value)


def test_absence_is_returned_without_a_retry():
    reads: list[str] = []

    def read_once(symbol):
        reads.append(symbol)
        return None

    assert price_read.read_price_with_retry(read_once, "NVDA", sleep=lambda _s: None) is None
    assert reads == ["NVDA"]


# --- the real entry stage: one name vs the desk -----------------------------

def _decision(symbol: str, action: str = "BUY") -> TradeDecision:
    long = action == "BUY"
    return TradeDecision(
        action=action, symbol=symbol, allocation_pct=10, entry_price=100.0,
        stop_loss=94.0 if long else 106.0, take_profit=118.0 if long else 88.0,
        reasoning="new name",
    )


def _pipeline(stamped_reader, positions=None):
    pipeline = MagicMock()
    pipeline.broker.get_latest_price_stamped = stamped_reader
    pipeline.broker.get_latest_price = lambda s: stamped_reader(s).price
    pipeline.broker.get_latest_quote.return_value = {"bid_price": 99.0, "ask_price": 101.0}
    pipeline.broker.get_intraday_snapshots = lambda syms: {}
    pipeline.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0}, positions or [], {},
    )
    return pipeline


def _run(pipeline, decisions, monkeypatch, positions=None, session="midday"):
    skips: list[tuple[str, str]] = []
    import src.stage_execution as stage_execution
    import src.price_feed_preflight as preflight

    def keep(_p, _c, sym, reason, *_a, **_k):
        skips.append((sym, reason))

    monkeypatch.setattr(stage_execution, "_record_execution_skip", keep)
    monkeypatch.setattr(preflight, "_record_execution_skip", keep)
    ctx = RunContext.start(session)
    ctx.cash, ctx.total_value, ctx.last_equity = 50_000.0, 100_000.0, 100_000.0
    ctx.positions, ctx.symbols_bars = positions or [], {}
    rc = ReasoningChain(macro_filter="x", news_check="x", earnings_check="x",
                        signal_conflicts="x", sizing_logic="x",
                        portfolio_balance="x", cash_target="x")
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=rc, decisions=decisions, portfolio_view="t")
    ExecutionStage(pipeline=pipeline).run(ctx)
    return skips


def _failing_only(bad: set[str]):
    def reader(symbol):
        if symbol in bad:
            raise PriceReadFailed(f"{symbol}: price read failed on all attempts")
        return _print(100.0)
    return reader


def _submitted(pipeline) -> set[str]:
    return {c.kwargs.get("symbol") or c.args[0] for c in pipeline.broker.submit_order.call_args_list}


@pytest.mark.parametrize("action", ["BUY", "SHORT"])
def test_one_name_failing_while_the_reference_reads_refuses_only_that_name(action, monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(sizing_refusal, "record_guarded_outcome",
                        lambda **kw: rows.append(kw))
    pipeline = _pipeline(_failing_only({"BAD"}))
    skips = _run(pipeline, [_decision("GOOD", action), _decision("BAD", action)], monkeypatch)
    assert ("BAD", PRICE_READ_FAILED) in skips
    price_reasons = {PRICE_READ_FAILED, PRICE_FEED_UNREADABLE, NO_SIZING_PRINT}
    assert not [r for sym, r in skips if sym == "GOOD" and r in price_reasons]
    assert "BAD" not in _submitted(pipeline)
    if action == "BUY":  # a SHORT dies later on the MagicMock borrow gate, not on price
        assert "GOOD" in _submitted(pipeline)
    assert sizing_refusal.price_feed_fault(pipeline) is None
    # The failed name was counted durably through the house ledger helper.
    assert [r["where"] for r in rows] == ["price_feed.single_name"]
    assert isinstance(rows[0]["exc"], PriceReadFailed)


def test_reference_failing_too_halts_every_further_entry_this_session(monkeypatch):
    # The reference symbol is the first approved entry (no positions held):
    # GOOD reads at preflight, then BAD fails and GOOD now fails too.
    state = {"reads": 0}

    def reader(symbol):
        state["reads"] += 1
        if symbol == "BAD" or state["reads"] > 1:
            raise PriceReadFailed(f"{symbol}: price read failed on all attempts")
        return _print(100.0)

    pipeline = _pipeline(reader)
    skips = _run(pipeline, [_decision("GOOD"), _decision("BAD"), _decision("LATER")], monkeypatch)
    assert ("BAD", PRICE_FEED_UNREADABLE) in skips
    assert ("LATER", PRICE_FEED_UNREADABLE) in skips
    assert pipeline.broker.submit_order.call_count <= 1  # GOOD may have gone before the fault
    fault = sizing_refusal.price_feed_fault(pipeline)
    assert fault and fault.startswith(PRICE_FEED_UNREADABLE)
    # Later entries in the SAME session are refused before any read happens.
    reads_before = state["reads"]
    price, why, detail = sizing_refusal.sizing_price_or_refusal(
        lambda _p, _s: 100.0, pipeline, "ANOTHER", "buy")
    assert (price, why) == (None, PRICE_FEED_UNREADABLE) and detail == fault
    assert state["reads"] == reads_before


def test_absence_is_a_per_name_skip_and_other_names_still_trade(monkeypatch):
    stale = LivePrice(price=100.0, source="quote_mid", trade_at=datetime.now(ET),
                      is_today=True, is_today_print=False)

    def reader(symbol):
        return stale if symbol == "THIN" else _print(100.0)

    pipeline = _pipeline(reader)
    skips = _run(pipeline, [_decision("GOOD"), _decision("THIN")], monkeypatch)
    assert ("THIN", NO_SIZING_PRINT) in skips
    assert ("THIN", PRICE_READ_FAILED) not in skips
    assert "GOOD" in _submitted(pipeline)
    assert sizing_refusal.price_feed_fault(pipeline) is None


def test_preflight_failure_places_no_entries_and_records_why(monkeypatch):
    # Nothing held, so the reference is the first approved entry; it fails
    # at the preflight and NO name is read for sizing afterwards.
    reads: list[str] = []

    def reader(symbol):
        reads.append(symbol)
        if symbol == "GOOD":
            raise PriceReadFailed("GOOD: price read failed on all attempts")
        return _print(100.0)

    pipeline = _pipeline(reader)
    skips = _run(pipeline, [_decision("GOOD"), _decision("ALSO")], monkeypatch)
    assert ("GOOD", PRICE_FEED_UNREADABLE) in skips and ("ALSO", PRICE_FEED_UNREADABLE) in skips
    assert reads == ["GOOD"]
    pipeline.broker.submit_order.assert_not_called()
    fault = sizing_refusal.price_feed_fault(pipeline)
    assert fault and "GOOD" in fault and "preflight" in fault
    # Existing protection is untouched: nothing cancelled, replaced or moved.
    for name in ("cancel_order", "replace_order", "cancel_all_orders", "amend_stop"):
        getattr(pipeline.broker, name).assert_not_called()


def test_preflight_uses_a_held_name_first_and_resets_last_sessions_fault():
    pipeline = SimpleNamespace(broker=SimpleNamespace(get_latest_price_stamped=lambda s: None),
                               price_feed_fault="price_feed_unreadable: yesterday", db=None)
    ctx = SimpleNamespace(positions=[{"symbol": "HELD"}],
                          portfolio_decision=SimpleNamespace(decisions=[_decision("NEW")]))
    assert price_feed_session_start(pipeline, ctx) is ctx
    assert pipeline.price_feed_fault is None
    assert pipeline.price_feed_reference == "HELD"
    # Absence at the reference (None) is the feed answering: entries proceed.
    decisions = [_decision("NEW")]
    assert preflight_price_feed(pipeline, ctx, decisions) is decisions


# --- the open: no print yet is DEFERRED, not refused ------------------------

def test_no_print_in_the_opening_session_is_deferred_then_sized_when_it_prints(monkeypatch):
    state = {"printed": False}
    quote_only = LivePrice(price=100.0, source="quote_mid", trade_at=datetime.now(ET),
                           is_today=True, is_today_print=False)

    def reader(symbol):
        return _print(100.0) if state["printed"] else quote_only

    pipeline = _pipeline(reader)
    skips = _run(pipeline, [_decision("EARLY")], monkeypatch, session="morning")
    assert ("EARLY", NO_PRINT_AT_OPEN_DEFERRED) in skips
    assert ("EARLY", NO_SIZING_PRINT) not in skips
    assert "EARLY" in pipeline.price_feed_deferred
    pipeline.broker.submit_order.assert_not_called()
    # The desk's next existing pass, after the feed has printed: sized normally.
    state["printed"] = True
    skips = _run(pipeline, [_decision("EARLY")], monkeypatch, session="intra_check")
    assert "EARLY" in _submitted(pipeline)
    assert "EARLY" not in pipeline.price_feed_deferred


def test_no_print_outside_the_opening_session_stays_a_per_name_refusal(monkeypatch):
    quote_only = LivePrice(price=100.0, source="quote_mid", trade_at=datetime.now(ET),
                           is_today=True, is_today_print=False)
    pipeline = _pipeline(lambda s: quote_only)
    skips = _run(pipeline, [_decision("THIN")], monkeypatch, session="midday")
    assert ("THIN", NO_SIZING_PRINT) in skips
    assert ("THIN", NO_PRINT_AT_OPEN_DEFERRED) not in skips


# --- the real AlpacaBroker read: retried, then typed ------------------------

def test_latest_price_raises_typed_failure_when_the_data_api_raises(monkeypatch):
    """A market-data outage is retried, then raised TYPED -- never "no price"."""
    from src.execution import price_read
    from src.refusal_errors import PriceReadFailed

    waits = []
    monkeypatch.setattr(price_read, "_sleep", waits.append)
    b = _broker()
    b._data_client = _price_client(raises=ConnectionError("data.alpaca.markets down"))
    with pytest.raises(PriceReadFailed) as err:
        b.get_latest_price("NVDA")
    assert isinstance(err.value.__cause__, ConnectionError)
    assert b._data_client.get_stock_latest_trade.call_count == 1 + price_read.MAX_RETRIES
    assert len(waits) == price_read.MAX_RETRIES
