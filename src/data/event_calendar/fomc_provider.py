"""FOMC calendar provider: JSON primary, HTML fallback, disk cache."""

import json
import logging
import random
import time
from datetime import date, timedelta
from pathlib import Path
from src.data import news as _transport  # the one module whose `urlopen` the rehearsal rebinds

from src.data.event_calendar.fomc import (
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
    FOMCCalendarParseError,
    FOMCMeeting,
    FOMCCoverage,
)
from src.data.event_calendar.fomc_parse import (
    _fomc_sorted,
    parse_fomc_meetings_from_json,
    parse_fomc_meetings_from_html,
)
from src.data.event_calendar.macro import (
    _FAILURE_REASON_MAX_LEN,
)
from src.trading_calendar import et_today


logger = logging.getLogger(__name__)


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
        request = _transport.Request(url, headers={"User-Agent": _FOMC_USER_AGENT})
        with _transport.urlopen(request, timeout=timeout) as response:  # noqa: S310 — fixed https host
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
