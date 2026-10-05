"""Three money-path reads must refuse, not fabricate a normal-looking answer.

Each test pairs a FAILING read (which must now reach a distinct, deliberate
state) with the GENUINE answer it used to be indistinguishable from.
"""
from types import SimpleNamespace

import pytest

from src.refusal_errors import (
    SizingPriceUnavailable,
    TradingCalendarUnavailable,
)


# --- 1. the trading-day check -------------------------------------------
def _account_reads(client):
    from src.execution.broker_parts.account_reads import AccountReads

    return AccountReads(
        client=client, shortable_cache={}, fractionable_cache={},
        trading_day_cache={}, session_open_cache={},
    )


class _BoomClient:
    def get_calendar(self, *a, **k):
        raise RuntimeError("alpaca calendar 503")


class _ClosedClient:
    def get_calendar(self, *a, **k):
        return []


def test_calendar_read_failure_is_not_reported_as_a_holiday():
    with pytest.raises(TradingCalendarUnavailable):
        _account_reads(_BoomClient()).is_trading_day()


def test_a_genuine_holiday_still_answers_false():
    assert _account_reads(_ClosedClient()).is_trading_day() is False


def test_pipeline_helper_propagates_the_unknown_state():
    from src.pipeline import TradingPipeline

    broker = SimpleNamespace(
        is_trading_day=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(TradingCalendarUnavailable):
        TradingPipeline._is_trading_day(SimpleNamespace(broker=broker))


def test_pipeline_helper_still_returns_a_real_answer():
    from src.pipeline import TradingPipeline

    broker = SimpleNamespace(is_trading_day=lambda: False)
    assert TradingPipeline._is_trading_day(
        SimpleNamespace(broker=broker)) is False


# --- 2. the evidence gate -----------------------------------------------
def _ctx():
    return SimpleNamespace(
        data_status={"technical": "read"}, analyses=[], run_id="r1",
    )


def test_a_crashing_evidence_gate_refuses_instead_of_proceeding(monkeypatch):
    from src import evidence_gate
    import src.pipeline_halt_gates as halt_gates

    def _boom(_status):
        raise RuntimeError("gate bug")

    monkeypatch.setattr(evidence_gate, "evaluate", _boom)
    result = halt_gates._evidence_gate_skip(
        SimpleNamespace(), _ctx(), "r1", session="intra_check",
    )
    assert result is not None, "a crashing gate must not let the desk decide"
    assert result["gate_unavailable"] is True
    assert result["orders"] == []
    assert "could not be evaluated" in result["reason"]


# --- 3. the sizing price read -------------------------------------------
def _pipeline_with(stamped):
    return SimpleNamespace(broker=SimpleNamespace(
        get_latest_price_stamped=stamped,
        get_intraday_snapshots=lambda syms: {},
        get_latest_price=lambda s: 0.0,
    ))


def test_a_failed_sizing_price_read_is_not_reported_as_no_price():
    from src.pipeline_stages import _today_sizing_price

    def _boom(_symbol):
        raise RuntimeError("quote feed down")

    with pytest.raises(SizingPriceUnavailable):
        _today_sizing_price(_pipeline_with(_boom), "NVDA")


def test_a_genuinely_absent_today_print_still_returns_none():
    from src.execution.broker import LivePrice
    from src.pipeline_stages import _today_sizing_price

    stale = LivePrice(
        price=100.0, source="quote_mid", trade_at=None, is_today=False,
        is_today_print=False,
    )
    assert stale.is_today_print is False
    assert _today_sizing_price(_pipeline_with(lambda s: stale), "NVDA") is None
