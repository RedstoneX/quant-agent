"""Session calendar — the real open/close of one trading date, and the phase
offsets expressed RELATIVE to those two edges.

Why this exists
---------------
`src/trading_calendar.SESSION_WINDOWS` pins every phase of the desk's day to a
FIXED minute of the Eastern clock (midday is 13:00-14:30, close is 15:30-16:00,
and so on). Those minutes are only correct on a normal 09:30-16:00 session. On
an early close the end-of-day position review fires after the market has
already shut; on a holiday every weekday phase fires anyway; and the desk's
most expensive paid tick keeps running against a closed market.

This module is the SOURCE the later conversions read. It converts nothing: no
caller is changed by this file landing, and behaviour only moves once a reader
is switched over to `phase_window()`.

Two properties matter more than anything else here:

1.  It FAILS LOUDLY when the exchange calendar cannot be read. The broker's
    `is_trading_day` now raises on failure, but the two edge reads this module
    needs still log and return None. At that edge surface, "today is a holiday"
    and "Alpaca did not answer" are therefore the same answer. Quietly treating
    an outage as a holiday, or worse as a normal 09:30-16:00 session,
    reintroduces exactly the bug this is here to remove, invisibly.
    `SessionCalendarUnavailable` is raised instead; a caller may catch it and
    decide, but no caller is handed a guess dressed as a fact.

2.  No holiday or early-close date is written down. A hardcoded 2026 holiday
    table is the same time bomb in a new costume: correct until the year
    turns, then silently wrong. Every date-specific fact comes from the
    exchange calendar the broker publishes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from src.trading_calendar import ET, SESSION_WINDOWS

Anchor = Literal["open", "close"]


class SessionCalendarUnavailable(RuntimeError):
    """The exchange calendar could not be read, so the session is UNKNOWN.

    Deliberately distinct from "this date is not a trading day". A caller that
    cannot tell those two apart will eventually treat an outage as a holiday
    (and skip real work) or as a normal session (and trade into a shut
    market). Both have happened; neither is allowed to happen quietly.
    """


@dataclass(frozen=True)
class PhaseOffset:
    """One phase of the trading day, stated as minutes from a session edge.

    `start_anchor`/`end_anchor` say WHICH edge each side hangs off, and the
    deltas are signed minutes from it. Expressing the day this way is the
    whole point of the module: on a 13:00 early close, a phase anchored at
    `close - 30` resolves to 12:30, where a phase pinned to 15:30 resolves to
    half an hour after the market has gone home.
    """

    name: str
    start_anchor: Anchor
    start_delta_minutes: int
    end_anchor: Anchor
    end_delta_minutes: int


# The six phases, re-expressed against the session edges. Each one reproduces
# its current `SESSION_WINDOWS` entry exactly on a 09:30-16:00 day; what
# changes is that they now MOVE with a short session instead of standing still
# while the market closes underneath them.
#
# Which edge a side hangs off is a meaning decision, not arithmetic:
#   - Anything tied to the opening bell (the pre-bell earnings prep, the
#     morning session, the start of the paid intraday tick) is open-anchored.
#   - Anything that is an end-of-day duty (the patient midday review, the
#     act-on-trigger close review, the end of the paid tick, the evening
#     report) is close-anchored, because what makes it due is the market
#     going home, not the clock reading a particular number.
PHASE_OFFSETS: dict[str, PhaseOffset] = {
    # 08:00-09:15 — the pre-bell preparation, due a fixed run-up before the
    # bell whenever the bell happens to ring.
    "earnings_preprocess": PhaseOffset("earnings_preprocess", "open", -90, "open", -15),
    # 09:30-12:00 — the opening stretch, measured from the bell.
    "morning": PhaseOffset("morning", "open", 0, "open", 150),
    # 09:30-16:00 — the paid tick. Starts at the bell and, critically, STOPS
    # at the real close rather than at 16:00; this is the phase that currently
    # keeps spending against a shut market on a half day.
    "intra_check": PhaseOffset("intra_check", "open", 0, "close", 0),
    # 13:00-14:30 — the patient position review. An end-of-day duty, so it is
    # measured backwards from the close: three hours out to ninety minutes
    # out. On a 13:00 close that is 10:00-11:30, before the market shuts,
    # instead of an hour after it already has.
    "midday": PhaseOffset("midday", "close", -180, "close", -90),
    # 15:30-16:00 — the act-on-trigger review that must land inside the last
    # half hour of whatever session is actually being traded.
    "close": PhaseOffset("close", "close", -30, "close", 0),
    # 20:00-22:00 — reporting only, after the market has gone home.
    "evening": PhaseOffset("evening", "close", 240, "close", 360),
}

@dataclass(frozen=True)
class Session:
    """One date's real session: both edges, or a flag saying there is none."""

    on_date: date
    is_trading_day: bool
    open_at: datetime | None
    close_at: datetime | None

    def require_edges(self) -> tuple[datetime, datetime]:
        """Both edges, or a loud refusal. Never a guessed 09:30-16:00."""
        if not self.is_trading_day or self.open_at is None or self.close_at is None:
            raise SessionCalendarUnavailable(
                f"{self.on_date} is not a trading day, so it has no session edges"
            )
        return self.open_at, self.close_at


def _edges(broker: Any, on_date: date) -> tuple[datetime | None, datetime | None]:
    """Both edges for one date, straight from the broker's cached calendar.

    An exception out of the broker is NOT swallowed here. The broker's own
    wrappers already catch their Alpaca errors and return None, so anything
    escaping is a programming fault in the reader and must be seen.
    """
    return (broker.get_session_open(on_date), broker.get_session_close(on_date))


def _calendar_is_answering(broker: Any, around: date) -> bool:
    """Is the exchange calendar readable at all right now?

    Asked only when the target date reads blank, to tell a genuine holiday
    from an outage — the broker cannot tell them apart for us, since both come
    back as None. Walks backwards day by day and stops at the first date that
    produces a real session; those reads are cached per date on the broker, so
    the walk costs nothing once the day is warm.
    """
    # The look-back is bounded by calendar structure, not a count: from the
    # day before `around` back to the first day of the previous calendar month.
    # An exchange always trades on some date in a whole month, so a span that
    # long returning nothing means the calendar is not answering.
    floor = (around.replace(day=1) - timedelta(days=1)).replace(day=1)
    probe = around - timedelta(days=1)
    while probe >= floor:
        opens, closes = _edges(broker, probe)
        if opens is not None and closes is not None:
            return True
        probe -= timedelta(days=1)
    return False


def session_for(broker: Any, on_date: date) -> Session:
    """The real session for `on_date`, or a loud failure.

    Returns a `Session` with `is_trading_day=False` and no edges when the
    exchange genuinely does not trade that date (weekend, holiday). Raises
    `SessionCalendarUnavailable` when the calendar cannot be read, which is a
    different thing and must never be mistaken for the first.
    """
    if broker is None:
        raise SessionCalendarUnavailable(
            f"no broker supplied, so the exchange calendar for {on_date} "
            "cannot be read; refusing to assume a normal session"
        )
    opens, closes = _edges(broker, on_date)
    if opens is not None and closes is not None:
        return Session(on_date=on_date, is_trading_day=True, open_at=opens, close_at=closes)
    if opens is not None or closes is not None:
        # Half an answer is not an answer. One edge present and the other
        # missing means the calendar entry is malformed or the second lookup
        # failed; filling the gap in with a standard 09:30 or 16:00 is the
        # precise mistake this module exists to stop.
        raise SessionCalendarUnavailable(
            f"the exchange calendar gave only one edge for {on_date} "
            f"(open={opens!r}, close={closes!r}); refusing to invent the other"
        )
    if _calendar_is_answering(broker, on_date):
        return Session(on_date=on_date, is_trading_day=False, open_at=None, close_at=None)
    raise SessionCalendarUnavailable(
        f"the exchange calendar returned nothing for {on_date} and nothing for "
        "any day back to the start of the previous month, so it is unreadable "
        "rather than closed; refusing to assume a normal session"
    )


def phase_window(broker: Any, on_date: date, phase: str) -> tuple[datetime, datetime]:
    """Resolve one phase to real wall-clock instants on `on_date`.

    Raises `SessionCalendarUnavailable` when the date does not trade or the
    calendar is unreadable, so a caller cannot accidentally receive a window
    on a day the market never opened.
    """
    offset = PHASE_OFFSETS.get(phase)
    if offset is None:
        raise KeyError(f"unknown session phase {phase!r}")
    opens, closes = session_for(broker, on_date).require_edges()
    anchors = {"open": opens, "close": closes}
    return (
        anchors[offset.start_anchor] + timedelta(minutes=offset.start_delta_minutes),
        anchors[offset.end_anchor] + timedelta(minutes=offset.end_delta_minutes),
    )


def covered_phases() -> frozenset[str]:
    """The phases this module can resolve. Exactly the Python minute table's
    keys today; the shell wrapper's extra eighth mode is a later piece."""
    return frozenset(PHASE_OFFSETS) & frozenset(SESSION_WINDOWS)


def et_datetime(on_date: date, minute_of_day: int) -> datetime:
    """An ET-aware instant `minute_of_day` minutes into `on_date`. Test and
    caller convenience so neither hand-rolls the timezone attachment."""
    return datetime(on_date.year, on_date.month, on_date.day, tzinfo=ET) + timedelta(
        minutes=minute_of_day
    )
