"""The hermetic MIDDAY session on an existing book.

Midday (13:00 ET) is the PATIENT position review: the reviewer prompt tells
the seat the afternoon is still open and to prefer TRAIL_STOP over SELL when a
name is drifting, not breaking (`_SESSION_DISPOSITION` in
`src/agents/position_reviewer.py`; the scheduler labels it "patient
disposition"). The close test covers the act-on-trigger session; this file
asserts what is specific to midday, from the inputs:

  disposition   the seat is asked with the MIDDAY framing, never the close one;
  patient       a reviewer TRAIL_STOP above the resting stop and below the
                price is executed in place: stop up, nothing sold, nothing
                cancelled, nothing re-submitted;
  no new sell   a HOLD at midday sells nothing and leaves the stop alone;
  market shut   on a non-trading day, and after an early close, the session
                reports that state, asks no model and touches no order.

Every seat is scripted; every expectation derives from the inputs.
"""

from __future__ import annotations

from datetime import datetime

from src.agents.position_reviewer import PositionReviewerAgent
from tests.e2e_held_book_support import run_held_book
from tests.test_e2e_close_existing_book import (
    INITIAL_STOP,
    ONE_R_PRICE,
    QTY,
    SYMBOL,
    _assert_hermetic,
    _assert_untouched,
    _bars,
    _news_says_nothing,
    _resting_sell_stops,
    _reviewer_says,
    _risk_says_yes,
    HOLD,
)

HOUR = 13
BARS = _bars(end=ONE_R_PRICE - 1.0)  # 97.00: trend intact, short of +1R


def _midday(tmp_path, monkeypatch, actions=HOLD, **kw):
    seen: list[str] = []
    real = PositionReviewerAgent.review

    def _spy(self, *a, **k):
        seen.append(k.get("session_type", "<default>"))
        return real(self, *a, **k)

    monkeypatch.setattr(PositionReviewerAgent, "review", _spy)
    answers = {"position": _reviewer_says(actions), "risk": _risk_says_yes(), "news": _news_says_nothing()}
    out = run_held_book(tmp_path, monkeypatch, session="midday", hour=HOUR, bars=BARS, answers=answers, **kw)
    return (*out[:4], seen)


def test_midday_asks_the_reviewer_with_the_midday_framing(tmp_path, monkeypatch):
    result, trace, trading, attempts, seen = _midday(tmp_path, monkeypatch)
    _assert_hermetic(result, trace, trading, attempts)
    assert seen == ["midday"], seen
    _assert_untouched(trading, INITIAL_STOP)


def test_midday_executes_a_reviewer_trail_stop_in_place_and_sells_nothing(
    tmp_path,
    monkeypatch,
):
    new_stop = INITIAL_STOP + 2.0  # 94.00: above 92, below 97
    assert INITIAL_STOP < new_stop < BARS[-1].close
    result, trace, trading, attempts, _ = _midday(
        tmp_path,
        monkeypatch,
        [
            {
                "action": "TRAIL_STOP",
                "symbol": SYMBOL,
                "reason": "drifting not breaking; lock part of the gain",
                "new_stop_price": new_stop,
            }
        ],
    )
    _assert_hermetic(result, trace, trading, attempts)
    stops = _resting_sell_stops(trading)
    assert [(s.stop_price, s.qty) for s in stops] == [(new_stop, QTY)], (
        [s.as_plain() for s in stops],
        trading.amended,
        trading.cancelled,
    )
    sells = [o for o in trading.submitted if "stop" not in o.order_type]
    assert sells == [] and trading.cancelled == [], ([o.as_plain() for o in trading.submitted], trading.cancelled)


def _assert_asked_nothing(result, trace, trading, attempts, seen, status):
    assert attempts == [], attempts
    assert result["status"] == status, result
    assert result["orders"] == [] and result["positions"] == 0, result
    assert seen == [], f"a model seat was asked on a shut market: {seen}"
    assert [k for k, _ in trace if k == "llm"] == [], trace
    assert trading.submitted == [] and trading.cancelled == [], (trading.submitted, trading.cancelled)
    assert trading.amended == [], trading.amended
    assert [(s.stop_price, s.qty) for s in _resting_sell_stops(trading)] == [(INITIAL_STOP, QTY)]


def test_midday_on_a_non_trading_day_reports_it_and_does_nothing(
    tmp_path,
    monkeypatch,
):
    # A reviewer that WOULD move the stop, if anything were allowed to act.
    result, trace, trading, attempts, seen = _midday(
        tmp_path,
        monkeypatch,
        [{"action": "TRAIL_STOP", "symbol": SYMBOL, "reason": "x", "new_stop_price": INITIAL_STOP + 2.0}],
        trading_day=False,
    )
    _assert_asked_nothing(result, trace, trading, attempts, seen, "market_holiday")


def test_midday_after_an_early_close_reports_it_and_does_nothing(
    tmp_path,
    monkeypatch,
):
    from src.trading_calendar import ET

    shut_at = datetime(2026, 10, 1, 12, 0, tzinfo=ET)  # closed an hour ago
    result, trace, trading, attempts, seen = _midday(
        tmp_path,
        monkeypatch,
        [{"action": "TRAIL_STOP", "symbol": SYMBOL, "reason": "x", "new_stop_price": INITIAL_STOP + 2.0}],
        session_close=shut_at,
    )
    _assert_asked_nothing(result, trace, trading, attempts, seen, "early_close")
