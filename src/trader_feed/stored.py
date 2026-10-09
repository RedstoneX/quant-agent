"""On-demand desk status and the stored-report readers/renderers for the dashboard.

Moved verbatim from src/trader_feed.py; see src/trader_feed/__init__.py.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from src.notifier import (
    _actionable_coverage_gaps,
    _attr_or_key,
    _clip_text,
    _DB_PATH as _NOTIFIER_DB_PATH,
    _fmt_signed_money,
    _lookup_company_profiles,
    _margin_interest_lines,
    _new_block,
    _new_section,
    _seal_section,
    company_name,
    describe_ai_cost,
    describe_data_status,
    describe_skipped_decision,
    fmt_time_12h,
    humanize_status,
    format_session_result as _base_format_session_result,
    TelegramNotifier,
)
from src.trading_calendar import SESSION_WINDOWS, et_now, in_session_window, to_et
from src.trader_feed.common import (
    _DB_PATH,
    _SWEEP_SYMBOLS,
    _b,
    _empty_snapshot,
    _number,
    _pnl_section_lines,
    _profiles,
    _status_emoji,
    _ticker_co,
    logger,
)
from src.trader_feed.decision import (
    _append_coverage_gaps,
)
from src.trader_feed.intraday import (
    _ON_DEMAND_COVERAGE_TEXT,
    _ON_DEMAND_PERIOD_LINE,
    _ON_DEMAND_SCANNED_TEXT,
    _format_hourly_desk_check,
    _movers_scanned_text,
    _position_rows_from_broker,
)
from src.trader_feed.dispatch import (
    format_session_result,
)


def format_desk_status(
    account: dict[str, Any],
    positions: Any,
    elapsed_seconds: float = 0.0,
    *,
    trade_count: int | None = None,
) -> str:
    """One desk-status message from live broker truth, rendered by the
    shared top-of-hour formatter.

    `account` is `AlpacaBroker.get_account()`'s dict and `positions` is
    `AlpacaBroker.get_positions()`'s list — both read-only broker calls.
    Raises nothing of its own; the caller decides what a broker failure
    means (see `scripts/desk_status.py`, which refuses to send at all
    rather than send a message with a fabricated P&L).
    """
    total_value = _number(account.get("portfolio_value"))
    last_equity = _number(account.get("last_equity"))
    daily_pnl = None
    daily_return_pct = None
    if total_value is not None and last_equity is not None:
        daily_pnl = total_value - last_equity
        if last_equity > 0:
            daily_return_pct = daily_pnl / last_equity * 100

    result = {
        "status": "ok",
        "daily_pnl": daily_pnl,
        "daily_return_pct": daily_return_pct,
        # No coverage audit was run, so there is no gap list — NOT an
        # empty list meaning "no gaps found". `coverage_text` below is what
        # the reader actually sees.
        "run_id": None,
    }
    snap = _empty_snapshot()
    snap["positions"] = _position_rows_from_broker(positions)
    return _format_hourly_desk_check(
        result,
        None,
        elapsed_seconds,
        snap=snap,
        trade_count=trade_count,
        period_line=_ON_DEMAND_PERIOD_LINE,
        coverage_text=_ON_DEMAND_COVERAGE_TEXT,
        scanned_text=_ON_DEMAND_SCANNED_TEXT,
    )


# === stored evening report, re-rendered (2026-09-18) ===
#
# The evening run's own output is now written to `evening_reports` (see
# `Database.save_evening_report`). This renders one of those rows back
# through `format_session_result`, the SAME entry point the live evening
# push uses, so the owner can read last night's report — or be shown what
# the report looks like — without running any part of the pipeline,
# without a broker call and without a model call.
#
# It holds no message format of its own, exactly like `format_desk_status`
# above: every future change to `_format_evening` reaches this with no
# edit here.
#
# The one thing it must add is honesty about gaps. A stored row can be
# partial (a run that died early, a storage failure, a row written before
# a field existed), and several of the evening formatter's inputs render as
# SILENCE when absent — silence that reads as "nothing was wrong". So the
# pieces whose absence would otherwise be indistinguishable from a clean
# result are named in plain words at the bottom. Nothing is ever defaulted,
# zeroed, estimated or back-filled.
_STORED_EVENING_GAP_WORDS: tuple[tuple[str, str], ...] = (
    ("stop_coverage_gaps", "the stop-coverage audit result"),
    ("stop_proximity", "which holdings were sitting near their stops"),
    ("earnings_proximity", "which holdings had earnings due"),
)


def read_stored_evening(
    date: str | None = None,
    db_path: Any = None,
) -> dict[str, Any] | None:
    """One `evening_reports` row, through a READ-ONLY connection.

    `date` is a trading day key; omit it for the most recent stored night.
    Mirrors `Database.get_evening_report`'s contract and return shape, but
    opens the file `mode=ro` the way `_read_run` does, so the on-demand
    command cannot write to (or create anything in) the live database —
    including the table itself, which is why a not-yet-migrated database
    returns None here rather than being altered.

    Returns None when there is no row, no table, no database file, or the
    stored JSON is unreadable. An unreadable row is an ABSENT report: the
    caller says so and renders nothing.
    """
    path = Path(db_path) if db_path is not None else Path(_DB_PATH)
    if not path.exists():
        return None
    conn = None
    try:
        conn = sqlite3.connect(
            f"file:{path.resolve()}?mode=ro",
            uri=True,
            timeout=1.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=1000")
        if date:
            row = conn.execute(
                "SELECT * FROM evening_reports WHERE date = ?",
                (date,),
            ).fetchone()
        else:
            row = conn.execute("SELECT * FROM evening_reports ORDER BY date DESC LIMIT 1").fetchone()
    except sqlite3.DatabaseError as exc:
        logger.warning("stored evening report read failed: %s", exc)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    if not row:
        return None
    record = dict(row)
    try:
        payload = json.loads(record.get("payload_json") or "")
    except (TypeError, ValueError):
        logger.error(
            "stored evening report for %s has unreadable payload",
            record.get("date"),
        )
        return None
    if not isinstance(payload, dict):
        return None
    positions = None
    if record.get("positions_json"):
        try:
            parsed = json.loads(record["positions_json"])
            positions = parsed if isinstance(parsed, list) else None
        except (TypeError, ValueError):
            positions = None
    return {
        "date": record.get("date"),
        "run_id": record.get("run_id"),
        "timestamp": record.get("timestamp"),
        "payload": payload,
        "positions": positions,
    }


def render_stored_evening(record: dict, elapsed_seconds: float = 0.0) -> str:
    """One stored evening report as a Telegram message.

    `record` is a `Database.get_evening_report` row. Raises ValueError if
    it carries no usable payload — the caller must then say the report is
    unavailable rather than render anything.
    """
    payload = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(payload, dict):
        raise ValueError("stored evening report has no usable payload")

    result = dict(payload)
    gaps: list[str] = []

    # The book must come from the stored snapshot: `_read_run`'s positions
    # query reads the CURRENT table, so letting it answer would print
    # today's holdings under that night's date.
    positions = record.get("positions")
    if isinstance(positions, list):
        result["_positions"] = positions
    else:
        result["_positions"] = []
        gaps.append("the book as it stood that night")

    for key, words in _STORED_EVENING_GAP_WORDS:
        if payload.get(key) is None:
            gaps.append(words)

    body = format_session_result("evening", result, elapsed_seconds)
    if not body:
        raise ValueError("stored evening report could not be rendered")

    date_text = record.get("date") or "date not recorded"
    run_id = record.get("run_id")
    header = [
        _b(f"STORED EVENING REPORT · {date_text}"),
        "   Read back from the stored record. Nothing was run to produce "
        "this: no broker call, no model call, no order.",
    ]
    # No run identifier (owner review, 2026-09-18): it means nothing to him
    # and he does not need it. The date above already says which session
    # this was.
    lines = [*header, "", body]

    if gaps:
        if len(gaps) == 1:
            named = gaps[0]
        else:
            named = ", ".join(gaps[:-1]) + " and " + gaps[-1]
        lines += [
            "",
            _b("NOT AVAILABLE"),
            f"   The stored record does not contain {named}. That is "
            f"missing information, not an empty result — do not read the "
            f"silence above as an all-clear.",
        ]
    return "\n".join(lines)


# === stored morning / midday / close reports (2026-09-18 gap sweep) ===
#
# Same rationale and shape as the evening pair above: `run_morning` and
# `run_position_review` compute `leverage` (the §11.2 gross-ceiling
# snapshot) and `stop_coverage_gaps` (the broker-truth stop audit) from
# live state and hand them to the notifier with no other durable home.
# `Database.save_session_report` now keeps them, keyed by (date, mode).

_STORED_SESSION_GAP_WORDS: tuple[tuple[str, str], ...] = (
    ("stop_coverage_gaps", "the stop-coverage audit result"),
    ("leverage", "the gross-exposure ceiling snapshot"),
)

_STORED_SESSION_LABELS = {
    "morning": "MORNING",
    "midday": "MIDDAY REVIEW",
    "close": "CLOSE REVIEW",
}


def read_stored_session_report(
    mode: str,
    date: str | None = None,
    db_path: Any = None,
) -> dict[str, Any] | None:
    """One `session_reports` row for `mode` ('morning', 'midday', 'close'),
    through a read-only connection. Mirrors `read_stored_evening`'s
    contract exactly — see its docstring for why an unreadable row is
    treated as absent and why this opens the file `mode=ro`.
    """
    path = Path(db_path) if db_path is not None else Path(_DB_PATH)
    if not path.exists():
        return None
    conn = None
    try:
        conn = sqlite3.connect(
            f"file:{path.resolve()}?mode=ro",
            uri=True,
            timeout=1.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=1000")
        if date:
            row = conn.execute(
                "SELECT * FROM session_reports WHERE date = ? AND mode = ?",
                (date, mode),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM session_reports WHERE mode = ? ORDER BY date DESC LIMIT 1",
                (mode,),
            ).fetchone()
    except sqlite3.DatabaseError as exc:
        logger.warning("stored %s report read failed: %s", mode, exc)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    if not row:
        return None
    record = dict(row)
    try:
        payload = json.loads(record.get("payload_json") or "")
    except (TypeError, ValueError):
        logger.error(
            "stored %s report for %s has unreadable payload",
            mode,
            record.get("date"),
        )
        return None
    if not isinstance(payload, dict):
        return None
    positions = None
    if record.get("positions_json"):
        try:
            parsed = json.loads(record["positions_json"])
            positions = parsed if isinstance(parsed, list) else None
        except (TypeError, ValueError):
            positions = None
    return {
        "date": record.get("date"),
        "mode": record.get("mode") or mode,
        "run_id": record.get("run_id"),
        "timestamp": record.get("timestamp"),
        "payload": payload,
        "positions": positions,
    }


def render_stored_session_report(
    mode: str,
    record: dict,
    elapsed_seconds: float = 0.0,
) -> str:
    """One stored morning/midday/close report as a Telegram message.

    `record` is a `read_stored_session_report`/`Database.get_session_report`
    row. Raises ValueError if it carries no usable payload.
    """
    if mode not in ("morning", "midday", "close"):
        raise ValueError(f"render_stored_session_report: unknown mode {mode!r}")
    payload = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(payload, dict):
        raise ValueError(f"stored {mode} report has no usable payload")

    result = dict(payload)
    gaps: list[str] = []

    # The book must come from the stored snapshot — see the identical
    # reasoning in `render_stored_evening`.
    positions = record.get("positions")
    if isinstance(positions, list):
        result["_positions"] = positions
    else:
        result["_positions"] = []
        gaps.append("the book as it stood at the end of that session")

    for key, words in _STORED_SESSION_GAP_WORDS:
        if payload.get(key) is None:
            gaps.append(words)

    body = format_session_result(mode, result, elapsed_seconds)
    if not body:
        raise ValueError(f"stored {mode} report could not be rendered")

    date_text = record.get("date") or "date not recorded"
    run_id = record.get("run_id")
    label = _STORED_SESSION_LABELS.get(mode, mode.upper())
    header = [
        _b(f"STORED {label} REPORT · {date_text}"),
        "   Read back from the stored record. Nothing was run to produce "
        "this: no broker call, no model call, no order.",
    ]
    # No run identifier (owner review, 2026-09-18): it means nothing to him
    # and he does not need it. The date above already says which session
    # this was.
    lines = [*header, "", body]

    if gaps:
        if len(gaps) == 1:
            named = gaps[0]
        else:
            named = ", ".join(gaps[:-1]) + " and " + gaps[-1]
        lines += [
            "",
            _b("NOT AVAILABLE"),
            f"   The stored record does not contain {named}. That is "
            f"missing information, not an empty result — do not read the "
            f"silence above as an all-clear.",
        ]
    return "\n".join(lines)


# === stored intra_check ticks (2026-09-18 gap sweep) ===
#
# intra_check fires roughly every 30 minutes, not once/day, so there is
# no single "the" report for a date the way evening/morning/midday/close
# have one. `Database.save_intra_check_report` keeps every tick, keyed by
# run_id.


def read_stored_intra_check(
    run_id: str | None = None,
    date: str | None = None,
    db_path: Any = None,
) -> dict[str, Any] | None:
    """One `intra_check_reports` row, through a read-only connection.

    `run_id` selects a specific tick. Otherwise `date` (or, absent that,
    the most recent row overall) returns the latest tick recorded for
    that day.
    """
    path = Path(db_path) if db_path is not None else Path(_DB_PATH)
    if not path.exists():
        return None
    conn = None
    try:
        conn = sqlite3.connect(
            f"file:{path.resolve()}?mode=ro",
            uri=True,
            timeout=1.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=1000")
        if run_id:
            row = conn.execute(
                "SELECT * FROM intra_check_reports WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        elif date:
            row = conn.execute(
                "SELECT * FROM intra_check_reports WHERE date = ? ORDER BY timestamp DESC LIMIT 1",
                (date,),
            ).fetchone()
        else:
            row = conn.execute("SELECT * FROM intra_check_reports ORDER BY timestamp DESC LIMIT 1").fetchone()
    except sqlite3.DatabaseError as exc:
        logger.warning("stored intra_check report read failed: %s", exc)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    if not row:
        return None
    record = dict(row)
    try:
        payload = json.loads(record.get("payload_json") or "")
    except (TypeError, ValueError):
        logger.error(
            "stored intra_check report for %s has unreadable payload",
            record.get("run_id"),
        )
        return None
    if not isinstance(payload, dict):
        return None
    positions = None
    if record.get("positions_json"):
        try:
            parsed = json.loads(record["positions_json"])
            positions = parsed if isinstance(parsed, list) else None
        except (TypeError, ValueError):
            positions = None
    return {
        "date": record.get("date"),
        "run_id": record.get("run_id"),
        "timestamp": record.get("timestamp"),
        "payload": payload,
        "positions": positions,
    }


def render_stored_intra_check(record: dict, elapsed_seconds: float = 0.0) -> str:
    """One stored intra_check tick, rendered honestly.

    Deliberately NOT `format_session_result("intra_check", ...)`. That
    formatter decides whether to speak at all from the CURRENT wall clock
    (`_is_hourly_checkpoint`, `_is_midday_collision_tick`) and from a live
    "last hour" evidence window (`_read_hour_evidence`/
    `_read_hour_trade_count`) measured from now, not from the tick's own
    time. Reusing it to replay an old tick would either render nothing
    (today's clock says "not a checkpoint minute") or silently substitute
    TODAY's last-hour activity for that tick's — both worse than a
    purpose-built read of exactly what this tick itself recorded.

    Renders the tick's own status, P&L, stop-coverage finding, book and
    intraday-scan outcome straight from the stored payload. This is not a
    byte-for-byte reproduction of whatever Telegram message (if any) that
    tick produced live — it is the tick's own durable record.
    """
    payload = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(payload, dict):
        raise ValueError("stored intra_check report has no usable payload")

    result = dict(payload)
    gaps: list[str] = []

    positions = record.get("positions")
    if isinstance(positions, list):
        result["_positions"] = positions
    else:
        result["_positions"] = []
        gaps.append("the book as it stood at this tick")

    if payload.get("stop_coverage_gaps") is None:
        gaps.append("the stop-coverage audit result")

    status = str(payload.get("status", "unknown"))
    date_text = record.get("date") or "date not recorded"
    run_id = record.get("run_id")
    lines = [
        _b(f"STORED HALF-HOURLY CHECK · {date_text}"),
        "   Read back from the stored record. Nothing was run to produce "
        "this: no broker call, no model call, no order. This tick's own "
        "status only — not a re-derivation of the hourly DESK CHECK "
        "message, which also reflects other ticks around it.",
    ]
    # P&L FIRST, directly under the heading block — owner, 2026-09-18. It
    # used to follow the status line; the status of a replayed tick is
    # desk detail, the P&L is his money.
    _new_section(lines, *_pnl_section_lines(result))
    _new_section(lines, f"{_status_emoji(status)} {humanize_status(status)}")
    _new_block(lines, _append_coverage_gaps, result)
    risk_positions = [
        row
        for row in result["_positions"]
        if isinstance(row, dict) and str(row.get("symbol", "")).upper() not in _SWEEP_SYMBOLS
    ]
    profiles = _profiles(risk_positions)
    lines.append(f"💼 Positions held: {len(risk_positions)}")
    for row in risk_positions:
        lines.append(f"   • {_ticker_co(str(row.get('symbol', '?')), profiles)}")
    scan = payload.get("intraday_scan")
    if isinstance(scan, dict):
        lines.append(f"🔎 Movers scanned: {_movers_scanned_text(scan)}")

    if gaps:
        if len(gaps) == 1:
            named = gaps[0]
        else:
            named = ", ".join(gaps[:-1]) + " and " + gaps[-1]
        lines += [
            "",
            _b("NOT AVAILABLE"),
            f"   The stored record does not contain {named}. That is "
            f"missing information, not an empty result — do not read the "
            f"silence above as an all-clear.",
        ]
    return "\n".join(lines)
