"""One FRED release-dates fetch: the retry, backoff and deadline loop for a single release."""

import logging
import time
from datetime import date
from urllib.parse import urlencode

from src.data.event_calendar.macro import (
    FRED_RELEASE_DATES_URL,
    MacroRelease,
)

logger = logging.getLogger(__name__)


def fetch_release_dates(
    provider, release: MacroRelease, start: date, end: date,
) -> tuple[list[date], str]:
    """Forward dates for one release. Returns (dates, failure_reason); the
    reason is "" on success.

    Lifted out of `MacroEventCalendarProvider._fetch_release_dates`, which calls
    it; `provider` supplies the transport, backoff and deadline state."""
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
        "api_key": provider.api_key,
        "file_type": "json",
        "realtime_start": start.isoformat(),
        "realtime_end": end.isoformat(),
        "include_release_dates_with_no_data": "true",
        "sort_order": "asc",
        "limit": 60,
    }
    url = f"{FRED_RELEASE_DATES_URL}?{urlencode(params)}"

    retries = (
        provider.max_retries
        if provider._consecutive_failed < provider.breaker_after_failed_releases
        else 0
    )
    for attempt in range(retries + 1):
        remaining = (
            provider._deadline - time.monotonic() if provider._deadline is not None
            else provider.request_timeout_s
        )
        if remaining <= 0:
            logger.warning(
                "FRED release-dates deadline exceeded before attempt %d/%d "
                "for %s — degrading now",
                attempt + 1, retries + 1, release.label,
            )
            return [], "fetch_deadline_exceeded"
        try:
            payload = provider._http_get_json(
                url, timeout=min(provider.request_timeout_s, remaining),
            )
        except Exception as e:  # noqa: BLE001 — any transport shape degrades
            reason = str(e) or type(e).__name__
            if attempt < retries:
                backoff = provider._next_backoff(attempt)
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
