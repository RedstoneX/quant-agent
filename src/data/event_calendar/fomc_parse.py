"""FOMC schedule parsers: pure functions over one already-fetched JSON or HTML document."""

import html as html_module
import logging
import re
from datetime import date, timedelta

from src.data.event_calendar.fomc import (
    _FOMC_MAX_MEETING_DAYS,
    FOMCCalendarParseError,
    FOMCMeeting,
)

logger = logging.getLogger(__name__)

_FOMC_DURATION_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4}
_FOMC_DURATION_RE = re.compile(
    r"\b(one|two|three|four)[-\s]day\s+meeting\b",
    re.IGNORECASE,
)
_FOMC_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
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
            f"Fed JSON calendar: expected a JSON object, got {type(payload).__name__} — the feed's shape has changed"
        )
    events = payload.get("events")
    if not isinstance(events, list):
        raise FOMCCalendarParseError(
            "Fed JSON calendar: no 'events' list in the payload — the feed's shape has changed"
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
    return " ".join(html_module.unescape(_FOMC_HTML_TAG_RE.sub(" ", fragment)).split())


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
            "Fed FOMC calendar page: no 'NNNN FOMC Meetings' year panel found — the page layout has changed"
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
        months = [_FOMC_MONTHS.get(part.strip()[:3].lower()) for part in month_text.split("/") if part.strip()]
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
                "Fed FOMC page: discarding implausible %d-day block %s..%s (from %r / %r)",
                span,
                start,
                end,
                month_text,
                day_text,
            )
            continue
        meetings.append(FOMCMeeting(start, end, True))

    if not meetings:
        raise FOMCCalendarParseError(
            "Fed FOMC calendar page: year panels found but no readable meeting row — the page layout has changed"
        )
    return _fomc_sorted(meetings)
