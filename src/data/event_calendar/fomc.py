"""FOMC meeting calendar: JSON primary, HTML fallback, disk cache, coverage and provider."""

import logging
from dataclasses import dataclass
from datetime import date


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
