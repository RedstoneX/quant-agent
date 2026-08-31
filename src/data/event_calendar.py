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

It does NOT cover FOMC meeting dates. FRED's release 101 ("FOMC Press
Release") is a DAILY release with no meeting schedule attached: queried over
2026-01-01..2026-08-30 it returns all 240 calendar days, and queried forward it
returns every calendar day (with the no-data flag) or nothing at all (without
it). There is no free FRED endpoint that yields the FOMC meeting calendar, and
the owner has permanently refused paid data sources. That gap is therefore
declared to the seats explicitly through `UNCOVERED_EVENTS` instead of being
left for a model to fill from memory — which is precisely the defect this
module exists to close.
"""

import json
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from datetime import date, timedelta
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

#: Scheduled events the FREE path demonstrably cannot supply. Rendered into
#: every seat's event-risk block so the absence is stated rather than inferred.
#: See this module's docstring for the live evidence behind the FOMC entry.
UNCOVERED_EVENTS: tuple[str, ...] = (
    "FOMC meeting / rate-decision dates — FRED's release 101 (\"FOMC Press "
    "Release\") carries no meeting schedule (it reports as a daily release, so "
    "its date list is every calendar day and is worthless as a calendar), and "
    "no other free FRED endpoint publishes the FOMC calendar. Treat the FOMC "
    "schedule as UNKNOWN and say so; do not supply a date from memory.",
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

def format_macro_events_section(
    events: list[MacroEvent] | None,
    coverage: EventCalendarCoverage | None,
    horizon_days: int,
    *,
    heading: str = "## Scheduled Macro Releases — FETCHED (do NOT answer from memory)",
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
    lines.append("Not covered by this calendar:")
    lines.extend(f"- {item}" for item in UNCOVERED_EVENTS)
    return "\n".join(lines) + "\n"


def format_event_risk_block(
    *,
    earnings: list[EarningsProximity] | None,
    events: list[MacroEvent] | None,
    coverage: EventCalendarCoverage | None,
    horizon_days: int,
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
    )
