"""FOMC meeting calendar: JSON primary, HTML fallback, disk cache, coverage and provider."""

import html as html_module
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.request import Request, urlopen

from src.data.event_calendar.macro import _FAILURE_REASON_MAX_LEN
from src.trading_calendar import et_today

logger = logging.getLogger(__name__)

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

