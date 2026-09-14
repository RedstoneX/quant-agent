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
It is a READER: positions and open stops from the broker, session evidence
from `alert_channel_checks` (the same rows `src/silence_watchdog.py` reads),
and one owner alert. It never places, modifies or cancels an order, and it
never writes to the trading database. Repair is the session sweep's job;
this only says, loudly, when that job has not been done.

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
From `scripts/alert_heartbeat.py`, after the channel probe. That unit is
the daily floor that fires at 06:15 ET seven days a week whether or not the
trading timers are enabled — the one place proven to still run while the
desk is paused, which is precisely when this check matters. Its result
never changes the probe's own verdict or exit code.
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


@dataclass(frozen=True)
class CoverageStatus:
    """`should_alert` is the only field callers act on."""

    trading_day: str | None            # the session judged, YYYY-MM-DD (ET)
    session_ran: bool | None           # None: database unreadable
    gaps: list[CoverageGap] = field(default_factory=list)
    broker_error: str | None = None
    db_error: str | None = None
    already_alerted_for_day: bool = False

    @property
    def unprotected_total(self) -> float:
        return round(sum(g.unprotected_value for g in self.gaps), 2)

    @property
    def is_exposed(self) -> bool:
        """Coverage is short AND the sweep that should have fixed it did
        not run. An unreadable database counts as "did not run": we
        cannot prove the sweep happened, and that is the finding."""
        return bool(self.gaps) and not self.session_ran

    @property
    def should_alert(self) -> bool:
        return self.is_exposed and not self.already_alerted_for_day


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

def uncovered_positions(broker: Any, *, sweep_symbol: str | None = None) -> tuple[list[CoverageGap], str | None]:
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
# the check
# ---------------------------------------------------------------------------

def check_coverage(
    broker: Any,
    *,
    now: datetime | None = None,
    sweep_symbol: str | None = None,
    db_path: str | Path | None = None,
    state_path: Path | None = None,
) -> CoverageStatus:
    """Read broker coverage and session evidence, decide, persist the
    once-per-day marker. Never raises; never writes to the broker."""
    moment = now or _utc_now()
    state = load_state(state_path)

    gaps, broker_error = uncovered_positions(broker, sweep_symbol=sweep_symbol)
    day = most_recent_trading_day(moment, broker)
    ran, db_error = session_ran_during(day, db_path)

    status = CoverageStatus(
        trading_day=day.isoformat(),
        session_ran=ran,
        gaps=gaps,
        broker_error=broker_error,
        db_error=db_error,
        already_alerted_for_day=(state.get("alerted_for_day") == day.isoformat()),
    )
    if status.should_alert:
        state["alerted_for_day"] = day.isoformat()
    state["last_result"] = {
        "trading_day": status.trading_day,
        "session_ran": status.session_ran,
        "gaps": [g.__dict__ for g in gaps],
        "broker_error": broker_error,
        "db_error": db_error,
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
    return (
        "🔴 UNPROTECTED SHARES, AND THE DESK IS NOT RUNNING\n"
        f"{len(status.gaps)} position(s) at the broker have protective-stop "
        "coverage short of what is held, and the session sweep that is "
        f"supposed to re-place it did not run on {status.trading_day} "
        f"({reason}).\n"
        + "\n".join(lines) + "\n"
        f"Total with no stop: ${status.unprotected_total:,.2f}.\n\n"
        "A sub-share remainder losing its DAY stop overnight is expected — "
        "the broker will not hold a fractional stop past the close. What is "
        "NOT expected is a whole trading day passing with no session to put "
        "it back. While the trading timers stay off, this exposure repeats "
        "every session.\n\n"
        "Your options: resume the desk (the first session sweep re-covers "
        "it), place the missing stop by hand (fractional stops must be DAY "
        "orders), or close the uncovered remainder. Nothing has been "
        "placed, changed or cancelled automatically. This message repeats "
        "at most once per trading day while the condition holds."
    )


def status_line(status: CoverageStatus) -> str:
    """One journal line for the heartbeat unit."""
    if status.broker_error:
        return f"coverage_watchdog: could NOT check the broker ({status.broker_error})"
    if not status.gaps:
        return "coverage_watchdog: OK — every held position is fully stop-covered"
    if status.session_ran:
        return (
            f"coverage_watchdog: {len(status.gaps)} position(s) short-covered "
            f"(${status.unprotected_total:,.2f}) but a session ran on "
            f"{status.trading_day}; the sweep owns it, not alerting"
        )
    if status.already_alerted_for_day:
        return (
            f"coverage_watchdog: STILL EXPOSED (${status.unprotected_total:,.2f}) "
            f"with no session on {status.trading_day}; already alerted for that day"
        )
    return (
        f"coverage_watchdog: EXPOSED — {len(status.gaps)} position(s), "
        f"${status.unprotected_total:,.2f} with no stop and no session on "
        f"{status.trading_day}"
    )
