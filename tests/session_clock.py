"""One place that builds a synthetic "this name printed today" timestamp.

WHY THIS EXISTS
---------------
`src/data/live_price.py::resolve_live_price` counts a point-in-time stamp as
this session's only when BOTH hold: its ET date equals the current ET date,
AND it is at or after 09:30 ET (`REGULAR_SESSION_OPEN_MIN`). That second
bound is deliberate and correct — the module's own docstring records the
desk rejecting date-equality alone on 2026-09-18, because a pre-market print
"could sit on the wrong side of the real price on a gap day".

Test helpers used to stamp their synthetic snapshots with `et_now()`. That is
today's date, so it satisfies the first bound — but between 00:00 and 09:30
ET it is BEFORE the open, so it fails the second one. Every synthetic "this
name moved today" snapshot then resolved as no-today-print, and 22 tests
across `test_intraday_scan.py`, `test_intraday_scan_crash_visibility.py` and
`test_invariants.py` went red for fourteen and a half hours of every day
while passing in CI whenever CI happened to run inside the session.

The defect was in the TEST HELPERS, not the resolver: the resolver was doing
exactly what it documents. A synthetic stamp that means "today's print" must
land inside today's regular session, so the wall clock cannot change what the
test asserts.

WHY THE STAMP MOVES AND THE CLOCK DOES NOT
------------------------------------------
Pinning the whole clock is the repo's other idiom for this (`_pin_clock` in
`tests/test_trader_feed.py`, reused by the owner-message tests) and it WOULD
work for the resolver on its own: `live_price_is_today` looks `et_now` up in
`trading_calendar`'s module globals at call time, so one `monkeypatch.setattr`
reaches the production path even though the caller passes `when=None`
(`src/pipeline.py:3246` and `:16404` both call `resolve_live_price(snap)`
with no `when`). An earlier draft of this docstring claimed otherwise; that
claim was wrong and is corrected here.

It is not used because of BLAST RADIUS, not impossibility. The tests being
fixed read the clock for more than the price stamp: the cooldown fixtures in
`test_intraday_scan.py` build their trade rows as `datetime.now(timezone.utc)
- timedelta(minutes=30)` and then assert against a cooldown window measured
from the pipeline's own clock. Freeze `et_now` at a fixed instant and those
real-wall-clock rows land hours away from it, so the cooldown assertions
break for a new reason — trading one clock coupling for another across ~34
call sites. Moving the one stamp that is actually mis-built is the smaller
change and leaves every other clock reader in those tests agreeing with each
other.

The cost of that choice, stated plainly rather than buried: the stamp can sit
in the FUTURE of the wall clock (10:00 ET while the suite runs at 00:49), and
on a weekend it names a session that did not happen. Both are faithful to
what the resolver actually tests — it is pure date-equality plus a
minute-of-day bound, with no future guard and no weekday test, verified in
`src/trading_calendar.py::live_price_is_today` — so neither makes a fixture
assert something the resolver would treat differently. If a future-stamp
guard is ever added to the resolver, this module is the single place that
has to change.

THE TIME ITSELF
---------------
09:30 ET is the NYSE core-session open, a published exchange fact already
held in `REGULAR_SESSION_OPEN_MIN`. 10:00 is a chosen round half-hour inside
it. It is a test-fixture constant, not a desk threshold: nothing in
production reads it, no rule is derived from it, and no test asserts a
behaviour that would differ at 11:00 or 14:00. The assert below is what keeps
it honest — it fails at import if the value ever drifts outside the session
bounds it depends on.
"""

from __future__ import annotations

from datetime import datetime

from src.trading_calendar import (
    REGULAR_SESSION_CLOSE_MIN,
    REGULAR_SESSION_OPEN_MIN,
    et_now,
)

#: Fixed wall-clock time, in minutes past ET midnight, that every synthetic
#: "today's print" is stamped at. Inside the regular session by construction
#: — the assert below is what makes that a fact rather than a comment.
IN_SESSION_MIN = 10 * 60  # 10:00 ET

assert REGULAR_SESSION_OPEN_MIN < IN_SESSION_MIN < REGULAR_SESSION_CLOSE_MIN, (
    "the synthetic in-session stamp must sit strictly inside the regular "
    f"session ({REGULAR_SESSION_OPEN_MIN}-{REGULAR_SESSION_CLOSE_MIN} min ET), "
    f"got {IN_SESSION_MIN}"
)


def todays_session_stamp() -> datetime:
    """Today's ET date at a fixed time inside the regular session.

    Use this anywhere a test builds a synthetic price stamp that is supposed
    to mean "this name printed in the CURRENT session" — `last_trade_at`,
    `minute_bar_at`, or any other field `resolve_live_price` judges. The
    result is independent of what time the suite happens to run.

    A stamp meaning "stale", "a prior session", or "before today's open" must
    NOT come from here — write that datetime explicitly in the test, so the
    intent is visible at the call site.
    """
    return _at(IN_SESSION_MIN)


def todays_session_bar_stamp() -> datetime:
    """Today's ET date at 00:00 — how a DAILY bar is dated, not stamped.

    `resolve_live_price` dates the forming session bar by date-equality
    alone, deliberately: a daily bar carries its OPENING timestamp (00:00
    ET), which is before the open by construction, so the 09:30 bound cannot
    apply to it. This is the correct stamp for `session_bar_at`, and it was
    never the clock-dependent one — it is here so both stamps a snapshot
    needs come from the same place and neither gets copied wrongly from the
    other.
    """
    return _at(0)


def todays_session_snapshot_stamps() -> tuple[datetime, datetime]:
    """`(trade_stamp, bar_stamp)` read off ONE clock call.

    A helper that needs both must not call the two functions above
    separately: a suite running across ET midnight would read the clock
    twice and get a trade stamp and a bar stamp on different DATES, which is
    precisely the "the two halves of one snapshot disagree about which
    session it is" bug board item 120 exists for.
    """
    now = et_now()
    return (
        now.replace(hour=IN_SESSION_MIN // 60, minute=IN_SESSION_MIN % 60,
                    second=0, microsecond=0),
        now.replace(hour=0, minute=0, second=0, microsecond=0),
    )


def _at(minute_of_day: int) -> datetime:
    return et_now().replace(
        hour=minute_of_day // 60,
        minute=minute_of_day % 60,
        second=0,
        microsecond=0,
    )
