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
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.silence_watchdog import KNOWN_MODES
from src.trading_day import most_recent_trading_day as _read_only_most_recent_trading_day
from src.trading_day import MAX_WEEKDAYS_BACK, _session_bounds_utc  # noqa: F401 -- moved to a read-only module
from src.coverage_watchdog_records import record_watchdog_pass  # noqa: F401 -- re-export
from src.coverage_watchdog_rows import (  # noqa: F401 -- re-export
    _scale_in_row_age_seconds,
    measured_window_bound_seconds,
)
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
from src.data_paths import db_path
from src.alert_claims import (  # noqa: F401 -- re-exported, lifted verbatim 2026-10-05
    STATE_PATH,
    _SUPPRESSION_LOG_LIMIT,
    _record_suppressed_alert,
    _typed_alerted_symbols,
    _utc_now,
    claim_typed_alert,
    load_state,
    release_typed_alert,
    repair_failure_alert_day,
    save_state,
)
from src.coverage_watchdog_parts.models import (  # noqa: F401 -- re-exported, lifted verbatim
    CoverageGap,
    CoverageStatus,
    RepairOutcome,
    UnguardedWindow,
    UnreadableStop,
)
from src.coverage_watchdog_parts.claims_repair import (  # noqa: F401 -- re-exported, lifted verbatim
    _exposure_alerted_symbols,
    _record_repair_failure_alert,
    _repair_failure_alerted_symbols,
    _resolution_notified_symbols,
    claim_repair_failure_alert,
    claim_repair_resolution_notice,
)
from src.coverage_watchdog_parts.awaiting_print import (  # noqa: F401 -- re-exported, lifted verbatim
    AWAITING_FIRST_PRINT_CODES,
    _awaiting_print_state,
    _write_awaiting_print_state,
    awaiting_first_print,
    clear_awaiting_first_print,
    note_awaiting_first_print,
    session_awaiting_print_symbols,
)
from src.coverage_watchdog_parts.claims_other import (  # noqa: F401 -- re-exported, lifted verbatim
    _elected_unfilled_alerted_symbols,
    _exit_declined_alerted_symbols,
    _kill_switch_block_alerted_symbols,
    _unguarded_alerted_symbols,
    _unreadable_alerted_symbols,
    claim_elected_unfilled_alert,
    claim_exit_declined_alert,
    claim_kill_switch_block_alert,
    claim_unreadable_stop_alert,
)

logger = logging.getLogger(__name__)

#: Same database every session and `src/alert_watchdog.py` write to.
DB_PATH = db_path()


TABLE = "alert_channel_checks"


#: Same tolerance the coverage reconciler uses for "covered < held".
_QTY_EPSILON = 1e-6


# ---------------------------------------------------------------------------
# which session are we judging?
# ---------------------------------------------------------------------------


def most_recent_trading_day(now: datetime, broker: Any = None, db: Any = None) -> date:
    """`src.trading_day.most_recent_trading_day`, recording a calendar-read failure.

    The walk lives in a read-only module the dashboard may import; the
    recording (a write) stays here, passed in as the error hook.
    """
    return _read_only_most_recent_trading_day(
        now,
        broker,
        on_error=lambda exc: record_watchdog_pass("calendar_lookup", exc, db=db),
    )


def _connect_ro(path: str) -> sqlite3.Connection:
    """Read-only by OS enforcement — this module never writes the trading DB."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def session_ran_during(day: date, db_path: str | Path | None = None, db: Any = None) -> tuple[bool | None, str | None]:
    """`(ran, error)` — did ANY known scheduled mode record a completed
    session inside `day`'s cash session? `ran` is None when the database
    cannot be read; the caller treats that as not proven."""
    path = str(db_path) if db_path is not None else str(DB_PATH)
    start, end = _session_bounds_utc(day)
    try:
        conn = _connect_ro(path)
    except Exception as exc:  # noqa: BLE001
        record_watchdog_pass("session_ran.connect", exc, db=db)
        return None, f"database unreadable: {exc}"
    try:
        placeholders = ",".join("?" for _ in KNOWN_MODES)
        rows = conn.execute(
            f"SELECT checked_at FROM {TABLE} WHERE source IN ({placeholders})",
            KNOWN_MODES,
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        record_watchdog_pass("session_ran.query", exc, db=db)
        return None, f"query failed (table missing on a fresh database?): {exc}"
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            record_watchdog_pass("session_ran.close", fault=True, db=db)
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
    sink: list[UnreadableStop] | None,
    *,
    symbol: str,
    held_qty: float,
    reason: str,
    is_short: bool,
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
        symbol,
        held_qty,
        reason,
    )
    if sink is not None:
        sink.append(
            UnreadableStop(
                symbol=symbol,
                held_qty=held_qty,
                reason=reason,
                is_short=is_short,
            )
        )


def uncovered_positions(
    broker: Any,
    *,
    sweep_symbol: str | None = None,
    skip_symbols: set[str] | None = None,
    counts: dict[str, int] | None = None,
    unreadable: list[UnreadableStop] | None = None,
    db: Any = None,
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
        record_watchdog_pass("uncovered.get_positions", exc, db=db)
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
                symbol,
                side=("buy" if is_short else "sell"),
            )
        except Exception as exc:  # noqa: BLE001
            record_watchdog_pass("uncovered.snapshot_stops", exc, db=db)
            _record_unreadable(
                unreadable,
                symbol=str(symbol),
                held_qty=held,
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
                unreadable,
                symbol=str(symbol),
                held_qty=held,
                is_short=is_short,
                reason=(
                    "the broker's open-order listing failed, so whether a "
                    "protective stop exists could not be established"
                ),
            )
            continue
        if specs is not None and not isinstance(specs, list):
            _record_unreadable(
                unreadable,
                symbol=str(symbol),
                held_qty=held,
                is_short=is_short,
                reason=(f"protective-stop snapshot in an unusable shape: {type(specs).__name__}"),
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
                unreadable,
                symbol=str(symbol),
                held_qty=held,
                is_short=is_short,
                reason=unparsable,
            )
            continue
        if covered + _QTY_EPSILON < held:
            uncovered = round(held - covered, 9)
            gaps.append(
                CoverageGap(
                    symbol=str(symbol),
                    held_qty=held,
                    covered_qty=covered,
                    uncovered_qty=uncovered,
                    unprotected_value=_notional(p, uncovered),
                    is_short=is_short,
                )
            )
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
                gap.symbol,
                side=protective_side,
            )
            record_watchdog_pass("replace.reread_stops", db=db)
        except Exception as exc:  # noqa: BLE001
            record_watchdog_pass("replace.reread_stops", exc, db=db)
            outcomes.append(
                RepairOutcome(
                    gap.symbol,
                    gap.uncovered_qty,
                    False,
                    f"could not re-read open stops before placing ({exc})",
                )
            )
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
                "coverage sweep: %s is already covered (%.4f of %.4f) by the time we got to it — placing nothing.",
                gap.symbol,
                covered_now,
                gap.held_qty,
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
                broker=broker,
                last_buy=last_buy,
                symbol=gap.symbol,
                uncovered_qty=shortfall,
                is_short=gap.is_short,
                db=db,
                outcome=repair,
                resting_stops=list(specs or []),
                caller="coverage_sweep",
            )
            record_watchdog_pass("replace.placement", db=db)
        except Exception as exc:  # noqa: BLE001
            record_watchdog_pass("replace.placement", exc, db=db)
            outcomes.append(
                RepairOutcome(
                    gap.symbol,
                    shortfall,
                    False,
                    f"placement raised ({exc})",
                )
            )
            continue
        outcomes.append(
            RepairOutcome(
                gap.symbol,
                shortfall,
                bool(placed),
                refusal_code=str(repair.get("repair_refusal_code") or ""),
                still_covered=covered_now > _QTY_EPSILON,
                detail=""
                if placed
                else (
                    repair.get("repair_refusal")
                    or (
                        "the broker did not accept a protective stop for the "
                        f"shortfall — see the journal for which guard stopped it "
                        f"(no recorded {opening} stop level, stop on the live-"
                        "price side, or retries exhausted)"
                    )
                ),
            )
        )
    return outcomes


# ---------------------------------------------------------------------------
# on-box state — one alert per trading day
# ---------------------------------------------------------------------------


def _scale_in_skip(
    broker: Any,
    db_path: str | Path | None,
    *,
    now: datetime | None = None,
    db: Any = None,
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
            list_open_entry_ids,
            pending_scale_in_symbols_from_path,
            trading_session_lock_held,
        )

        path = db_path if db_path is not None else DB_PATH
        symbols = pending_scale_in_symbols_from_path(path)
    except Exception:  # noqa: BLE001
        record_watchdog_pass("scale_in_skip.symbols", fault=True, db=db)
        return set()
    if not symbols:
        return set()
    if trading_session_lock_held():
        stale = _scale_in_symbols_past_measured_bound(path, symbols, now=now, db=db)
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
    db_path: str | Path | None,
    symbols: set[str],
    *,
    now: datetime | None = None,
    db: Any = None,
) -> set[str]:
    """Of `symbols`, those whose window is older than every measured one.

    Empty — meaning "treat them all as normal live windows" — whenever the
    desk has no measured history to compare against, whenever the rows
    cannot be read, and for any row whose write time cannot be parsed. Each
    of those is a case where calling a window overdue would be an invented
    figure rather than a measured one.
    """
    bound, observations = measured_window_bound_seconds(db_path, db=db)
    if bound is None or observations <= 0:
        return set()
    try:
        from src.execution.scale_in import pending_scale_in_rows_from_path

        rows = pending_scale_in_rows_from_path(db_path)
    except Exception:  # noqa: BLE001
        record_watchdog_pass("scale_in_bound.rows", fault=True, db=db)
        return set()
    moment = now or _utc_now()
    stale: set[str] = set()
    for row in rows or []:
        symbol = str(row.get("symbol") or "").strip()
        if not symbol or symbol not in symbols:
            continue
        age = _scale_in_row_age_seconds(row.get("created_at"), moment, db)
        if age is not None and age > bound:
            stale.add(symbol)
    return stale


def deliberately_unguarded(
    broker: Any,
    db_path: str | Path | None,
    *,
    skip_symbols: set[str] | None = None,
    now: datetime | None = None,
    db: Any = None,
) -> list[UnguardedWindow]:
    """Every symbol the sweep skipped for a live scale-in, named, with how
    long its protection has been deliberately down.

    Board item 193's live-risk half. Reports only symbols that were ACTUALLY
    skipped this run (`skip_symbols`, the same set handed to
    `uncovered_positions`), so the report can never describe a position the
    sweep in fact checked. Places no order and changes no state.
    """
    skip = skip_symbols if skip_symbols is not None else _scale_in_skip(broker, db_path, db=db)
    if not skip:
        return []
    try:
        from src.execution.scale_in import pending_scale_in_rows_from_path

        rows = pending_scale_in_rows_from_path(
            db_path if db_path is not None else DB_PATH,
        )
    except Exception:  # noqa: BLE001
        record_watchdog_pass("unguarded.rows", fault=True, db=db)
        return []
    bound, observations = measured_window_bound_seconds(
        db_path if db_path is not None else DB_PATH,
        db=db,
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
                max(0.0, (moment - stamp).total_seconds()),
                1,
            )
        except Exception:  # noqa: BLE001
            record_watchdog_pass("unguarded.window_seconds", fault=True, db=db)
            seconds = None
        try:
            qty = float(row.get("position_qty_before_sell") or 0.0)
        except (TypeError, ValueError):
            qty = 0.0
        out.append(
            UnguardedWindow(
                symbol=symbol,
                held_qty=abs(qty),
                is_short=qty < 0,
                since_utc=created,
                seconds_open=seconds,
                bound_seconds=bound,
                bound_observations=observations,
            )
        )
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


# ---------------------------------------------------------------------------
# the broker-write lock shared with intra_check (board item 127)
# ---------------------------------------------------------------------------

#: The advisory flock file `TradingPipeline._intraday_scan_process_lock`
#: takes beside the database. Same file, so this repair pass and
#: `intra_check`'s broker-writing preamble exclude each other. Not a number
#: and not new: the name is the one the pipeline has used since 2026-08-19.
REPAIR_LOCK_NAME = ".intraday_scan.lock"


@contextlib.contextmanager
def repair_lock(db_path: str | Path | None = None, db: Any = None):
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
        record_watchdog_pass("repair_lock", exc, db=db)
        logger.warning("coverage sweep: could not establish the repair lock (%s)", exc)
    try:
        yield held
    finally:
        if fh is not None:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                record_watchdog_pass("repair_lock.close", fault=True, db=db)


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
    scale_in_skip = _scale_in_skip(broker, db_path, db=db)
    gaps, broker_error = uncovered_positions(
        broker,
        sweep_symbol=sweep_symbol,
        skip_symbols=scale_in_skip,
        counts=counts,
        unreadable=unreadable,
        db=db,
    )
    unguarded = deliberately_unguarded(
        broker,
        db_path,
        skip_symbols=scale_in_skip,
        now=moment,
        db=db,
    )
    positions_checked = None if broker_error else int(counts.get("positions_checked", 0))
    day = most_recent_trading_day(moment, broker, db)
    ran, db_error = session_ran_during(day, db_path, db)

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
        with repair_lock(db_path, db) as held:
            if not held:
                repair_deferred = (
                    "another desk process holds the broker-write lock "
                    f"({REPAIR_LOCK_NAME}); this tick placed nothing and the "
                    "next one re-reads the broker"
                )
                market_reason = f"{market_reason}, but {repair_deferred}"
            else:
                repairs = replace_missing_stops(
                    broker,
                    gaps,
                    last_buy=last_buy,
                    sweep_symbol=sweep_symbol,
                    db=db,
                )
                if any(r.placed for r in repairs):
                    refreshed, refresh_error = uncovered_positions(
                        broker,
                        sweep_symbol=sweep_symbol,
                        skip_symbols=scale_in_skip,
                        unreadable=unreadable,
                        db=db,
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
        market_reason = f"{market_reason}, but no recorded-stop lookup was supplied, so nothing was placed"

    if last_buy is not None:
        try:
            from src.execution.stop_records import (
                reconcile_recorded_stop_levels,
            )

            try:
                positions = broker.get_positions()
                record_watchdog_pass("stop_level.get_positions", db=db)
            except Exception:
                record_watchdog_pass("stop_level.get_positions", fault=True, db=db)
                positions = []
            if not isinstance(positions, list):
                positions = []
            mismatches = reconcile_recorded_stop_levels(
                broker=broker,
                last_buy=last_buy,
                positions=positions,
                sweep_symbol=sweep_symbol,
                skip_symbols=scale_in_skip,
                db=db,
            )
            # Log every pass; do not page from this 30-minute unit. An
            # out-of-band mismatch is never write-back-cleared, so paging
            # here would fire ~48 times a day with no acknowledgement.
            # The session coverage sweep pages.
            for item in mismatches:
                logger.error(
                    "STOP RECORD MISMATCH: %s — %s (short=%s)",
                    item.symbol,
                    item.reason,
                    item.is_short,
                )
            record_watchdog_pass("stop_level_reconcile", db=db)
        except Exception as exc:  # noqa: BLE001
            record_watchdog_pass("stop_level_reconcile", exc, db=db)
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
        r
        for r in repairs
        if str(r.symbol).strip()
        and not r.placed
        and awaiting_first_print(
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
            outcome.symbol,
            outcome.detail or "no reason given",
        )
    waiting_symbols = {str(r.symbol).strip().upper() for r in waiting_now}
    placed_symbols = [str(r.symbol).strip().upper() for r in repairs if r.placed and str(r.symbol).strip()]
    resolution_symbols: tuple[str, ...] = ()
    if placed_symbols:
        clear_awaiting_first_print(placed_symbols, now=moment, state=state)
        # The retraction half. Claimed here, beside every other marker, so
        # the caller sends exactly what was reserved.
        resolution_symbols = tuple(
            claim_repair_resolution_notice(
                placed_symbols,
                now=moment,
                state=state,
            )
        )
    failing_symbols = [
        str(r.symbol).strip().upper()
        for r in repairs
        if not r.placed and str(r.symbol).strip() and str(r.symbol).strip().upper() not in waiting_symbols
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
    unreadable_symbols = [str(r.symbol).strip().upper() for r in unreadable if str(r.symbol).strip()]

    # Board item 193. Same day-keyed, per-symbol claim the unreadable page
    # uses, and claimed HERE rather than by the caller for the same reason:
    # the marker is written inside this function, so a caller that re-claimed
    # would find this run's own marker and silence the message it wrote.
    already_unguarded = _unguarded_alerted_symbols(state, failure_day)
    # Item 211 defect 2: per symbol per day, like both siblings.
    exposure_symbols = {str(g.symbol).strip().upper() for g in gaps if str(g.symbol).strip()}
    already_exposed = _exposure_alerted_symbols(state, day.isoformat())
    unguarded_fresh = [r for r in unguarded if r.over_bound and str(r.symbol).strip().upper() not in already_unguarded]

    status = CoverageStatus(
        trading_day=day.isoformat(),
        session_ran=ran,
        gaps=gaps,
        gaps_detected=gaps_detected,
        broker_error=broker_error,
        db_error=db_error,
        already_alerted_for_day=bool(exposure_symbols) and all(sym in already_exposed for sym in exposure_symbols),
        repairs=repairs,
        market_open=market_open,
        market_reason=market_reason,
        already_alerted_repair_failure_for_day=bool(failing_symbols)
        and all(sym in already_failed for sym in failing_symbols),
        positions_checked=positions_checked,
        repair_deferred=repair_deferred,
        unreadable=list(unreadable),
        already_alerted_unreadable_for_day=bool(unreadable_symbols)
        and all(sym in already_unreadable for sym in unreadable_symbols),
        # Board item 172. What THIS run is entitled to say out loud: the
        # rows whose symbol has not already been reported today. Built from
        # the same upper-cased comparison the claim itself uses, so a
        # mixed-case symbol from the broker cannot slip past the filter and
        # re-page under a different spelling.
        unreadable_fresh=[
            r for r in unreadable if str(r.symbol).strip() and str(r.symbol).strip().upper() not in already_unreadable
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
            "symbols": sorted(already_unguarded | {str(r.symbol).strip().upper() for r in unguarded_fresh}),
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
            run_id=str(summary.get("run_id") or ""),
            agent_name=SWEEP_AGENT_NAME,
            kind="pipeline_event",
            scope="run",
            symbol=None,
            evidence_json=json.dumps(summary, sort_keys=True, default=str),
        )
        record_watchdog_pass("sweep_run_record", db=db)
        return True
    except Exception as exc:  # noqa: BLE001 — a record is never trading authority
        record_watchdog_pass("sweep_run_record", exc, db=db)
        logger.warning("%s: could not write the run record: %s", SWEEP_LOG_NAME, exc)
        return False


# --- Generic per-symbol, per-trading-day alert claim -------------------------
# Same state file, same trading-day key and the same claim-before-send
# discipline as `claim_repair_failure_alert`, but keyed by an arbitrary
# `kind` so a new fail-closed page does not need its own pair of helpers.
# Callers that page the owner about a per-symbol condition use this; the
# older named helpers keep their own keys so their history is unaffected.
