"""Stop-coverage watchdog for a desk that is NOT running sessions.

THE GAP THIS CLOSES (docs/INCIDENT_HISTORY.md, 2026-09-12)
------------------------------------------------------------
Spec §11.1 protects a fractional position with TWO orders: a GTC stop over
the whole shares and a DAY stop over the sub-share remainder. The broker
refuses any fractional order that is not DAY (measured 2026-09-01, and
Alpaca's own fractional-trading page says the same), so the DAY leg lapses
at 16:00 ET by design and the design's stated precondition is that "the
next session's coverage sweep re-places it".

Nothing checked that precondition. When the trading timers were paused on
2026-09-03 the sweep stopped with them, and ORCL's 0.3089-share remainder
sat with no stop through six full trading sessions (09-03 to 09-11) while
every record on the box still described the lapse as "expected overnight".
The evening run had honestly reported the lapse in dollars on 09-02; there
was simply no run afterwards to report anything at all. An alarm that lives
inside the sessions cannot fire on the absence of sessions.

WHAT THIS IS, AND IS NOT
-------------------------
It reads positions and open stops from the broker and session evidence from
`alert_channel_checks` (the same rows `src/silence_watchdog.py` reads), and
it sends one owner alert. It never cancels, modifies, resizes or closes
anything: the ONE mutation it can make is to ADD a protective stop over
shares the broker is not watching.

IT NOW REPAIRS, NOT ONLY REPORTS (docs/INCIDENT_HISTORY.md, 2026-09-14)
------------------------------------------------------------------------
Alerting alone left the owner holding the only repair tool, once a day, by
hand. Item 53's ruling is that the daily path should put the missing DAY
stop back instead of only naming it — the desk's protection should not
depend on the desk being switched on.

The re-placement is NOT a new code path. It calls
`src.execution.stop_repair.repair_stop_coverage`, the same function
`TradingPipeline._reconcile_stop_coverage` calls during a normal session,
at the same level (the stop recorded on the position's own last opening
row: BUY for a long, SHORT for a short), with
the same guards (never a sell-stop at/above the live price, never a
buy-stop at/below it, never invented when the row has none) and the same
tif derivation (`_derive_stop_tif`: whole shares GTC, sub-share DAY,
because the broker refuses anything else).

WHAT IT STILL CANNOT DO — say it plainly
-----------------------------------------
It cannot make a sub-share remainder safe OVERNIGHT. No durable fractional
stop exists at this broker; a DAY order is the only fractional order it will
accept, and a DAY order stops existing at the close. Every re-placement here
buys protection for ONE session and lapses at 16:00 ET like the one before
it. The overnight gap on the remainder is not solved by this change and is
not solvable in code — only by holding whole shares or by not holding the
remainder.

IT CANNOT FIRE BEFORE THE OPEN
-------------------------------
The unit it rides on fires at 06:15 ET, more than three hours before the
bell. A fractional DAY stop submitted into a shut market is a rejection at
best and a surprise queued order at worst (the same judgement
`TradingPipeline._reconcile_stop_coverage` already makes for case (a)), and
this desk does not guess at broker behaviour it has not measured. So
`session_is_open` gates every placement on the exchange calendar the broker
publishes — `is_trading_day`, `get_session_open`, `get_session_close`, no
clock arithmetic of our own — and an unreadable calendar means NO placement,
never a hopeful one. At 06:15 the answer is always "shut": that run alerts
exactly as it did before and says the stop goes back at the open. The run
that actually repairs is the session-hours one
(`scripts/systemd/quant-agent-coverage-sweep.timer`, the same `*:0/30` tick
the desk's own session timers use, self-gating on the calendar).

It is not a nightly alert. An uncovered remainder at 06:15 ET is the
ORDINARY state of every fractional position on a healthy desk, and the
owner ratified that it must not page (config/settings.yaml, `ALERTING`).
So the condition here is narrower and is derived from the design itself,
not from a threshold:

    (a) the broker holds a position whose open protective stops cover
        LESS than the held quantity, AND
    (b) NO scheduled session completed during the most recent trading
        session — so the sweep that was supposed to re-place the missing
        coverage never ran.

(b) is exactly "the design's precondition was false for a whole session".
No number is introduced: the session-hours window is
`trading_calendar.SESSION_WINDOWS["intra_check"]` plus the same
`SLACK_MINUTES` the silence watchdog already derives from the timer
cadence.

ONE ALERT PER TRADING DAY, NOT ONE PER EPISODE
-----------------------------------------------
`src/silence_watchdog.py` alerts once per silence episode because the
thing it reports (the desk is down) does not get worse by the day. This
does: every trading session the desk stays paused with a short-covered
position is a fresh day of real exposure, and the owner can end it at any
time (resume the desk, or cover/close the remainder by hand). Re-alerting
once per trading day matches docs/WORK.md item 41's "at most once every
24 hours while it stays broken" — an existing ruling, not a new one.

WHERE IT RUNS
--------------
Two entry points, one function, both through `scripts/alert_heartbeat.py`:

  * after the channel probe on the 06:15 ET alert-heartbeat unit — the
    daily floor that fires seven days a week whether or not the trading
    timers are enabled. Market shut, so this run reports and never places.
    Its result never changes the probe's own verdict or exit code.
  * `--coverage-only`, on the every-30-minutes coverage-sweep unit. Sends no
    probe, touches nothing outside a real session, and is the run that puts
    the DAY stop back.
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import sqlite3
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.silence_watchdog import KNOWN_MODES, SLACK_MINUTES
from src.trading_calendar import ET, SESSION_WINDOWS
from src.trading_day import MAX_WEEKDAYS_BACK, _session_bounds_utc, most_recent_trading_day  # noqa: F401 -- moved to a read-only module
from src.coverage_watchdog_text import (  # noqa: F401 -- re-exported, lifted verbatim
    exit_declined_text,
    unreadable_stop_text,
    alert_text,
    repair_failure_text,
    repair_resolution_text,
    repair_performed_text,
    status_line,
    SWEEP_LOG_NAME,
    SWEEP_AGENT_NAME,
    sweep_summary,
    sweep_log_line,
)

logger = logging.getLogger(__name__)

#: Same database every session and `src/alert_watchdog.py` write to.
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "quant_agent.db"

#: On-box record, gitignored like its siblings under data/alerting/.
STATE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "alerting" / "coverage_heartbeat.json"
)

TABLE = "alert_channel_checks"


#: Same tolerance the coverage reconciler uses for "covered < held".
_QTY_EPSILON = 1e-6


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
            self.seconds_open is not None
            and self.bound_seconds is not None
            and self.seconds_open > self.bound_seconds
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

    trading_day: str | None            # the session judged, YYYY-MM-DD (ET)
    session_ran: bool | None           # None: database unreadable
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
        return any(
            str(r.symbol).strip().upper() not in told for r in self.repaired
        )

    @property
    def repairs_awaiting_print(self) -> list[RepairOutcome]:
        """Attempts that did not place because the name has not printed
        today, with the whole-share leg still standing watch. Not failures
        of the desk and not this run's to page — see `awaiting_first_print`.
        """
        return [
            r for r in self.repairs
            if not r.placed and awaiting_first_print(
                refusal_code=r.refusal_code, still_covered=r.still_covered,
                market_open=self.market_open,
            )
        ]

    @property
    def repair_failures(self) -> list[RepairOutcome]:
        awaiting = {id(r) for r in self.repairs_awaiting_print}
        return [
            r for r in self.repairs
            if not r.placed and id(r) not in awaiting
        ]

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


def _utc_now() -> datetime:
    """Seam for tests — real code never patches `datetime` itself."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# which session are we judging?
# ---------------------------------------------------------------------------

def _connect_ro(path: str) -> sqlite3.Connection:
    """Read-only by OS enforcement — this module never writes the trading DB."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def session_ran_during(day: date, db_path: str | Path | None = None) -> tuple[bool | None, str | None]:
    """`(ran, error)` — did ANY known scheduled mode record a completed
    session inside `day`'s cash session? `ran` is None when the database
    cannot be read; the caller treats that as not proven."""
    path = str(db_path) if db_path is not None else str(DB_PATH)
    start, end = _session_bounds_utc(day)
    try:
        conn = _connect_ro(path)
    except Exception as exc:  # noqa: BLE001
        return None, f"database unreadable: {exc}"
    try:
        placeholders = ",".join("?" for _ in KNOWN_MODES)
        rows = conn.execute(
            f"SELECT checked_at FROM {TABLE} WHERE source IN ({placeholders})",
            KNOWN_MODES,
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        return None, f"query failed (table missing on a fresh database?): {exc}"
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    for row in rows:
        when = _parse_iso(str(row["checked_at"]))
        if when is not None and start <= when < end:
            return True, None
    return False, None


def _parse_iso(stamp: str) -> datetime | None:
    try:
        when = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# broker truth — read only
# ---------------------------------------------------------------------------

def _record_unreadable(
    sink: list[UnreadableStop] | None, *, symbol: str, held_qty: float,
    reason: str, is_short: bool,
) -> None:
    """Log an unreadable stop at ERROR and, when a sink was supplied, record
    it for the caller. Board item 172.

    The log line is unconditional ON PURPOSE. A caller that does not pass a
    sink still must not be able to lose the finding silently, because
    silently losing it is the defect this exists to close.
    """
    logger.error(
        "STOP UNREADABLE: %s holding %.4f — %s. Whether a protective stop "
        "exists for this position is UNKNOWN; this is not a measured gap "
        "and must not be reported as coverage.",
        symbol, held_qty, reason,
    )
    if sink is not None:
        sink.append(UnreadableStop(
            symbol=symbol, held_qty=held_qty, reason=reason,
            is_short=is_short,
        ))


def uncovered_positions(
    broker: Any, *, sweep_symbol: str | None = None,
    skip_symbols: set[str] | None = None,
    counts: dict[str, int] | None = None,
    unreadable: list[UnreadableStop] | None = None,
) -> tuple[list[CoverageGap], str | None]:
    """Every held position whose open protective stops cover less than the
    held quantity. Longs are checked against SELL stops, shorts against BUY
    stops, exactly as `TradingPipeline._reconcile_stop_coverage` does. The
    cash-sweep vehicle is deliberately stopless and is skipped.

    Returns `(gaps, error)`; on a broker failure `gaps` is empty and
    `error` says why — the caller reports "could not check", never "clean".

    `counts`, when given, receives `positions_checked` — how many held
    positions were compared against their stops — so a clean run can say
    how much it looked at (board item 131). It changes nothing here.

    `unreadable`, when given, receives one `UnreadableStop` per position
    whose stops could not be READ (board item 172).

    ONE SYMBOL'S READ FAILURE NO LONGER ENDS THE PASS. It used to
    `return [], error` the instant `snapshot_protective_stops` raised for
    any single symbol, which threw away every gap already found and every
    symbol not yet reached — so one flaky name could hide a genuinely naked
    position behind it, and the whole run reported "could not check". The
    unreadable symbol is now recorded by name and the sweep carries on. A
    `get_positions` failure is still a whole-pass error, because without the
    position list there is nothing to iterate and no per-symbol finding to
    make.
    """
    try:
        positions = broker.get_positions()
    except Exception as exc:  # noqa: BLE001
        return [], f"get_positions failed: {exc}"
    if not isinstance(positions, list):
        return [], "get_positions returned no list"
    gaps: list[CoverageGap] = []
    for p in positions:
        symbol = getattr(p, "symbol", None)
        try:
            qty = float(getattr(p, "qty", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not symbol or qty == 0 or (sweep_symbol and symbol == sweep_symbol):
            continue
        if skip_symbols and str(symbol) in skip_symbols:
            continue
        if counts is not None:
            counts["positions_checked"] = counts.get("positions_checked", 0) + 1
        is_short = qty < 0
        held = abs(qty)
        try:
            ok, specs = broker.snapshot_protective_stops(
                symbol, side=("buy" if is_short else "sell"),
            )
        except Exception as exc:  # noqa: BLE001
            _record_unreadable(
                unreadable, symbol=str(symbol), held_qty=held,
                is_short=is_short,
                reason=f"snapshot_protective_stops raised: {exc}",
            )
            continue
        # The broker's own order listing swallows its exception, so
        # `ok=False` with no specs — not a raise — is the COMMON way a stop
        # becomes unreadable. Ignoring it here is what made a broker outage
        # read as a confirmed naked position. Board item 172.
        if not ok:
            _record_unreadable(
                unreadable, symbol=str(symbol), held_qty=held,
                is_short=is_short,
                reason=(
                    "the broker's open-order listing failed, so whether a "
                    "protective stop exists could not be established"
                ),
            )
            continue
        if specs is not None and not isinstance(specs, list):
            _record_unreadable(
                unreadable, symbol=str(symbol), held_qty=held,
                is_short=is_short,
                reason=(
                    "protective-stop snapshot in an unusable shape: "
                    f"{type(specs).__name__}"
                ),
            )
            continue
        covered = 0.0
        unparsable = ""
        for s in specs or []:
            try:
                covered += float(s.get("qty", 0) or 0)
            except (TypeError, ValueError, AttributeError) as exc:
                # A stop order whose quantity cannot be parsed is a stop
                # nobody can size. Counting it as zero would understate
                # coverage and read as a gap; skipping it would overstate
                # coverage by omission. Neither is known, so neither is
                # claimed — the symbol is reported unreadable instead.
                unparsable = f"protective stop in an unreadable shape: {exc}"
                break
        if unparsable:
            _record_unreadable(
                unreadable, symbol=str(symbol), held_qty=held,
                is_short=is_short, reason=unparsable,
            )
            continue
        if covered + _QTY_EPSILON < held:
            uncovered = round(held - covered, 9)
            gaps.append(CoverageGap(
                symbol=str(symbol), held_qty=held, covered_qty=covered,
                uncovered_qty=uncovered,
                unprotected_value=_notional(p, uncovered),
                is_short=is_short,
            ))
    return gaps, None


def _notional(position: Any, qty: float) -> float:
    try:
        price = float(getattr(position, "current_price", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    if not (math.isfinite(price) and price > 0):
        return 0.0
    return round(price * qty, 2)


# ---------------------------------------------------------------------------
# may we place an order right now? the exchange calendar answers, nobody else
# ---------------------------------------------------------------------------

def session_is_open(broker: Any, now: datetime) -> tuple[bool, str]:
    """`(open_now, reason)` — delegates to the ONE shared answer.

    `src.market_session.market_open_verdict` reads the broker calendar twice,
    falls back to the weekday-and-clock check, and only then answers OPEN.
    This used to fail CLOSED, which left a naked position naked whenever the
    calendar read blipped; a positive "shut" is still honoured.
    """
    from src.market_session import market_open_verdict

    return market_open_verdict(broker, now)


# ---------------------------------------------------------------------------
# the one mutation: ADD a protective stop over shares nobody is watching
# ---------------------------------------------------------------------------

def replace_missing_stops(
    broker: Any,
    gaps: list[CoverageGap],
    *,
    last_buy: Any,
    sweep_symbol: str | None = None,
    db: Any = None,
) -> list[RepairOutcome]:
    """Put back the protective stop for each uncovered gap. ADD-ONLY.

    The caller has already established that the session is open; this does
    not consult the clock, exactly as the in-session sweep's repair does not.

    IT CANNOT DOUBLE-COVER. Each symbol's open protective orders are re-read
    from the broker immediately before its own placement, and the quantity
    placed is the shortfall in THAT read. `snapshot_protective_stops` filters
    `QueryOrderStatus.OPEN`, which is Alpaca's live-order set — new, accepted,
    pending_new, partially_filled — so an order still in flight from an
    earlier tick of this same unit counts as coverage and shrinks the
    shortfall to zero, and zero is not placed. The gap list handed in is a
    snapshot taken earlier in the pass and is deliberately NOT trusted for
    this decision.

    IT CANNOT SELL. The only broker call underneath is
    `_submit_protective_stop_retrying`. Nothing here computes a target, and
    no quantity reaches a close/reduce path — a zero shortfall skips, it does
    not zero a position.

    Shorts are repaired the same way as longs: the SHORT entry row carries
    the recorded stop, and the protective order is a BUY stop above the
    tape. Inventing a level when that row has none is still refused.
    """
    from src.execution.stop_repair import repair_stop_coverage

    outcomes: list[RepairOutcome] = []
    for gap in gaps:
        if sweep_symbol and gap.symbol == sweep_symbol:
            continue
        protective_side = "buy" if gap.is_short else "sell"
        opening = "SHORT" if gap.is_short else "BUY"
        # Fresh broker truth for THIS symbol, taken as late as possible.
        try:
            _ok, specs = broker.snapshot_protective_stops(
                gap.symbol, side=protective_side,
            )
        except Exception as exc:  # noqa: BLE001
            outcomes.append(RepairOutcome(
                gap.symbol, gap.uncovered_qty, False,
                f"could not re-read open stops before placing ({exc})",
            ))
            continue
        covered_now = 0.0
        for s in specs or []:
            try:
                covered_now += float(s.get("qty", 0) or 0)
            except (TypeError, ValueError):
                continue
        shortfall = round(gap.held_qty - covered_now, 9)
        if shortfall <= _QTY_EPSILON:
            logger.info(
                "coverage sweep: %s is already covered (%.4f of %.4f) by the "
                "time we got to it — placing nothing.",
                gap.symbol, covered_now, gap.held_qty,
            )
            continue
        # `repair` collects the refusal REASON (docs/WORK.md item 88). The
        # message below used to list every guard that COULD have stopped the
        # repair and send the owner to the journal to work out which one did
        # — including the case this item is about, a recorded stop of zero,
        # which reads as "no recorded stop level" and is not the same thing.
        # `held_qty` / `covered_qty` / the resting orders ride along so a
        # refusal's durable row says what the broker already held.
        repair: dict = {"held_qty": gap.held_qty, "covered_qty": covered_now}
        try:
            placed = repair_stop_coverage(
                broker=broker, last_buy=last_buy,
                symbol=gap.symbol, uncovered_qty=shortfall,
                is_short=gap.is_short, db=db, outcome=repair,
                resting_stops=list(specs or []), caller="coverage_sweep",
            )
        except Exception as exc:  # noqa: BLE001
            outcomes.append(RepairOutcome(
                gap.symbol, shortfall, False, f"placement raised ({exc})",
            ))
            continue
        outcomes.append(RepairOutcome(
            gap.symbol, shortfall, bool(placed),
            refusal_code=str(repair.get("repair_refusal_code") or ""),
            still_covered=covered_now > _QTY_EPSILON,
            detail="" if placed else (
                repair.get("repair_refusal")
                or (
                    "the broker did not accept a protective stop for the "
                    f"shortfall — see the journal for which guard stopped it "
                    f"(no recorded {opening} stop level, stop on the live-"
                    "price side, or retries exhausted)"
                )
            ),
        ))
    return outcomes


# ---------------------------------------------------------------------------
# on-box state — one alert per trading day
# ---------------------------------------------------------------------------

def load_state(path: Path | None = None) -> dict[str, Any]:
    try:
        raw = json.loads((path or STATE_PATH).read_text())
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("alerted_for_day", None)
    raw.setdefault("exposure_alerted_symbols", None)
    # Kept as a truthful record of the last day a placement-failure alert
    # went out, and still written; it is no longer what SUPPRESSES one.
    # Suppression reads `repair_failure_alerted_symbols` below, which is
    # keyed per position as well as per day — see
    # `_repair_failure_alerted_symbols`.
    raw.setdefault("repair_failure_alerted_for_day", None)
    raw.setdefault("repair_failure_alerted_symbols", None)
    raw.setdefault("last_result", None)
    raw.setdefault("updated_at", None)
    return raw


def save_state(state: dict[str, Any], path: Path | None = None) -> bool:
    """Atomic write, same shape as the sibling watchdogs. Never raises."""
    target = path or STATE_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# the placement-failure marker — shared with the live session path
# ---------------------------------------------------------------------------
# A failed session-hours re-placement can be found by EITHER of two
# processes: this standalone unit, or the coverage reconcile a live session
# runs on itself (`src/pipeline.py`). On 2026-09-18 the session found two
# and said in the log that it alerts, while the only code that could alert
# lived here — and no watchdog process ran that day. Both paths now send,
# and both claim the same marker so the owner is not told twice about the
# same position.
#
# The marker is keyed per POSITION as well as per day, which is the honest
# version of "at most once a trading day": a 10:00 failure on one name must
# never be swallowed by an earlier alert about a different one. That is the
# same trap `should_alert_repair_failure` was written to avoid, one level
# down.


def repair_failure_alert_day(now: datetime | None = None) -> str:
    """The ET calendar date a placement failure is filed under.

    Deliberately NOT `most_recent_trading_day`: that answers "which session
    should have re-placed a stop overnight", which at 10:00 ET is still
    yesterday. A placement that just failed is happening TODAY, and both
    paths must agree on the key or the shared marker is no marker at all.
    """
    return (now or _utc_now()).astimezone(ET).date().isoformat()


def _exposure_alerted_symbols(state: dict[str, Any], day: str) -> set[str]:
    """Which UNPROTECTED positions the owner was already paged about today.

    Item 211 defect 2, a live-risk hole. "This position is unprotected" was
    deduped on a bare per-DAY marker while both of its siblings
    (placement-failure, unreadable-stop) dedupe per symbol per day. A
    SECOND name going naked later the same day was therefore silenced
    completely — the exact trap `should_alert_repair_failure` was written
    to avoid, one level down. Same shape, same discipline.
    """
    raw = state.get("exposure_alerted_symbols")
    if not isinstance(raw, dict) or raw.get("day") != day:
        return set()
    return {
        str(sym).strip().upper()
        for sym in (raw.get("symbols") or [])
        if str(sym).strip()
    }


def _repair_failure_alerted_symbols(state: dict[str, Any], day: str) -> set[str]:
    raw = state.get("repair_failure_alerted_symbols")
    if not isinstance(raw, dict) or raw.get("day") != day:
        return set()
    return {
        str(sym).strip().upper()
        for sym in (raw.get("symbols") or [])
        if str(sym).strip()
    }


def _record_repair_failure_alert(
    state: dict[str, Any], day: str, symbols: Iterable[str],
) -> None:
    merged = _repair_failure_alerted_symbols(state, day) | {
        str(sym).strip().upper() for sym in symbols if str(sym).strip()
    }
    state["repair_failure_alerted_symbols"] = {
        "day": day, "symbols": sorted(merged),
    }
    state["repair_failure_alerted_for_day"] = day


def claim_repair_failure_alert(
    symbols: Iterable[str], *, now: datetime | None = None,
    path: Path | None = None,
) -> list[str]:
    """Reserve today's placement-failure alert for `symbols` and return the
    ones NOT already alerted today, in the order given.

    An empty list means every one of them has already been reported and the
    caller should stay quiet. A non-empty list is the caller's to send, and
    is recorded as sent before it returns — an unwritable state file
    therefore errs towards telling the owner twice rather than not at all,
    which is the right way round for an unprotected position.
    """
    day = repair_failure_alert_day(now)
    state = load_state(path)
    already = _repair_failure_alerted_symbols(state, day)
    fresh = [
        sym for sym in dict.fromkeys(
            str(raw).strip().upper() for raw in symbols if str(raw).strip()
        )
        if sym not in already
    ]
    if not fresh:
        return []
    _record_repair_failure_alert(state, day, fresh)
    save_state(state, path)
    return fresh


def _resolution_notified_symbols(state: dict[str, Any], day: str) -> set[str]:
    raw = state.get("repair_resolution_notified_symbols")
    if not isinstance(raw, dict) or raw.get("day") != day:
        return set()
    return {
        str(sym).strip().upper()
        for sym in (raw.get("symbols") or [])
        if str(sym).strip()
    }


def claim_repair_resolution_notice(
    symbols: Iterable[str], *, now: datetime | None = None,
    path: Path | None = None, state: dict[str, Any] | None = None,
) -> list[str]:
    """Reserve today's RESOLUTION notice and return the symbols entitled to
    one: the names the owner was actually paged about today and has not yet
    been told about again.

    THE RETRACTION HALF, and it is not decoration. A red "COULD NOT PUT THE
    PROTECTIVE STOP BACK ... Place the missing stop by hand" was, until now,
    the owner's last word on a position for the rest of the trading day even
    when the desk's own next sweep put the stop back minutes later
    (2026-09-23, RSG: paged 13:30:45, repaired 13:45:45, never retracted).
    He was left holding an instruction to do by hand a thing that was
    already done.

    Deliberately its OWN marker rather than a release of the placement-
    failure claim. Releasing that claim would make the sentence both alerts
    end on -- "Each position is reported at most once per trading day" --
    false, and would open a fail/succeed/fail name to two messages per
    cycle. The failure claim therefore stands for the day exactly as before;
    this adds one retraction per symbol per day and nothing else.

    Claim-before-send, same as every marker here: an unwritable state file
    errs towards telling the owner twice.
    """
    day = repair_failure_alert_day(now)
    own_state = state is None
    st = load_state(path) if own_state else state
    paged = _repair_failure_alerted_symbols(st, day)
    already = _resolution_notified_symbols(st, day)
    fresh = [
        sym for sym in dict.fromkeys(
            str(raw).strip().upper() for raw in symbols if str(raw).strip()
        )
        if sym in paged and sym not in already
    ]
    if not fresh:
        return []
    st["repair_resolution_notified_symbols"] = {
        "day": day, "symbols": sorted(already | set(fresh)),
    }
    if own_state:
        save_state(st, path)
    return fresh


# ---------------------------------------------------------------------------
# a refusal that is the TAPE's, and a refusal that is the desk's
# ---------------------------------------------------------------------------
# Measured, production log, all retained rotations (2026-08-21..2026-09-23):
# the stop-repair refusal `no_trade_print_today` occurred 6 times on 4
# sessions -- NET and RSG on 09-18, BRK-B, NUE and RSG on 09-21, RSG on
# 09-23. Every one was raised between 13:30:43 and 13:30:45 UTC, inside 45
# seconds of the opening bell, and every one was resolved in the same
# session. Not one survived to a second observation.
#
# The refusal itself is right and is NOT changed here: a stop priced off a
# quote the tape never confirmed can fire immediately
# (`src/execution/stop_repair.py`). What was wrong is WHICH pass pages. The
# first attempt, seconds after the bell, paged the owner in red and told him
# to place a stop by hand -- for a name that had simply not printed yet, on
# a desk whose own next pass places it.
#
# NO CONSTANT, and deliberately none. docs/OUTCOME.md forbids fitting a
# threshold to this desk's own past record ("an arbitrary number with a
# backtest stapled to it") and requires reformulating the rule so it needs
# no number at all before anything else is tried. A sweep count or a delay
# fitted to the six observations above would have been exactly the banned
# shape. What is named below is a CONDITION, re-read from the tape every
# pass: has this name printed today, and is anything still standing watch
# over the position. The measurement above is why the condition is worth
# naming; it is not the source of any number, because there is none.
#
# THE RETRY IS NOT DEFERRED. docs/INCIDENT_HISTORY.md (2026-09-18) records
# "waiting for a later check of the day to retry" as explicitly rejected,
# because waiting adds unprotected time. That ruling stands and is not
# touched: every pass still attempts the repair exactly as before, at the
# same cadence, with the same guards. What waits is the PAGE, not the fix.
#
# A refusal for any other reason is a fault on its first observation and
# still pages immediately -- a broker rejection with retries exhausted
# (2026-09-16, BRK-B), a corrupt recorded stop level, a level the tape has
# already passed. None of those resolves by the tape catching up.

#: Refusal codes (`_refuse(..., code=...)` in `src/execution/stop_repair.py`)
#: that say the TAPE has not produced a price yet, rather than that the desk
#: failed at something. An allowlist on purpose: a refusal code added later
#: pages on its first observation, exactly as every code does today, until
#: somebody looks at it and puts it here deliberately.
AWAITING_FIRST_PRINT_CODES: frozenset[str] = frozenset({"no_trade_print_today"})


def awaiting_first_print(
    *, refusal_code: str, still_covered: bool, market_open: bool,
) -> bool:
    """Whether a repair that did not place is waiting on the TAPE rather than
    reporting a failure of the desk. Pure; reads no state and no clock.

    All three terms are load-bearing:

    * `refusal_code` in `AWAITING_FIRST_PRINT_CODES` -- the desk declined
      because this name has no confirmed print today, not because anything
      it tried was refused.
    * `still_covered` -- the durable whole-share GTC leg is standing watch
      over the rest of the position. It is the whole reason waiting is
      tolerable, so a position with NOTHING covering it is never in this
      state and pages on the first attempt, whatever the refusal said.
    * `market_open` -- the session still has passes to come, and each one
      re-reads the tape. Once the market is shut there is no later pass to
      resolve it and the quiet state has run out; see
      `session_awaiting_print_symbols`, which is what makes the silence
      end rather than simply lapse into the expected overnight bucket.
    """
    return (
        bool(market_open)
        and bool(still_covered)
        and str(refusal_code or "") in AWAITING_FIRST_PRINT_CODES
    )


def _awaiting_print_state(state: dict[str, Any], day: str) -> set[str]:
    raw = state.get("awaiting_first_print_symbols")
    if not isinstance(raw, dict) or raw.get("day") != day:
        return set()
    return {
        str(sym).strip().upper()
        for sym in (raw.get("symbols") or [])
        if str(sym).strip()
    }


def _write_awaiting_print_state(
    state: dict[str, Any], day: str, symbols: set[str],
) -> None:
    state["awaiting_first_print_symbols"] = {
        "day": day, "symbols": sorted(symbols),
    }


def note_awaiting_first_print(
    symbol: str, *, now: datetime | None = None, path: Path | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    """Record that `symbol` spent a pass of THIS session waiting for its
    first print with its sub-share remainder uncovered.

    The quiet state above has to end somewhere or it is a suppression. This
    is what ends it: a name still on this list when the market is shut never
    got its stop back all session, which is not the ratified overnight lapse
    (that one is a stop that was placed and expired at 16:00 by design) and
    must not be filed as one.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return
    day = repair_failure_alert_day(now)
    own_state = state is None
    st = load_state(path) if own_state else state
    current = _awaiting_print_state(st, day)
    if sym in current:
        return
    _write_awaiting_print_state(st, day, current | {sym})
    if own_state:
        save_state(st, path)


def clear_awaiting_first_print(
    symbols: Iterable[str], *, now: datetime | None = None,
    path: Path | None = None, state: dict[str, Any] | None = None,
) -> None:
    """Forget the awaiting-print state for names whose gap has closed."""
    day = repair_failure_alert_day(now)
    own_state = state is None
    st = load_state(path) if own_state else state
    current = _awaiting_print_state(st, day)
    wanted = {str(raw).strip().upper() for raw in symbols if str(raw).strip()}
    if not (current & wanted):
        return
    _write_awaiting_print_state(st, day, current - wanted)
    if own_state:
        save_state(st, path)


def session_awaiting_print_symbols(
    *, now: datetime | None = None, path: Path | None = None,
    state: dict[str, Any] | None = None,
) -> set[str]:
    """The names that waited on a first print at some point today and have
    not been repaired since. Read by the market-shut pass, which is the one
    entitled to say the session's repair path is exhausted."""
    day = repair_failure_alert_day(now)
    st = load_state(path) if state is None else state
    return _awaiting_print_state(st, day)


# ---------------------------------------------------------------------------
# the elected-but-unfilled marker — its own identity, the same machinery
# ---------------------------------------------------------------------------
# A protective stop that FIRED and did not FILL is a different condition
# from a protective stop the desk could not PLACE, and it must not share
# the placement-failure key: one key would let either condition silence the
# other on the same name, and "do not double-alert" does not mean "alert
# about only one of two real faults". Same state file, same trading-day
# key, same claim-before-send discipline, separate identity.


def _elected_unfilled_alerted_symbols(state: dict[str, Any], day: str) -> set[str]:
    raw = state.get("elected_unfilled_alerted_symbols")
    if not isinstance(raw, dict) or raw.get("day") != day:
        return set()
    return {
        str(sym).strip().upper()
        for sym in (raw.get("symbols") or [])
        if str(sym).strip()
    }


def claim_elected_unfilled_alert(
    symbols: Iterable[str], *, now: datetime | None = None,
    path: Path | None = None,
) -> list[str]:
    """Reserve today's elected-but-unfilled alert for `symbols` and return
    the ones NOT already alerted today, in the order given.

    Same contract as `claim_repair_failure_alert`. The caller MUST call
    `release_elected_unfilled_alert` if the send then fails.
    """
    day = repair_failure_alert_day(now)
    state = load_state(path)
    already = _elected_unfilled_alerted_symbols(state, day)
    fresh = [
        sym for sym in dict.fromkeys(
            str(raw).strip().upper() for raw in symbols if str(raw).strip()
        )
        if sym not in already
    ]
    if not fresh:
        return []
    merged = already | set(fresh)
    state["elected_unfilled_alerted_symbols"] = {
        "day": day, "symbols": sorted(merged),
    }
    save_state(state, path)
    return fresh


# ---------------------------------------------------------------------------
# the kill-switch-block marker — its own identity, the same machinery
# ---------------------------------------------------------------------------
# The desk's own kill switch refusing a protective stop is a THIRD distinct
# condition from a placement failure and from an elected-but-unfilled stop:
# the broker was never even asked. It must not share either key, for the
# same reason those two don't share one — silencing one condition must
# never silence a different one on the same name. Same state file, same
# trading-day key, same claim-before-send discipline, separate identity.


def _kill_switch_block_alerted_symbols(state: dict[str, Any], day: str) -> set[str]:
    raw = state.get("kill_switch_block_alerted_symbols")
    if not isinstance(raw, dict) or raw.get("day") != day:
        return set()
    return {
        str(sym).strip().upper()
        for sym in (raw.get("symbols") or [])
        if str(sym).strip()
    }


def claim_kill_switch_block_alert(
    symbols: Iterable[str], *, now: datetime | None = None,
    path: Path | None = None,
) -> list[str]:
    """Reserve today's kill-switch-block alert for `symbols` and return the
    ones NOT already alerted today, in the order given.

    Same contract as `claim_repair_failure_alert`: a kill switch left on
    all day would otherwise page the owner on every retry of every symbol
    it touches; an unwritable state file errs towards telling the owner
    twice rather than not at all, which is the right way round for a
    naked position.
    """
    day = repair_failure_alert_day(now)
    state = load_state(path)
    already = _kill_switch_block_alerted_symbols(state, day)
    fresh = [
        sym for sym in dict.fromkeys(
            str(raw).strip().upper() for raw in symbols if str(raw).strip()
        )
        if sym not in already
    ]
    if not fresh:
        return []
    merged = already | set(fresh)
    state["kill_switch_block_alerted_symbols"] = {
        "day": day, "symbols": sorted(merged),
    }
    save_state(state, path)
    return fresh


# ---------------------------------------------------------------------------
# the unreadable-stop marker — its own identity again, board item 172
# ---------------------------------------------------------------------------
# "I could not ask the broker whether this position has a stop" is a third
# condition, distinct from a placement failure and from an elected-unfilled
# stop, and it gets a third key for the reason the second one got a second:
# one key lets either condition silence the other on the same name, and the
# owner would be told about one real fault while another went unreported.


def _unreadable_alerted_symbols(state: dict[str, Any], day: str) -> set[str]:
    raw = state.get("unreadable_stop_alerted_symbols")
    if not isinstance(raw, dict) or raw.get("day") != day:
        return set()
    return {
        str(sym).strip().upper()
        for sym in (raw.get("symbols") or [])
        if str(sym).strip()
    }


def claim_unreadable_stop_alert(
    symbols: Iterable[str], *, now: datetime | None = None,
    path: Path | None = None,
) -> list[str]:
    """Reserve today's unreadable-stop alert for `symbols` and return the
    ones NOT already alerted today, in the order given. Board item 172.

    Same contract as `claim_repair_failure_alert`, including that an
    unwritable state file errs towards telling the owner twice rather than
    not at all — which is the right way round when what is unknown is
    whether a position has any loss protection at all.

    Per SYMBOL, not per run: a broker that cannot describe AAPL's stops at
    09:35 and cannot describe MSFT's at 14:05 is two findings, and the
    second must not be swallowed by the first. The coverage sweep runs on a
    30-minute cadence, so without this the same name would page the owner
    roughly a dozen times a session.
    """
    day = repair_failure_alert_day(now)
    state = load_state(path)
    already = _unreadable_alerted_symbols(state, day)
    fresh = [
        sym for sym in dict.fromkeys(
            str(raw).strip().upper() for raw in symbols if str(raw).strip()
        )
        if sym not in already
    ]
    if not fresh:
        return []
    merged = already | set(fresh)
    state["unreadable_stop_alerted_symbols"] = {
        "day": day, "symbols": sorted(merged),
    }
    save_state(state, path)
    return fresh


# ---------------------------------------------------------------------------
# the declined-exit marker — a FOURTH key, for the same reason as the third
# ---------------------------------------------------------------------------
# "the desk decided to leave this position and could not" is its own
# condition. Sharing the unreadable key would let a stop it could not read
# silence the exit it then refused on the strength of that same read —
# exactly the swallowing this family of keys exists to prevent, and the two
# happen together by construction.

def release_typed_alert(
    kind: str, symbols: Iterable[str], *, now: datetime | None = None,
    path: Path | None = None,
) -> None:
    """Give back today's `kind` claim for `symbols`.

    `claim_typed_alert` reserves the symbol BEFORE the message is handed to
    the notifier, which is the right order -- two processes finding the same
    condition at the same moment must not both send. But the reservation is
    saved whether or not the send lands, so a muted or failed delivery used
    to burn the symbol's one page for the whole trading day and the owner
    was never told at all. `send_owner_alert` reports whether it landed;
    when it did not, the caller hands the claim back here so the next
    attempt -- the 30-minute watchdog, the next session entry -- can try
    again. Never raises: an alerting bug must not break the path it reports
    on. Releasing a claim that is not held is a no-op.
    """
    key = str(kind).strip() or "unspecified"
    try:
        day = repair_failure_alert_day(now)
        state = load_state(path)
        already = _typed_alerted_symbols(state, day, key)
        giving_back = {
            str(raw).strip().upper() for raw in symbols if str(raw).strip()
        }
        remaining = already - giving_back
        if remaining == already:
            return
        state[f"typed_alerted_symbols::{key}"] = {
            "day": day, "symbols": sorted(remaining),
        }
        save_state(state, path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "release_typed_alert(%s) failed: %s — the claim stays held and "
            "today's page for those symbols will not be retried", key, exc,
        )


def _exit_declined_alerted_symbols(state: dict[str, Any], day: str) -> set[str]:
    raw = state.get("exit_declined_alerted_symbols")
    if not isinstance(raw, dict) or raw.get("day") != day:
        return set()
    return {
        str(sym).strip().upper()
        for sym in (raw.get("symbols") or [])
        if str(sym).strip()
    }


def claim_exit_declined_alert(
    symbols: Iterable[str], *, now: datetime | None = None,
    path: Path | None = None,
) -> list[str]:
    """Reserve today's declined-exit alert for `symbols` and return the ones
    NOT already alerted today, in the order given.

    Same contract as `claim_unreadable_stop_alert`, including erring towards
    telling the owner twice over not at all: a position the desk decided to
    leave and could not is not a state to under-report.
    """
    day = repair_failure_alert_day(now)
    state = load_state(path)
    already = _exit_declined_alerted_symbols(state, day)
    fresh = [
        sym for sym in dict.fromkeys(
            str(raw).strip().upper() for raw in symbols if str(raw).strip()
        )
        if sym not in already
    ]
    if not fresh:
        return []
    state["exit_declined_alerted_symbols"] = {
        "day": day, "symbols": sorted(already | set(fresh)),
    }
    save_state(state, path)
    return fresh


def _scale_in_row_age_seconds(created_at: Any, moment: datetime) -> float | None:
    """Approximate seconds since a scale-in write-ahead row was written.

    The write-ahead row's `created_at` is a DATABASE WRITE time, not the
    broker's cancel acknowledgement, so this is an approximation of how
    long protection has been down and is labelled as one everywhere it is
    used. The exact figure is the `unprotected_window_closed` event the
    session files at rearm. `None` when the stamp cannot be read — an
    unreadable stamp must never be treated as a long window.
    """
    text = str(created_at or "")
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return round(max(0.0, (moment - stamp).total_seconds()), 1)


def _scale_in_skip(
    broker: Any, db_path: str | Path | None, *, now: datetime | None = None,
) -> set[str]:
    """Symbols mid scale-in that this watchdog must not report or repair.

    A live cancel-confirm-buy window looks uncovered on purpose. Adding a
    stop here would re-create the wash-trade block the sequence just
    cleared. Crash recovery belongs to the session drain; this only
    stays out of the way while a session lock is held or a DAY add is
    still working.

    Board item 193. The session-lock arm of that skip used to be
    UNBOUNDED: while the wrapper's lock directory existed, every symbol
    holding a scale-in write-ahead row was skipped for as long as the row
    survived. A session that cancelled the protective stop and then hung
    or died WITHOUT releasing the lock therefore left the WHOLE held
    position naked, with the one watchdog that could re-protect it
    deliberately looking away, indefinitely. The window is a property of
    the broker's order model and cannot be removed — a resting protective
    SELL and a working BUY collide on the same symbol, and the quantity
    amend that would otherwise resize the resting stop in place is refused
    by this broker on a fractional order (42210000), which scale-in adds
    routinely are — but the SKIP does not have to be unbounded.

    So the lock-held skip is now bounded by the desk's OWN MEASUREMENT: the
    longest unprotected window it has ever closed and recorded
    (`measured_window_bound_seconds`). Nothing here is a chosen number.
      * Within that bound, or with no measured history at all, behaviour is
        exactly as before — skip, because this is a normal live window.
      * Past that bound, the symbol is no longer taken on the lock's word.
        It falls back to the same collision test the crash path already
        uses: a WORKING entry order still rests, so a stop would collide and
        the skip stands; nothing rests, so the collision that justified the
        skip is gone and the sweep is allowed to re-protect the position.

    The residual risk is deliberate and is the conservative side of the
    trade: if a live session is merely slower than every window ever
    measured and has not yet submitted its add, the sweep may place a stop
    that then blocks the add, and the add's own failure path restores from
    the write-ahead row. An add refused with the position protected is a
    better outcome than a position left naked with no watchdog.
    """
    try:
        from src.execution.scale_in import (
            list_open_entry_ids, pending_scale_in_symbols_from_path,
            trading_session_lock_held,
        )
        path = db_path if db_path is not None else DB_PATH
        symbols = pending_scale_in_symbols_from_path(path)
    except Exception:  # noqa: BLE001
        return set()
    if not symbols:
        return set()
    if trading_session_lock_held():
        stale = _scale_in_symbols_past_measured_bound(path, symbols, now=now)
        if not stale:
            return set(symbols)
        skip = set(symbols) - stale
        for symbol in sorted(stale):
            if list_open_entry_ids(broker, symbol):
                # The collision is real: a working entry order still rests,
                # so a protective stop here would be blocked anyway.
                skip.add(symbol)
                logger.warning(
                    "scale-in %s has been unprotected for longer than the "
                    "longest window this desk has ever measured, but a "
                    "working entry order still rests on it — the sweep still "
                    "cannot place a stop without colliding with it",
                    symbol,
                )
            else:
                logger.error(
                    "scale-in %s has been unprotected for longer than the "
                    "longest window this desk has ever measured and NO entry "
                    "order is working on it — the session lock is no longer "
                    "reason enough to look away, handing it to the coverage "
                    "sweep to re-protect",
                    symbol,
                )
        return skip
    skip: set[str] = set()
    for symbol in symbols:
        if list_open_entry_ids(broker, symbol):
            skip.add(symbol)
    return skip


def _scale_in_symbols_past_measured_bound(
    db_path: str | Path | None, symbols: set[str], *,
    now: datetime | None = None,
) -> set[str]:
    """Of `symbols`, those whose window is older than every measured one.

    Empty — meaning "treat them all as normal live windows" — whenever the
    desk has no measured history to compare against, whenever the rows
    cannot be read, and for any row whose write time cannot be parsed. Each
    of those is a case where calling a window overdue would be an invented
    figure rather than a measured one.
    """
    bound, observations = measured_window_bound_seconds(db_path)
    if bound is None or observations <= 0:
        return set()
    try:
        from src.execution.scale_in import pending_scale_in_rows_from_path
        rows = pending_scale_in_rows_from_path(db_path)
    except Exception:  # noqa: BLE001
        return set()
    moment = now or _utc_now()
    stale: set[str] = set()
    for row in rows or []:
        symbol = str(row.get("symbol") or "").strip()
        if not symbol or symbol not in symbols:
            continue
        age = _scale_in_row_age_seconds(row.get("created_at"), moment)
        if age is not None and age > bound:
            stale.add(symbol)
    return stale


def measured_window_bound_seconds(
    db_path: str | Path | None,
) -> tuple[float | None, int]:
    """The longest scale-in unprotected window the desk has MEASURED, and how
    many measurements that is drawn from.

    Board item 193. Nothing here is a chosen threshold. Every closed window
    files an `unprotected_window_closed` event carrying its own
    `window_seconds`, both ends read from the broker's acknowledgements; the
    bound is the maximum of those. `(None, 0)` when no window has ever been
    measured, and the caller must then decline to call anything overdue
    rather than invent a figure to compare against.
    """
    if not db_path:
        return None, 0
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT evidence_json FROM specialist_evidence "
                "WHERE kind = 'pipeline_event' "
                "AND evidence_json LIKE '%unprotected_window_closed%' "
                "ORDER BY id DESC LIMIT 2000",
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None, 0
    best: float | None = None
    seen = 0
    for (raw,) in rows:
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        if payload.get("outcome") != "unprotected_window_closed":
            continue
        value = payload.get("window_seconds")
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        seen += 1
        best = value if best is None else max(best, value)
    return best, seen


def deliberately_unguarded(
    broker: Any, db_path: str | Path | None, *,
    skip_symbols: set[str] | None = None,
    now: datetime | None = None,
) -> list[UnguardedWindow]:
    """Every symbol the sweep skipped for a live scale-in, named, with how
    long its protection has been deliberately down.

    Board item 193's live-risk half. Reports only symbols that were ACTUALLY
    skipped this run (`skip_symbols`, the same set handed to
    `uncovered_positions`), so the report can never describe a position the
    sweep in fact checked. Places no order and changes no state.
    """
    skip = skip_symbols if skip_symbols is not None else _scale_in_skip(broker, db_path)
    if not skip:
        return []
    try:
        from src.execution.scale_in import pending_scale_in_rows_from_path
        rows = pending_scale_in_rows_from_path(
            db_path if db_path is not None else DB_PATH,
        )
    except Exception:  # noqa: BLE001
        return []
    bound, observations = measured_window_bound_seconds(
        db_path if db_path is not None else DB_PATH,
    )
    moment = now or _utc_now()
    out: list[UnguardedWindow] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        if not symbol or symbol not in skip:
            continue
        created = str(row.get("created_at") or "")
        seconds: float | None = None
        try:
            stamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            seconds = round(
                max(0.0, (moment - stamp).total_seconds()), 1,
            )
        except Exception:  # noqa: BLE001
            seconds = None
        try:
            qty = float(row.get("position_qty_before_sell") or 0.0)
        except (TypeError, ValueError):
            qty = 0.0
        out.append(UnguardedWindow(
            symbol=symbol, held_qty=abs(qty), is_short=qty < 0,
            since_utc=created, seconds_open=seconds,
            bound_seconds=bound, bound_observations=observations,
        ))
    return out


def unguarded_text(rows: list[UnguardedWindow]) -> str:
    """The owner message for a deliberate unguarded window that has outlived
    every window the desk has ever measured.

    Two marks, not three: the desk put this position in this state on
    purpose and a live session is mid-sequence on it. What is wrong is the
    DURATION — a window still open past the longest one ever measured is a
    session that probably died holding the stop down, and nobody is going to
    put it back without being told.
    """
    detail = "\n".join(
        f"  {r.symbol}: {r.held_qty:.4f}{' (short)' if r.is_short else ''} "
        f"held with protection deliberately down for about "
        f"{r.seconds_open:.0f}s (since {r.since_utc} UTC), against a longest "
        f"measured window of {r.bound_seconds:.1f}s over "
        f"{r.bound_observations} measurement(s)"
        for r in rows
    )
    return (
        "🛑🛑 POSITION UNGUARDED LONGER THAN EVER MEASURED\n"
        f"{len(rows)} position(s) have their protective stop cancelled ON "
        "PURPOSE for a scale-in that has not finished. The cancel is "
        "correct — the resting stop holds the shares and blocks the add — "
        "and the coverage sweep leaves these alone by design so it cannot "
        "re-create that block. What is not correct is how long it has "
        "lasted.\n"
        f"{detail}\n"
        "The duration is taken from the write-ahead row's write time, so "
        "treat it as approximate; the exact figure is the window event the "
        "session files when it rearms. Nothing has been sold, resized or "
        "placed. Check the position's open orders at the broker and place a "
        "stop by hand if the adding session is gone. At most once per "
        "symbol per trading day."
    )


def _unguarded_alerted_symbols(state: dict[str, Any], day: str) -> set[str]:
    raw = state.get("unguarded_alerted_symbols") or {}
    if not isinstance(raw, dict) or raw.get("day") != day:
        return set()
    return {
        str(sym).strip().upper()
        for sym in (raw.get("symbols") or [])
        if str(sym).strip()
    }


# ---------------------------------------------------------------------------
# the broker-write lock shared with intra_check (board item 127)
# ---------------------------------------------------------------------------

#: The advisory flock file `TradingPipeline._intraday_scan_process_lock`
#: takes beside the database. Same file, so this repair pass and
#: `intra_check`'s broker-writing preamble exclude each other. Not a number
#: and not new: the name is the one the pipeline has used since 2026-08-19.
REPAIR_LOCK_NAME = ".intraday_scan.lock"


@contextlib.contextmanager
def repair_lock(db_path: str | Path | None = None):
    """Non-blocking `fcntl.flock(LOCK_EX | LOCK_NB)` on `REPAIR_LOCK_NAME`
    beside the database. Yields True when held, False when another process
    holds it or the lock cannot be established (fail closed: a repair that
    cannot prove it is alone does not run; the next tick retries). Released
    by the kernel on process death, so a killed sweep cannot wedge it."""
    import fcntl

    lock_path = Path(db_path if db_path is not None else DB_PATH).parent / REPAIR_LOCK_NAME
    fh = None
    held = False
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = True
        except BlockingIOError:
            held = False
    except Exception as exc:  # noqa: BLE001 — unknowable lock state: do not write
        logger.warning("coverage sweep: could not establish the repair lock (%s)", exc)
    try:
        yield held
    finally:
        if fh is not None:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------

def check_coverage(
    broker: Any,
    *,
    now: datetime | None = None,
    sweep_symbol: str | None = None,
    db_path: str | Path | None = None,
    state_path: Path | None = None,
    last_buy: Any = None,
    db: Any = None,
) -> CoverageStatus:
    """Read broker coverage and session evidence, put back what is missing if
    the market is open, decide, persist the once-per-day markers. Never
    raises.

    `last_buy` is the callable that answers "what stop level did this
    position's own opening row record?" — BUY for a long, SHORT for a
    short. Pass it and placement is possible; leave it None and this stays
    the pure reader it was, which is what a caller with no database handle
    must do rather than repair against a guessed level.
    """
    moment = now or _utc_now()
    state = load_state(state_path)

    counts: dict[str, int] = {}
    unreadable: list[UnreadableStop] = []
    # Board item 193. Read ONCE and reused everywhere below: the set the
    # sweep skipped has to be the same set the report names, or the report
    # could describe a position the sweep actually checked (or miss one it
    # did not), which is exactly the untruth this item exists to remove.
    scale_in_skip = _scale_in_skip(broker, db_path)
    gaps, broker_error = uncovered_positions(
        broker, sweep_symbol=sweep_symbol, skip_symbols=scale_in_skip,
        counts=counts, unreadable=unreadable,
    )
    unguarded = deliberately_unguarded(
        broker, db_path, skip_symbols=scale_in_skip, now=moment,
    )
    positions_checked = (
        None if broker_error else int(counts.get("positions_checked", 0))
    )
    day = most_recent_trading_day(moment, broker)
    ran, db_error = session_ran_during(day, db_path)

    # ---- the repair pass ----------------------------------------------
    # Only inside a session the exchange calendar confirms, only when a
    # level can be read, only ever ADDING an order. Then re-read the broker
    # so `gaps` is the RESIDUAL exposure rather than the pre-repair one: an
    # alert must describe the book as it stands after this run, not before.
    repairs: list[RepairOutcome] = []
    market_open, market_reason = session_is_open(broker, moment)
    # This function is the STANDALONE path (the every-30-minutes
    # coverage-sweep unit, or the 06:15 heartbeat) — never the repair a live
    # session runs on itself. `run_if_et_window.sh`'s cross-mode session
    # lock (morning/midday/close/evening/earnings_preprocess) already
    # serializes those sessions against EACH OTHER, but this unit is a
    # separate systemd timer with its own process and was never a party to
    # that lock, so it could place a stop at the exact instant a live
    # session was cancelling one to sell (docs/INCIDENT_HISTORY.md,
    # 2026-09-17: both fired on the same shared `*:0/30` tick and ran their
    # coverage reconcile ~90ms apart). Deferring here loses nothing: a
    # session that holds the lock runs this SAME repair
    # (`TradingPipeline._reconcile_stop_coverage`) itself, near the very
    # start of its own run, so the gap this tick skips is one this tick did
    # not need to fill. `trading_session_lock_held()` never sees
    # `intra_check` — that mode is deliberately exempt from the lock (the
    # flash-crash breaker). The 2026-09-17 schedule split moved intra_check
    # off this unit's tick; board item 127 (2026-09-19) now also closes that
    # pairing with a lock rather than by timing alone: the repair pass
    # below runs only while this process holds the SAME advisory flock
    # `intra_check` now holds around its broker-writing preamble and its
    # paid scan (`TradingPipeline._intraday_scan_process_lock`,
    # `REPAIR_LOCK_NAME` beside the database). Contended means another desk
    # process is writing to the broker right now; this tick defers exactly as
    # it defers to a session, and the next tick re-reads the broker.
    from src.execution.scale_in import trading_session_lock_held
    session_active = trading_session_lock_held()
    # Counted HERE, before the repair block below can rebind `gaps` to the
    # post-repair re-read. See `CoverageStatus.gaps_detected`.
    gaps_detected = len(gaps)
    repair_deferred = ""
    if gaps and market_open and last_buy is not None and not session_active:
        with repair_lock(db_path) as held:
            if not held:
                repair_deferred = (
                    "another desk process holds the broker-write lock "
                    f"({REPAIR_LOCK_NAME}); this tick placed nothing and the "
                    "next one re-reads the broker"
                )
                market_reason = f"{market_reason}, but {repair_deferred}"
            else:
                repairs = replace_missing_stops(
                    broker, gaps, last_buy=last_buy, sweep_symbol=sweep_symbol,
                    db=db,
                )
                if any(r.placed for r in repairs):
                    refreshed, refresh_error = uncovered_positions(
                        broker, sweep_symbol=sweep_symbol,
                        skip_symbols=scale_in_skip,
                        unreadable=unreadable,
                    )
                    if refresh_error is None:
                        gaps = refreshed
                    else:
                        broker_error = broker_error or refresh_error
                    # The re-read can surface a symbol the first read could
                    # describe, and can repeat one it could not. Dedupe on
                    # symbol, keeping the FIRST reason seen, so the owner
                    # message names each position once.
                    seen: set[str] = set()
                    deduped: list[UnreadableStop] = []
                    for row in unreadable:
                        if row.symbol in seen:
                            continue
                        seen.add(row.symbol)
                        deduped.append(row)
                    unreadable[:] = deduped
    elif gaps and market_open and last_buy is not None and session_active:
        repair_deferred = (
            "a trading session currently holds the lock, so this tick defers "
            "the repair to that session's own coverage reconcile rather than "
            "risk placing a stop the session is in the middle of cancelling"
        )
        market_reason = f"{market_reason}, but {repair_deferred}"
    elif gaps and market_open and last_buy is None:
        market_reason = (
            f"{market_reason}, but no recorded-stop lookup was supplied, so "
            "nothing was placed"
        )

    if last_buy is not None:
        try:
            from src.execution.stop_records import (
                reconcile_recorded_stop_levels,
            )
            try:
                positions = broker.get_positions()
            except Exception:
                positions = []
            if not isinstance(positions, list):
                positions = []
            mismatches = reconcile_recorded_stop_levels(
                broker=broker, last_buy=last_buy, positions=positions,
                sweep_symbol=sweep_symbol,
                skip_symbols=scale_in_skip, db=db,
            )
            # Log every pass; do not page from this 30-minute unit. An
            # out-of-band mismatch is never write-back-cleared, so paging
            # here would fire ~48 times a day with no acknowledgement.
            # The session coverage sweep pages.
            for item in mismatches:
                logger.error(
                    "STOP RECORD MISMATCH: %s — %s (short=%s)",
                    item.symbol, item.reason, item.is_short,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("coverage watchdog stop-level reconcile failed: %s", exc)

    # Suppression for a failed placement is per position and keyed on
    # today's ET date, shared with the live session path (see
    # `claim_repair_failure_alert`).
    failure_day = repair_failure_alert_day(moment)
    already_failed = _repair_failure_alerted_symbols(state, failure_day)
    # A refusal that is the TAPE's, not the desk's, is not this unit's to
    # page either. Same classifier as the live session's coverage reconcile
    # (`awaiting_first_print`) and the same day-keyed marker, because the two
    # are separate processes that can each find the identical refusal on the
    # identical position — and it was the FIRST attempt paging, not the
    # condition, that sent the owner to place a stop by hand that the desk
    # placed itself minutes later. The repair itself is unchanged and was
    # already attempted above; only the page waits.
    waiting_now = [
        r for r in repairs
        if str(r.symbol).strip() and not r.placed and awaiting_first_print(
            refusal_code=r.refusal_code,
            still_covered=r.still_covered,
            market_open=market_open,
        )
    ]
    for outcome in waiting_now:
        note_awaiting_first_print(outcome.symbol, now=moment, state=state)
        logger.warning(
            "coverage sweep: %s is awaiting its first trade print of the "
            "session (%s) — the whole-share leg still covers it, the repair "
            "will be attempted again, and the pass that finds the market "
            "shut with this still true is the one that pages.",
            outcome.symbol, outcome.detail or "no reason given",
        )
    waiting_symbols = {
        str(r.symbol).strip().upper() for r in waiting_now
    }
    placed_symbols = [
        str(r.symbol).strip().upper() for r in repairs
        if r.placed and str(r.symbol).strip()
    ]
    resolution_symbols: tuple[str, ...] = ()
    if placed_symbols:
        clear_awaiting_first_print(placed_symbols, now=moment, state=state)
        # The retraction half. Claimed here, beside every other marker, so
        # the caller sends exactly what was reserved.
        resolution_symbols = tuple(claim_repair_resolution_notice(
            placed_symbols, now=moment, state=state,
        ))
    failing_symbols = [
        str(r.symbol).strip().upper() for r in repairs
        if not r.placed and str(r.symbol).strip()
        and str(r.symbol).strip().upper() not in waiting_symbols
    ]

    # Board item 172. Claimed BEFORE the status is built, exactly as the
    # placement-failure marker is: the claim is what makes the report
    # once-per-symbol-per-day across this unit and the live session, which
    # are separate processes that can each find the same unreadable stop.
    already_unreadable = _unreadable_alerted_symbols(state, failure_day)
    # Upper-cased to match what `_unreadable_alerted_symbols` reads back and
    # what `claim_unreadable_stop_alert` writes. A raw symbol here would
    # never match the stored set, so a mixed-case name from the broker would
    # page on every 30-minute tick — the dedup silently not applying.
    unreadable_symbols = [
        str(r.symbol).strip().upper() for r in unreadable
        if str(r.symbol).strip()
    ]

    # Board item 193. Same day-keyed, per-symbol claim the unreadable page
    # uses, and claimed HERE rather than by the caller for the same reason:
    # the marker is written inside this function, so a caller that re-claimed
    # would find this run's own marker and silence the message it wrote.
    already_unguarded = _unguarded_alerted_symbols(state, failure_day)
    # Item 211 defect 2: per symbol per day, like both siblings.
    exposure_symbols = {
        str(g.symbol).strip().upper() for g in gaps if str(g.symbol).strip()
    }
    already_exposed = _exposure_alerted_symbols(state, day.isoformat())
    unguarded_fresh = [
        r for r in unguarded
        if r.over_bound and str(r.symbol).strip().upper() not in already_unguarded
    ]

    status = CoverageStatus(
        trading_day=day.isoformat(),
        session_ran=ran,
        gaps=gaps,
        gaps_detected=gaps_detected,
        broker_error=broker_error,
        db_error=db_error,
        already_alerted_for_day=bool(exposure_symbols) and all(
            sym in already_exposed for sym in exposure_symbols
        ),
        repairs=repairs,
        market_open=market_open,
        market_reason=market_reason,
        already_alerted_repair_failure_for_day=bool(failing_symbols) and all(
            sym in already_failed for sym in failing_symbols
        ),
        positions_checked=positions_checked,
        repair_deferred=repair_deferred,
        unreadable=list(unreadable),
        already_alerted_unreadable_for_day=bool(unreadable_symbols) and all(
            sym in already_unreadable for sym in unreadable_symbols
        ),
        # Board item 172. What THIS run is entitled to say out loud: the
        # rows whose symbol has not already been reported today. Built from
        # the same upper-cased comparison the claim itself uses, so a
        # mixed-case symbol from the broker cannot slip past the filter and
        # re-page under a different spelling.
        unreadable_fresh=[
            r for r in unreadable
            if str(r.symbol).strip()
            and str(r.symbol).strip().upper() not in already_unreadable
        ],
        resolution_notice_symbols=resolution_symbols,
        unguarded=list(unguarded),
        unguarded_fresh=unguarded_fresh,
    )
    if status.should_alert:
        state["alerted_for_day"] = day.isoformat()
        state["exposure_alerted_symbols"] = {
            "day": day.isoformat(),
            "symbols": sorted(already_exposed | exposure_symbols),
        }
    if status.should_alert_repair_failure:
        _record_repair_failure_alert(state, failure_day, failing_symbols)
    if status.should_alert_unguarded:
        state["unguarded_alerted_symbols"] = {
            "day": failure_day,
            "symbols": sorted(
                already_unguarded
                | {str(r.symbol).strip().upper() for r in unguarded_fresh}
            ),
        }
    if status.should_alert_unreadable:
        state["unreadable_stop_alerted_symbols"] = {
            "day": failure_day,
            "symbols": sorted(already_unreadable | set(unreadable_symbols)),
        }
    state["last_result"] = {
        "trading_day": status.trading_day,
        "session_ran": status.session_ran,
        "gaps": [g.__dict__ for g in gaps],
        "broker_error": broker_error,
        "db_error": db_error,
        "market_open": market_open,
        "market_reason": market_reason,
        "repairs": [r.__dict__ for r in repairs],
        "unreadable": [r.__dict__ for r in unreadable],
        "unguarded": [r.__dict__ for r in unguarded],
    }
    state["updated_at"] = moment.replace(microsecond=0).isoformat()
    save_state(state, state_path)
    return status


# ---------------------------------------------------------------------------
# the durable record of every run (board item 131)
# ---------------------------------------------------------------------------
#
# Until 2026-09-19 a sweep run left one `print` in the systemd journal and a
# state file that each run overwrote. Nothing reached `quant_agent.log`, and
# nothing reached the database, so "has it ever run?" could be answered only
# by someone who knew to read the journal of that one unit. Every run now
# leaves BOTH a `COVERAGE SWEEP` line in the desk's log and one row in the
# desk's existing lifecycle-event stream (`specialist_evidence`,
# `kind='pipeline_event'`, `scope='run'` — the shape
# `_record_pipeline_event` and the de-lever shortfall record use). Observability
# only: nothing reads either to decide anything.


def record_sweep_run(db: Any, summary: dict[str, Any]) -> bool:
    """One `specialist_evidence` row for this run. Never raises; False when
    there is no database handle or the write failed (logged)."""
    if db is None:
        return False
    try:
        db.insert_specialist_evidence(
            run_id=str(summary.get("run_id") or ""), agent_name=SWEEP_AGENT_NAME,
            kind="pipeline_event", scope="run", symbol=None,
            evidence_json=json.dumps(summary, sort_keys=True, default=str),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — a record is never trading authority
        logger.warning("%s: could not write the run record: %s", SWEEP_LOG_NAME, exc)
        return False


# --- Generic per-symbol, per-trading-day alert claim -------------------------
# Same state file, same trading-day key and the same claim-before-send
# discipline as `claim_repair_failure_alert`, but keyed by an arbitrary
# `kind` so a new fail-closed page does not need its own pair of helpers.
# Callers that page the owner about a per-symbol condition use this; the
# older named helpers keep their own keys so their history is unaffected.

def _typed_alerted_symbols(
    state: dict[str, Any], day: str, kind: str,
) -> set[str]:
    raw = state.get(f"typed_alerted_symbols::{kind}")
    if not isinstance(raw, dict) or raw.get("day") != day:
        return set()
    return {
        str(sym).strip().upper()
        for sym in (raw.get("symbols") or [])
        if str(sym).strip()
    }


#: How many suppression records to retain per alert type. Bounds the state
#: file; the running `count` is never truncated, only the per-event list.
_SUPPRESSION_LOG_LIMIT = 50


def _record_suppressed_alert(
    state: dict[str, Any], kind: str, day: str, keys: Iterable[str],
) -> None:
    """Durably note an alert this helper declined to resend (item 211).

    Nothing is silently dropped: the owner not being paged a second time is
    a presentation decision, and the underlying fact still has to be
    readable afterwards or the desk has stopped reporting its true state.
    """
    log = state.get("suppressed_alerts")
    if not isinstance(log, dict):
        log = {}
    entry = log.get(kind)
    if not isinstance(entry, dict) or entry.get("day") != day:
        entry = {"day": day, "count": 0, "events": []}
    events = entry.get("events")
    if not isinstance(events, list):
        events = []
    for key in keys:
        entry["count"] = int(entry.get("count") or 0) + 1
        events.append({"key": key, "day": day})
    entry["events"] = events[-_SUPPRESSION_LOG_LIMIT:]
    log[kind] = entry
    state["suppressed_alerts"] = log


def claim_typed_alert(
    kind: str, symbols: Iterable[str], *, now: datetime | None = None,
    path: Path | None = None,
) -> list[str]:
    """Reserve today's `kind` alert for `symbols`; return those NOT yet
    alerted today, in the order given.

    Same contract as `claim_repair_failure_alert`, including that a state
    file that cannot be read errs towards telling the owner twice over not
    at all.
    """
    key = str(kind).strip() or "unspecified"
    day = repair_failure_alert_day(now)
    state = load_state(path)
    already = _typed_alerted_symbols(state, day, key)
    fresh = [
        sym for sym in dict.fromkeys(
            str(raw).strip().upper() for raw in symbols if str(raw).strip()
        )
        if sym not in already
    ]
    stale = [sym for sym in dict.fromkeys(
        str(raw).strip().upper() for raw in symbols if str(raw).strip()
    ) if sym in already]
    if stale:
        _record_suppressed_alert(state, key, day, stale)
    if not fresh:
        if stale:
            save_state(state, path)
        return []
    state[f"typed_alerted_symbols::{key}"] = {
        "day": day, "symbols": sorted(already | set(fresh)),
    }
    save_state(state, path)
    return fresh
