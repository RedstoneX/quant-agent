"""FRED release-dates provider."""

import logging
import random
import time
from datetime import date, timedelta
from src.data.fred_series_client import http_get_json

from src.data.event_calendar.macro import (
    _FAILURE_REASON_MAX_LEN,
    RELEASE_SCHEDULE_LOOKAHEAD_DAYS,
    MacroRelease,
    MACRO_RELEASES,
    ReleaseFailure,
)
from src.data.event_calendar.macro_cache import (
    RELEASE_SCHEDULE_CACHE_PATH,
    ReleaseScheduleCache,
)
from src.data.event_calendar.macro_types import (
    MacroEvent,
    EventCalendarCoverage,
)
from src.data.event_calendar.macro_fetch import (
    fetch_release_dates,
)
from src.trading_calendar import et_today


logger = logging.getLogger(__name__)


class MacroEventCalendarProvider:
    """Forward schedule of US macro releases, from FRED's free release-dates API.

    `src/data/fred_series_client.py` (the client `src/data/macro.py` uses) exposes no releases/dates
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
        return http_get_json(url, timeout, "quant-agent event-calendar")

    def _fetch_release_dates(
        self, release: MacroRelease, start: date, end: date,
    ) -> tuple[list[date], str]:
        """Forward dates for one release. Returns (dates, failure_reason); the
        reason is "" on success."""
        return fetch_release_dates(self, release, start, end)

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
