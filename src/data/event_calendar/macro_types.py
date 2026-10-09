"""Macro event value types: one release date and the coverage report."""

from dataclasses import dataclass, field
from datetime import date

from src.data.event_calendar.macro import (
    ReleaseFailure,
)


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
        when = (
            "TODAY"
            if self.days_away == 0
            else ("TOMORROW" if self.days_away == 1 else f"in {self.days_away} calendar days")
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
            f"{label} (cached {age}d ago)" if age else f"{label} (cached today)" for label, age in self.from_cache
        )
        return (
            f" SERVED FROM THE PRE-OPEN SCHEDULE CACHE, not fetched this run: "
            f"{served}. Published release schedules change rarely, so a cached "
            f"copy is real data — but it is stated, never passed off as a live "
            f"read."
        )

    def _describe_coverage(self) -> str:
        if self.configured == 0:
            return "Macro event calendar: NO releases configured (misconfiguration)."
        if self.succeeded == 0:
            names = ", ".join(f"{f.label} ({f.reason})" for f in self.failed)
            return (
                f"Macro event calendar: 0/{self.configured} release schedules "
                f"returned this run — the calendar is UNAVAILABLE. FAILED: "
                f"{names}. An empty calendar here means NOT FETCHED, never "
                f'"no events scheduled".'
            )
        if not self.failed:
            return (
                f"Macro event calendar: {self.succeeded}/{self.configured} release schedules returned. Full coverage."
            )
        names = ", ".join(f"{f.label} ({f.reason})" for f in self.failed)
        return (
            f"Macro event calendar: {self.succeeded}/{self.configured} release "
            f"schedules returned this run. FAILED: {names}. Treat this as a "
            f"coverage GAP, not a confirmed empty calendar — a release whose "
            f"schedule did not fetch is not a release that isn't happening."
        )
