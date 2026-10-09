from __future__ import annotations

from dataclasses import dataclass, field
from src.coverage_watchdog_parts.awaiting_print import awaiting_first_print


@dataclass(frozen=True)
class CoverageGap:
    symbol: str
    held_qty: float
    covered_qty: float
    uncovered_qty: float
    unprotected_value: float  # dollars; 0.0 when the price is unknowable
    #: A short is protected by a BUY stop read off its SHORT entry row —
    #: the mirrored repair of a long's SELL stop off its BUY row.
    is_short: bool = False


@dataclass(frozen=True)
class UnreadableStop:
    """A held position whose protective stops could not be READ at all.

    Board item 172. This is not a `CoverageGap` and must never be rendered
    as one: a gap is a measured shortfall, and this is the absence of a
    measurement. The position may be perfectly protected or completely
    naked — the desk does not know which, and "does not know" is the
    finding. Folding it into either the covered set or the gap set would
    state a fact nobody established.

    Produced only by a FAILED READ: the broker raised, or the snapshot came
    back in a shape whose quantities cannot be parsed. A snapshot that
    returns cleanly and lists no stops is a readable answer meaning "there
    is no stop", which is a `CoverageGap`, not this.
    """

    symbol: str
    held_qty: float
    reason: str
    is_short: bool = False


@dataclass(frozen=True)
class UnguardedWindow:
    """A position the coverage sweep DELIBERATELY did not check, because a
    live scale-in holds its protective stop cancelled on purpose.

    Board item 193. The skip is correct and stays: re-placing the stop here
    re-creates the opposite-side block the cancel just cleared. What was
    wrong is that the skipped symbol then vanished from the coverage report
    entirely, so the one moment the desk is naked was the one moment the
    report said nothing at all. This row is that symbol said out loud.

    `seconds_open` is measured from the write-ahead row's own `created_at`,
    which is a WRITE time, not the broker's cancel acknowledgement — so it
    approximates the window rather than measuring it, and every rendering of
    it says so. `bound_seconds` is not a chosen number: it is the LONGEST
    window the desk has actually measured and closed, read back out of its
    own recorded events. With no measured history there is no bound and
    nothing is called overdue.
    """

    symbol: str
    held_qty: float
    is_short: bool
    since_utc: str
    seconds_open: float | None
    bound_seconds: float | None
    bound_observations: int

    @property
    def over_bound(self) -> bool:
        return (
            self.seconds_open is not None and self.bound_seconds is not None and self.seconds_open > self.bound_seconds
        )


@dataclass(frozen=True)
class RepairOutcome:
    """One attempt to put a missing protective stop back. `placed` False with
    a `detail` is a FAILURE that must be reported, never swallowed."""

    symbol: str
    qty: float
    placed: bool
    detail: str = ""
    #: The machine-readable half of `detail` — `_refuse(..., code=...)` in
    #: `src/execution/stop_repair.py`. Empty when the failure came from
    #: somewhere else (an exception, a re-read that raised). What the
    #: alerting path needs in order to tell a refusal that is the TAPE's
    #: from one that is the desk's; `detail` is prose and cannot be tested.
    refusal_code: str = ""
    #: Whether anything was still standing watch over the position at the
    #: moment of the attempt (the durable whole-share GTC leg). False means
    #: nothing is, which is never a state this unit waits out.
    still_covered: bool = False


@dataclass(frozen=True)
class CoverageStatus:
    """`should_alert` and `should_alert_repair_failure` are the only fields
    callers act on."""

    trading_day: str | None  # the session judged, YYYY-MM-DD (ET)
    session_ran: bool | None  # None: database unreadable
    gaps: list[CoverageGap] = field(default_factory=list)
    broker_error: str | None = None
    db_error: str | None = None
    already_alerted_for_day: bool = False
    #: Placement attempts made THIS run. Empty when the market was shut.
    repairs: list[RepairOutcome] = field(default_factory=list)
    #: Why placement was or was not possible — the exchange calendar's answer,
    #: rendered for the journal and the alert. Never a guess.
    market_open: bool = False
    market_reason: str = ""
    already_alerted_repair_failure_for_day: bool = False
    #: Held positions actually examined this run (the cash-sweep vehicle and
    #: mid scale-in names excluded). None when the broker could not be read.
    #: Observability only — board item 131: nothing decides on it.
    positions_checked: int | None = None
    #: Why a repair the gaps called for was NOT attempted this run (a live
    #: session owns the desk, or another desk process holds the repair
    #: lock). Empty when nothing was deferred. Observability only.
    repair_deferred: str = ""
    #: Held positions whose protective stops could not be READ this run
    #: (board item 172). Kept OUT of `gaps` and out of `unprotected_total`:
    #: their exposure is unknown, not zero and not measured, and adding a
    #: guessed number to a dollar total the owner reads would be worse than
    #: saying the read failed.
    unreadable: list[UnreadableStop] = field(default_factory=list)
    already_alerted_unreadable_for_day: bool = False
    #: The subset of `unreadable` whose symbols were NOT already reported to
    #: the owner today — i.e. exactly what this run claimed. The owner-facing
    #: text promises "at most once per symbol per trading day", and rendering
    #: `unreadable` wholesale broke that promise: with AAPL already reported
    #: and NVDA newly unreadable, the gate correctly re-opens for NVDA and the
    #: message then re-named AAPL too. The live session's own path filters to
    #: its claim before building the message; this is the same discipline on
    #: the standalone sweep, which could not re-claim (the claim is written
    #: inside `check_coverage`, so re-claiming would find its own marker and
    #: silence the message it was written for).
    unreadable_fresh: list[UnreadableStop] = field(default_factory=list)
    #: Names this run put a stop back on that the owner was PAGED about
    #: earlier today, and has not yet been told about. Claimed inside
    #: `check_coverage` exactly as `unreadable_fresh` is, so the caller
    #: sends what was claimed rather than re-claiming and silencing itself.
    #: Empty is the ordinary case: a repair the owner was never alarmed
    #: about produces no all-clear.
    resolution_notice_symbols: tuple[str, ...] = ()
    #: How many gaps this run DETECTED, before any repair.
    #:
    #: 2026-09-30: the sweep repaired AAPL and reported "gaps 0, repairs
    #: attempted 1". It was not two code paths disagreeing — detection and
    #: the repair trigger read the same list. It is one variable doing two
    #: jobs: after a successful placement `check_coverage` RE-READS the
    #: broker and rebinds `gaps` to what is STILL uncovered, so `gaps`
    #: silently changes meaning from "found" to "left" and the summary
    #: counted the second. An operator scans the gap count, so the line
    #: concealed the very event it was reporting. Both numbers are kept
    #: now: this one is what was found, `gaps` is what remains.
    #: None means the status was assembled by hand rather than by
    #: `check_coverage`; readers fall back to len(gaps).
    gaps_detected: int | None = None
    #: Board item 193. Positions this run DELIBERATELY did not check because
    #: a live scale-in holds their protective stop cancelled on purpose.
    #: Deliberately NOT folded into `gaps`: a gap is a defect the sweep tries
    #: to repair, and repairing one of these re-creates the opposite-side
    #: block the cancel just cleared. They are reported, never acted on.
    unguarded: list[UnguardedWindow] = field(default_factory=list)
    #: The subset of `unguarded` whose symbols this run is entitled to page
    #: about — over the longest measured window and not already reported
    #: today. Claimed inside `check_coverage`, exactly as `unreadable_fresh`
    #: is, so the caller sends what was claimed instead of re-claiming and
    #: silencing its own message.
    unguarded_fresh: list[UnguardedWindow] = field(default_factory=list)

    @property
    def unguarded_over_bound(self) -> list[UnguardedWindow]:
        """Deliberate windows that have outlived every window the desk has
        measured. Empty whenever no window has ever been measured: with no
        bound there is nothing to be over, and inventing one would be a
        guessed number governing an owner page."""
        return [r for r in self.unguarded if r.over_bound]

    @property
    def should_alert_unguarded(self) -> bool:
        return bool(self.unguarded_fresh)

    @property
    def unprotected_total(self) -> float:
        return round(sum(g.unprotected_value for g in self.gaps), 2)

    @property
    def repaired(self) -> list[RepairOutcome]:
        return [r for r in self.repairs if r.placed]

    @property
    def should_alert_repair_performed(self) -> bool:
        """A repair actually happened and the owner has not been told.

        2026-09-30: the sweep put AAPL's stop back after the position had
        been unprotected for 13m22s and reported "alert none sent". A
        COVERAGE REPAIRED event is never routine — it means something
        upstream failed silently, and the last line of defence is the only
        thing that noticed. It pages, on the same owner channel as every
        other message this unit sends.

        Suppressed when `resolution_notice_symbols` already covers every
        repaired name: that is the all-clear for a gap he was ALREADY
        paged about, and two messages about one event is the noise that
        makes him stop reading them. A run that repairs nothing stays
        silent exactly as before.
        """
        if not self.repaired:
            return False
        told = {str(s).strip().upper() for s in self.resolution_notice_symbols}
        return any(str(r.symbol).strip().upper() not in told for r in self.repaired)

    @property
    def repairs_awaiting_print(self) -> list[RepairOutcome]:
        """Attempts that did not place because the name has not printed
        today, with the whole-share leg still standing watch. Not failures
        of the desk and not this run's to page — see `awaiting_first_print`.
        """
        return [
            r
            for r in self.repairs
            if not r.placed
            and awaiting_first_print(
                refusal_code=r.refusal_code,
                still_covered=r.still_covered,
                market_open=self.market_open,
            )
        ]

    @property
    def repair_failures(self) -> list[RepairOutcome]:
        awaiting = {id(r) for r in self.repairs_awaiting_print}
        return [r for r in self.repairs if not r.placed and id(r) not in awaiting]

    @property
    def is_exposed(self) -> bool:
        """Coverage is short AND the sweep that should have fixed it did
        not run. An unreadable database counts as "did not run": we
        cannot prove the sweep happened, and that is the finding.

        `gaps` is the RESIDUAL gap — what is still uncovered after any
        placement this run made — so a remainder that was successfully
        re-covered does not report itself as exposure it no longer is.
        """
        return bool(self.gaps) and not self.session_ran

    @property
    def should_alert(self) -> bool:
        return self.is_exposed and not self.already_alerted_for_day

    #: `already_alerted_for_day` is keyed per SYMBOL per day (item 211
    #: defect 2): it is true only when EVERY currently-uncovered position
    #: has already been reported today, so a second name going naked later
    #: the same day still pages.

    @property
    def should_alert_repair_failure(self) -> bool:
        """A failed placement is its own alarm, on its own once-a-day
        marker. Sharing the exposure marker would let the 06:15 report
        swallow a 10:00 failure to put the stop back — a silence with an
        uncovered position behind it, which is the whole defect item 53
        was opened on.

        `already_alerted_repair_failure_for_day` is now true only when
        EVERY currently-failing position has already been reported today
        (`_repair_failure_alerted_symbols`), so a new name failing later in
        the day cannot be swallowed either — the same reasoning applied one
        level down. The marker is shared with the live session path, which
        can find the identical condition in a process this one knows
        nothing about."""
        return bool(self.repair_failures) and not self.already_alerted_repair_failure_for_day

    @property
    def should_alert_unreadable(self) -> bool:
        """A stop the broker could not be ASKED about is its own alarm, on
        its own once-a-day-per-symbol marker. Board item 172.

        Deliberately NOT gated on `session_ran`, unlike `should_alert`.
        That gate exists because an uncovered remainder is expected to be
        re-covered by the next session's own sweep, so alerting before the
        session has had its chance would be noise. Nothing re-reads a stop
        the broker refused to describe — a session running changes nothing
        about it — so waiting for one would only delay the report.
        """
        return bool(self.unreadable) and not self.already_alerted_unreadable_for_day
