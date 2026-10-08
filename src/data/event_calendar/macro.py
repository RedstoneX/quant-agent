"""FRED macro-release calendar: the release table, its disk cache and the provider."""

import logging
from dataclasses import dataclass


logger = logging.getLogger(__name__)

FRED_RELEASE_DATES_URL = "https://api.stlouisfed.org/fred/release/dates"

#: Exception text longer than this is truncated before it reaches a log line or
#: a seat's prompt — mirrors `src.data.macro._FAILURE_REASON_MAX_LEN` and
#: `src.data.news._FAILURE_REASON_MAX_LEN` (same shape of problem).
_FAILURE_REASON_MAX_LEN = 200

#: How far ahead ONE `/fred/release/dates` request looks. This is the FETCH
#: window and it is deliberately NOT the horizon: `get_upcoming_events`'s
#: `horizon_days` still decides what counts as imminent and what is returned to
#: a seat. Fetching wide costs nothing extra — it is the same single request
#: per release, with a later `realtime_end`.
#:
#: WHERE 120 COMES FROM. Every statistical release in `MACRO_RELEASES` except
#: Initial Jobless Claims (weekly) publishes MONTHLY, so a window of N days
#: contains roughly N/30 scheduled dates. The window has to clear one full
#: monthly cadence with margin, or a release that is simply 5 weeks out is
#: indistinguishable from a release with no published schedule at all. Four
#: monthly cycles (120 days) is the smallest round multiple of that cadence
#: that still returns 3 dates per release when the nearest one has just passed,
#: which is what makes "the wide query came back EMPTY" a trustworthy signal of
#: a genuinely unpublished schedule rather than an artefact of the window.
#:
#: Live-verified against the FRED API on 2026-09-23 (read-only GETs, real key,
#: the same params the code sends). Next 10 days vs next 120 days:
#:   * release 10  (CPI):          0 dates | 2026-10-14, 2026-11-10, 2026-12-10
#:   * release 46  (PPI):          0 dates | 2026-10-15, 2026-11-13, 2026-12-15
#:   * release 9   (Retail Sales): 0 dates | 2026-10-15, 2026-11-17, 2026-12-16
#:   * release 50  (Employment):   1 date  | 3 dates
#: Three of the four came back empty at 10 days and the code recorded that as
#: `no_scheduled_dates_published` — a SOURCE FAILURE — which is what produced
#: the recurring "Macro event calendar PARTIAL this run" warning in production.
#: The schedules were published the whole time; the window was too narrow to
#: see them.
#:
#: The FRED row `limit` below (60) is well clear of what this window can
#: return: 120 days is ~4 dates for a monthly release and ~17 for weekly
#: Initial Jobless Claims.
RELEASE_SCHEDULE_LOOKAHEAD_DAYS = 120


@dataclass(frozen=True)
class MacroRelease:
    """One FRED release whose forward schedule the desk cares about."""

    release_id: int
    label: str
    why: str


#: The scheduled US macro releases that actually move an equity book, each
#: verified live against `/fred/release/dates` on 2026-08-31 before being wired
#: in here (same discipline as the Phase 4.2 FRED series additions in
#: `src/data/macro.py`). Deliberately short: this is an event-risk calendar,
#: not a data warehouse — a seat that has to read forty rows will read none.
MACRO_RELEASES: tuple[MacroRelease, ...] = (
    MacroRelease(10, "CPI", "headline/core inflation print — the single most "
                            "reliable single-day vol event outside earnings"),
    MacroRelease(50, "Employment Situation (NFP)",
                 "payrolls + unemployment rate; moves rate expectations"),
    MacroRelease(46, "PPI", "producer prices; leads CPI and re-prices margins"),
    MacroRelease(54, "Personal Income and Outlays (PCE)",
                 "the Fed's preferred inflation gauge"),
    MacroRelease(53, "GDP", "growth print; released alongside PCE by BEA"),
    MacroRelease(9, "Retail Sales (advance)",
                 "consumer demand; hits discretionary names hardest"),
    MacroRelease(180, "Initial Jobless Claims",
                 "weekly, Thursdays — the high-frequency labor read"),
)

#: Scheduled events this calendar does NOT fetch. Rendered into every seat's
#: event-risk block so the boundary is stated rather than inferred — a seat
#: that is shown a calendar and not told where it stops will assume it stops
#: nowhere.
#:
#: FOMC meeting dates were the first entry here and are NOT any more: they are
#: fetched from the Federal Reserve's own free calendar (see this module's
#: docstring and `FOMCCalendarProvider`). The entries below are the events for
#: which no free source is wired in this system.
UNCOVERED_EVENTS: tuple[str, ...] = (
    "Non-US central bank decisions — ECB, BoJ, BoE. No free source is wired "
    "for these. Treat their schedule as UNKNOWN and say so; do not supply a "
    "date from memory.",
    "One-off and non-statistical US events — Treasury quarterly refunding, "
    "OPEC+ meetings, index rebalances / quad-witching, and the release dates "
    "of FOMC minutes (as distinct from the meetings themselves, which ARE "
    "fetched above). Not fetched by this calendar. Treat their dates as "
    "UNKNOWN; do not supply a date from memory.",
)


@dataclass
class ReleaseFailure:
    """One configured FRED release whose forward dates did not come back on a
    `get_upcoming_events()` call. Mirrors `src.data.macro.SeriesFailure`."""

    release_id: int
    label: str
    reason: str
