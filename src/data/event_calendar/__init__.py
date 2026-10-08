"""Scheduled-event calendar — macro releases (FRED) and per-symbol earnings dates.

Why this module exists
----------------------
`src/models.py::RiskVerdict.reasoning_chain.event_risk` is a REQUIRED narrative
field: the Risk Manager must state, for every trade it judges, whether an
earnings report or a macro release lands inside the next few sessions. Until
this module, nothing fetched either fact.

* `MarketDataProvider.get_next_earnings_date` existed (added specifically to
  answer the earnings half) and had **zero callers** anywhere in `src/` or
  `tests/` — recorded in `docs/STATE.md` on 2026-08-27 as "available but
  unwired" and still unwired at the time this landed.
* No module fetched a calendar of scheduled macro releases at all, while both
  `config/prompts/macro_analyst.md` and `config/prompts/risk_manager.md`
  instructed the model to reason about upcoming events.

So the mandatory event-risk check was answered from the model's own memory. A
remembered earnings date is a fabricated figure wearing a confident sentence.

The standing rule this module follows
-------------------------------------
**A labelled absence beats a fabricated figure.** The reference example in this
codebase is `pace_status` (`src/pipeline.py`) with its
`unavailable_no_pinned_horizon` value: when the input for a metric is missing,
the metric is NOT produced and the seat is told, in its own prompt, *which*
absence it is looking at. The degraded-news / degraded-macro coverage
advisories (`src.data.news.NewsCoverage`, `src.data.macro.MacroCoverage`) are
the reference for telling the desk that a feed is impaired. Both patterns are
reused verbatim below rather than a third one being invented:

* every earnings answer carries an explicit `status` — `measured`, or one of
  four named `unavailable_*` reasons (see `EARNINGS_STATUSES`);
* the macro calendar ships an `EventCalendarCoverage` whose shape, vocabulary
  (`ok` / `partial` / `failed`) and `describe()` contract mirror
  `MacroCoverage` field for field.

Fetch discipline
----------------
The FRED side follows `src/data/macro.py`'s established policy (rebuilt in
PR #162) rather than inventing a new fetch style: config-driven retries,
exponential backoff with jitter, a consecutive-failure breaker, and — the one
hard guarantee — a REAL wall-clock ceiling for one `get_upcoming_events()`
call, enforced by clipping every request timeout and every backoff sleep to
whatever budget remains. A slow or dead FRED must never stall the trading
session that reads this.

The earnings side gets the same treatment for the same reason:
`get_next_earnings_date` has no internal timeout (unlike `get_ohlcv` /
`get_valuation_metrics`, which are both `ThreadPoolExecutor`-bounded), so a
yfinance stall on one symbol could otherwise hang the risk stage. Every symbol
is bounded individually AND the whole sweep shares one wall-clock budget.

What the FREE path covers, and what it does not
-----------------------------------------------
Live-verified against the FRED API on 2026-08-31 (read-only GETs, real key,
responses recorded in the PR): `/fred/release/dates` returns real forward
schedules for the statistical releases in `MACRO_RELEASES` — e.g. release 10
(CPI) came back `2026-09-11, 2026-10-14, 2026-11-10, 2026-12-10` and release 50
(Employment Situation) `2026-09-04, 2026-10-02, 2026-11-06, 2026-12-04`.

FOMC meeting dates — the second source
--------------------------------------
FRED cannot supply these. Release 101 ("FOMC Press Release") is a DAILY
release with no meeting schedule attached: queried over 2026-01-01..2026-08-30
it returns all 240 calendar days, and queried forward it returns every calendar
day (with the no-data flag) or nothing at all (without it). It is a publication
feed, not a calendar.

The Federal Reserve publishes its own schedule, free, and `FOMCCalendarProvider`
below reads it. Two endpoints were checked live on 2026-08-31 (read-only GETs,
no key, responses recorded in the PR):

* **`https://www.federalreserve.gov/json/calendar.json` — PRIMARY.** Structured
  JSON (UTF-8 **with a BOM**, so it must be decoded `utf-8-sig`), 2,582 events
  under `events`, each with `type` / `title` / `month` (`YYYY-MM`) / `days`.
  135 carry `type == "FOMC"`; the 57 titled `FOMC Meeting` are the meetings
  themselves, the rest are minutes and press conferences. `days` is the day the
  meeting CONCLUDES (the decision day) and the `description` states the block
  length — `"Two-day meeting, September 15 - 16"`. Live response covered
  2017-01..2026-12 and returned exactly eight 2026 meetings, every one a
  two-day block: Jan 27-28, Mar 17-18, Apr 28-29, Jun 16-17, Jul 28-29,
  Sep 15-16, Oct 27-28, Dec 8-9.
* **`https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm` —
  FALLBACK ONLY.** The rendered calendar page. It is preferred nowhere, for the
  obvious reason, but it is kept because the live check found one thing the
  JSON feed does not have: **2027**. The page already lists the eight 2027
  meetings (Jan 26-27 … Dec 7-8); the JSON feed's events stop at 2026-12. A
  JSON-only implementation would therefore run out of schedule in December and
  report "no meeting" for a window it simply could not see — the exact
  false-reassurance this module exists to prevent. So the HTML page is fetched
  ONLY when the JSON schedule fails or stops short of the requested horizon,
  and all of its parsing lives in one function, `parse_fomc_meetings_from_html`,
  which raises `FOMCCalendarParseError` rather than returning a plausible empty
  list if the Fed ever redesigns the page.

Neither endpoint needs a key and neither is paid. Rejected without being
wired: FRED release 101 (above); `/feeds/press_all.xml` and
`/feeds/press_monetary.xml` (RSS of press releases already published —
backward-looking, no forward schedule); `/json/fomc.json`, `/feeds/fomc.xml`,
`/calendar.ics` and `/newsevents/calendar.ics` (all HTTP 404 — they do not
exist).

The schedule is cached on disk (`data/fomc_calendar.json`) because FOMC dates
change roughly twice a year, and a stale cache degrades HONESTLY: it is served
with the `measured_from_stale_cache` status and its age stated to the seat,
never silently as if it were fresh.

What the FREE path still does not cover is declared in `UNCOVERED_EVENTS`.
"""

from src.data.event_calendar.macro import (  # noqa: F401
    FRED_RELEASE_DATES_URL,
    _FAILURE_REASON_MAX_LEN,
    RELEASE_SCHEDULE_LOOKAHEAD_DAYS,
    MacroRelease,
    MACRO_RELEASES,
    UNCOVERED_EVENTS,
    ReleaseFailure,
    RELEASE_SCHEDULE_CACHE_SCHEMA,
    RELEASE_SCHEDULE_CACHE_PATH,
    ReleaseScheduleCache,
    MacroEvent,
    EventCalendarCoverage,
    MacroEventCalendarProvider,
)
from src.data.event_calendar.fomc import (  # noqa: F401
    FOMC_JSON_CALENDAR_URL,
    FOMC_HTML_CALENDAR_URL,
    _FOMC_USER_AGENT,
    _FOMC_MAX_MEETING_DAYS,
    _FOMC_CACHE_BACKFILL_DAYS,
    FOMC_SOURCE_JSON,
    FOMC_SOURCE_HTML,
    FOMC_SOURCE_CACHE,
    FOMC_MEASURED,
    FOMC_MEASURED_STALE_CACHE,
    FOMC_UNAVAILABLE_FETCH_FAILED,
    FOMC_UNAVAILABLE_DEADLINE_EXCEEDED,
    _FOMC_ABSENCE_TEXT,
    FOMCCalendarParseError,
    FOMCMeeting,
    FOMCCoverage,
    _FOMC_DURATION_WORDS,
    _FOMC_DURATION_RE,
    _FOMC_MONTHS,
    _fomc_sorted,
    parse_fomc_meetings_from_json,
    _FOMC_HTML_YEAR_RE,
    _FOMC_HTML_ROW_RE,
    _FOMC_HTML_TAG_RE,
    _FOMC_HTML_DAYS_RE,
    _fomc_html_text,
    parse_fomc_meetings_from_html,
    FOMCCalendarProvider,
)
from src.data.event_calendar.earnings import (  # noqa: F401
    EARNINGS_MEASURED,
    EARNINGS_NO_FETCHED_DATE,
    EARNINGS_LOOKUP_FAILED,
    EARNINGS_LOOKUP_TIMEOUT,
    EARNINGS_DEADLINE_EXCEEDED,
    EARNINGS_STATUSES,
    _EARNINGS_ABSENCE_TEXT,
    EARNINGS_EVENT_WINDOW_SESSIONS,
    EarningsProximity,
    fetch_earnings_proximity,
)
from src.data.event_calendar.rendering import (  # noqa: F401
    format_fomc_section,
    format_macro_events_section,
    format_event_risk_block,
)
