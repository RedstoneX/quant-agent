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
at the same level (the stop recorded on the position's own last BUY), with
the same guards (never at/above the live price, never invented when the row
has none) and the same tif derivation (`_derive_stop_tif`: whole shares GTC,
sub-share DAY, because the broker refuses anything else).

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

import json
import logging
import math
import os
import sqlite3
import tempfile
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
    #: A short is protected by a BUY stop and has no recorded BUY row to read
    #: a level from, so it is reported and never repaired — the same line the
    #: in-session sweep draws.
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
        was opened on."""
        return bool(self.repair_failures) and not self.already_alerted_repair_failure_for_day


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

def uncovered_positions(
    broker: Any, *, sweep_symbol: str | None = None,
    skip_symbols: set[str] | None = None,
) -> tuple[list[CoverageGap], str | None]:
    """Every held position whose open protective stops cover less than the
    held quantity. Longs are checked against SELL stops, shorts against BUY
    stops, exactly as `TradingPipeline._reconcile_stop_coverage` does. The
    cash-sweep vehicle is deliberately stopless and is skipped.

    Returns `(gaps, error)`; on a broker failure `gaps` is empty and
    `error` says why — the caller reports "could not check", never "clean".
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
        is_short = qty < 0
        try:
            _ok, specs = broker.snapshot_protective_stops(
                symbol, side=("buy" if is_short else "sell"),
            )
        except Exception as exc:  # noqa: BLE001
            return [], f"snapshot_protective_stops failed for {symbol}: {exc}"
        covered = 0.0
        for s in specs or []:
            try:
                covered += float(s.get("qty", 0) or 0)
            except (TypeError, ValueError):
                continue
        held = abs(qty)
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

    Shorts are skipped, as the in-session sweep skips them: there is no
    recorded BUY row to read a short's protective level from, and inventing
    one is the policy call neither path will make.
    """
    from src.execution.stop_repair import repair_stop_coverage

    outcomes: list[RepairOutcome] = []
    for gap in gaps:
        if sweep_symbol and gap.symbol == sweep_symbol:
            continue
        if gap.is_short:
            logger.warning(
                "coverage sweep: %s is a SHORT with %.4f share(s) uncovered — "
                "flagged, not repaired: there is no recorded BUY row to read "
                "its protective level from and inventing one is a policy "
                "call this path will not make.",
                gap.symbol, gap.uncovered_qty,
            )
            continue
        # Fresh broker truth for THIS symbol, taken as late as possible.
        try:
            _ok, specs = broker.snapshot_protective_stops(gap.symbol, side="sell")
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
        try:
            placed = repair_stop_coverage(
                broker=broker, last_buy=last_buy,
                symbol=gap.symbol, uncovered_qty=shortfall,
            )
        except Exception as exc:  # noqa: BLE001
            outcomes.append(RepairOutcome(
                gap.symbol, shortfall, False, f"placement raised ({exc})",
            ))
            continue
        outcomes.append(RepairOutcome(
            gap.symbol, shortfall, bool(placed),
            "" if placed else (
                "the broker did not accept a protective stop for the "
                "shortfall — see the journal for which guard stopped it "
                "(no recorded BUY stop level, stop at/above the live price, "
                "or retries exhausted)"
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
    raw.setdefault("repair_failure_alerted_for_day", None)
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
) -> CoverageStatus:
    """Read broker coverage and session evidence, put back what is missing if
    the market is open, decide, persist the once-per-day markers. Never
    raises.

    `last_buy` is the callable that answers "what stop level did this
    position's own BUY record?" — pass it and placement is possible; leave it
    None and this stays the pure reader it was, which is what a caller with
    no database handle must do rather than repair against a guessed level.
    """
    moment = now or _utc_now()
    state = load_state(state_path)

    gaps, broker_error = uncovered_positions(
        broker, sweep_symbol=sweep_symbol, skip_symbols=_scale_in_skip(broker, db_path),
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
    if gaps and market_open and last_buy is not None:
        repairs = replace_missing_stops(
            broker, gaps, last_buy=last_buy, sweep_symbol=sweep_symbol,
        )
        if any(r.placed for r in repairs):
            refreshed, refresh_error = uncovered_positions(
                broker, sweep_symbol=sweep_symbol,
                skip_symbols=_scale_in_skip(broker, db_path),
            )
            if refresh_error is None:
                gaps = refreshed
            else:
                broker_error = broker_error or refresh_error
    elif gaps and market_open and last_buy is None:
        market_reason = (
            f"{market_reason}, but no recorded-stop lookup was supplied, so "
            "nothing was placed"
        )

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
        already_alerted_repair_failure_for_day=(
            state.get("repair_failure_alerted_for_day") == day.isoformat()
        ),
    )
    if status.should_alert:
        state["alerted_for_day"] = day.isoformat()
    if status.should_alert_repair_failure:
        state["repair_failure_alerted_for_day"] = day.isoformat()
    state["last_result"] = {
        "trading_day": status.trading_day,
        "session_ran": status.session_ran,
        "gaps": [g.__dict__ for g in gaps],
        "broker_error": broker_error,
        "db_error": db_error,
        "market_open": market_open,
        "market_reason": market_reason,
        "repairs": [r.__dict__ for r in repairs],
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
    if not status.gaps:
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
