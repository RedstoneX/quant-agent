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

logger = logging.getLogger(__name__)

#: Same database every session and `src/alert_watchdog.py` write to.
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "quant_agent.db"

#: On-box record, gitignored like its siblings under data/alerting/.
STATE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "alerting" / "coverage_heartbeat.json"
)

TABLE = "alert_channel_checks"

#: How many weekdays back to look for the most recent trading day. A long
#: weekend plus a holiday is three; five is comfortably past that and bounds
#: the calendar lookups when the broker's calendar cannot be read at all.
MAX_WEEKDAYS_BACK = 5

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
class RepairOutcome:
    """One attempt to put a missing protective stop back. `placed` False with
    a `detail` is a FAILURE that must be reported, never swallowed."""

    symbol: str
    qty: float
    placed: bool
    detail: str = ""


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

    @property
    def unprotected_total(self) -> float:
        return round(sum(g.unprotected_value for g in self.gaps), 2)

    @property
    def repaired(self) -> list[RepairOutcome]:
        return [r for r in self.repairs if r.placed]

    @property
    def repair_failures(self) -> list[RepairOutcome]:
        return [r for r in self.repairs if not r.placed]

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

def most_recent_trading_day(now: datetime, broker: Any = None) -> date:
    """The most recent weekday whose cash session has already ENDED (plus
    the timer slack) and that the broker's calendar confirms as a trading
    day.

    "Already ended" rather than "strictly before today": at the 06:15 ET
    run this is yesterday either way, but run by hand at 23:50 ET on a
    Friday it must judge Friday, not Thursday — a session that has not
    finished cannot yet have failed to re-place anything, and one that has
    finished can. Holidays are excluded through `broker.is_trading_day`
    when available. That helper answers False on a calendar-read failure,
    so a broker outage would walk PAST a real trading day and could judge
    a holiday-free week as "no session, because there was no day" — to
    keep the failure on the alerting side, the walk is bounded and falls
    back to the most recent plain weekday, which can only over-alert on a
    holiday, never suppress a real gap.
    """
    today_et = now.astimezone(ET).date()
    _start, today_end = _session_bounds_utc(today_et)
    candidate = today_et if now >= today_end else today_et - timedelta(days=1)
    first_weekday: date | None = None
    checked = 0
    while checked < MAX_WEEKDAYS_BACK:
        if candidate.weekday() < 5:
            if first_weekday is None:
                first_weekday = candidate
            checked += 1
            if broker is None:
                return candidate
            try:
                if broker.is_trading_day(candidate):
                    return candidate
            except Exception as exc:  # noqa: BLE001
                logger.warning("coverage watchdog: calendar lookup failed for %s: %s", candidate, exc)
                return candidate
        candidate -= timedelta(days=1)
    return first_weekday or (today_et - timedelta(days=1))


def _session_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """[09:30 ET, 16:00 ET + SLACK_MINUTES) for `day`, in UTC. Only a session
    completing inside the cash session can have re-placed a DAY stop; the
    evening run sees a shut market and, correctly, places nothing."""
    lo, hi = SESSION_WINDOWS["intra_check"]
    midnight = datetime(day.year, day.month, day.day, tzinfo=ET)
    start = midnight + timedelta(minutes=lo)
    end = midnight + timedelta(minutes=hi + SLACK_MINUTES)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


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
    """`(open_now, reason)` from the calendar the BROKER publishes.

    Both edges are read (`get_session_open` / `get_session_close`) rather
    than assumed: 09:30-16:00 is the usual session, not a guaranteed one, and
    a number typed here would be exactly the invented threshold this desk
    refuses. `is_trading_day` rules out weekends and holidays first.

    FAILS CLOSED. Any unreadable edge returns False: a protective stop is
    worth placing only when we know the market will accept it, and this
    module has no measurement of what Alpaca does with a fractional DAY stop
    submitted into a shut market. "We could not tell" is reported as a
    reason, never rounded up to "go ahead".
    """
    today = now.astimezone(ET).date()
    try:
        if not broker.is_trading_day(today):
            return False, f"{today} is not a trading day"
    except Exception as exc:  # noqa: BLE001
        return False, f"trading-calendar lookup failed ({exc})"
    try:
        opens = broker.get_session_open(today)
        closes = broker.get_session_close(today)
    except Exception as exc:  # noqa: BLE001
        return False, f"session-hours lookup failed ({exc})"
    if opens is None or closes is None:
        return False, "the broker's calendar did not give both session edges"
    try:
        if now < opens:
            return False, f"the session has not opened yet (opens {opens:%H:%M %Z})"
        if now >= closes:
            return False, f"the session has closed (closed {closes:%H:%M %Z})"
    except TypeError as exc:  # noqa: BLE001 - naive/aware mismatch
        return False, f"session-hours comparison failed ({exc})"
    return True, f"the session is open until {closes:%H:%M %Z}"


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
            "" if placed else (
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

    Same contract as `claim_repair_failure_alert`, including that an
    unwritable state file errs towards telling the owner twice rather than
    not at all.
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


def unreadable_stop_text(rows: Iterable[UnreadableStop]) -> str:
    """The owner message for stops that could not be READ. Board item 172.

    Says the unknown as an unknown. It does not claim the positions are
    naked and it does not reassure that they are covered, because the whole
    point is that neither was established. Severity in the leading words,
    never colour alone (`src/notifier.py` convention).
    """
    rows = list(rows)
    detail = "\n".join(
        f"  {r.symbol}: holding {r.held_qty:.4f}"
        f"{' (short)' if r.is_short else ''} — {r.reason}"
        for r in rows
    )
    return (
        "🛑🛑 PROTECTIVE STOP UNREADABLE\n"
        f"The broker could not be asked whether {len(rows)} held "
        "position(s) have a protective stop. This is NOT a report that they "
        "are unprotected — it is a report that the desk does not know, and "
        "cannot find out, which of the two is true.\n"
        f"{detail}\n"
        "Per-position stops are the desk's only loss protection, so an "
        "unanswerable question about one is worth a look now: check the "
        "position's open orders at the broker directly and place a stop by "
        "hand if none is standing. Nothing has been sold, resized or "
        "cancelled. THIS ALERT is sent at most once per symbol per trading "
        "day; the condition itself keeps showing in the session messages "
        "for as long as it lasts, the same way a missing stop does."
    )


def _scale_in_skip(broker: Any, db_path: str | Path | None) -> set[str]:
    """Symbols mid scale-in that this watchdog must not report or repair.

    A live cancel-confirm-buy window looks uncovered on purpose. Adding a
    stop here would re-create the wash-trade block the sequence just
    cleared. Crash recovery belongs to the session drain; this only
    stays out of the way while a session lock is held or a DAY add is
    still working.
    """
    try:
        from src.execution.scale_in import (
            list_open_entry_ids, pending_scale_in_symbols_from_path,
            trading_session_lock_held,
        )
        symbols = pending_scale_in_symbols_from_path(
            db_path if db_path is not None else DB_PATH,
        )
    except Exception:  # noqa: BLE001
        return set()
    if not symbols:
        return set()
    if trading_session_lock_held():
        return set(symbols)
    skip: set[str] = set()
    for symbol in symbols:
        if list_open_entry_ids(broker, symbol):
            skip.add(symbol)
    return skip


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
    gaps, broker_error = uncovered_positions(
        broker, sweep_symbol=sweep_symbol, skip_symbols=_scale_in_skip(broker, db_path),
        counts=counts, unreadable=unreadable,
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
                        skip_symbols=_scale_in_skip(broker, db_path),
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
                skip_symbols=_scale_in_skip(broker, db_path),
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
    failing_symbols = [
        str(r.symbol).strip().upper() for r in repairs
        if not r.placed and str(r.symbol).strip()
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

    status = CoverageStatus(
        trading_day=day.isoformat(),
        session_ran=ran,
        gaps=gaps,
        broker_error=broker_error,
        db_error=db_error,
        already_alerted_for_day=(state.get("alerted_for_day") == day.isoformat()),
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
    )
    if status.should_alert:
        state["alerted_for_day"] = day.isoformat()
    if status.should_alert_repair_failure:
        _record_repair_failure_alert(state, failure_day, failing_symbols)
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
    }
    state["updated_at"] = moment.replace(microsecond=0).isoformat()
    save_state(state, state_path)
    return status


def alert_text(status: CoverageStatus) -> str:
    """Severity in the leading word, never colour alone (`src/notifier.py`
    convention)."""
    lines = []
    for g in status.gaps:
        dollars = f"${g.unprotected_value:,.2f}" if g.unprotected_value else "value unknown"
        lines.append(
            f"  {g.symbol}: holding {g.held_qty:.4f}, stop covers {g.covered_qty:.4f}, "
            f"{g.uncovered_qty:.4f} share(s) with NO stop ({dollars})"
        )
    reason = (
        "the database could not be read, so a session cannot be proven"
        if status.session_ran is None
        else "no scheduled session completed during that session"
    )
    if status.market_open:
        what_happens = (
            "The market is OPEN and the automatic re-placement did NOT close "
            f"this gap ({status.market_reason}). Nothing was sold, resized or "
            "cancelled — the only action this check can take is adding a "
            "protective stop, and it could not.\n\n"
        )
    else:
        what_happens = (
            f"The market is shut right now ({status.market_reason}), so no "
            "stop can be placed at this moment. The coverage sweep will put "
            "a DAY stop back over the remainder at the open, automatically, "
            "whether or not the desk is switched on.\n\n"
            "THE PART THAT IS NOT FIXED, AND CANNOT BE: a sub-share remainder "
            "is unprotected OVERNIGHT no matter what. This broker accepts "
            "fractional orders only as DAY orders, so every stop over a "
            "remainder stops existing at 16:00 ET. Re-placing it each session "
            "restores intraday protection and does nothing at all for a gap "
            "down before the open. The only ways to remove that exposure are "
            "to hold whole shares or not to hold the remainder.\n\n"
        )
    return (
        "🔴 UNPROTECTED SHARES, AND THE DESK IS NOT RUNNING\n"
        f"{len(status.gaps)} position(s) at the broker have protective-stop "
        "coverage short of what is held, and the session sweep that is "
        f"supposed to re-place it did not run on {status.trading_day} "
        f"({reason}).\n"
        + "\n".join(lines) + "\n"
        f"Total with no stop: ${status.unprotected_total:,.2f}.\n\n"
        + what_happens +
        "Your options: resume the desk, place the missing stop by hand "
        "(fractional stops must be DAY orders), or close the uncovered "
        "remainder. Nothing has been sold, resized or cancelled. This "
        "message repeats at most once per trading day while the condition "
        "holds."
    )


def repair_failure_text(status: CoverageStatus) -> str:
    """The alarm for a placement that was attempted and did NOT land.

    Separate from the exposure alert and on its own once-a-day marker: a
    failure to put the stop back is the state item 53 exists to make
    impossible to miss, and it must not be swallowed by an earlier report
    that merely described the same shares as uncovered.
    """
    lines = [
        f"  {r.symbol}: {r.qty:.4f} share(s) still with no stop — {r.detail}"
        for r in status.repair_failures
    ]
    return (
        "🔴 COULD NOT PUT THE PROTECTIVE STOP BACK\n"
        f"The coverage sweep found {len(status.repair_failures)} position(s) "
        "short of stop coverage during OPEN market hours and tried to place "
        "the missing protective stop. It did not land.\n"
        + "\n".join(lines) + "\n\n"
        "These shares are unprotected right now, during the session, which "
        "is not the expected overnight lapse. Nothing was sold, resized or "
        "cancelled. Place the stop by hand or close the position. This "
        "message repeats at most once per trading day."
    )


def status_line(status: CoverageStatus) -> str:
    """One journal line for the heartbeat unit."""
    if status.broker_error:
        return f"coverage_watchdog: could NOT check the broker ({status.broker_error})"
    placed = ""
    if status.repaired:
        placed = (
            "; RE-PLACED " + ", ".join(
                f"{r.symbol} {r.qty:.4f}" for r in status.repaired
            ) + " (DAY over any sub-share part — lapses at the close again)"
        )
    if status.repair_failures:
        placed += "; FAILED to place " + ", ".join(
            f"{r.symbol} {r.qty:.4f}" for r in status.repair_failures
        )
    # Board item 172. Appended to EVERY branch below, including the clean
    # one: a pass that could not read one symbol's stops has not checked
    # every held position, and a line saying it has would be false.
    if status.unreadable:
        placed += "; COULD NOT READ the stops of " + ", ".join(
            r.symbol for r in status.unreadable
        )
    if not status.gaps:
        if status.unreadable:
            return (
                f"coverage_watchdog: {len(status.unreadable)} position(s) "
                "UNREADABLE; every position that could be read is fully "
                "stop-covered" + placed
            )
        return (
            "coverage_watchdog: OK — every held position is fully stop-covered"
            + placed
        )
    if status.session_ran:
        return (
            f"coverage_watchdog: {len(status.gaps)} position(s) short-covered "
            f"(${status.unprotected_total:,.2f}) but a session ran on "
            f"{status.trading_day}; the sweep owns it, not alerting" + placed
        )
    if status.already_alerted_for_day:
        return (
            f"coverage_watchdog: STILL EXPOSED (${status.unprotected_total:,.2f}) "
            f"with no session on {status.trading_day}; already alerted for "
            "that day" + placed
        )
    return (
        f"coverage_watchdog: EXPOSED — {len(status.gaps)} position(s), "
        f"${status.unprotected_total:,.2f} with no stop and no session on "
        f"{status.trading_day} [{status.market_reason}]" + placed
    )


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

#: The log prefix a reader greps for. One name for both entry points.
SWEEP_LOG_NAME = "COVERAGE SWEEP"
#: `agent_name` on the evidence row — distinct from 'pipeline' so the
#: dashboard's per-session feed never mistakes a sweep for a session.
SWEEP_AGENT_NAME = "coverage_sweep"


def sweep_summary(
    status: CoverageStatus | None, *, entry: str, run_id: str,
    alerts: Iterable[str] = (), error: str | None = None,
) -> dict[str, Any]:
    """Everything one run did, as one flat dict. `status` None means the run
    could not get as far as a check (`error` says why)."""
    if status is None:
        return {
            "stage": "coverage_sweep", "outcome": "could_not_run",
            "reason": error or "", "entry": entry, "run_id": run_id,
        }
    if status.broker_error:
        outcome = "could_not_check"
    elif status.repair_failures:
        outcome = "repair_failed"
    # Board item 172, ranked ABOVE 'repaired', 'clean' and 'gaps_left'. A
    # pass that could not read a position's stops did not establish that
    # position's coverage, and 'clean' is the one word that must never
    # describe a run holding an unanswered question about loss protection.
    elif status.unreadable:
        outcome = "unreadable_stops"
    elif status.repaired:
        outcome = "repaired"
    elif not status.gaps:
        outcome = "clean"
    elif status.repair_deferred:
        outcome = "repair_deferred"
    else:
        outcome = "gaps_left"
    return {
        "stage": "coverage_sweep",
        "outcome": outcome,
        "reason": status.repair_deferred or status.market_reason or "",
        "entry": entry,
        "run_id": run_id,
        "trading_day": status.trading_day,
        "session_ran": status.session_ran,
        "market_open": status.market_open,
        "positions_checked": status.positions_checked,
        "gaps_found": len(status.gaps),
        "gap_symbols": [g.symbol for g in status.gaps],
        "unprotected_usd": status.unprotected_total,
        "repairs_attempted": len(status.repairs),
        "repairs_succeeded": len(status.repaired),
        "repairs_failed": len(status.repair_failures),
        "repaired": [
            {"symbol": r.symbol, "qty": r.qty} for r in status.repaired
        ],
        "failed": [
            {"symbol": r.symbol, "qty": r.qty, "detail": r.detail}
            for r in status.repair_failures
        ],
        "repair_deferred": status.repair_deferred,
        "broker_error": status.broker_error,
        "db_error": status.db_error,
        # Board item 172. `positions_checked` counts positions the sweep
        # LOOKED at, which includes the ones it could not read, so the two
        # numbers together say how much of the book was actually settled.
        "unreadable_count": len(status.unreadable),
        "unreadable_symbols": [r.symbol for r in status.unreadable],
        "alerts": list(alerts),
    }


def sweep_log_line(summary: dict[str, Any]) -> str:
    """The one greppable line per run."""
    if summary.get("outcome") == "could_not_run":
        return (
            f"{SWEEP_LOG_NAME} {summary.get('run_id')} ({summary.get('entry')}): "
            f"could_not_run — {summary.get('reason')}"
        )
    alerts = summary.get("alerts") or []
    return (
        f"{SWEEP_LOG_NAME} {summary.get('run_id')} ({summary.get('entry')}): "
        f"{summary.get('outcome')} — positions checked "
        f"{summary.get('positions_checked')}, gaps {summary.get('gaps_found')}, "
        f"repairs attempted {summary.get('repairs_attempted')} / succeeded "
        f"{summary.get('repairs_succeeded')} / failed "
        f"{summary.get('repairs_failed')}, alert "
        f"{'; '.join(alerts) if alerts else 'none sent'}"
        + (f", deferred: {summary['repair_deferred']}" if summary.get("repair_deferred") else "")
        + (f", broker error: {summary['broker_error']}" if summary.get("broker_error") else "")
        + (
            ", UNREADABLE stops: "
            + ", ".join(summary.get("unreadable_symbols") or [])
            if summary.get("unreadable_count") else ""
        )
    )


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
