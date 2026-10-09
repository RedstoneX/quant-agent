"""The one market-open answer, exercised through the watchdog's gate.

`src.market_session.market_open_verdict` is the single answer shared by the
coverage watchdog and the protection pipeline. These tests used to live in
tests/test_coverage_watchdog.py; they are about the session gate rather than
about coverage, and that file had reached its size ceiling.

THE RULE THEY PIN. Every piece of the broker's exchange calendar that
ANSWERS is authoritative on its own — an early close, a half day and a
holiday all come back SHUT even when the regular-session window would say
open. The weekday-and-clock window is a last resort used only when the
calendar bounds nothing, and the fail-open is reached only when both
sources are unreadable, where it is logged and the reason names the cause.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src import coverage_watchdog
from src.execution.broker import AlpacaBroker
from src.trading_calendar import ET

_FRI_1005 = datetime(2026, 9, 11, 10, 5, tzinfo=ET).astimezone(timezone.utc)


# ---- confirmed closed answers stop placement; unreadable may fall open ----


def test_session_gate_says_shut_on_a_non_trading_day():
    broker = MagicMock()
    broker.is_trading_day.return_value = False
    open_now, reason = coverage_watchdog.session_is_open(broker, _FRI_1005)
    assert open_now is False and "not a trading day" in reason


def test_session_gate_falls_back_to_the_clock_when_the_calendar_cannot_be_read():
    broker = MagicMock()
    broker.is_trading_day.side_effect = RuntimeError("calendar down")
    open_now, reason = coverage_watchdog.session_is_open(broker, _FRI_1005)
    assert open_now is True and "calendar down" in reason


@patch("src.execution.broker.TradingClient")
def test_real_broker_calendar_failure_reaches_protective_fallback(mock_tc_cls):
    """Exercise the real AlpacaBroker -> AccountReads boundary, not a mock that
    raises above the broker's calendar error handling.

    The fallback is intentionally confined to this protective-stop gate; public
    trading sessions use ``TradingPipeline._is_trading_day`` and propagate.
    """
    client = MagicMock()
    client.get_calendar.side_effect = RuntimeError("calendar down")
    mock_tc_cls.return_value = client
    broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)

    open_now, reason = coverage_watchdog.session_is_open(broker, _FRI_1005)

    assert open_now is True
    assert "calendar down" in reason


@patch("src.execution.broker.TradingClient")
def test_real_broker_successful_empty_calendar_stays_shut(mock_tc_cls):
    client = MagicMock()
    client.get_calendar.return_value = []
    mock_tc_cls.return_value = client
    broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)

    open_now, reason = coverage_watchdog.session_is_open(broker, _FRI_1005)

    assert open_now is False
    assert "not a trading day" in reason
    client.get_calendar.assert_called_once()


def test_session_gate_falls_back_to_the_clock_when_an_edge_is_missing():
    broker = MagicMock()
    broker.is_trading_day.return_value = True
    broker.get_session_open.return_value = None
    broker.get_session_close.return_value = datetime(2026, 9, 11, 16, 0, tzinfo=ET)
    open_now, reason = coverage_watchdog.session_is_open(broker, _FRI_1005)
    assert open_now is True and "both session edges" in reason


def test_session_gate_respects_an_early_close_from_the_calendar():
    """13:00 on a half-day is the calendar's answer, not ours. Nothing here
    may assume 16:00."""
    broker = MagicMock()
    broker.is_trading_day.return_value = True
    broker.get_session_open.return_value = datetime(2026, 9, 11, 9, 30, tzinfo=ET)
    broker.get_session_close.return_value = datetime(2026, 9, 11, 13, 0, tzinfo=ET)
    after = datetime(2026, 9, 11, 14, 0, tzinfo=ET).astimezone(timezone.utc)
    open_now, reason = coverage_watchdog.session_is_open(broker, after)
    assert open_now is False and "has closed" in reason
    open_now, _ = coverage_watchdog.session_is_open(broker, _FRI_1005)
    assert open_now is True


def test_an_unreadable_calendar_still_answers_shut_outside_market_hours():
    """Failing OPEN must not degrade into answering open at every hour.

    The ruling is that an unknown must not stop the desk acting, not that the
    desk should pretend the market is always open. With the broker calendar
    unreadable, the weekday-and-clock fallback still decides, and its "shut"
    is honoured exactly as its "open" is -- otherwise a stop would be
    attempted at three in the morning on every calendar outage.
    """
    broker = MagicMock()
    broker.is_trading_day.side_effect = RuntimeError("calendar down")
    after_hours = datetime(2026, 9, 11, 20, 30, tzinfo=ET)

    open_now, reason = coverage_watchdog.session_is_open(broker, after_hours)

    assert open_now is False
    assert "calendar down" in reason


def test_a_holiday_is_shut_even_when_the_session_window_says_open():
    """The exact shape of the bug this file was split out for: the calendar
    answers "not a trading day" while 10:05 on a weekday sits squarely
    inside the regular-session window. The calendar wins."""
    broker = MagicMock()
    broker.is_trading_day.return_value = False
    broker.get_session_open.side_effect = RuntimeError("no session on a holiday")
    broker.get_session_close.side_effect = RuntimeError("no session on a holiday")
    open_now, reason = coverage_watchdog.session_is_open(broker, _FRI_1005)
    assert open_now is False
    assert "not a trading day" in reason


def test_an_early_close_wins_over_the_window_even_with_no_readable_open():
    """One unreadable edge must not throw away the edge that DID answer.
    13:30 on a 13:00 half day is shut, with the session open unreadable."""
    broker = MagicMock()
    broker.is_trading_day.return_value = True
    broker.get_session_open.side_effect = RuntimeError("open time unreadable")
    broker.get_session_close.return_value = datetime(2026, 11, 27, 13, 0, tzinfo=ET)
    half_day = datetime(2026, 11, 27, 13, 30, tzinfo=ET).astimezone(timezone.utc)
    open_now, reason = coverage_watchdog.session_is_open(broker, half_day)
    assert open_now is False
    assert "has closed" in reason
