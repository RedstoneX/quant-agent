"""ONE answer to "is the market open right now", for every caller.

Two copies of this question used to disagree on the failure path: the
coverage watchdog's gate failed CLOSED (a naked position stayed naked), the
protection pipeline's failed OPEN. A single function now answers for both.

Order of evidence, each tried only when the one before could not answer:
  1. the broker's exchange calendar (trading day, session open, session
     close), read twice (a retry) before it is called unreadable;
  2. the weekday-and-clock check `trading_calendar.in_regular_session`;
  3. only when BOTH cannot answer: OPEN.

WHY OPEN. A positive "shut" answer (holiday, before the open, after the
close, from either source) is honoured. An UNKNOWN is never a reason to do
nothing (owner, 2026-10-02): a GTC protective stop resting through a shut
session is a safe state, and the broker rejects an order it genuinely
cannot accept, so trying costs one rejected order that is recorded, while
refusing costs a naked position. No new number: the retry count is one
repeat of the same read, not a tuned threshold.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from src.trading_calendar import ET, in_regular_session

logger = logging.getLogger("src.market_session")

def _read_calendar(broker: Any, now: datetime) -> tuple[bool, str]:
    """Positive answer from the broker calendar, or raise if unreadable."""
    today = now.astimezone(ET).date()
    if not broker.is_trading_day(today):
        return False, f"{today} is not a trading day"
    opens = broker.get_session_open(today)
    closes = broker.get_session_close(today)
    if opens is None or closes is None:
        raise ValueError("the broker's calendar did not give both session edges")
    if now < opens:
        return False, f"the session has not opened yet (opens {opens:%H:%M %Z})"
    if now >= closes:
        return False, f"the session has closed (closed {closes:%H:%M %Z})"
    return True, f"the session is open until {closes:%H:%M %Z}"


def market_open_verdict(broker: Any, now: datetime) -> tuple[bool, str]:
    """`(open_now, reason)`. See the module docstring for the failure rule."""
    last_exc: Exception | None = None
    for _attempt in ("first read", "one repeat of the same read"):
        try:
            return _read_calendar(broker, now)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    try:
        is_open = in_regular_session(now)
    except Exception as exc2:  # noqa: BLE001
        logger.warning(
            "market-open: calendar (%s) and weekday/clock (%s) both unreadable "
            "— answering OPEN", last_exc, exc2,
        )
        return True, (
            f"calendar ({last_exc}) and clock ({exc2}) unreadable — treated as open"
        )
    return is_open, (
        f"broker calendar unreadable ({last_exc}); weekday/clock check says "
        f"{'open' if is_open else 'shut'}"
    )
