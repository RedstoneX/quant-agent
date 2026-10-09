"""The macro fetch's wall-clock budget, as a standalone service.

This is the fair-share arithmetic board items 119/187 ratified, lifted out of
`MacroDataProvider` so it can be constructed and exercised with no provider,
no FRED client and no network anywhere in sight. It knows about seconds,
retry policy and which series have had their turn; it knows nothing about
HTTP, caches, coverage or freshness.

MEASURED SHAPE OF THE DEFECT THIS EXISTS FOR (retained journald,
2026-09-01..09-26, `_UID=1001`). Across that window the macro fetch logged 91
"deadline already exceeded -- skipping <series> without an attempt" lines
against only 12 observation errors and 9 metadata timeouts. So the dominant
failure was never provider flakiness: it was series that were never asked.

WHICH series were never asked is the proof. The skip count is monotonic in a
series' position in the configured list: the last two 13 times each, the next
12, 10, 9, 7, 7, 5, 3, 1 -- and the first five never once. A provider outage
does not sort itself by list index. A single first-come-first-served
wall-clock budget does exactly that: whoever runs first spends whatever it
likes, and the tail of the list pays.

The ceiling is NOT too short. The pre-open prefetch does the same fifteen
observations plus fifteen metadata calls -- thirty serial HTTPS round trips --
under no budget pressure, and systemd timed it at 11 s, 13 s and 16 s on three
midday runs and 56 s, 57 s and 91 s on three evening runs [measured,
`quant-agent-macro-prefetch.service` Starting->Finished, 2026-09-23..09-25].
That is roughly 0.4-1.9 s per call. Ninety seconds is ample for the whole job;
what consumes it is a single hung socket held open for the full 15 s
`request_timeout_s`, which alone costs as much as an entire healthy run, and
which `max_retries=2` can multiply to 45 s plus backoff on ONE series.

So the fix is not a longer deadline and not a different retry count. It is
that no series may spend another series' turn. Each series is reserved an
equal share of the ceiling -- `deadline span / number of configured series`,
90/15 = 6 s on the trading path -- and a series in progress may only spend
whatever is left ABOVE the reserves of the series still waiting. That is not a
new number: it is arithmetic over `total_fetch_deadline_s` and the length of
the configured series list, both of which already exist.

Retries and metadata are explicitly second-class. A retry, its backoff sleep,
and a `/fred/series` metadata lookup may only draw on the SURPLUS above the
waiting series' reserves. A metadata miss degrades to `freshness: unknown`
(honest, and already the behaviour); an observation that is never asked for is
a hole in the economics seat. The first must never buy itself the second.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class MacroFetchBudget:
    """Wall-clock accounting for one macro fetch run.

    Every collaborator arrives BY VALUE through `build_macro_fetch_budget`:
    the series list, the retry/timeout settings, a monotonic clock and a
    jitter source. Nothing here reaches back into whatever built it, which is
    what makes the clock and the jitter substitutable in a test.
    """

    series_ids: tuple[str, ...]
    request_timeout_s: float
    max_retries: int
    retry_backoff_base_s: float
    retry_backoff_max_s: float
    retry_backoff_jitter_s: float
    total_fetch_deadline_s: float
    monotonic: Callable[[], float]
    jitter: Callable[[float], float]

    #: Wall-clock deadline for the CURRENT run, in `monotonic()` units. None
    #: outside a run (e.g. a direct single-indicator call has no shared-budget
    #: concept to enforce).
    deadline: float | None = None
    #: The SPAN (seconds) of the deadline currently in force. Stored alongside
    #: `deadline` because the fair-share reservation is a function of the
    #: span, and the span differs between the trading path and the prefetch.
    deadline_span_s: float | None = None
    #: Series already RESOLVED on the current run -- served from cache,
    #: attempted on the wire, or recorded as not attempted. This is the
    #: denominator of the fair-share reservation: it is how a series that has
    #: not had its turn yet keeps a claim on the shared budget.
    resolved: set[str] = field(default_factory=set)

    # --- arming ---------------------------------------------------------

    def arm(self, span_s: float) -> None:
        """Open a run of `span_s` seconds and clear the resolved set."""
        self.deadline = self.monotonic() + span_s
        self.deadline_span_s = span_s
        self.resolved = set()

    def disarm(self) -> None:
        """Close the run. Scoped to one call -- a later direct single-series
        call must not inherit a stale, already-expired deadline."""
        self.deadline = None
        self.deadline_span_s = None

    def mark_resolved(self, series_id: str) -> None:
        """This series has had its turn: it stops holding a reserve and
        starts spending."""
        self.resolved.add(series_id)

    # --- derived ceilings -----------------------------------------------

    @property
    def prefetch_deadline_s(self) -> float:
        """Wall-clock ceiling for one pre-open prefetch run.

        Computed from the settings already in force, never stored:

            observations  N series x (max_retries + 1) attempts x timeout
            backoff       N series x the capped backoff curve
            metadata      N series x 1 attempt x timeout

        At the shipped defaults (15 series, 15 s timeout, 2 retries, 2 s/8 s/
        1 s backoff) that is 675 + 120 + 225 = 1,020 s, and it is a gross
        upper bound: `breaker_after_failed_series` drops every series after
        the first total failure to a single attempt, so a real outage finishes
        far sooner.

        1,020 s is what makes the schedule work. The prefetch fires at 08:45
        ET -- after the 08:30 BLS release slot, so it cannot hold a pre-CPI
        snapshot -- and the morning session reads macro at the measured
        09:30:49 ET. Even the gross bound lands at 09:02 ET, 28 minutes clear.

        THERE IS NO PACING SLEEP, deliberately. An earlier draft spaced each
        request by `request_timeout_s` to stay under FRED's rate limit. Two
        findings killed it: FRED publishes no rate limit on its own API
        documentation (checked 2026-09-23 -- the 120/min figure circulates
        only in third-party clients), and the macro FRED client (formerly `fredapi`) is a bare `urlopen` with
        no internal retry or sleep of its own (verified against the installed
        package: zero retry/sleep/backoff in `fredapi/fred.py`; its replacement never retries).
        So a strictly serial walk already has exactly one request in flight and
        is paced by round-trip latency itself -- a structural guarantee. A
        sleep on top of that would be an unsourced constant defending against
        nothing anyone can name, which is precisely what this desk's
        no-arbitrary-numbers rule exists to stop. What PR #565 tripped was
        CONCURRENCY, not serial throughput.
        """
        attempts = self.max_retries + 1
        backoff_per_series = sum(
            min(self.retry_backoff_base_s * (2**attempt), self.retry_backoff_max_s) + self.retry_backoff_jitter_s
            for attempt in range(self.max_retries)
        )
        n = len(self.series_ids)
        wire = n * self.request_timeout_s * (attempts + 1)
        backoff = n * backoff_per_series
        return wire + backoff

    @property
    def per_series_reserve_s(self) -> float:
        """The share of the active wall-clock ceiling reserved for each
        configured series' first attempt.

        Derived, never stored: the deadline span in force divided by the
        number of series that span has to cover. On the trading path that is
        90 s / 15 = 6 s; inside the prefetch the span is
        `prefetch_deadline_s`, so the reserve is large enough that
        `request_timeout_s` binds first and the prefetch is unaffected.
        """
        span = self.deadline_span_s if self.deadline_span_s is not None else self.total_fetch_deadline_s
        return span / len(self.series_ids)

    def remaining_s(self) -> float | None:
        """Seconds left on the current run's ceiling, or None when no ceiling
        is in force."""
        if self.deadline is None:
            return None
        return self.deadline - self.monotonic()

    def reserved_for_waiting_s(self, series_id: str) -> float:
        """Budget that belongs to configured series which have not had their
        turn yet, and which `series_id` must therefore not spend."""
        waiting = {s for s in self.series_ids if s not in self.resolved and s != series_id}
        return len(waiting) * self.per_series_reserve_s

    def observation_allowance_s(self, series_id: str) -> float | None:
        """Wall clock this series' FIRST attempt may consume, or None when no
        ceiling is in force.

        Never less than this series' own reserve (so its turn cannot be
        confiscated by an earlier series), never more than what is actually
        left, and never more than `request_timeout_s`.
        """
        remaining = self.remaining_s()
        if remaining is None:
            return None
        allowance = max(
            self.per_series_reserve_s,
            remaining - self.reserved_for_waiting_s(series_id),
        )
        return min(self.request_timeout_s, remaining, allowance)

    def surplus_s(self, series_id: str) -> float | None:
        """Wall clock available for SECOND-CLASS work on this series -- a
        retry, its backoff sleep, or a metadata lookup. This is strictly the
        budget above every still-waiting series' reserve, so second-class work
        can never cost another series its attempt."""
        remaining = self.remaining_s()
        if remaining is None:
            return None
        return remaining - self.reserved_for_waiting_s(series_id)

    def next_backoff(self, attempt: int, series_id: str | None = None) -> float:
        """Exponential backoff with jitter, clipped to whatever remains of the
        fetch deadline so a retry sleep can never itself blow the wall-clock
        ceiling the run promises callers.

        When a series is named, the clip is tighter still: the sleep is
        second-class work like the retry it precedes, so it may only draw on
        the SURPLUS above the reserves of series that have not been asked yet
        (board items 119/187). A run that sleeps a series' turn away is the
        same starvation as a run that spends it on the wire.

        `attempt` is 0-indexed (the attempt that just failed). Backoff doubles
        each attempt from `retry_backoff_base_s`, capped at
        `retry_backoff_max_s`, then gets uniform(0, `retry_backoff_jitter_s`)
        added -- jitter keeps a many-series outage from retrying every series
        in lockstep against FRED.
        """
        base = min(
            self.retry_backoff_base_s * (2**attempt),
            self.retry_backoff_max_s,
        )
        backoff = base + self.jitter(self.retry_backoff_jitter_s)
        if self.deadline is not None:
            remaining = self.deadline - self.monotonic()
            if series_id is not None:
                remaining = min(remaining, self.surplus_s(series_id) or 0.0)
            backoff = max(0.0, min(backoff, remaining))
        return backoff


def _uniform_jitter(width: float) -> float:
    return random.uniform(0, width)


def build_macro_fetch_budget(
    *,
    series_ids: tuple[str, ...],
    request_timeout_s: float,
    max_retries: int,
    retry_backoff_base_s: float,
    retry_backoff_max_s: float,
    retry_backoff_jitter_s: float,
    total_fetch_deadline_s: float,
    monotonic: Callable[[], float] = time.monotonic,
    jitter: Callable[[float], float] = _uniform_jitter,
) -> MacroFetchBudget:
    """Build the budget from its collaborators, BY VALUE.

    The clamping that guarantees these invariants lives at the provider's own
    constructor (and at `MacroConfig`'s validator); this function asserts the
    one invariant the arithmetic here cannot survive without -- a non-empty
    series list, which is the divisor of every reserve.
    """
    if not series_ids:
        raise ValueError(
            "MacroFetchBudget needs at least one series id: the per-series "
            "reserve divides the ceiling by the number of series."
        )
    return MacroFetchBudget(
        series_ids=tuple(series_ids),
        request_timeout_s=float(request_timeout_s),
        max_retries=int(max_retries),
        retry_backoff_base_s=float(retry_backoff_base_s),
        retry_backoff_max_s=float(retry_backoff_max_s),
        retry_backoff_jitter_s=float(retry_backoff_jitter_s),
        total_fetch_deadline_s=float(total_fetch_deadline_s),
        monotonic=monotonic,
        jitter=jitter,
    )
