"""ONE answer to "is the market open right now", for every caller.

Two copies of this question used to disagree on the failure path: the
coverage watchdog's gate failed CLOSED (a naked position stayed naked), the
protection pipeline's failed OPEN. A single function now answers for both.

Order of evidence, each tried only when the one before could not answer:
  1. the broker's exchange calendar (trading day, session open, session
     close), read twice (a retry) before it is called unreadable. Every
     piece that answers is AUTHORITATIVE on its own: an early close, a
     half day and a holiday all come back shut even if the rest of the
     calendar is unreadable, and one unreadable piece never demotes the
     question to the clock;
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
from typing import Any, Callable

from src.time_format import fmt_time_12h
from src.trading_calendar import ET, in_regular_session

logger = logging.getLogger("src.market_session")

def _calendar_edge(
    broker: Any, getter: str, today: Any, problems: list[str],
) -> datetime | None:
    """One session edge, or None when the broker could not give that edge.

    A getter that raises, is absent, or hands back anything that is not a
    datetime has NOT answered. Judged by type and not by truthiness so that
    one unreadable edge cannot be silently compared against `now` — the
    comparison would raise and throw away the OTHER edge, which is exactly
    how an early close came back "open".
    """
    try:
        value = getattr(broker, getter, None)
        value = value(today) if callable(value) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("market-open: %s unreadable for %s: %s", getter, today, exc)
        problems.append(f"{getter}: {exc}")
        return None
    if isinstance(value, datetime):
        return value
    problems.append(f"{getter} gave {value!r}, which is not a session time")
    return None


def _read_calendar(broker: Any, now: datetime) -> tuple[bool, str]:
    """Positive answer from the broker calendar, or raise if unreadable.

    Each piece of the calendar is read on its own and any piece that DOES
    answer is authoritative. Only when no piece bounds the session at all is
    this called unreadable and the clock-based window allowed to speak.
    """
    today = now.astimezone(ET).date()
    problems: list[str] = []
    try:
        is_trading = broker.is_trading_day(today)
    except Exception as exc:  # noqa: BLE001
        logger.warning("market-open: is_trading_day unreadable for %s: %s", today, exc)
        problems.append(str(exc))
        is_trading = None
    if is_trading is not None and not is_trading:
        return False, f"{today} is not a trading day"
    opens = _calendar_edge(broker, "get_session_open", today, problems)
    closes = _calendar_edge(broker, "get_session_close", today, problems)
    # A positively-established "outside the session" wins over everything,
    # including the regular-session window. An early close (13:00) and a
    # half day are precisely the case where the window says open and the
    # calendar says shut; the calendar is right.
    if opens is not None and now < opens:
        return False, f"the session has not opened yet (opens {_stamp(opens)})"
    if closes is not None and now >= closes:
        return False, f"the session has closed (closed {_stamp(closes)})"
    if opens is None or closes is None:
        # Inside no bound we can prove. Not an "open" answer: an unbounded
        # read must not assert a session, so hand the question on.
        # Every reason this read failed travels with the exception: the
        # fallback's own message quotes it, so a calendar outage is named in
        # the owner-visible reason instead of being flattened to "unreadable".
        raise ValueError(
            "the broker's calendar did not give both session edges"
            + (f" ({'; '.join(problems)})" if problems else "")
        )
    return True, f"the session is open until {_stamp(closes)}"


def _stamp(moment: datetime) -> str:
    """Date + clock for an owner-visible session edge, in exchange time."""
    return fmt_time_12h(moment.astimezone(ET))


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


def market_open_now(broker: Any, clock: Callable[[], datetime]) -> bool:
    """`market_open_verdict`, with reading the clock inside the question.

    FAILS TOWARD OPEN when the clock source itself raises, deliberately and
    for the same asymmetry as every other unknown here: the callers use this
    to tell a fractional DAY stop that LAPSED at the close from one that
    FAILED to be placed, and answering "shut" would suppress a real naked-
    position alert. Answering "open" costs a redundant banner. The fail-open
    stays narrow (only a raising clock reaches it) and loud (logged with the
    cause). It may permit only additive/replacement protective-stop work,
    never an entry or ordinary trading decision.
    """
    try:
        now = clock()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "market-open: the clock source is unreadable (%s) — answering OPEN "
            "so a naked-position alert is never suppressed", exc,
        )
        return True
    return market_open_verdict(broker, now)[0]
