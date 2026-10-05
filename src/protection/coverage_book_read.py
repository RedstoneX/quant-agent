"""Reading the held book for the stop-coverage sweep, and what to do when
the broker will not say.

`TradingPipeline._reconcile_stop_coverage` used to open with a bare
``try: positions = self.broker.get_positions() / except: return []``. Its
return type is ``list[dict]`` of coverage gaps, and every caller reads an
empty list as "no gaps found — every position is covered". So one failed
broker read turned the desk's ONLY audit for a naked position into a
clean bill of health, in the morning, evening, position-review and
intraday sessions alike. Removing the account-level loss halt made
per-position stops the sole loss protection, which makes that silence the
most dangerous sentence this code can say.

The owner's ruling (2026-10-02) is that the desk must ACT once it has
exhausted retries and different ways of asking, and that duplicating a
stop the broker then refuses is an acceptable price. So:

1. RETRY the read, bounded (never an unbounded loop at session entry).
2. ASK A DIFFERENT WAY. The broker exposes exactly one whole-book
   endpoint, so the second source is the desk's OWN record of the open
   book — ``db.get_symbols_with_open_ledger_qty()``, the signed ledger
   quantity per symbol.
3. Report an UNKNOWN that no caller can mistake for all-clear.

The unknown is expressed in the convention this sweep ALREADY has for
"could not be asked" at the per-symbol level (board item 172):
``coverage='unreadable'`` rows, which `src/notifier/gaps.py` renders as
the 🛑🛑 STOP UNREADABLE banner and which every gap consumer already
buckets separately from a measured shortfall. `src/coverage_watchdog.py`
returns ``(gaps, error)`` for the same failure for the same reason; this
keeps that spirit without a third convention and without changing a
return type shared by four sessions.

A row built here carries ``covered_qty=None`` — the coverage is UNKNOWN,
not zero. Nothing downstream may price a stop off it: the caller runs the
existing repair path, which places only against the level recorded on the
position's own opening row and refuses outright when there is none. An
unknown is never repaired against a guessed level.
"""
from __future__ import annotations

from src.sentinel.guarded import NO_LEDGER, record_guarded_pass
import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


def read_positions_with_retry(
    broker: Any,
    *,
    # Bounded, and short: this runs at session entry with the market moving.
    attempts: int = 3,
    sleep: Callable[[float], None] | None = None,
) -> tuple[list | None, str | None]:
    """``(positions, None)`` on success, ``(None, reason)`` once the read
    has failed every bounded attempt. A non-list reply is a failure too:
    it is not a book, and treating it as an empty one is the same defect.
    """
    last = "get_positions never ran"
    for attempt in range(1, max(1, int(attempts)) + 1):
        try:
            positions = broker.get_positions()
        except Exception as exc:  # noqa: BLE001
            last = f"get_positions raised: {exc}"
        else:
            if isinstance(positions, list):
                if attempt > 1:
                    logger.warning(
                        "coverage reconcile: positions read succeeded on "
                        "attempt %d of %d.", attempt, attempts,
                    )
                return positions, None
            last = (
                "get_positions returned "
                f"{type(positions).__name__}, not a list of positions"
            )
        logger.warning(
            "coverage reconcile: positions read attempt %d of %d failed: %s",
            attempt, attempts, last,
        )
        if attempt < attempts:
            backoff = (0.5, 1.5)
            idx = min(attempt - 1, len(backoff) - 1)
            try:
                (sleep or time.sleep)(backoff[idx])
            except Exception:  # noqa: BLE001
                pass
    return None, last


def unverified_book_rows(
    db: Any,
    *,
    error: str,
    attempts: int = 3,
    skip_symbols: set | None = None,
    sweep_symbol: str | None = None,
) -> list[dict]:
    """The UNKNOWN, as gap rows no caller can read as all-clear.

    Always at least one row: a book-level marker saying the held book
    itself could not be established. Plus one row per symbol the desk's
    own ledger believes is open, so each known holding is treated as
    unverified and can be run through the repair path by the caller.
    Symbols the WAL drain already owns, and the deliberately stopless
    cash-sweep vehicle, are skipped exactly as the measured path skips
    them.
    """
    rows: list[dict] = [{
        "symbol": "(whole book)", "held_qty": None, "covered_qty": None,
        "coverage": "unreadable", "repaired": False, "is_short": False,
        "book_unreadable": True,
        "read_error": (
            "the broker could not list open positions after "
            f"{attempts} attempts ({error}), so whether any "
            "position is protected could not be established; the desk's own "
            "ledger was used as a second source and every name it knows "
            "about is listed below as unverified"
        ),
    }]
    try:
        ledger = db.get_symbols_with_open_ledger_qty() or {}
        if not isinstance(ledger, dict):
            raise TypeError(f"ledger read returned {type(ledger).__name__}")
        ledger = dict(ledger)
        record_guarded_pass(db, "coverage_book_read.ledger_read")
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(db, "coverage_book_read.ledger_read", exc)
        rows[0]["read_error"] += (
            f" — except the ledger could not be read either ({exc}), so not "
            "even the list of held names is known"
        )
        return rows
    skip = {str(s).strip().upper() for s in (skip_symbols or set()) if s}
    if sweep_symbol:
        skip.add(str(sweep_symbol).strip().upper())
    for symbol, qty in sorted(ledger.items()):
        try:
            signed = float(qty or 0)
        except (TypeError, ValueError):
            signed = 0.0
        if not symbol or signed == 0 or str(symbol).strip().upper() in skip:
            continue
        rows.append({
            "symbol": symbol, "held_qty": signed, "covered_qty": None,
            "coverage": "unreadable", "repaired": False,
            "is_short": signed < 0, "book_unreadable": True,
            "read_error": (
                "the broker would not list open positions, so this holding "
                "— known only from the desk's own ledger — could not be "
                "checked for a protective stop and is treated as unverified"
            ),
        })
    return rows


def unverified_book_sweep(
pipeline: Any, read_error: str, pending_syms: set,
) -> list[dict]:
    """The broker would not list positions. Return an UNKNOWN that no
    caller can mistake for all-clear, and make the safe state true for
    every holding the desk itself knows about.

    Owner ruling 2026-10-02: after retries and a different way of
    asking are exhausted, the desk ACTS — a duplicate stop the broker
    refuses is an acceptable cost against a naked position left alone.
    The repair places only against the level recorded on the position's
    own opening row and refuses when there is none, so an unknown is
    never repaired against a guessed level.
    """
    try:
        sweeper = pipeline._sweeper()
        sweep_symbol = (
            sweeper.symbol if sweeper is not None
            else pipeline._retired_cash_park_symbol()
        )
        record_guarded_pass(pipeline, "coverage_book_read.sweep_symbol")
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(pipeline, "coverage_book_read.sweep_symbol", exc)
        sweep_symbol = None
    rows = unverified_book_rows(
        # `getattr`: a half-built pipeline with no `db` must still get
        # the UNKNOWN row, never an exception out of the sweep.
        getattr(pipeline, "db", None), error=read_error,
        skip_symbols=pending_syms,
        sweep_symbol=sweep_symbol,
    )
    logger.critical(
        "STOP-COVERAGE SWEEP COULD NOT READ THE BOOK: %s. Reporting "
        "UNKNOWN, not clean, and re-placing protection on the %d "
        "holding(s) the desk's own ledger knows about.",
        read_error, max(0, len(rows) - 1),
    )
    for row in rows:
        if row.get("held_qty") in (None, 0):
            continue
        try:
            row["repaired"] = pipeline._repair_stop_coverage(
                str(row["symbol"]), abs(float(row["held_qty"])),
                is_short=bool(row.get("is_short")), outcome=row,
            )
            record_guarded_pass(pipeline, "coverage_book_read.repair", context={"symbol": str(row.get("symbol"))})
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(pipeline, "coverage_book_read.repair", exc, context={"symbol": str(row.get("symbol"))})
            row["repaired"] = False
            row["repair_refusal"] = f"the repair itself failed: {exc}"
    try:
        pipeline._alert_owner_unreadable_stop(rows)
    except Exception as exc:  # noqa: BLE001
        logger.error("could not alert on an unreadable book: %s", exc)
    return rows
