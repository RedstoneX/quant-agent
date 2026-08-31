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

import html as html_module
import json
import logging
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.trading_calendar import et_today

logger = logging.getLogger(__name__)

FRED_RELEASE_DATES_URL = "https://api.stlouisfed.org/fred/release/dates"

#: Exception text longer than this is truncated before it reaches a log line or
#: a seat's prompt — mirrors `src.data.macro._FAILURE_REASON_MAX_LEN` and
#: `src.data.news._FAILURE_REASON_MAX_LEN` (same shape of problem).
_FAILURE_REASON_MAX_LEN = 200


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


@dataclass
class MacroEvent:
    """One scheduled macro release landing inside the requested horizon."""

    release_id: int
    label: str
    why: str
    event_date: date
    days_away: int

    def describe(self) -> str:
        when = "TODAY" if self.days_away == 0 else (
            "TOMORROW" if self.days_away == 1 else f"in {self.days_away} calendar days"
        )
        return f"{self.event_date.isoformat()} ({when}): {self.label} — {self.why}"


@dataclass
class EventCalendarCoverage:
    """How much of the configured release set actually returned a schedule on
    one `get_upcoming_events()` call.

    Field-for-field the same contract as `src.data.macro.MacroCoverage` (and,
    behind it, `src.data.news.NewsCoverage`) — same `configured`/`succeeded`/
    `failed` accounting, the same `ok`/`partial`/`failed` status vocabulary
    `MorningResearchStage` already uses for `news`/`tech`/`macro`, and the same
    `describe()` contract of naming what happened rather than going quiet.
    Reusing the shape is the point: the desk already knows how to read it, and
    a parallel third convention is exactly what the standing rule forbids.
    """

    configured: int
    succeeded: int
    failed: list[ReleaseFailure] = field(default_factory=list)

    @property
    def failed_count(self) -> int:
        return len(self.failed)

    @property
    def complete(self) -> bool:
        """True only when every configured release returned a schedule.

        Zero configured releases is deliberately NOT complete — that is a
        configuration error, not full coverage of nothing (mirrors
        `MacroCoverage.complete` / `NewsCoverage.complete`).
        """
        return self.configured > 0 and self.failed_count == 0

    @property
    def status(self) -> str:
        if self.configured == 0 or self.succeeded == 0:
            return "failed"
        if self.failed:
            return "partial"
        return "ok"

    def describe(self) -> str:
        if self.configured == 0:
            return (
                "Macro event calendar: NO releases configured (misconfiguration)."
            )
        if self.succeeded == 0:
            names = ", ".join(f"{f.label} ({f.reason})" for f in self.failed)
            return (
                f"Macro event calendar: 0/{self.configured} release schedules "
                f"returned this run — the calendar is UNAVAILABLE. FAILED: "
                f"{names}. An empty calendar here means NOT FETCHED, never "
                f"\"no events scheduled\"."
            )
        if not self.failed:
            return (
                f"Macro event calendar: {self.succeeded}/{self.configured} "
                f"release schedules returned. Full coverage."
            )
        names = ", ".join(f"{f.label} ({f.reason})" for f in self.failed)
        return (
            f"Macro event calendar: {self.succeeded}/{self.configured} release "
            f"schedules returned this run. FAILED: {names}. Treat this as a "
            f"coverage GAP, not a confirmed empty calendar — a release whose "
            f"schedule did not fetch is not a release that isn't happening."
        )


class MacroEventCalendarProvider:
    """Forward schedule of US macro releases, from FRED's free release-dates API.

    `fredapi` (the client `src/data/macro.py` uses) exposes no releases/dates
    method at all — its surface is series-only — so this issues the HTTP GET
    itself, with the stdlib `urllib` the rest of this package already uses for
    third-party HTTP (`src/data/earnings.py` for EDGAR, `src/data/news.py` for
    wires), which is also what carries the production egress proxy wiring.

    Resilience parameters mirror `MacroDataProvider.__init__` exactly, and the
    pipeline threads the same `config.macro.*` values into both: it is the same
    host, the same failure mode, and the same operator setting. The one
    parameter of its own is `total_fetch_deadline_s`, which is much tighter
    than the macro summary's — this calendar is a nice-to-have layered on a
    session that must not be delayed for it.
    """

    def __init__(
        self,
        api_key: str,
        *,
        request_timeout_s: float = 15.0,
        max_retries: int = 2,
        retry_backoff_base_s: float = 2.0,
        retry_backoff_max_s: float = 8.0,
        retry_backoff_jitter_s: float = 1.0,
        breaker_after_failed_releases: int = 1,
        total_fetch_deadline_s: float = 20.0,
        releases: tuple[MacroRelease, ...] = MACRO_RELEASES,
    ):
        # Fail fast on a missing key, exactly as MacroDataProvider does: an
        # unset key would otherwise fail every request and present as an empty
        # calendar, which is the one thing this module must never be confused
        # with. Constructing loudly at startup beats a silent all-day gap.
        if not api_key or not api_key.strip():
            raise ValueError(
                "FRED_API_KEY is empty or unset. Set it in .env — the macro "
                "event calendar cannot be fetched without FRED access. Pass an "
                "explicit non-empty string here only if you intend to exercise "
                "the offline / mock path."
            )
        self.api_key = api_key
        # Defensive clamping mirrors MacroDataProvider / SECForm4Provider — a
        # caller or config typo can't produce a zero timeout or an inverted
        # backoff window.
        self.request_timeout_s = max(1.0, float(request_timeout_s))
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_base_s = max(0.0, float(retry_backoff_base_s))
        self.retry_backoff_max_s = max(
            self.retry_backoff_base_s, float(retry_backoff_max_s),
        )
        self.retry_backoff_jitter_s = max(0.0, float(retry_backoff_jitter_s))
        self.breaker_after_failed_releases = max(1, int(breaker_after_failed_releases))
        # Never below one request's own timeout — a shorter deadline would
        # abort every fetch immediately without ever really trying.
        self.total_fetch_deadline_s = max(
            self.request_timeout_s, float(total_fetch_deadline_s),
        )
        self.releases = tuple(releases)
        self._consecutive_failed = 0
        self._deadline: float | None = None
        #: Coverage snapshot from the most recent `get_upcoming_events()` call.
        #: A side channel for the same reason `MacroDataProvider.last_coverage`
        #: is one — the return value is consumed as a plain list by several
        #: call sites and changing its shape has a wider blast radius than the
        #: fix warrants.
        self.last_coverage: EventCalendarCoverage | None = None

    # --- fetch plumbing ----------------------------------------------------

    def _next_backoff(self, attempt: int) -> float:
        """Exponential backoff with jitter, clipped to whatever remains of the
        fetch deadline — so a retry sleep can never itself blow the wall-clock
        ceiling `get_upcoming_events()` promises. Identical policy to
        `MacroDataProvider._next_backoff`; `attempt` is 0-indexed (the attempt
        that just failed)."""
        base = min(
            self.retry_backoff_base_s * (2 ** attempt),
            self.retry_backoff_max_s,
        )
        backoff = base + random.uniform(0, self.retry_backoff_jitter_s)
        if self._deadline is not None:
            remaining = self._deadline - time.monotonic()
            backoff = max(0.0, min(backoff, remaining))
        return backoff

    def _http_get_json(self, url: str, timeout: float) -> dict:
        """One GET returning parsed JSON. Split out so tests can substitute a
        transport without patching urllib globally."""
        request = Request(url, headers={"User-Agent": "quant-agent event-calendar"})
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 — fixed https host
            payload = response.read()
        return json.loads(payload.decode("utf-8"))

    def _fetch_release_dates(
        self, release: MacroRelease, start: date, end: date,
    ) -> tuple[list[date], str]:
        """Forward dates for one release. Returns (dates, failure_reason); the
        reason is "" on success."""
        # `include_release_dates_with_no_data=true` is REQUIRED for forward
        # dates and is not an optimisation: FRED only marks a release date as
        # "has data" once the data has actually been published, so with the
        # flag off every future scheduled date is filtered out and the response
        # is `count: 0`. Live-verified 2026-08-31 — release 10 (CPI) over
        # 2026-08-31..2026-12-31 returned `count: 0` without the flag and the
        # four real scheduled dates with it. Verified in the other direction
        # too, so the flag cannot be inventing dates: over a PAST window
        # (2026-01-01..2026-08-30) release 10 returned the identical eight
        # dates with the flag on and off.
        params = {
            "release_id": release.release_id,
            "api_key": self.api_key,
            "file_type": "json",
            "realtime_start": start.isoformat(),
            "realtime_end": end.isoformat(),
            "include_release_dates_with_no_data": "true",
            "sort_order": "asc",
            "limit": 60,
        }
        url = f"{FRED_RELEASE_DATES_URL}?{urlencode(params)}"

        retries = (
            self.max_retries
            if self._consecutive_failed < self.breaker_after_failed_releases
            else 0
        )
        for attempt in range(retries + 1):
            remaining = (
                self._deadline - time.monotonic() if self._deadline is not None
                else self.request_timeout_s
            )
            if remaining <= 0:
                logger.warning(
                    "FRED release-dates deadline exceeded before attempt %d/%d "
                    "for %s — degrading now",
                    attempt + 1, retries + 1, release.label,
                )
                return [], "fetch_deadline_exceeded"
            try:
                payload = self._http_get_json(
                    url, timeout=min(self.request_timeout_s, remaining),
                )
            except Exception as e:  # noqa: BLE001 — any transport shape degrades
                reason = str(e) or type(e).__name__
                if attempt < retries:
                    backoff = self._next_backoff(attempt)
                    logger.warning(
                        "FRED release-dates error for %s (attempt %d/%d): %s — "
                        "retrying in %.1fs",
                        release.label, attempt + 1, retries + 1, e, backoff,
                    )
                    if backoff > 0:
                        time.sleep(backoff)
                    continue
                logger.warning(
                    "FRED release-dates error for %s: %s", release.label, e,
                )
                return [], reason

            rows = (payload or {}).get("release_dates")
            if not isinstance(rows, list):
                return [], "malformed_response"
            dates: list[date] = []
            for row in rows:
                raw = (row or {}).get("date") if isinstance(row, dict) else None
                if not raw:
                    continue
                try:
                    dates.append(date.fromisoformat(str(raw)))
                except ValueError:
                    continue
            if not dates:
                # A clean response carrying no scheduled date. Distinct from
                # the exception path: usually the source agency has not
                # published its next schedule yet. Still an ABSENCE, never a
                # confirmation that nothing is coming.
                return [], "no_scheduled_dates_published"
            return dates, ""
        return [], "exhausted"

    # --- public API --------------------------------------------------------

    def get_upcoming_events(self, horizon_days: int = 10) -> list[MacroEvent]:
        """Scheduled macro releases landing within `horizon_days` calendar days.

        Sets `self.last_coverage` before returning, always — including on the
        total-failure path, where an EMPTY LIST MUST NOT be read as "no events
        scheduled". The coverage object is the only thing that distinguishes
        those two, which is why every caller is expected to render it.
        """
        horizon_days = max(0, int(horizon_days))
        today = et_today()
        end = today + timedelta(days=horizon_days)

        self._deadline = time.monotonic() + self.total_fetch_deadline_s
        self._consecutive_failed = 0
        succeeded = 0
        failures: list[ReleaseFailure] = []
        events: list[MacroEvent] = []
        try:
            for release in self.releases:
                # Hard wall-clock check FIRST — if earlier releases in this same
                # call ate the whole budget, skip without even attempting. This
                # is what actually bounds the worst case; retry/backoff below is
                # best-effort recovery, not a ceiling.
                if time.monotonic() >= self._deadline:
                    logger.warning(
                        "Event-calendar deadline (%.0fs) already exceeded — "
                        "skipping %s without an attempt",
                        self.total_fetch_deadline_s, release.label,
                    )
                    failures.append(ReleaseFailure(
                        release.release_id, release.label,
                        "fetch_deadline_exceeded",
                    ))
                    self._consecutive_failed += 1
                    continue

                dates, reason = self._fetch_release_dates(release, today, end)
                if reason:
                    failures.append(ReleaseFailure(
                        release.release_id, release.label,
                        reason[:_FAILURE_REASON_MAX_LEN],
                    ))
                    self._consecutive_failed += 1
                    continue

                self._consecutive_failed = 0
                succeeded += 1
                for event_date in dates:
                    if today <= event_date <= end:
                        events.append(MacroEvent(
                            release_id=release.release_id,
                            label=release.label,
                            why=release.why,
                            event_date=event_date,
                            days_away=(event_date - today).days,
                        ))
        finally:
            self._deadline = None
            self.last_coverage = EventCalendarCoverage(
                configured=len(self.releases),
                succeeded=succeeded,
                failed=failures,
            )

        events.sort(key=lambda e: (e.event_date, e.label))
        return events


# --- FOMC meeting calendar -------------------------------------------------

#: Structured JSON feed — PRIMARY. See the module docstring for the live
#: response this was chosen on.
FOMC_JSON_CALENDAR_URL = "https://www.federalreserve.gov/json/calendar.json"

#: Rendered calendar page — FALLBACK ONLY, used when the JSON feed fails or its
#: schedule stops before the end of the requested horizon (which it does at
#: every year boundary: the JSON feed carries the current year, the page
#: carries the next one too).
FOMC_HTML_CALENDAR_URL = (
    "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
)

#: Identifies this desk to the Fed's servers. Same courtesy as the EDGAR
#: fetcher in `src/data/earnings.py`.
_FOMC_USER_AGENT = "quant-agent event-calendar"

#: Sanity bound on one meeting block. Scheduled FOMC meetings are one or two
#: days; an unscheduled/emergency one is a single day. Anything a parser
#: produces outside this is a parse error wearing a plausible shape, and is
#: dropped rather than shown to a seat as a fact.
_FOMC_MAX_MEETING_DAYS = 4

#: Meetings older than this are dropped before caching — the cache is a
#: forward calendar, not an archive. A small backward margin is kept so a
#: meeting that concluded yesterday still reads as "just happened".
_FOMC_CACHE_BACKFILL_DAYS = 45

FOMC_SOURCE_JSON = "federalreserve.gov/json/calendar.json"
FOMC_SOURCE_HTML = "federalreserve.gov/monetarypolicy/fomccalendars.htm"
FOMC_SOURCE_CACHE = "on-disk cache of a previous fetch"

#: Provenance vocabulary for the FOMC schedule, in the `pace_status` /
#: `EARNINGS_STATUSES` style: one value for a real answer, and a NAMED reason
#: for every way the answer can be absent. `measured_from_stale_cache` is a
#: real answer whose provenance is degraded — it carries dates, and it says out
#: loud that they are old, which is what "a stale cache must degrade honestly"
#: means here.
FOMC_MEASURED = "measured"
FOMC_MEASURED_STALE_CACHE = "measured_from_stale_cache"
FOMC_UNAVAILABLE_FETCH_FAILED = "unavailable_fetch_failed"
FOMC_UNAVAILABLE_DEADLINE_EXCEEDED = "unavailable_deadline_exceeded"

#: Deliberately four values and not five. There is no
#: "source answered but published nothing" status, because there is no path to
#: it: both parse boundaries raise `FOMCCalendarParseError` on a document with
#: no readable meeting rather than returning an empty list, so that case
#: arrives here as a fetch failure with the parser's message attached. A status
#: nothing can produce is a status nobody can trust.
FOMC_STATUSES = (
    FOMC_MEASURED,
    FOMC_MEASURED_STALE_CACHE,
    FOMC_UNAVAILABLE_FETCH_FAILED,
    FOMC_UNAVAILABLE_DEADLINE_EXCEEDED,
)

_FOMC_ABSENCE_TEXT = {
    FOMC_UNAVAILABLE_FETCH_FAILED: (
        "FOMC schedule UNAVAILABLE — the Federal Reserve's calendar did not "
        "answer this run and no cached schedule exists"
    ),
    FOMC_UNAVAILABLE_DEADLINE_EXCEEDED: (
        "FOMC schedule UNAVAILABLE — the fetch exceeded its wall-clock ceiling "
        "and was abandoned so the session would not wait on it"
    ),
}


class FOMCCalendarParseError(ValueError):
    """A Fed calendar document did not contain a readable meeting schedule.

    Raised — never swallowed into an empty list — precisely so a redesign of
    the Fed's page or feed surfaces as a loud, named failure that degrades the
    seat's block to UNAVAILABLE, instead of a plausible "no meetings" answer
    that reads like reassurance.
    """


@dataclass(frozen=True)
class FOMCMeeting:
    """One scheduled FOMC meeting block.

    `end_date` is the day the meeting CONCLUDES — the rate decision, the
    statement and (when held) the press conference all land on it, so it is the
    day that actually carries the event risk. `start_date` is the first day of
    the block; for a one-day meeting the two are equal.

    `duration_stated` records whether the SOURCE said how long the block runs.
    False means the source gave only a concluding date and the start was not
    published — the block is then reported as a single day and labelled, rather
    than a second day being assumed into existence.
    """

    start_date: date
    end_date: date
    duration_stated: bool = True

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    def days_away(self, today: date) -> int:
        """Calendar days from `today` to the DECISION day (negative if past)."""
        return (self.end_date - today).days

    def intersects(self, start: date, end: date) -> bool:
        """True when any day of the block falls inside [start, end]."""
        return self.start_date <= end and self.end_date >= start

    def describe(self, today: date, horizon_end: date | None = None) -> str:
        away = self.days_away(today)
        if away < 0:
            when = f"concluded {abs(away)} calendar days ago"
        elif away == 0:
            when = "TODAY"
        elif away == 1:
            when = "TOMORROW"
        else:
            when = f"in {away} calendar days"
        if self.start_date == self.end_date:
            block = (
                f"{self.end_date.isoformat()} (one day"
                + ("" if self.duration_stated else "; the source published a "
                                                   "concluding date only, so "
                                                   "the block length is UNKNOWN")
                + ")"
            )
        else:
            block = (
                f"{self.start_date.isoformat()} to {self.end_date.isoformat()} "
                f"({self.days}-day meeting)"
            )
        flag = ""
        if horizon_end is not None and self.intersects(today, horizon_end) and away >= 0:
            flag = "  ** FOMC RATE DECISION INSIDE THIS HORIZON **"
        return (
            f"{block} — rate decision / statement on "
            f"{self.end_date.isoformat()}, {when}{flag}"
        )


@dataclass
class FOMCCoverage:
    """Where this run's FOMC schedule came from, and how far it reaches.

    Same job as `EventCalendarCoverage` and `src.data.macro.MacroCoverage`: the
    returned meeting list is meaningless on its own, because an empty list is
    produced both by "no meeting is scheduled in this window" and by "nothing
    was fetched". This object is the only thing that tells those apart, so
    every renderer is expected to print it.

    `schedule_through` is the last meeting the SOURCE published. When it falls
    before `horizon_end`, the tail of the horizon is not covered by any
    published schedule and no one may claim it is empty — that is what
    `covers_horizon` guards, and it is the concrete failure the HTML fallback
    exists for (the JSON feed's schedule ends with the calendar year).
    """

    status: str
    source: str = ""
    reason: str = ""
    schedule_through: date | None = None
    horizon_end: date | None = None
    cache_age_days: int | None = None

    @property
    def measured(self) -> bool:
        return self.status in (FOMC_MEASURED, FOMC_MEASURED_STALE_CACHE)

    @property
    def covers_horizon(self) -> bool:
        """True only when a published schedule actually spans the whole window.

        The one condition under which "no FOMC meeting is scheduled in this
        horizon" may be stated as a fact.
        """
        if not self.measured or self.schedule_through is None:
            return False
        if self.horizon_end is None:
            return False
        return self.schedule_through >= self.horizon_end

    def describe(self) -> str:
        if not self.measured:
            detail = _FOMC_ABSENCE_TEXT.get(
                self.status,
                "FOMC schedule UNAVAILABLE — treat the FOMC calendar as UNKNOWN",
            )
            extra = f" ({self.reason})" if self.reason else ""
            return (
                f"FOMC calendar: {detail}{extra} [{self.status}]. This is an "
                f"absence of data, NOT a confirmation that no meeting is "
                f"scheduled. Treat the FOMC schedule as UNKNOWN and say so; do "
                f"not supply a meeting date from memory."
            )

        parts = [f"FOMC calendar: fetched from {self.source or 'an unnamed source'}"]
        # Each part is rendered as its own sentence, so each starts capitalised
        # — a coverage line a seat skims past is a coverage line that did not
        # do its job.
        if self.status == FOMC_MEASURED_STALE_CACHE:
            age = (
                f"{self.cache_age_days} days old"
                if self.cache_age_days is not None else "of unknown age"
            )
            parts.append(
                f"STALE — the live Fed calendar did not answer this run "
                f"({self.reason or 'no reason recorded'}), so this schedule is "
                f"the cached copy, {age}. Published FOMC dates do change; treat "
                f"a date this close to its meeting as indicative, not confirmed"
            )
        through = (
            f"Published schedule runs through {self.schedule_through.isoformat()}"
            if self.schedule_through is not None
            else "The source published no schedule end this run"
        )
        if self.covers_horizon or self.horizon_end is None:
            parts.append(through)
        else:
            parts.append(
                f"{through}, which is BEFORE THE END OF THIS HORIZON "
                f"({self.horizon_end.isoformat()}) — whether a meeting falls in "
                f"the uncovered tail is UNKNOWN, so this section cannot rule "
                f"out a meeting in that tail"
            )
            if self.reason and self.status == FOMC_MEASURED:
                # The fallback was reached for precisely to extend the schedule
                # past the horizon, and did not answer. Naming it here is what
                # lets an operator tell "the Fed has not published that far
                # yet" from "our second source is broken"
                parts.append(
                    f"The fallback source did not answer either ({self.reason})"
                )
        return ". ".join(parts) + "."


# --- the two parse boundaries ----------------------------------------------
#
# Both are pure functions over one already-fetched document, and both raise
# `FOMCCalendarParseError` rather than returning an empty list when the
# document does not look like a schedule. That is the isolation the fallback
# needs: if the Fed redesigns either surface, exactly one of these two
# functions fails, loudly, and the seat is told the calendar is unavailable.

_FOMC_DURATION_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4}
_FOMC_DURATION_RE = re.compile(
    r"\b(one|two|three|four)[-\s]day\s+meeting\b", re.IGNORECASE,
)
_FOMC_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _fomc_sorted(meetings) -> list[FOMCMeeting]:
    """De-duplicate by block and order soonest-first."""
    unique = {(m.start_date, m.end_date): m for m in meetings}
    return sorted(unique.values(), key=lambda m: (m.end_date, m.start_date))


def parse_fomc_meetings_from_json(payload) -> list[FOMCMeeting]:
    """Meetings out of `federalreserve.gov/json/calendar.json`.

    Shape confirmed against the live feed on 2026-08-31: `events` is a list of
    objects; a meeting is `type == "FOMC"` AND `title == "FOMC Meeting"` (the
    same `type` also carries `FOMC Minutes` and `FOMC Press Conference` rows,
    which are NOT meetings and must not be reported as ones). `month` is
    `YYYY-MM` and `days` is the day of that month on which the meeting
    CONCLUDES. The block length comes from the description's leading phrase,
    e.g. `"Two-day meeting, September 15 - 16"`, which is read as a DURATION
    and subtracted from the concluding date — deliberately, because that also
    gets the month-straddling blocks right (`"Two-day meeting, October 31 -
    November 1"` has `month` `2017-11` and `days` `1`) without parsing two
    month names.
    """
    if not isinstance(payload, dict):
        raise FOMCCalendarParseError(
            f"Fed JSON calendar: expected a JSON object, got "
            f"{type(payload).__name__} — the feed's shape has changed"
        )
    events = payload.get("events")
    if not isinstance(events, list):
        raise FOMCCalendarParseError(
            "Fed JSON calendar: no 'events' list in the payload — the feed's "
            "shape has changed"
        )

    meetings: list[FOMCMeeting] = []
    for row in events:
        if not isinstance(row, dict):
            continue
        if str(row.get("type") or "").strip().upper() != "FOMC":
            continue
        title = " ".join(str(row.get("title") or "").split()).lower()
        if title != "fomc meeting":
            continue
        raw_month = str(row.get("month") or "").strip()
        raw_day = str(row.get("days") or "").strip()
        try:
            year_text, month_text = raw_month.split("-")
            end = date(int(year_text), int(month_text), int(raw_day))
        except (ValueError, TypeError):
            continue

        description = html_module.unescape(str(row.get("description") or ""))
        match = _FOMC_DURATION_RE.search(description)
        if match:
            span = _FOMC_DURATION_WORDS[match.group(1).lower()]
            if span > _FOMC_MAX_MEETING_DAYS:
                continue
            meetings.append(FOMCMeeting(end - timedelta(days=span - 1), end, True))
        else:
            # The source gave a concluding date and no block length. Report one
            # day and SAY the length is unpublished — never assume a second day
            # into existence.
            meetings.append(FOMCMeeting(end, end, False))

    if not meetings:
        raise FOMCCalendarParseError(
            f"Fed JSON calendar: {len(events)} events returned but not one "
            f"readable 'FOMC Meeting' among them — the feed's shape has changed"
        )
    return _fomc_sorted(meetings)


_FOMC_HTML_YEAR_RE = re.compile(r">\s*(\d{4})\s+FOMC Meetings\s*<")
_FOMC_HTML_ROW_RE = re.compile(
    r"fomc-meeting__month[^>]*>(?P<month>.*?)</div>"
    r".*?fomc-meeting__date[^>]*>(?P<date>.*?)</div>",
    re.DOTALL,
)
_FOMC_HTML_TAG_RE = re.compile(r"<[^>]+>")
_FOMC_HTML_DAYS_RE = re.compile(r"(\d{1,2})\s*(?:[-–—]\s*(\d{1,2}))?")


def _fomc_html_text(fragment: str) -> str:
    return " ".join(
        html_module.unescape(_FOMC_HTML_TAG_RE.sub(" ", fragment)).split()
    )


def parse_fomc_meetings_from_html(document: str) -> list[FOMCMeeting]:
    """Meetings out of the rendered `fomccalendars.htm` page. FALLBACK ONLY.

    Every assumption this makes about rendered markup is contained here and
    nowhere else, which is the whole reason it is a standalone function: the
    page is a human document that the Fed may restyle without notice, and when
    it does, this raises `FOMCCalendarParseError` and the caller degrades to a
    labelled absence.

    Structure confirmed against the live page on 2026-08-31: one panel per year
    headed `<h4><a>2027 FOMC Meetings</a></h4>`, then one row per meeting
    carrying `fomc-meeting__month` (`"January"`, or `"Oct/Nov"` when the block
    straddles a month end) and `fomc-meeting__date` (`"27-28"`, `"17-18*"`
    where the asterisk marks a Summary of Economic Projections, or `"31-1"`).
    Year panels are NOT in chronological order on the live page (2026, 2025 …
    2021, then 2027), so each row takes the year of the nearest heading ABOVE
    it rather than any assumed ordering.
    """
    text = document if isinstance(document, str) else ""
    headings = [(m.start(), int(m.group(1))) for m in _FOMC_HTML_YEAR_RE.finditer(text)]
    if not headings:
        raise FOMCCalendarParseError(
            "Fed FOMC calendar page: no 'NNNN FOMC Meetings' year panel found "
            "— the page layout has changed"
        )

    meetings: list[FOMCMeeting] = []
    for row in _FOMC_HTML_ROW_RE.finditer(text):
        year = None
        for position, heading_year in headings:
            if position < row.start():
                year = heading_year
            else:
                break
        if year is None:
            continue

        month_text = _fomc_html_text(row.group("month"))
        day_text = _fomc_html_text(row.group("date"))
        months = [
            _FOMC_MONTHS.get(part.strip()[:3].lower())
            for part in month_text.split("/") if part.strip()
        ]
        if not months or any(m is None for m in months) or len(months) > 2:
            continue
        day_match = _FOMC_HTML_DAYS_RE.search(day_text)
        if not day_match:
            continue
        first_day = int(day_match.group(1))
        last_day = int(day_match.group(2)) if day_match.group(2) else first_day

        try:
            start = date(year, months[0], first_day)
            if len(months) == 2:
                # "Oct/Nov 31-1", and at a year end "Dec/Jan" rolls the year.
                end_year = year + 1 if months[1] < months[0] else year
                end = date(end_year, months[1], last_day)
            else:
                end = date(year, months[0], last_day)
        except ValueError:
            continue

        span = (end - start).days + 1
        if span < 1 or span > _FOMC_MAX_MEETING_DAYS:
            # A plausible-looking but wrong parse. Dropping it is mandatory:
            # a fabricated meeting block is worse than a missing one.
            logger.warning(
                "Fed FOMC page: discarding implausible %d-day block %s..%s "
                "(from %r / %r)", span, start, end, month_text, day_text,
            )
            continue
        meetings.append(FOMCMeeting(start, end, True))

    if not meetings:
        raise FOMCCalendarParseError(
            "Fed FOMC calendar page: year panels found but no readable meeting "
            "row — the page layout has changed"
        )
    return _fomc_sorted(meetings)


class FOMCCalendarProvider:
    """The FOMC meeting schedule, from the Federal Reserve's own free calendar.

    Fetch discipline is `MacroEventCalendarProvider`'s, which is
    `src/data/macro.py`'s (PR #162): config-driven retries, exponential backoff
    with jitter, and — the hard guarantee — a REAL wall-clock ceiling for one
    `get_meetings()` call, enforced by clipping every request timeout and every
    backoff sleep to whatever budget remains. The Fed's site being slow must
    never delay a trading session.

    Source order is JSON feed, then the rendered page, and the page is reached
    for only when the feed failed OR its schedule stops before the end of the
    requested horizon. See the module docstring for why that second condition
    is not hypothetical.

    Caching exists because FOMC dates are set a year ahead and change perhaps
    twice a year, so refetching every session is pure waste. The cache is only
    trusted without a fetch while it is BOTH younger than `cache_ttl_days` AND
    long enough to span the horizon; past that it is refreshed, and it is
    served stale only when the live sources are unreachable — labelled
    `measured_from_stale_cache`, with its age stated in the text the seat
    reads. A cache that quietly passed for fresh data would be the same defect
    in a new coat.
    """

    def __init__(
        self,
        *,
        request_timeout_s: float = 10.0,
        max_retries: int = 2,
        retry_backoff_base_s: float = 2.0,
        retry_backoff_max_s: float = 8.0,
        retry_backoff_jitter_s: float = 1.0,
        total_fetch_deadline_s: float = 15.0,
        cache_path: str = "data/fomc_calendar.json",
        cache_ttl_days: float = 7.0,
        json_url: str = FOMC_JSON_CALENDAR_URL,
        html_url: str = FOMC_HTML_CALENDAR_URL,
    ):
        # Same defensive clamping as MacroEventCalendarProvider — a config typo
        # must not be able to produce a zero timeout or an inverted window.
        self.request_timeout_s = max(1.0, float(request_timeout_s))
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_base_s = max(0.0, float(retry_backoff_base_s))
        self.retry_backoff_max_s = max(
            self.retry_backoff_base_s, float(retry_backoff_max_s),
        )
        self.retry_backoff_jitter_s = max(0.0, float(retry_backoff_jitter_s))
        self.total_fetch_deadline_s = max(
            self.request_timeout_s, float(total_fetch_deadline_s),
        )
        self.cache_path = Path(cache_path)
        self.cache_ttl_days = max(0.0, float(cache_ttl_days))
        self.json_url = json_url
        self.html_url = html_url
        self._deadline: float | None = None
        #: Provenance of the most recent `get_meetings()` call — the side
        #: channel, for the same reason `MacroDataProvider.last_coverage` and
        #: `MacroEventCalendarProvider.last_coverage` are ones.
        self.last_coverage: FOMCCoverage | None = None
        #: The full schedule behind the last call, horizon filtering aside.
        #: Lets a caller name the NEXT meeting even when none is imminent.
        self.last_schedule: list[FOMCMeeting] = []

    # --- fetch plumbing ----------------------------------------------------

    def _remaining(self) -> float:
        if self._deadline is None:
            return self.request_timeout_s
        return self._deadline - time.monotonic()

    def _next_backoff(self, attempt: int) -> float:
        """Identical policy to `MacroEventCalendarProvider._next_backoff`, and
        clipped the same way, so a retry sleep can never itself blow the
        wall-clock ceiling `get_meetings()` promises."""
        base = min(
            self.retry_backoff_base_s * (2 ** attempt), self.retry_backoff_max_s,
        )
        backoff = base + random.uniform(0, self.retry_backoff_jitter_s)
        if self._deadline is not None:
            backoff = max(0.0, min(backoff, self._remaining()))
        return backoff

    def _http_get_bytes(self, url: str, timeout: float) -> bytes:
        """One GET returning raw bytes. Split out so tests substitute a
        transport instead of patching urllib globally — the same seam
        `MacroEventCalendarProvider._http_get_json` provides."""
        request = Request(url, headers={"User-Agent": _FOMC_USER_AGENT})
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 — fixed https host
            return response.read()

    def _fetch_document(self, url: str) -> tuple[bytes | None, str]:
        """Fetch one URL with retry/backoff inside the deadline.

        Returns (body, failure_reason); the reason is "" on success and
        `fetch_deadline_exceeded` when the budget ran out.
        """
        for attempt in range(self.max_retries + 1):
            remaining = self._remaining()
            if remaining <= 0:
                logger.warning(
                    "FOMC calendar deadline exceeded before attempt %d/%d for "
                    "%s — degrading now", attempt + 1, self.max_retries + 1, url,
                )
                return None, "fetch_deadline_exceeded"
            try:
                return self._http_get_bytes(
                    url, timeout=min(self.request_timeout_s, remaining),
                ), ""
            except Exception as e:  # noqa: BLE001 — any transport shape degrades
                reason = (str(e) or type(e).__name__)[:_FAILURE_REASON_MAX_LEN]
                if attempt < self.max_retries:
                    backoff = self._next_backoff(attempt)
                    logger.warning(
                        "FOMC calendar error for %s (attempt %d/%d): %s — "
                        "retrying in %.1fs",
                        url, attempt + 1, self.max_retries + 1, e, backoff,
                    )
                    if backoff > 0:
                        time.sleep(backoff)
                    continue
                logger.warning("FOMC calendar error for %s: %s", url, e)
                return None, reason
        return None, "exhausted"

    def _fetch_json_schedule(self) -> tuple[list[FOMCMeeting], str]:
        body, reason = self._fetch_document(self.json_url)
        if body is None:
            return [], f"json:{reason}"
        try:
            # The live feed is served UTF-8 WITH A BOM — `utf-8` alone raises
            # here, so this encoding choice is load-bearing, not cosmetic.
            payload = json.loads(body.decode("utf-8-sig"))
        except Exception as e:  # noqa: BLE001
            return [], f"json:undecodable:{str(e)[:80]}"
        try:
            return parse_fomc_meetings_from_json(payload), ""
        except FOMCCalendarParseError as e:
            logger.warning("Fed JSON calendar unparseable: %s", e)
            return [], f"json:{str(e)[:_FAILURE_REASON_MAX_LEN]}"

    def _fetch_html_schedule(self) -> tuple[list[FOMCMeeting], str]:
        body, reason = self._fetch_document(self.html_url)
        if body is None:
            return [], f"html:{reason}"
        try:
            document = body.decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            return [], f"html:undecodable:{str(e)[:80]}"
        try:
            return parse_fomc_meetings_from_html(document), ""
        except FOMCCalendarParseError as e:
            logger.warning("Fed FOMC calendar page unparseable: %s", e)
            return [], f"html:{str(e)[:_FAILURE_REASON_MAX_LEN]}"

    # --- cache -------------------------------------------------------------

    def _load_cache(self) -> tuple[list[FOMCMeeting], date | None]:
        """Cached schedule and the day it was fetched. Never raises."""
        try:
            if not self.cache_path.exists():
                return [], None
            raw = json.loads(self.cache_path.read_text()) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("FOMC calendar cache unreadable (%s) — ignoring", e)
            return [], None
        try:
            fetched_on = date.fromisoformat(str(raw.get("fetched_on")))
        except (TypeError, ValueError):
            fetched_on = None
        meetings: list[FOMCMeeting] = []
        for row in raw.get("meetings") or []:
            try:
                start = date.fromisoformat(str(row["start_date"]))
                end = date.fromisoformat(str(row["end_date"]))
            except (KeyError, TypeError, ValueError):
                continue
            if end < start or (end - start).days + 1 > _FOMC_MAX_MEETING_DAYS:
                continue
            meetings.append(FOMCMeeting(
                start, end, bool(row.get("duration_stated", True)),
            ))
        if not meetings:
            return [], None
        return _fomc_sorted(meetings), fetched_on

    def _save_cache(self, meetings: list[FOMCMeeting], today: date, source: str) -> None:
        """Never raises: an unwritable cache costs a refetch, not a session."""
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps({
                "fetched_on": today.isoformat(),
                "source": source,
                "meetings": [
                    {
                        "start_date": m.start_date.isoformat(),
                        "end_date": m.end_date.isoformat(),
                        "duration_stated": m.duration_stated,
                    }
                    for m in meetings
                ],
            }, indent=1))
        except Exception as e:  # noqa: BLE001
            logger.warning("FOMC calendar cache unwritable: %s", e)

    # --- public API --------------------------------------------------------

    def get_meetings(self, horizon_days: int = 10) -> list[FOMCMeeting]:
        """Scheduled FOMC meetings from today onward, soonest first.

        Returns the FORWARD schedule (not only the part inside the horizon) so
        a caller can always name the next meeting; `horizon_days` decides how
        far the coverage promise has to reach, and therefore whether "no
        meeting in this window" may be asserted at all.

        Sets `self.last_coverage` before returning, ALWAYS — including on every
        failure path, where an empty list must never be read as "no meeting
        scheduled".
        """
        horizon_days = max(0, int(horizon_days))
        today = et_today()
        horizon_end = today + timedelta(days=horizon_days)
        self._deadline = time.monotonic() + self.total_fetch_deadline_s
        try:
            return self._get_meetings(today, horizon_end)
        finally:
            self._deadline = None

    def _get_meetings(self, today: date, horizon_end: date) -> list[FOMCMeeting]:
        def _forward(meetings: list[FOMCMeeting]) -> list[FOMCMeeting]:
            return [m for m in meetings if m.end_date >= today]

        def _through(meetings: list[FOMCMeeting]) -> date | None:
            return max((m.end_date for m in meetings), default=None)

        cached, fetched_on = self._load_cache()
        cache_age = (today - fetched_on).days if fetched_on else None
        cache_through = _through(cached)
        cache_is_fresh = (
            cache_age is not None
            and 0 <= cache_age <= self.cache_ttl_days
            and cache_through is not None
            and cache_through >= horizon_end
        )
        if cache_is_fresh:
            # Both conditions matter. A young cache whose schedule stops before
            # the horizon is NOT usable as-is: it cannot answer the question
            # being asked, so the sources are consulted instead.
            self.last_schedule = cached
            self.last_coverage = FOMCCoverage(
                status=FOMC_MEASURED, source=FOMC_SOURCE_CACHE,
                schedule_through=cache_through, horizon_end=horizon_end,
                cache_age_days=cache_age,
            )
            return _forward(cached)

        meetings: list[FOMCMeeting] = []
        sources: list[str] = []
        failures: list[str] = []

        live, reason = self._fetch_json_schedule()
        if live:
            meetings = live
            sources.append(FOMC_SOURCE_JSON)
        elif reason:
            failures.append(reason)

        # The fallback is reached for on exactly two conditions, and neither is
        # a preference: the structured feed gave nothing, or its schedule stops
        # inside the window we must be able to speak about.
        through = _through(meetings)
        if not meetings or through is None or through < horizon_end:
            if self._remaining() > 0:
                fallback, reason = self._fetch_html_schedule()
                if fallback:
                    meetings = _fomc_sorted(meetings + fallback)
                    sources.append(FOMC_SOURCE_HTML)
                elif reason:
                    failures.append(reason)
            else:
                failures.append("html:fetch_deadline_exceeded")

        if meetings:
            keep = [
                m for m in meetings
                if m.end_date >= today - timedelta(days=_FOMC_CACHE_BACKFILL_DAYS)
            ]
            new_through = _through(keep)
            # Never overwrite a longer cached schedule with a shorter fetched
            # one. The JSON feed alone reaches only to the end of the calendar
            # year, so a run that never needed the fallback would otherwise
            # throw away a next-year tail an earlier run had already merged in
            # — and then be unable to answer a December horizon without the
            # fallback answering again. Keeping the longer copy costs nothing:
            # it is only ever SERVED subject to the same freshness and span
            # checks as any other cache.
            if (
                cache_through is None
                or (new_through is not None and new_through >= cache_through)
            ):
                self._save_cache(keep, today, " + ".join(sources))
            else:
                logger.info(
                    "FOMC calendar: keeping cached schedule through %s; this "
                    "run's sources only reached %s", cache_through, new_through,
                )
            self.last_schedule = meetings
            self.last_coverage = FOMCCoverage(
                status=FOMC_MEASURED, source=" + ".join(sources),
                reason="; ".join(failures)[:_FAILURE_REASON_MAX_LEN],
                schedule_through=_through(meetings), horizon_end=horizon_end,
            )
            return _forward(meetings)

        # Nothing live. A stale cache is still real published data and beats
        # silence — but it is handed over WEARING ITS AGE, never as if fresh.
        if cached:
            self.last_schedule = cached
            self.last_coverage = FOMCCoverage(
                status=FOMC_MEASURED_STALE_CACHE, source=FOMC_SOURCE_CACHE,
                reason="; ".join(failures)[:_FAILURE_REASON_MAX_LEN],
                schedule_through=cache_through, horizon_end=horizon_end,
                cache_age_days=cache_age,
            )
            return _forward(cached)

        joined = "; ".join(failures)
        status = (
            FOMC_UNAVAILABLE_DEADLINE_EXCEEDED
            if failures and all("fetch_deadline_exceeded" in f for f in failures)
            else FOMC_UNAVAILABLE_FETCH_FAILED
        )
        self.last_schedule = []
        self.last_coverage = FOMCCoverage(
            status=status, reason=joined[:_FAILURE_REASON_MAX_LEN],
            horizon_end=horizon_end,
        )
        return []


# --- earnings proximity ----------------------------------------------------

EARNINGS_MEASURED = "measured"
EARNINGS_NO_FETCHED_DATE = "unavailable_no_fetched_date"
EARNINGS_LOOKUP_FAILED = "unavailable_lookup_failed"
EARNINGS_LOOKUP_TIMEOUT = "unavailable_lookup_timeout"
EARNINGS_DEADLINE_EXCEEDED = "unavailable_deadline_exceeded"

#: The complete status vocabulary, mirroring `pace_status`'s
#: measured / too_early / n/a_breakout / unavailable_no_pinned_horizon shape:
#: one value for a real figure, and a NAMED reason for every way the figure can
#: be absent. Never collapse these into a bare None — the whole point is that a
#: reader can tell "the source said nothing" from "the source never answered".
EARNINGS_STATUSES = (
    EARNINGS_MEASURED,
    EARNINGS_NO_FETCHED_DATE,
    EARNINGS_LOOKUP_FAILED,
    EARNINGS_LOOKUP_TIMEOUT,
    EARNINGS_DEADLINE_EXCEEDED,
)

_EARNINGS_ABSENCE_TEXT = {
    EARNINGS_NO_FETCHED_DATE: (
        "NO FETCHED DATE — the earnings-date source answered with nothing for "
        "this symbol. It does NOT mean no report is due: the source cannot "
        "distinguish \"no scheduled date published\" from \"date unknown\". "
        "Treat the earnings date as UNKNOWN"
    ),
    EARNINGS_LOOKUP_FAILED: (
        "LOOKUP FAILED — the earnings-date fetch raised. No date was obtained; "
        "treat the earnings date as UNKNOWN"
    ),
    EARNINGS_LOOKUP_TIMEOUT: (
        "LOOKUP TIMED OUT — the earnings-date fetch exceeded its per-symbol "
        "ceiling and was abandoned. Treat the earnings date as UNKNOWN"
    ),
    EARNINGS_DEADLINE_EXCEEDED: (
        "NOT ATTEMPTED — the earnings sweep's wall-clock budget was exhausted "
        "before this symbol. Treat the earnings date as UNKNOWN"
    ),
}


@dataclass
class EarningsProximity:
    """How far away one symbol's next scheduled earnings report is.

    `sessions_away` is populated ONLY when `status == "measured"`. On every
    other status it is None and `status` names which absence this is — the
    `pace_status` contract, applied to the same class of problem.
    """

    symbol: str
    sessions_away: int | None
    status: str

    @property
    def measured(self) -> bool:
        return self.status == EARNINGS_MEASURED and self.sessions_away is not None

    def describe(self) -> str:
        if self.measured:
            imminent = " ** INSIDE THE 3-SESSION EVENT WINDOW **" if (
                self.sessions_away <= 3
            ) else ""
            unit = "session" if self.sessions_away == 1 else "sessions"
            return (
                f"{self.symbol}: next earnings ~{self.sessions_away} {unit} "
                f"away (fetched){imminent}"
            )
        detail = _EARNINGS_ABSENCE_TEXT.get(
            self.status, "UNAVAILABLE — treat the earnings date as UNKNOWN",
        )
        return f"{self.symbol}: {detail} [{self.status}]"


def fetch_earnings_proximity(
    market_provider,
    symbols,
    *,
    per_symbol_timeout_s: float = 8.0,
    total_deadline_s: float = 20.0,
) -> list[EarningsProximity]:
    """Next-earnings proximity for each symbol, from real fetched data.

    This is the caller `MarketDataProvider.get_next_earnings_date` never had.

    `get_next_earnings_date` swallows its own exceptions and returns None for
    BOTH "unknown" and "nothing scheduled" — its own docstring says callers
    must treat None as *unknown*, never as *no earnings soon*. This function is
    where that instruction is actually honoured: None becomes the explicit
    `unavailable_no_fetched_date` label rather than an empty line the reader
    fills in for themselves.

    Bounded twice over, because that method has no timeout of its own: each
    symbol gets `per_symbol_timeout_s`, and the sweep as a whole gets
    `total_deadline_s` of wall clock. Symbols not reached inside the budget are
    returned labelled `unavailable_deadline_exceeded` — they are never dropped,
    because a symbol silently missing from this list would read to the seat as
    a symbol with nothing to report.
    """
    per_symbol_timeout_s = max(0.5, float(per_symbol_timeout_s))
    total_deadline_s = max(per_symbol_timeout_s, float(total_deadline_s))
    deadline = time.monotonic() + total_deadline_s

    ordered: list[str] = []
    for raw in symbols or []:
        symbol = str(raw or "").strip().upper()
        if symbol and symbol not in ordered:
            ordered.append(symbol)

    results: list[EarningsProximity] = []
    for symbol in ordered:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            results.append(EarningsProximity(
                symbol, None, EARNINGS_DEADLINE_EXCEEDED,
            ))
            continue
        # Deliberately NOT a `with` block. `ThreadPoolExecutor.__exit__` calls
        # `shutdown(wait=True)`, which blocks until the worker finishes — so on
        # the timeout path the context-manager form waits out the very stall the
        # timeout exists to escape, and the per-symbol ceiling bounds nothing at
        # all. `shutdown(wait=False)` abandons the stuck worker and lets the
        # sweep move on, which is the whole point of having a ceiling. The
        # abandoned thread is bounded in number by `total_deadline_s` and by
        # yfinance's own socket timeouts underneath it.
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            sessions = executor.submit(
                market_provider.get_next_earnings_date, symbol,
            ).result(timeout=min(per_symbol_timeout_s, remaining))
        except FuturesTimeout:
            logger.warning(
                "earnings-date lookup timed out for %s (>%.1fs)",
                symbol, per_symbol_timeout_s,
            )
            results.append(EarningsProximity(
                symbol, None, EARNINGS_LOOKUP_TIMEOUT,
            ))
            continue
        except Exception as e:  # noqa: BLE001 — any provider shape degrades
            logger.warning("earnings-date lookup failed for %s: %s", symbol, e)
            results.append(EarningsProximity(
                symbol, None, EARNINGS_LOOKUP_FAILED,
            ))
            continue
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if sessions is None:
            results.append(EarningsProximity(
                symbol, None, EARNINGS_NO_FETCHED_DATE,
            ))
            continue
        try:
            results.append(EarningsProximity(
                symbol, max(0, int(sessions)), EARNINGS_MEASURED,
            ))
        except (TypeError, ValueError):
            results.append(EarningsProximity(
                symbol, None, EARNINGS_LOOKUP_FAILED,
            ))
    return results


# --- rendering -------------------------------------------------------------

def format_fomc_section(
    meetings: list[FOMCMeeting] | None,
    coverage: FOMCCoverage | None,
    horizon_days: int,
    *,
    heading: str = "### FOMC meetings — FETCHED from the Federal Reserve",
) -> str:
    """The FOMC half of the event calendar, rendered once for every seat.

    Like `format_macro_events_section`, there is no branch here that renders as
    silence, and exactly one branch is allowed to say the reassuring thing —
    "no meeting inside this window" — which requires a published schedule that
    demonstrably spans the whole window (`FOMCCoverage.covers_horizon`). Every
    other path says UNKNOWN and says why.
    """
    horizon_days = max(0, int(horizon_days))
    today = et_today()
    horizon_end = today + timedelta(days=horizon_days)
    lines = [heading, ""]

    if coverage is None:
        lines.append(
            "- NOT FETCHED this run — the FOMC meeting calendar was not "
            "consulted. Treat the FOMC schedule as UNKNOWN and say so; do not "
            "supply a meeting date from memory."
        )
        return "\n".join(lines) + "\n"

    if not coverage.measured:
        lines.append(f"- {coverage.describe()}")
        return "\n".join(lines) + "\n"

    forward = [m for m in (meetings or []) if m.end_date >= today]
    inside = [m for m in forward if m.intersects(today, horizon_end)]

    if inside:
        lines.extend(f"- {m.describe(today, horizon_end)}" for m in inside)
    elif coverage.covers_horizon and coverage.status == FOMC_MEASURED:
        # The ONLY place this block is permitted to be reassuring, and the
        # provenance test is part of the condition: a schedule that spans the
        # horizon but came off a stale cache gets the hedged wording below
        # instead, because "nothing is coming" is a stronger claim than a
        # cached copy of a published schedule can carry on its own.
        lines.append(
            f"- None. The published FOMC schedule spans the next "
            f"{horizon_days} calendar days and no meeting falls inside it."
        )
    elif coverage.covers_horizon:
        lines.append(
            f"- None inside the next {horizon_days} calendar days according to "
            f"the CACHED schedule — read the coverage line below before "
            f"treating that as settled."
        )
    else:
        lines.append(
            "- None inside this horizon — but read the coverage line below: "
            "the published schedule does not reach the end of the horizon, so "
            "this is NOT a confirmed empty window."
        )

    upcoming = [m for m in forward if m not in inside]
    if upcoming:
        lines.append(
            f"- Next scheduled meeting beyond this horizon: "
            f"{upcoming[0].describe(today)}"
        )
    lines.append(f"- {coverage.describe()}")
    return "\n".join(lines) + "\n"


def format_macro_events_section(
    events: list[MacroEvent] | None,
    coverage: EventCalendarCoverage | None,
    horizon_days: int,
    *,
    heading: str = "## Scheduled Macro Releases — FETCHED (do NOT answer from memory)",
    fomc_meetings: list[FOMCMeeting] | None = None,
    fomc_coverage: FOMCCoverage | None = None,
) -> str:
    """The macro half of the event calendar, rendered once for every seat.

    There is no branch here that renders as silence. A seat shown an empty
    section reads it as "nothing to worry about", and an empty calendar and an
    unfetched calendar look identical unless something says which is which —
    that confusion is the fabrication this module exists to prevent.
    """
    lines = [heading, ""]
    if coverage is None:
        lines.append(
            "- NOT FETCHED this run — the macro event calendar was not "
            "consulted. Treat every scheduled release date as UNKNOWN and say "
            "so; do not substitute a date you recall."
        )
    else:
        if events:
            lines.extend(f"- {e.describe()}" for e in events)
        elif coverage.status == "ok":
            lines.append(
                f"- None. All {coverage.configured} tracked release schedules "
                f"fetched successfully and none lands inside the next "
                f"{horizon_days} calendar days."
            )
        else:
            lines.append(
                "- None returned — but read the coverage line below: this "
                "calendar is impaired, so an empty list here does NOT mean an "
                "empty calendar."
            )
        lines.append(f"- {coverage.describe()}")

    lines.append("")
    # The FOMC schedule rides inside this section rather than beside it, so
    # both seats read one identically-worded calendar — the same reason
    # `format_event_risk_block` delegates here instead of re-rendering.
    # No horizon is quoted when nothing was fetched, for the same reason the
    # macro heading above drops it: "the next 0 calendar days" would be a
    # number describing a window that was never queried.
    fomc_heading = (
        "### FOMC meetings — NOT FETCHED" if fomc_coverage is None
        else (
            f"### FOMC meetings, next {horizon_days} calendar days — FETCHED "
            f"from the Federal Reserve (do NOT answer from memory)"
        )
    )
    lines.append(format_fomc_section(
        fomc_meetings, fomc_coverage, horizon_days, heading=fomc_heading,
    ).rstrip("\n"))

    if UNCOVERED_EVENTS:
        lines.append("")
        lines.append("Not covered by this calendar:")
        lines.extend(f"- {item}" for item in UNCOVERED_EVENTS)
    return "\n".join(lines) + "\n"


def format_event_risk_block(
    *,
    earnings: list[EarningsProximity] | None,
    events: list[MacroEvent] | None,
    coverage: EventCalendarCoverage | None,
    horizon_days: int,
    fomc_meetings: list[FOMCMeeting] | None = None,
    fomc_coverage: FOMCCoverage | None = None,
) -> str:
    """The Event Risk section the Risk Manager reads instead of its memory.

    Earnings proximity per symbol under review, then the shared macro-release
    section. Same absence discipline throughout.
    """
    lines = [
        "## Event Risk — FETCHED DATA (do NOT answer this from memory)",
        "",
        "Everything below was fetched this run. Where a line says UNAVAILABLE, "
        "UNKNOWN or NOT COVERED, that is the answer — say so plainly in "
        "`reasoning_chain.event_risk` rather than substituting a date you "
        "recall. A remembered earnings or release date is a fabricated figure.",
        "",
        "### Next scheduled earnings, per symbol under review",
    ]
    if earnings:
        lines.extend(f"- {e.describe()}" for e in earnings)
        unknown = [e.symbol for e in earnings if not e.measured]
        if unknown:
            lines.append(
                f"  ⚠️ Earnings proximity is UNKNOWN for: {', '.join(unknown)}. "
                f"An unknown earnings date is an unquantified binary event, not "
                f"an absent one — size or veto accordingly and name it."
            )
    else:
        lines.append(
            "- NOT FETCHED this run — no earnings proximity is available for "
            "any symbol under review. This is an absence of data, NOT a "
            "confirmation that no name reports soon."
        )

    lines.append("")
    # No horizon is quoted when nothing was fetched — "the next 0 calendar
    # days" would be a number describing a window that was never queried.
    heading = (
        "### Scheduled US macro releases" if coverage is None
        else f"### Scheduled US macro releases, next {horizon_days} calendar days"
    )
    return "\n".join(lines) + "\n" + format_macro_events_section(
        events, coverage, horizon_days, heading=heading,
        fomc_meetings=fomc_meetings, fomc_coverage=fomc_coverage,
    )
