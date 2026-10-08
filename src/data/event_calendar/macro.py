"""FRED macro-release calendar: the release table, its disk cache and the provider."""

import json
import logging
import os
import random
import time
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


#: On-disk schema tag for the release-schedule cache. An unrecognised tag is
#: a miss, which degrades to the live fetch this cache exists to relieve.
RELEASE_SCHEDULE_CACHE_SCHEMA = "fred-release-schedule-cache/1"

#: Default location of that cache. Sits beside the FRED series cache because
#: it is written by the same pre-open job, for the same reason.
RELEASE_SCHEDULE_CACHE_PATH = "data/macro/release_schedule_cache.json"


class ReleaseScheduleCache:
    """Forward release schedules written ahead of the open, read at the open.

    WHY THIS EXISTS — board items 187 / 119
    ---------------------------------------
    Item 187's fair-share split (below, in `get_upcoming_events`) stopped one
    slow release from eating the whole 20 s ceiling, and it stopped the tail of
    `MACRO_RELEASES` starving on every single run. It did not make the fetch
    fast enough to belong on the trading path, and the production log says so:
    on 2026-09-30, with the split deployed, the 13:33 morning run still came
    back 1/7 and the 14:04 run 0/7 [measured, `/home/qamc/quant-agent/
    quant_agent.log`]. Seven serial HTTPS round trips to FRED, started inside
    the first minutes after the opening bell — the same minute the fifteen-
    series macro fetch used to fail in — cannot be made reliable by dividing
    the same twenty seconds more fairly.

    The fix is the one that already worked for the series fetch
    (`src/data/macro_series_cache.py`): move the wire off the trading path.
    A forward release schedule is the most cacheable thing this desk fetches
    — FRED publishes CPI, PPI, PCE, GDP, Retail Sales, the Employment
    Situation and Initial Jobless Claims months ahead, and the dates change
    rarely. Nothing about "when is the next CPI" requires being asked at
    09:30:49.

    WHAT MAKES A CACHED COPY USABLE — two conditions, both existing numbers
    ----------------------------------------------------------------------
    Exactly the pair `FOMCCalendar` already uses on the same class of data (a
    published forward calendar from a government source), for the same
    reasons, with the same constant:

    1. The entry was written no more than `cache_ttl_days` ago.
    2. Its schedule still reaches the horizon being asked about. A young entry
       whose last date stops short of the horizon cannot answer the question
       and is a miss, not a near-enough hit.

    No new threshold is introduced here.

    WHAT IT DOES NOT DO
    -------------------
    It never invents a schedule. A release that is in neither the cache nor
    the wire's answer stays a named failure in `EventCalendarCoverage`, and a
    release answered from cache is reported as cached, with its age, wherever
    the coverage prose travels. Only the prefetch writes: a trading session
    can never turn its own partial run into tomorrow's cached answer.
    """

    def __init__(self, path: str = RELEASE_SCHEDULE_CACHE_PATH):
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            if not self.path.exists():
                return {}
            raw = json.loads(self.path.read_text()) or {}
        except Exception as e:  # noqa: BLE001 — a broken cache is a miss
            logger.warning("Release-schedule cache unreadable (%s) — ignoring", e)
            return {}
        if raw.get("schema") != RELEASE_SCHEDULE_CACHE_SCHEMA:
            return {}
        entries = raw.get("releases")
        return entries if isinstance(entries, dict) else {}

    def load(self, release_id: int) -> tuple[list[date], date] | None:
        """`(dates, fetched_on)` for one release, or None on any miss."""
        entry = self._read().get(str(release_id))
        if not isinstance(entry, dict):
            return None
        try:
            fetched_on = date.fromisoformat(str(entry.get("fetched_on")))
            dates = sorted(
                date.fromisoformat(str(d)) for d in (entry.get("dates") or [])
            )
        except Exception:  # noqa: BLE001 — a malformed entry is a miss
            return None
        if not dates:
            return None
        return dates, fetched_on

    def save(self, release_id: int, label: str, dates: list[date], today: date) -> None:
        """Never raises: an unwritable cache costs a live fetch, not a session."""
        try:
            entries = self._read()
            entries[str(release_id)] = {
                "label": label,
                "fetched_on": today.isoformat(),
                "dates": [d.isoformat() for d in sorted(dates)],
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps({
                "schema": RELEASE_SCHEDULE_CACHE_SCHEMA,
                "releases": entries,
            }, indent=2))
            os.replace(tmp, self.path)
        except Exception as e:  # noqa: BLE001
            logger.warning("Release-schedule cache unwritable: %s", e)


@dataclass
class MacroEvent:
    """One scheduled macro release date.

    `get_upcoming_events` returns only those landing INSIDE the requested
    horizon. The same shape is reused for the next date BEYOND the horizon
    (`EventCalendarCoverage.next_beyond_horizon`) rather than inventing a
    second near-identical type — a seat that can read one can read the other.
    """

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

    One field is this class's own and has no `MacroCoverage` counterpart:
    `next_beyond_horizon`. A release whose schedule IS published but whose next
    date falls outside the horizon is a SUCCESS — the release is covered and
    nothing is imminent — and it used to be recorded as the failure
    `no_scheduled_dates_published` purely because the fetch window was 10 days
    wide and the releases are monthly (see `RELEASE_SCHEDULE_LOOKAHEAD_DAYS`).
    Carrying the date here is what lets the desk say "CPI lands on 14 October"
    instead of going quiet about it.
    """

    configured: int
    succeeded: int
    failed: list[ReleaseFailure] = field(default_factory=list)
    #: Next scheduled date for each release that HAS a published schedule but
    #: nothing inside the horizon. Never a failure; never rendered as one.
    next_beyond_horizon: list[MacroEvent] = field(default_factory=list)
    #: `(label, age_in_days)` for each release answered out of the pre-open
    #: schedule cache rather than the wire this run. A success, and a stated
    #: one — never silently indistinguishable from a live read.
    from_cache: list[tuple[str, int]] = field(default_factory=list)

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
        """Coverage prose plus, when any release came off the pre-open cache,
        an explicit sentence saying so.

        A cached forward schedule is real published data, but it is not a
        reading taken this run, and the desk's standing rule is that the seat
        is told what it is actually looking at.
        """
        return self._describe_coverage() + self._describe_cache()

    def _describe_cache(self) -> str:
        if not self.from_cache:
            return ""
        served = ", ".join(
            f"{label} (cached {age}d ago)" if age else f"{label} (cached today)"
            for label, age in self.from_cache
        )
        return (
            f" SERVED FROM THE PRE-OPEN SCHEDULE CACHE, not fetched this run: "
            f"{served}. Published release schedules change rarely, so a cached "
            f"copy is real data — but it is stated, never passed off as a live "
            f"read."
        )

    def _describe_coverage(self) -> str:
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
        schedule_cache_path: str = RELEASE_SCHEDULE_CACHE_PATH,
        cache_ttl_days: float = 7.0,
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
        self.schedule_cache = ReleaseScheduleCache(schedule_cache_path)
        #: Same constant, same meaning and same justification as
        #: `FOMCCalendar.cache_ttl_days` — see `ReleaseScheduleCache`.
        self.cache_ttl_days = max(0.0, float(cache_ttl_days))
        #: True only inside `prefetch_release_schedules()`. The prefetch never
        #: READS the cache (its whole job is to refresh it) and the trading
        #: path never WRITES it (so a partial session cannot become tomorrow's
        #: cached answer).
        self._prefetch_mode = False

    # --- cache -------------------------------------------------------------

    def _serve_from_cache(
        self, release: MacroRelease, today: date, horizon_end: date,
    ) -> tuple[list[date], int] | None:
        """`(dates, age_days)` from the pre-open cache, or None to go to the
        wire. Both conditions in `ReleaseScheduleCache` must hold."""
        if self._prefetch_mode:
            return None
        loaded = self.schedule_cache.load(release.release_id)
        if loaded is None:
            return None
        dates, fetched_on = loaded
        age_days = (today - fetched_on).days
        if age_days < 0 or age_days > self.cache_ttl_days:
            return None
        # The entry must still be able to ANSWER the question. Its query
        # window ran `RELEASE_SCHEDULE_LOOKAHEAD_DAYS` forward from the day it
        # was written, so it speaks for dates up to that point and no further;
        # asked about a horizon beyond it, it cannot say whether a release
        # lands there, and silence would read as "nothing scheduled".
        covered_through = fetched_on + timedelta(days=RELEASE_SCHEDULE_LOOKAHEAD_DAYS)
        if covered_through < horizon_end:
            return None
        # An entry whose every known date is already past is an exhausted
        # schedule, not a forward one.
        if max(dates) < today:
            return None
        return dates, age_days

    def _collect(
        self, release: MacroRelease, dates: list[date], today: date,
        end: date, events: list[MacroEvent], beyond: list[MacroEvent],
    ) -> None:
        """Sort one release's published dates into inside-horizon events and,
        failing that, the next date beyond it."""
        def _event(event_date: date) -> MacroEvent:
            return MacroEvent(
                release_id=release.release_id,
                label=release.label,
                why=release.why,
                event_date=event_date,
                days_away=(event_date - today).days,
            )

        inside = [d for d in dates if today <= d <= end]
        if inside:
            events.extend(_event(d) for d in inside)
            return
        # Published, but nothing imminent. Name the next date rather than
        # going quiet about a release the desk is still covering.
        ahead = [d for d in dates if d > end]
        if ahead:
            beyond.append(_event(min(ahead)))

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
        #
        # The WIDTH of `realtime_start`..`realtime_end` matters just as much as
        # the flag, and getting it wrong looks exactly like a source failure.
        # Live-verified 2026-09-23 (read-only GETs, real key, these same
        # params): over the next 10 days release 10 (CPI) returned 0 dates,
        # release 46 (PPI) 0 and release 9 (Retail Sales) 0, while over the
        # next 120 days they returned 2026-10-14/11-10/12-10, 2026-10-15/
        # 11-13/12-15 and 2026-10-15/11-17/12-16 respectively. Release 50
        # (Employment Situation) returned 1 date at 10 days and 3 at 120. The
        # releases are MONTHLY; a 10-day window is empty most of the month.
        # So the caller passes `RELEASE_SCHEDULE_LOOKAHEAD_DAYS`, not the
        # horizon, and filters afterwards.
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

        Each release is FETCHED over `RELEASE_SCHEDULE_LOOKAHEAD_DAYS` and
        FILTERED to `horizon_days`. That is one request per release either way
        — the same call count, a later `realtime_end` — and it separates three
        outcomes the old 10-day fetch could only see as two:

          * a date inside the horizon        -> success, event returned;
          * a published schedule, all of it
            beyond the horizon               -> SUCCESS, nothing imminent, the
                                                next date recorded on the
                                                coverage object;
          * nothing at all over 120 days     -> genuine
                                                `no_scheduled_dates_published`.

        The middle case used to be counted as the third one, which is what put
        CPI, PPI and Retail Sales in the FAILED list of most production runs.
        """
        horizon_days = max(0, int(horizon_days))
        today = et_today()
        end = today + timedelta(days=horizon_days)
        # Never narrower than the horizon a caller asked for.
        fetch_end = today + timedelta(
            days=max(horizon_days, RELEASE_SCHEDULE_LOOKAHEAD_DAYS),
        )

        # Board item 187: seven releases share one 20s deadline (item
        # 119's own diagnosis of the same disease in the FRED SERIES fetch —
        # "the observation calls ... share the same worker slots ... so a
        # healthy batch spends nearly the whole clock and one slow series
        # starves the rest" — applies unchanged here, and this fetch has no
        # cache to move it off the trading path). Left as one shared
        # deadline, a single early release can burn up to `request_timeout_s`
        # (15s) of the 20s total on its own, and — because `self.releases`
        # is always walked in the same MACRO_RELEASES order — the SAME tail
        # releases (PPI, PCE, GDP, Retail Sales, Initial Jobless Claims)
        # starve on every run while CPI/NFP at the front almost always get
        # through; production logs from 09-24/09-25 show exactly that set
        # failing `fetch_deadline_exceeded` run after run.
        #
        # Fix: split the REMAINING budget evenly across the releases not yet
        # attempted, recomputed fresh before each one. No release can eat
        # more than its fair share of what is actually left, so a slow
        # response degrades the NEXT release's budget proportionally instead
        # of erasing it — and because the split is recomputed off the true
        # remaining time (not a static 1/7th), a release that returns fast
        # hands its unused time forward to the rest. This introduces no new
        # timeout/threshold constant: the per-release budget is a derived
        # fraction of the existing operator-set `total_fetch_deadline_s`,
        # not a guessed number.
        global_deadline = time.monotonic() + self.total_fetch_deadline_s
        self._deadline = global_deadline
        self._consecutive_failed = 0
        succeeded = 0
        from_cache: list[tuple[str, int]] = []
        failures: list[ReleaseFailure] = []
        events: list[MacroEvent] = []
        beyond: list[MacroEvent] = []
        try:
            for idx, release in enumerate(self.releases):
                # The CACHE comes before the deadline check, because a release
                # answered off the pre-open cache costs no wall clock at all
                # and therefore hands its whole share forward to the releases
                # that do have to go to the wire.
                cached = self._serve_from_cache(release, today, end)
                if cached is not None:
                    dates, age_days = cached
                    self._consecutive_failed = 0
                    succeeded += 1
                    from_cache.append((release.label, age_days))
                    self._collect(release, dates, today, end, events, beyond)
                    continue

                # Hard wall-clock check FIRST — if earlier releases in this same
                # call ate the whole budget, skip without even attempting. This
                # is what actually bounds the worst case; retry/backoff below is
                # best-effort recovery, not a ceiling.
                remaining_global = global_deadline - time.monotonic()
                if remaining_global <= 0:
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

                # This release's fair share of whatever time is actually
                # left, split across itself and every release still to come.
                # `self._deadline` is what `_fetch_release_dates` and
                # `_next_backoff` clip their own timeouts/sleeps to, so
                # tightening it here — never past `global_deadline` — is
                # what stops one release from spending the whole run.
                releases_left = len(self.releases) - idx
                self._deadline = time.monotonic() + (remaining_global / releases_left)

                dates, reason = self._fetch_release_dates(
                    release, today, fetch_end,
                )
                # Restore the real ceiling so the NEXT release's fair share
                # is computed off true remaining time, not this release's
                # tightened sub-budget.
                self._deadline = global_deadline
                if reason:
                    failures.append(ReleaseFailure(
                        release.release_id, release.label,
                        reason[:_FAILURE_REASON_MAX_LEN],
                    ))
                    self._consecutive_failed += 1
                    continue

                # The schedule IS published. That is the success condition,
                # whether or not anything lands inside the horizon.
                self._consecutive_failed = 0
                succeeded += 1
                if self._prefetch_mode and dates:
                    self.schedule_cache.save(
                        release.release_id, release.label, dates, today,
                    )
                self._collect(release, dates, today, end, events, beyond)
        finally:
            self._deadline = None
            beyond.sort(key=lambda e: (e.event_date, e.label))
            self.last_coverage = EventCalendarCoverage(
                configured=len(self.releases),
                succeeded=succeeded,
                failed=failures,
                next_beyond_horizon=beyond,
                from_cache=from_cache,
            )

        events.sort(key=lambda e: (e.event_date, e.label))
        return events

    def prefetch_release_schedules(self) -> EventCalendarCoverage | None:
        """Refresh the on-disk release-schedule cache ahead of the open.

        Runs from the existing pre-open timer job, off the trading path, where
        a slow FRED costs nobody a session. It reads no cache (that would make
        the job a no-op after its first success) and it is the only writer.

        The ceiling here is deliberately the SAME `total_fetch_deadline_s` the
        trading path uses, not a longer one: the measurement in
        `get_upcoming_events` says the budget was never the problem, and a
        prefetch that needs a bigger ceiling than the thing it replaces would
        be the widened timeout this item exists to refuse. What changes is
        that a run which misses is retried by the NEXT scheduled prefetch
        instead of landing on the 09:30 session.
        """
        from src.data.event_reask import prefetch_with_reasks

        return prefetch_with_reasks(self)
