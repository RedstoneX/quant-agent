"""Read-only trading-day helpers.

Split out of `src.coverage_watchdog` so read-only callers (the dashboard's
import closure, via `src.refusal_signature`) get the calendar walk without
reaching the write-capable repair code under `src/execution/`. Imports only
`src.silence_watchdog` and `src.trading_calendar` (both stdlib-only closures);
`tests/test_api_cannot_trade.py` pins that.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from src.silence_watchdog import SLACK_MINUTES
from src.trading_calendar import ET, SESSION_WINDOWS

# Same logger the watchdog used, so existing log filters keep matching.
logger = logging.getLogger("src.coverage_watchdog")

#: How many weekdays back to look for the most recent trading day. A long
#: weekend plus a holiday is three; five is comfortably past that and bounds
#: the calendar lookups when the broker's calendar cannot be read at all.
MAX_WEEKDAYS_BACK = 5


def most_recent_trading_day(now: datetime, broker: Any = None, on_error: Any = None) -> date:
    """The most recent weekday whose cash session has already ENDED (plus
    the timer slack) and that the broker's calendar confirms as a trading
    day.

    "Already ended" rather than "strictly before today": at the 06:15 ET
    run this is yesterday either way, but run by hand at 23:50 ET on a
    Friday it must judge Friday, not Thursday — a session that has not
    finished cannot yet have failed to re-place anything, and one that has
    finished can. Holidays are excluded through `broker.is_trading_day`
    when available. A calendar-read exception falls back immediately to the
    candidate below. A pathological all-False answer could otherwise walk
    PAST a real trading day and judge a holiday-free week as "no session,
    because there was no day" — the bounded walk therefore falls back to the
    most recent plain weekday, which can only over-alert on a holiday, never
    suppress a real gap.
    """
    today_et = now.astimezone(ET).date()
    _start, today_end = _session_bounds_utc(today_et)
    candidate = today_et if now >= today_end else today_et - timedelta(days=1)
    first_weekday: date | None = None
    checked = 0
    while checked < MAX_WEEKDAYS_BACK:
        if candidate.weekday() < 5:
            if first_weekday is None:
                first_weekday = candidate
            checked += 1
            if broker is None:
                return candidate
            try:
                if broker.is_trading_day(candidate):
                    return candidate
            except Exception as exc:  # noqa: BLE001
                if on_error is not None:
                    on_error(exc)  # caller records it; this module stays read-only
                logger.warning("coverage watchdog: calendar lookup failed for %s: %s", candidate, exc)
                return candidate
        candidate -= timedelta(days=1)
    return first_weekday or (today_et - timedelta(days=1))


def _session_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """[09:30 ET, 16:00 ET + SLACK_MINUTES) for `day`, in UTC. Only a session
    completing inside the cash session can have re-placed a DAY stop; the
    evening run sees a shut market and, correctly, places nothing."""
    lo, hi = SESSION_WINDOWS["intra_check"]
    midnight = datetime(day.year, day.month, day.day, tzinfo=ET)
    start = midnight + timedelta(minutes=lo)
    end = midnight + timedelta(minutes=hi + SLACK_MINUTES)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)
