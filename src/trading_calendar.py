"""Trading-calendar module — single source of truth for US-trading-day semantics.

Everything that encodes "which trading day?" or "are we in session window X?"
goes through here. Prior to this module the same questions were answered in
five different places (util/time, scheduler, broker, pipeline, wrapper.sh)
and drifted — this consolidates them.

What belongs here:
- Timezone primitives (ET / UTC).
- "What trading day are we in?" via ET wall clock.
- "Is this a weekday?" — cheap, no network.
- Session-window definitions (morning/midday/evening/…) and window checks.
- The session-date key string used by daily_pnl and insights rows.

What does NOT belong here:
- Alpaca-calendar queries for *market holidays*. Holiday detection needs a
  live broker connection, so `is_trading_day()` stays on `AlpacaBroker`.
  Callers that only need a weekday heuristic use `is_weekday()` here.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

SessionMode = Literal[
    "earnings_preprocess", "morning", "intra_check", "midday", "close", "evening"
]

# Session windows as (start_minute_of_day, end_minute_of_day) in ET.
# These are the authoritative source. `scripts/run_if_et_window.sh` has the
# same table hardcoded for zero-dep launchd gating; `test_trading_calendar.py`
# asserts the two stay in sync.
SESSION_WINDOWS: dict[str, tuple[int, int]] = {
    "earnings_preprocess": (480, 555),   # 08:00 - 09:15 ET
    "morning":             (570, 720),   # 09:30 - 12:00 ET
    "intra_check":         (570, 960),   # 09:30 - 16:00 ET  (P&L circuit-breaker, no LLM, every 30min tick; NOT subject to once-per-day guard)
    "midday":              (780, 870),   # 13:00 - 14:30 ET  (position reviewer, patient)
    "close":               (930, 960),   # 15:30 - 16:00 ET  (position reviewer, act-on-trigger; 30min width guarantees a 30-min launchd tick lands inside regardless of phase)
    "evening":             (1200, 1320), # 20:00 - 22:00 ET  (reporting only)
}


# US equities regular (core) trading session, minutes of day in ET:
# 09:30-16:00. Source: NYSE "Hours & Calendars" (core trading session
# 9:30 a.m.-4:00 p.m. ET), the same bounds `SESSION_WINDOWS` already uses
# for the intra_check window. Early-close days (13:00 ET) are NOT modelled
# here — the holiday/early-close calendar needs a broker connection (see
# module docstring); on those days this errs toward "bar not yet complete"
# (stale-but-labelled), never toward treating a partial bar as complete.
REGULAR_SESSION_OPEN_MIN = 570   # 09:30 ET
REGULAR_SESSION_CLOSE_MIN = 960  # 16:00 ET

# Existing intra_check fire step — the same 30 used by
# `TradingScheduler._build_intra_check_trigger` (`range(lo, hi+1, 30)`) and
# `scripts/systemd/quant-agent-intra_check.timer` (`OnCalendar=*:0/30`).
# This is the cadence already on the box, not a pad invented after 09:30.
INTRA_CHECK_TICK_MINUTES = 30


def intra_check_tick_minutes() -> int:
    """Existing intra_check cadence in minutes (not a post-open pad)."""
    return INTRA_CHECK_TICK_MINUTES


def intra_tick_minute(when: datetime | None = None) -> int | None:
    """Cadence fire that owns `when` inside the intra_check ET window.

    Floors to ``SESSION_WINDOWS['intra_check']`` start + N times the
    existing tick. A 09:37 leftover of the 09:30 fire still belongs to
    minute 570 (the shared open with morning). None outside the window.
    """
    now = when if when is not None else et_now()
    lo, hi = SESSION_WINDOWS["intra_check"]
    minute = _minute_of_day(now)
    if minute < lo or minute > hi:
        return None
    step = INTRA_CHECK_TICK_MINUTES
    return lo + ((minute - lo) // step) * step


def is_open_session_intra_tick(when: datetime | None = None) -> bool:
    """True when this intra_check fire is the 09:30 open morning also owns.

    Morning and intra_check share ``SESSION_WINDOWS`` start (570). That
    shared open tick is the paid open, not a separate INTRADAY look —
    including leftover minutes until the next existing cadence fire.
    """
    tick = intra_tick_minute(when)
    if tick is None:
        return False
    morning_start, _morning_end = SESSION_WINDOWS["morning"]
    return tick == morning_start


def first_paid_intraday_tick_after(morning_finished: datetime) -> int | None:
    """First existing intra_check cadence minute strictly after morning ended.

    Derived from ``SESSION_WINDOWS['intra_check']`` plus
    ``INTRA_CHECK_TICK_MINUTES``. Not ``09:30 + pad``. None when no later
    fire remains in the window.
    """
    lo, hi = SESSION_WINDOWS["intra_check"]
    finished = _minute_of_day(morning_finished)
    for tick in range(lo, hi + 1, INTRA_CHECK_TICK_MINUTES):
        if tick > finished:
            return tick
    return None


def et_now() -> datetime:
    """Current instant as a timezone-aware datetime in US/Eastern."""
    return datetime.now(ET)


def et_today() -> date:
    """Current trading-day date in US/Eastern.

    Example: host in SGT at 2026-04-18 09:00 SGT → ET is 2026-04-17 21:00 →
    this returns date(2026, 4, 17) — the trading day that just ended.
    """
    return et_now().date()


def to_et(when: datetime) -> datetime:
    """Convert any datetime (naive-UTC or aware) into US/Eastern-aware.

    Naive datetimes are assumed to be UTC — that's how SQLite stores
    `datetime('now')` and how most log timestamps land.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(ET)


def session_date_key(when: datetime | None = None) -> str:
    """ET-trading-day key as 'YYYY-MM-DD'.

    The shared string key for all per-day tables: daily_pnl, insights,
    snapshot directories. Using this everywhere ensures a host in SGT and
    a host in NYC index the same trading session under the same key.
    """
    d = (to_et(when).date() if when is not None else et_today())
    return d.isoformat()


def is_weekday(d: date | None = None) -> bool:
    """True when `d` (defaults to today in ET) is Mon-Fri.

    This is a CHEAP weekday check — it does NOT know about market holidays.
    For authoritative "will the exchange be open?" use `broker.is_trading_day()`,
    which queries Alpaca's official calendar.
    """
    target = d if d is not None else et_today()
    return target.weekday() < 5  # Mon=0 .. Sun=6


def trading_sessions_held(start: date, end: date) -> int:
    """Weekend-aware count of trading sessions between two calendar dates.

    Counts weekdays (Mon-Fri) strictly AFTER `start` up to and including
    `end` — i.e. how many new sessions' worth of price action have occurred
    since `start`. A same-day round trip is 0; consecutive weekdays give the
    same answer as a plain calendar-day count; a Friday-to-Monday hold gives
    1, not the 2-3 calendar days that elapsed, because only one session's
    worth of real price action happened in between.

    2026-09-04 audit follow-up: `exit_guard.noise_band_atr` scales its band
    by `sqrt(sessions)`, citing `levels.py::derive_structural_target`'s
    `sqrt(expected_horizon_sessions)` as precedent — but that precedent
    counts TRADING SESSIONS, and the caller in `pipeline.py` was passing
    calendar days (`(today - entry_date).days`), which include weekends.
    A Friday-to-Monday hold was getting `sqrt(3)` instead of `sqrt(1)`,
    over-widening the band after every weekend/holiday. This function is
    the fix: callers that want the levels.py-style scaling should count
    SESSIONS with this, not calendar days.

    This is a CHEAP approximation — Mon-Fri only, no US market-holiday
    calendar (per the module docstring, holiday detection needs a live
    broker connection via `AlpacaBroker.is_trading_day`). A market holiday
    inside the range is silently counted as a session, so this can still
    overstate the true session count by one per holiday crossed — a small
    residual gap versus the pure weekend case, which it fixes exactly.

    Returns 0 if `end` is not after `start`.
    """
    from datetime import timedelta
    if end <= start:
        return 0
    count = 0
    d = start + timedelta(days=1)
    while d <= end:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


_QUARTER_END_MONTHS = (3, 6, 9, 12)


def quarter_of(d: date | None = None) -> int:
    """Return the calendar quarter (1-4) that `d` (default today-ET) falls in."""
    target = d if d is not None else et_today()
    return (target.month - 1) // 3 + 1


def quarter_label(d: date | None = None) -> str:
    """'YYYY-QN' label — the key used by data/evolution/ subdirectories."""
    target = d if d is not None else et_today()
    return f"{target.year}-Q{quarter_of(target)}"


def is_last_business_day_of_quarter(d: date | None = None) -> bool:
    """True when `d` is the last Mon-Fri of a quarter-end month.

    CHEAP — weekday-only. Ignores market holidays. The quarterly meta-
    reflector scheduler uses this for the coarse "are we near quarter end?"
    check; the broker-calendar-aware version (`broker.is_last_trading_day
    _of_quarter`) tightens it on early-close/holiday days so the reflection
    doesn't fire on the wrong date (e.g. Dec 31 is Sunday in some years,
    last trading day is Dec 29).
    """
    from datetime import timedelta
    target = d if d is not None else et_today()
    if target.month not in _QUARTER_END_MONTHS:
        return False
    if target.weekday() >= 5:
        return False  # Sat/Sun can't be last business day anyway
    # Walk forward day-by-day through the remainder of the month; if we find
    # any later weekday still inside the same month, `target` isn't last.
    probe = target + timedelta(days=1)
    while probe.month == target.month:
        if probe.weekday() < 5:
            return False
        probe += timedelta(days=1)
    return True


def _minute_of_day(when: datetime) -> int:
    et = to_et(when)
    return et.hour * 60 + et.minute


def in_session_window(mode: SessionMode, when: datetime | None = None) -> bool:
    """True when `when` (defaults to now) falls inside this mode's ET window.

    Inclusive of both endpoints — matches wrapper.sh's `-lt`/`-gt` semantics.
    Weekend short-circuits to False, mirroring the wrapper.
    """
    now = when if when is not None else et_now()
    if not is_weekday(to_et(now).date()):
        return False
    window = SESSION_WINDOWS.get(mode)
    if window is None:
        raise ValueError(f"unknown session mode: {mode}")
    lo, hi = window
    minute = _minute_of_day(now)
    return lo <= minute <= hi


def in_regular_session(when: datetime | None = None) -> bool:
    """True while the regular US equities session is in progress.

    [09:30, 16:00) ET on a weekday. While this is True, TODAY's daily bar is
    still forming: any price-vs-level comparison must use a live price, and
    the completed-bar series ends at the previous session. Holidays are not
    known here (weekday heuristic); a holiday reads as "in session" but the
    live-price freshness check (`live_price_is_today`) then marks the price
    stale because no trade prints today.
    """
    now = when if when is not None else et_now()
    if not is_weekday(to_et(now).date()):
        return False
    minute = _minute_of_day(now)
    return REGULAR_SESSION_OPEN_MIN <= minute < REGULAR_SESSION_CLOSE_MIN


def last_completed_bar_date(when: datetime | None = None) -> date:
    """Latest calendar date whose DAILY bar can be complete at `when`.

    On a weekday at/after 16:00 ET that is today; at any other time it is
    the previous calendar day. The result is a filter bound (`bar.date <=
    this`), not a claim that a bar exists on that date — weekends and
    holidays simply have no bar, so the bound needs no holiday calendar.
    """
    from datetime import timedelta

    now = when if when is not None else et_now()
    d = to_et(now).date()
    if is_weekday(d) and _minute_of_day(now) >= REGULAR_SESSION_CLOSE_MIN:
        return d
    return d - timedelta(days=1)


def live_price_is_today(last_trade_at, when: datetime | None = None) -> bool:
    """True when a provider trade timestamp falls on the current ET date.

    A snapshot's last trade from a prior session (holiday, halt, feed gap)
    is NOT a live price for today and must be labelled stale, never shown as
    current. A missing or naive timestamp is treated as not-today (unknown
    freshness fails visible rather than passing as live).
    """
    if last_trade_at is None or not isinstance(last_trade_at, datetime):
        return False
    if last_trade_at.tzinfo is None:
        return False
    now = when if when is not None else et_now()
    return to_et(last_trade_at).date() == to_et(now).date()


def format_window(mode: SessionMode) -> str:
    """Human-friendly 'HH:MM-HH:MM ET' rendering — for logs and tests."""
    lo, hi = SESSION_WINDOWS[mode]
    return (
        f"{time(lo // 60, lo % 60).strftime('%H:%M')}"
        f"-{time(hi // 60, hi % 60).strftime('%H:%M')} ET"
    )
