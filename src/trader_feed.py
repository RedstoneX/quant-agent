"""Trader-oriented Telegram presentation for QAMC.

Observability only: this module never calls the broker, never mutates the
trading database, and never changes a trading decision. It reads already
persisted, validated evidence and turns it into a phone-friendly trader feed.

Non-trading modes and hard failure/skip statuses fall back to the established
formatter unchanged.
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
    _clip_text,
    _DB_PATH as _NOTIFIER_DB_PATH,
    _fmt_signed_money,
    _lookup_company_profiles,
    _new_block,
    _new_section,
    _seal_section,
    company_name,
    fmt_time_12h,
    format_session_result as _base_format_session_result,
    TelegramNotifier,
)
from src.trading_calendar import et_now

logger = logging.getLogger(__name__)

_DB_PATH = _NOTIFIER_DB_PATH
_SWEEP_SYMBOLS = frozenset({"SGOV", "BIL"})
_BASE_ONLY_STATUSES = frozenset(
    {
        "market_holiday", "early_close", "broker_error", "analysis_error",
        "fetch_error",
        # Guard 1 (2026-09-02): the kill switch's early check returns before
        # any of the rich per-mode data (orders/positions/trades) exists to
        # render, same shape-mismatch reason "broker_error" is here. Routes
        # to the base notifier.py formatter, which renders a plain
        # "status: kill_switch_halted" line and — critically — is NOT
        # silenced for intra_check the way "ok"/"market_holiday" are.
        "kill_switch_halted",
    }
)
# 2026-08-31 visibility fix (src/pipeline.py's `_run_intraday_opportunity_scan`
# / `_intraday_opportunity_scan_body`): these three now attach an explicit
# `result["intraday_scan"]["status"]` dict where a "never engaged a real
# candidate" tick used to leave no `intraday_scan` key at all. They stay off
# the Telegram feed on purpose — same as the old no-key ticks, per the
# "ordinary ~30-minute OK ticks are silent" policy below — because nothing
# about them needs an operator's attention: disabled-by-config and
# lock-contention are routine scheduling noise, and "no opportunity" means
# the scan ran and correctly found nothing. Only a real candidate engaged
# (intraday_no_trades/intraday_executed) or a genuine problem (crashed/
# suspended/analysis_error) is worth a message.
_INTRADAY_SILENT_STATUSES = frozenset({
    "intraday_scan_disabled", "intraday_scan_lock_contended",
    "intraday_scan_no_opportunity",
})


def format_session_result(
    mode: str,
    result: dict | None,
    elapsed_seconds: float,
    error: BaseException | None = None,
) -> str | None:
    """Build the Telegram message without affecting trading.

    Enrichment is intentionally fail-soft: if the read-only evidence lookup or
    formatter has any problem, the existing notifier format is used instead.
    """
    if error is not None or not isinstance(result, dict):
        return _base_format_session_result(mode, result, elapsed_seconds, error=error)

    status = str(result.get("status", "unknown"))
    if status in _BASE_ONLY_STATUSES or status.startswith("pm_") or status == "paid_analysis_suspended":
        return _base_format_session_result(mode, result, elapsed_seconds, error=None)

    try:
        if mode == "intra_check":
            return _format_intra_check(result, elapsed_seconds)

        if mode in ("midday", "close"):
            return _format_position_review(mode, result, elapsed_seconds)

        if mode in ("morning", "once"):
            return _format_decision_session(mode, result, elapsed_seconds)

        # Evening is already rich; earnings/meta/daily have special noise policy.
        return _base_format_session_result(mode, result, elapsed_seconds, error=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("trader-feed enrichment failed for %s: %s", mode, exc)
        return _base_format_session_result(mode, result, elapsed_seconds, error=None)


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60)}s"


def _clip(value: Any, limit: int = 140) -> str:
    """Collapse whitespace/newlines to one line, then clip via the shared,
    boundary-aware `_clip_text` (src/notifier.py) instead of a raw
    `text[:limit]` slice.

    This used to hard-cut mid-word — the operator's actual complaint was a
    BUY CRM alert whose PM rationale read "...strong heavy accumulation
    volume" and just stopped there, an artifact of `_append_pm` calling
    this with `limit=105` on LLM prose that routinely runs 300-500+ chars.
    Callers below have also had their limits raised substantially (this
    formatter renders several such bullets per message, well inside
    Telegram's real 4096-char budget)."""
    text = " ".join(str(value or "").split())
    return _clip_text(text, limit, marker="…")


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _status_emoji(status: str) -> str:
    if status in {"executed", "intraday_executed"}:
        return "🟢"
    if status in {"reviewed", "intraday_no_trades", "no_trades", "ok"}:
        return "🔵"
    if status in {"rejected", "hard_risk_block", "symbol_block", "buys_unfunded"}:
        return "🟡"
    if "error" in status or status in {
        "failed", "emergency_sold",
        # docs/WORK.md item 32 (2026-09-14): the daily-loss breaker's status.
        # "emergency_sold" is kept alongside it so historical runs still
        # render; nothing emits it any more.
        "daily_loss_halted",
        "kill_switch_halted",
    }:
        return "🔴"
    return "⚪"


# === Scan-first markup helpers (2026-09-17 redesign) ===
#
# Owner feedback on the pre-redesign messages: "unless I read everything
# word for word, I have no idea what was actually really done and what
# just failed or was killed." Every formatter below now leads with a
# plain-English outcome word and three short, structured sections — DONE /
# BLOCKED-FAILED / LOOKED AT, NO TRADE (HELD / WATCH for a position review)
# — built from the SAME underlying evidence the old wall-of-text used, so
# nothing is newly invented; it is re-shaped. The full per-stock reasoning
# the owner said he is happy with is kept, verbatim, inside a collapsed
# `<b>DETAILS</b>` / `<blockquote expandable>` block (Telegram Bot API HTML
# style, https://core.telegram.org/bots/api#html-style, documents
# `<blockquote>` and `<blockquote expandable>` as supported parse_mode=HTML
# tags) — one tap opens it. `<b>`/`<blockquote expandable>` are literal
# tags in the plain text this module returns; see
# `src.notifier._escape_with_markup` for how they survive `html.escape`
# without any other text being able to inject one.


def _b(text: str) -> str:
    """A literal `<b>...</b>` section header — see the module-level note
    above. Never build a `<b>` tag around text some other way; the escape
    step in src/notifier.py only preserves this exact fixed string."""
    return f"<b>{text}</b>"


def _ticker_co(symbol: str, profiles: dict[str, Any] | None) -> str:
    """'SYM (Company Name)', or bare 'SYM' when the company cache doesn't
    know it — never a broken "(?)" placeholder, same posture as
    `src.notifier._append_company_identities`."""
    symbol = str(symbol or "?").upper()
    name = company_name(symbol, profiles) if profiles is not None else None
    return f"{symbol} ({name})" if name else symbol


def _all_symbols(*groups: Any) -> list[str]:
    """First-seen-order, deduped, upper-cased union of every symbol across
    `groups` (each a list of dicts with a 'symbol' key, or a plain list of
    ticker strings) — one CompanyProfileStore batch fetch per message
    instead of one per section."""
    seen: list[str] = []
    for group in groups:
        for item in group or []:
            raw = item.get("symbol") if isinstance(item, dict) else item
            sym = str(raw or "").strip().upper()
            if sym and sym not in seen:
                seen.append(sym)
    return seen


# Plain-English "who actually stopped it" for an execution-skip `reason`
# code (src/pipeline_stages.py's `_record_execution_skip` call sites) — the
# operator asked to be told desk safety check / risk manager / broker /
# not-filled-in-time, not an internal snake_case reason code, and the desk
# guards must say so explicitly — the whole point of this wording is that
# it is NOT the broker. Each value is a complete phrase ready to sit before
# a colon (`_append_blocked` renders "{who}: {reason}"). Risk-manager
# refusals and "not filled in time" are classified separately (they don't
# come through `execution_skips` — see `_blocked_rows`).
_SKIP_WHO_LABELS: dict[str, str] = {
    "fat_finger_guard": "Blocked by desk safety check (not the broker)",
    "unusable_stop": "Blocked by desk safety check — unusable stop (not the broker)",
    "kill_switch_halted": "Blocked by desk safety check — kill switch (not the broker)",
    "broker_rejected": "Blocked by the broker",
    "daily_loss_recheck": "Blocked by desk safety check — daily-loss breaker",
    "insufficient_cash": "Blocked by the desk — insufficient cash",
    "below_min_notional": "Blocked by the desk — order too small",
    "no_price": "Blocked by the desk — no verifiable price",
    "stale_entry": "Blocked by the desk — price moved since the decision",
    "qty_zero": "Blocked by the desk — sizing rounds to zero",
    "latency_window": "Blocked by the desk — too slow (latency)",
    "slippage_gated": "Blocked by the desk — price ran past the slippage limit",
    "borrow_gate": "Blocked by the desk — short not available to borrow",
    "short_add_blocked": "Blocked by the desk — adding to a short isn't supported",
    "rotation_room_not_freed": "Blocked by the desk — no rotation room freed",
}


def _skip_who(reason: str) -> str:
    return _SKIP_WHO_LABELS.get(str(reason or ""), "Blocked by the desk")


def _decision_action_for(symbol: str, snap: dict[str, Any]) -> str:
    """The PM/constructor's own action word for `symbol` this run (BUY /
    SELL / SHORT / COVER / HOLD / ...), so a BLOCKED/FAILED line can say
    "SHORT FLNC" rather than a bare "? FLNC"."""
    for row in snap.get("pm_orders") or []:
        if isinstance(row, dict) and str(row.get("symbol", "")).upper() == symbol:
            return str(row.get("action", "?")).upper()
    return "?"


def _symbol_stop(symbol: str, snap: dict[str, Any]) -> float | None:
    """The stop QAMC actually intends for `symbol` — the constructor's own
    `TradeDecision.stop_loss`, overridden by a risk-manager modification of
    the SAME field when one exists (Risk can retune a symbol's stop without
    the constructor knowing — see `RiskModification`). Not the broker's
    fill data: `trades` carries no stop column at all."""
    stop: float | None = None
    for row in snap.get("pm_orders") or []:
        if isinstance(row, dict) and str(row.get("symbol", "")).upper() == symbol:
            stop = _number(row.get("stop_loss"))
            break
    for row in snap.get("risk_mods") or []:
        if (
            isinstance(row, dict)
            and str(row.get("symbol", "")).upper() == symbol
            and str(row.get("field", "")).lower() == "stop_loss"
        ):
            new_value = _number(row.get("new_value"))
            if new_value is not None:
                stop = new_value
    return stop


def _trade_reached_broker(fill_status: Any) -> bool:
    """True once an order is live at the broker — filled, or still working
    ('submitted'/'pending_submit'). False for every terminal-fail status
    (`canceled`, `expired`, `rejected`, `submit_failed`, ...): a `trades`
    row exists, but nothing is protecting the operator's capital."""
    return str(fill_status or "").lower() in {"filled", "submitted", "pending_submit"}


def _classify_trades(snap: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """Real (non-HOLD, non-sweep) trades split into (reached_broker,
    did_not_reach) — see `_trade_reached_broker`."""
    _, real = _execution_rows(snap)
    reached: list[dict] = []
    stalled: list[dict] = []
    for row in real:
        (reached if _trade_reached_broker(row.get("fill_status")) else stalled).append(row)
    return reached, stalled


def _done_rows(snap: dict[str, Any]) -> list[dict]:
    reached, _ = _classify_trades(snap)
    return reached


def _blocked_rows(result: dict, snap: dict[str, Any]) -> list[dict]:
    """Everything that did NOT make it, one row per symbol, each carrying
    who actually stopped it and why — see NEW LAYOUT item 4. Three sources,
    in priority order (a symbol is never listed twice): the execution
    skips QAMC's own deterministic gates recorded; symbols the risk manager
    refused outright (`RiskVerdict.rejected_symbols`, Phase 10.1 — a
    per-symbol refusal distinct from the whole-plan `approved` bool); and
    a real trade row that reached neither a fill nor a live working order
    (a DAY order that expired unfilled, a submit that ultimately failed).
    """
    rows: list[dict] = []
    seen: set[str] = set()

    skips = [row for row in (result.get("execution_skips") or []) if isinstance(row, dict)]
    if not skips:
        skips = [row for row in (snap.get("skips") or []) if isinstance(row, dict)]
    for row in skips:
        symbol = str(row.get("symbol", "?")).upper()
        if symbol in seen:
            continue
        rows.append({
            "symbol": symbol,
            "action": _decision_action_for(symbol, snap),
            "who": _skip_who(row.get("reason", "")),
            "reason": row.get("detail") or row.get("reason") or "blocked",
        })
        seen.add(symbol)

    risk = snap.get("risk")
    if isinstance(risk, dict):
        for row in risk.get("rejected_symbols") or []:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol", "?")).upper()
            if symbol in seen:
                continue
            rows.append({
                "symbol": symbol,
                "action": _decision_action_for(symbol, snap),
                "who": "Blocked by risk manager",
                "reason": row.get("reason") or "refused without a stated reason",
            })
            seen.add(symbol)

    _, stalled = _classify_trades(snap)
    for row in stalled:
        symbol = str(row.get("symbol", "?")).upper()
        if symbol in seen:
            continue
        status = str(row.get("fill_status") or "unknown")
        rows.append({
            "symbol": symbol,
            "action": str(row.get("action", "?")).upper(),
            "who": "Not filled in time",
            "reason": f"order never became a live fill (status: {status})",
        })
        seen.add(symbol)

    return rows


def _looked_at_rows(
    snap: dict[str, Any], candidates: list[str] | None, acted_symbols: set[str],
) -> list[dict]:
    """Analyzed signals that were neither done nor blocked — the PM/
    constructor's silent "pass". Addresses the 5-analyzed/5-actionable
    defect: the header used to claim more than the message ever said what
    happened to."""
    rows = []
    for row in _signal_rows(snap, candidates):
        symbol = str(row.get("symbol", "")).upper()
        if not symbol or symbol in acted_symbols:
            continue
        rows.append(row)
    return rows


def _outcome_word(status: str, done_count: int, blocked_count: int) -> str:
    """ONE plain word for the header line — TRADED / NO CHANGE / FAILED /
    PARTIAL — computed from the SAME counts the sections below render, so
    the header can never claim something the body doesn't show."""
    if _status_emoji(status) == "🔴":
        return "FAILED"
    if done_count and blocked_count:
        return "PARTIAL"
    if done_count:
        return "TRADED"
    if blocked_count:
        return "FAILED"
    return "NO CHANGE"


# Reserved characters, not a trading number: `_wrap_details` must guarantee
# its own output fits inside `TelegramNotifier.MAX_MESSAGE_CHARS` so the
# tag-blind emergency clip in `notifier._build_payload` never has to run
# for an ordinary alert (that fallback can only cut on a text/entity
# boundary, not a `<blockquote expandable>` boundary). Covers the Mission
# Control `<a href>` link `send()` appends after this text, plus slack for
# `html.escape` expanding a handful of '&'/'"'/"'" characters in PM/risk
# prose — this module never learns the real escaped length, only text.py's
# `_build_payload` does.
_DETAILS_SAFETY_RESERVE_CHARS = 250


def _wrap_details(lines: list[str], detail_lines: list[str]) -> None:
    """Append the full, unabridged per-stock reasoning as a collapsed
    `<b>DETAILS</b>` / `<blockquote expandable>` block — NEW LAYOUT item 7.
    Content is unchanged from the pre-redesign message; only its
    presentation (collapsed, tapped open) is new. Sized to fit the
    remaining Telegram budget so a clip, if one is needed, lands inside
    DETAILS and never inside the scan-first sections above it.
    """
    text = "\n".join(line for line in detail_lines if line is not None).strip("\n")
    if not text:
        return
    wrapper_overhead = len("<b>DETAILS</b>\n<blockquote expandable></blockquote>")
    used = len("\n".join(lines))
    budget = (
        TelegramNotifier.MAX_MESSAGE_CHARS - used - wrapper_overhead
        - _DETAILS_SAFETY_RESERVE_CHARS
    )
    budget = max(0, budget)
    if len(text) > budget:
        text = _clip_text(text, budget, marker="\n[details truncated — see Mission Control]")
    start = len(lines)
    lines.append(_b("DETAILS"))
    lines.append(f"<blockquote expandable>{text}</blockquote>")
    _seal_section(lines, start)


def _empty_snapshot() -> dict[str, Any]:
    return {
        "macro": None,
        "tech": [],
        "pm_reasoning": None,
        "pm_targets": [],
        "pm_orders": [],
        "risk": None,
        "risk_mods": [],
        "skips": [],
        "trades": [],
        "positions": [],
        "agent_summaries": {},
        "cost": None,
        "calls": 0,
    }


def _read_run(run_id: str | None) -> dict[str, Any]:
    """Read forensic state for one run through a SQLite read-only connection."""
    snapshot = _empty_snapshot()
    if not run_id or run_id == "?" or not Path(_DB_PATH).exists():
        return snapshot

    conn = None
    try:
        uri = f"file:{Path(_DB_PATH).resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=1000")

        try:
            rows = conn.execute(
                "SELECT agent_name, kind, symbol, evidence_json "
                "FROM specialist_evidence WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
            for row in rows:
                try:
                    data = json.loads(row["evidence_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                agent = row["agent_name"]
                kind = row["kind"]
                if agent == "macro_analyst" and kind == "analysis":
                    snapshot["macro"] = data
                elif agent == "tech_analyst" and kind == "analysis":
                    snapshot["tech"].append(data)
                elif agent == "portfolio_manager" and kind == "reasoning":
                    snapshot["pm_reasoning"] = data
                elif agent == "portfolio_manager" and kind == "target":
                    snapshot["pm_targets"].append(data)
                elif agent == "portfolio_manager" and kind == "proposed_order":
                    snapshot["pm_orders"].append(data)
                elif agent == "risk_manager" and kind == "verdict":
                    snapshot["risk"] = data
                elif agent == "risk_manager" and kind == "modification":
                    snapshot["risk_mods"].append(data)
                elif agent == "execution" and kind == "execution_skip":
                    snapshot["skips"].append(data)
        except sqlite3.DatabaseError:
            pass

        try:
            rows = conn.execute(
                "SELECT symbol, action, qty, price, reasoning, fill_status, "
                "fill_qty, fill_price FROM trades WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
            snapshot["trades"] = [dict(row) for row in rows]
        except sqlite3.DatabaseError:
            pass

        try:
            rows = conn.execute(
                "SELECT symbol, qty, avg_entry, current_price, market_value, "
                # qty != 0: shorts have a negative qty and must not be
                # invisible in the trader feed.
                "unrealized_pnl FROM positions WHERE qty != 0 "
                "ORDER BY ABS(market_value) DESC",
            ).fetchall()
            snapshot["positions"] = [dict(row) for row in rows]
        except sqlite3.DatabaseError:
            pass

        try:
            try:
                rows = conn.execute(
                    "SELECT agent_name, output_summary, cost_usd, provider_requests "
                    "FROM agent_logs WHERE run_id = ? ORDER BY id",
                    (run_id,),
                ).fetchall()
                snapshot["calls"] = sum(
                    (1 if row["provider_requests"] is None
                     else max(0, int(row["provider_requests"]))) for row in rows
                )
            except sqlite3.DatabaseError:
                rows = conn.execute(
                    "SELECT agent_name, output_summary, cost_usd FROM agent_logs "
                    "WHERE run_id = ? ORDER BY id",
                    (run_id,),
                ).fetchall()
                snapshot["calls"] = len(rows)
            for row in rows:
                snapshot["agent_summaries"][row["agent_name"]] = row["output_summary"]
            if rows and all(row["cost_usd"] is not None for row in rows):
                snapshot["cost"] = sum(float(row["cost_usd"]) for row in rows)
        except sqlite3.DatabaseError:
            pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("trader-feed DB read failed for %s: %s", run_id, exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return snapshot


def extract_alert_symbols(run_id: str | None, result: dict | None) -> list[str]:
    """Every symbol worth making tappable in this alert, first-seen order,
    capped (see `TelegramNotifier._MAX_LINKED_SYMBOLS` in src/notifier.py).

    Deliberately narrow: pulled only from the SAME structured fields the
    renderers above already iterate for a `symbol` key (PM proposed orders,
    executed trades, execution skips, stop-coverage gaps, the midday/close
    reviewer's `result["review"]["actions"]` — including a HOLD or a
    decided-but-unexecuted action that never became a broker trade — and the
    top-level `result["orders"]` the base formatter's own
    `_append_company_identities` uses) — never a scan of the free-text PM/
    risk rationale, which routinely contains capitalized words ("ALL",
    "GO", "PASS") that would false-positive as tickers.

    Read-only and fail-soft like the rest of this module: a symbol that
    can't be determined is just not linked — see
    `TelegramNotifier._linkify_symbols` for why a bad/missing symbol must
    never be able to break message delivery.
    """
    symbols: list[str] = []

    def _add(raw: Any) -> None:
        sym = str(raw or "").strip().upper()
        if sym and sym not in symbols:
            symbols.append(sym)

    if isinstance(result, dict):
        for row in result.get("orders") or []:
            if isinstance(row, dict):
                _add(row.get("symbol"))
        for row in result.get("stop_coverage_gaps") or []:
            if isinstance(row, dict):
                _add(row.get("symbol"))
        review = result.get("review")
        if isinstance(review, dict):
            for row in review.get("actions") or []:
                if isinstance(row, dict):
                    _add(row.get("symbol"))

    try:
        snap = _read_run(run_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("extract_alert_symbols: run read failed for %s: %s", run_id, exc)
        snap = _empty_snapshot()

    for key in ("pm_orders", "trades", "skips"):
        for row in snap.get(key) or []:
            if isinstance(row, dict):
                _add(row.get("symbol"))

    return symbols[:10]


def _append_market(lines: list[str], snap: dict[str, Any]) -> None:
    macro = snap.get("macro")
    if not isinstance(macro, dict):
        return
    bits = [
        str(value)
        for value in (macro.get("regime"), macro.get("equity_outlook"), macro.get("confidence"))
        if value
    ]
    guidance = macro.get("position_guidance") or {}
    target = guidance.get("target_invested_pct") if isinstance(guidance, dict) else None
    text = " / ".join(bits)
    if isinstance(target, (int, float)):
        text += f" · target {target:g}% invested"
    if text:
        lines.append(f"📊 Market: {text}")


def _append_book(lines: list[str], snap: dict[str, Any]) -> None:
    positions = [row for row in (snap.get("positions") or []) if isinstance(row, dict)]
    if not positions:
        return
    risk_rows = [row for row in positions if str(row.get("symbol", "")).upper() not in _SWEEP_SYMBOLS]
    sweep_rows = [row for row in positions if str(row.get("symbol", "")).upper() in _SWEEP_SYMBOLS]
    invested = sum(_number(row.get("market_value")) or 0.0 for row in risk_rows)
    parked = sum(_number(row.get("market_value")) or 0.0 for row in sweep_rows)
    text = f"💼 Book: {len(risk_rows)} risk pos · ${invested:,.0f} invested"
    if parked > 0:
        text += f" · ${parked:,.0f} T-bills"
    lines.append(text)


def _signal_rows(
    snap: dict[str, Any], candidates: list[str] | None = None,
) -> list[dict]:
    """The tech rows a signals listing renders as bullets — filtered to
    `candidates` when given, priority-ordered — shared by `_append_signals`
    (the live per-run listing) and the hourly desk-check summary so both
    name exactly the symbols they actually show, never more and never a
    second, divergent ordering.

    Uncapped (2026-09-17): this used to cut off at 4 rows while the header
    line right above it ("N analyzed") kept the true count — a live
    message read "5 analyzed" and then listed only 4, silently dropping
    the 5th (SOXX). The header must never claim more than the bullets
    beneath it actually show.
    """
    tech = [row for row in (snap.get("tech") or []) if isinstance(row, dict)]
    if candidates:
        wanted = {str(symbol).upper() for symbol in candidates}
        tech = [row for row in tech if str(row.get("symbol", "")).upper() in wanted]
    priority = {"strong_buy": 0, "strong_sell": 0, "buy": 1, "sell": 1, "neutral": 2}
    return sorted(
        tech,
        key=lambda row: (
            priority.get(str(row.get("rating", "")).lower(), 3),
            str(row.get("symbol", "")),
        ),
    )


def _signal_row_line(row: dict, profiles: dict[str, Any] | None = None) -> str:
    """One '   • SYM: RATING/conviction · R/R x.xx — reason' bullet, the
    one place that renders a tech-analysis row this way — shared by
    `_append_signals` (unchanged, default `profiles=None`: this text is
    the "full existing per-stock reasons... unchanged in content" DETAILS
    content) and the hourly desk-check summary, which passes `profiles` to
    put the company name inline since it has no separate DONE/LOOKED-AT
    section to have already introduced it."""
    sym = str(row.get("symbol", "?")).upper()
    label = _ticker_co(sym, profiles) if profiles is not None else sym
    rating = str(row.get("rating", "?")).upper()
    conviction = str(row.get("conviction", "?")).lower()
    rr = row.get("risk_reward")
    rr_text = f" · R/R {rr:g}" if isinstance(rr, (int, float)) else ""
    reason = _clip(row.get("reasoning"), 420)
    text = f"   • {label}: {rating}/{conviction}{rr_text}"
    if reason:
        text += f" — {reason}"
    return text


def _append_signals(
    lines: list[str],
    snap: dict[str, Any],
    candidates: list[str] | None = None,
) -> None:
    tech = [row for row in (snap.get("tech") or []) if isinstance(row, dict)]
    if candidates:
        wanted = {str(symbol).upper() for symbol in candidates}
        tech = [row for row in tech if str(row.get("symbol", "")).upper() in wanted]
    if not tech:
        if candidates:
            lines.append(f"🔎 Triggered: {', '.join(candidates[:5])}")
        return

    actionable = [
        row for row in tech
        if str(row.get("rating", "")).lower() not in ("", "neutral")
    ]
    lines.append(f"🔎 Signals: {len(tech)} analyzed · {len(actionable)} actionable")
    for row in _signal_rows(snap, candidates):
        lines.append(_signal_row_line(row))


def _append_pm(lines: list[str], snap: dict[str, Any]) -> None:
    reasoning = snap.get("pm_reasoning")
    orders = [row for row in (snap.get("pm_orders") or []) if isinstance(row, dict)]
    targets = [row for row in (snap.get("pm_targets") or []) if isinstance(row, dict)]
    pm_summary = (snap.get("agent_summaries") or {}).get("portfolio_manager")
    if not (reasoning or orders or targets or pm_summary):
        return

    actionable = [row for row in orders if str(row.get("action", "")).upper() != "HOLD"]
    holds = [row for row in orders if str(row.get("action", "")).upper() == "HOLD"]
    if orders:
        lines.append(f"🧠 PM/Constructor: {len(actionable)} change(s) · {len(holds)} hold(s)")
        for row in actionable[:4]:
            action = str(row.get("action", "?")).upper()
            symbol = str(row.get("symbol", "?")).upper()
            allocation = row.get("allocation_pct")
            alloc_text = f" {allocation:g}%" if isinstance(allocation, (int, float)) else ""
            reason = _clip(row.get("reasoning"), 420)
            text = f"   • {action} {symbol}{alloc_text}"
            if reason:
                text += f" — {reason}"
            lines.append(text)
        for row in holds[:2]:
            symbol = str(row.get("symbol", "?")).upper()
            reason = _clip(row.get("reasoning"), 420)
            text = f"   • PASS {symbol}"
            if reason:
                text += f" — {reason}"
            lines.append(text)
    elif targets:
        lines.append(f"🧠 PM: {len(targets)} target(s), no constructed order evidence")

    # Labelled "PM view (this check)" rather than a bare "View:" — this text
    # is the PM's own prose verbatim (never reworded here) and on an
    # intraday tick it can say something like "no trades today", which read
    # as a bare "View:" is easy to mistake for the whole day's verdict
    # rather than what it actually is: this one check's reasoning.
    portfolio_view = reasoning.get("portfolio_view") if isinstance(reasoning, dict) else None
    if portfolio_view:
        lines.append(f"   PM view (this check): {_clip(portfolio_view, 550)}")
    elif pm_summary and str(pm_summary).lower() != "no trades":
        lines.append(f"   PM view (this check): {_clip(pm_summary, 550)}")


def _append_risk(lines: list[str], snap: dict[str, Any]) -> None:
    risk = snap.get("risk")
    if not isinstance(risk, dict):
        return
    approved = risk.get("approved")
    label = "APPROVED" if approved is True else "REJECTED" if approved is False else "UNKNOWN"
    category = risk.get("reason_category") or "?"
    scale = risk.get("scale_all_buys")
    scale_text = f" · buy size {scale * 100:.0f}%" if isinstance(scale, (int, float)) else ""
    mods = snap.get("risk_mods") or []
    # Phase 10.1: a verdict can now be APPROVED overall and still have refused
    # individual names. Reading only `approved` would show that run as a clean
    # approval and never mention the trade that died.
    rejected = risk.get("rejected_symbols") or []
    rej_syms = sorted({
        str(r.get("symbol")) for r in rejected if isinstance(r, dict) and r.get("symbol")
    }) if isinstance(rejected, list) else []
    refused_text = f" · refused {', '.join(rej_syms)}" if rej_syms else ""
    lines.append(
        f"🛡️ Risk: {label} · {category}{scale_text} · {len(mods)} mod(s){refused_text}"
    )
    reason = _clip(risk.get("reasoning"), 550)
    if reason:
        lines.append(f"   {reason}")


def _execution_rows(snap: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    trades = [row for row in (snap.get("trades") or []) if isinstance(row, dict)]
    sweep = [row for row in trades if str(row.get("action", "")).upper().startswith("SWEEP_")]
    real = [
        row for row in trades
        if str(row.get("action", "")).upper() != "HOLD"
        and not str(row.get("action", "")).upper().startswith("SWEEP_")
    ]
    return sweep, real


def _append_gate_and_execution(
    lines: list[str],
    result: dict,
    snap: dict[str, Any],
    *,
    explain_no_trade: bool = True,
) -> None:
    status = str(result.get("status", "unknown"))
    skips = [row for row in (result.get("execution_skips") or []) if isinstance(row, dict)]
    if not skips:
        skips = [row for row in (snap.get("skips") or []) if isinstance(row, dict)]

    if status in {"hard_risk_block", "symbol_block"}:
        lines.append(f"⚙️ Deterministic gate: BLOCKED — {_clip(result.get('reason'), 650)}")
    elif skips:
        lines.append(f"⚙️ Execution gate: {len(skips)} skip(s)")
        for row in skips[:3]:
            symbol = str(row.get("symbol", "?")).upper()
            reason = str(row.get("reason", "?"))
            detail = _clip(row.get("detail"), 420)
            text = f"   • {symbol}: {reason}"
            if detail:
                text += f" — {detail}"
            lines.append(text)

    sweep, real = _execution_rows(snap)
    if sweep:
        releases = sum(1 for row in sweep if str(row.get("action", "")).upper() == "SWEEP_SELL")
        parks = sum(1 for row in sweep if str(row.get("action", "")).upper() == "SWEEP_BUY")
        bits = []
        if releases:
            bits.append(f"{releases} T-bill cash release")
        if parks:
            bits.append(f"{parks} cash-park buy")
        if bits:
            lines.append("💵 Cash management: " + " · ".join(bits))

    if real:
        lines.append(f"⚡ Execution: {len(real)} broker action(s)")
        for row in real[:6]:
            action = str(row.get("action", "?")).upper()
            symbol = str(row.get("symbol", "?")).upper()
            qty = _number(row.get("fill_qty")) or _number(row.get("qty"))
            price = _number(row.get("fill_price")) or _number(row.get("price"))
            fill_status = row.get("fill_status") or "recorded"
            qty_text = f" {qty:g}" if qty is not None else ""
            price_text = f" @ ${price:,.2f}" if price is not None and price > 0 else ""
            lines.append(f"   • {action} {symbol}{qty_text}{price_text} · {fill_status}")
    elif explain_no_trade:
        _append_no_trade_reason(lines, result, snap, skips)


def _append_no_trade_reason(
    lines: list[str],
    result: dict,
    snap: dict[str, Any],
    skips: list[dict],
) -> None:
    status = str(result.get("status", "unknown"))
    risk = snap.get("risk")
    pm_orders = [row for row in (snap.get("pm_orders") or []) if isinstance(row, dict)]
    actionable = [row for row in pm_orders if str(row.get("action", "")).upper() != "HOLD"]
    pm_summary = str((snap.get("agent_summaries") or {}).get("portfolio_manager") or "")

    if status == "rejected":
        reason = _clip(result.get("reason"), 650)
        lines.append(f"⏸️ NO TRADE — Risk vetoed the plan{': ' + reason if reason else ''}")
    elif status in {"hard_risk_block", "symbol_block"}:
        lines.append("⏸️ NO TRADE — deterministic eligibility blocked the proposed action(s)")
    elif status == "buys_unfunded" or skips:
        lines.append("⏸️ NO TRADE — decision(s) survived review but execution could not complete")
    elif isinstance(risk, dict) and risk.get("approved") is False:
        lines.append(f"⏸️ NO TRADE — Risk rejected: {_clip(risk.get('reasoning'), 550)}")
    elif pm_orders and not actionable:
        lines.append("⏸️ NO TRADE — PM/constructor produced HOLD only")
    elif snap.get("pm_reasoning") or pm_summary:
        lines.append("⏸️ NO TRADE — PM produced no executable portfolio change")
    else:
        lines.append("⏸️ NO TRADE — no market-risk order was submitted; detailed PM evidence unavailable")


def _append_footer(lines: list[str], snap: dict[str, Any], elapsed: float) -> None:
    """Duration and AI spend only — the owner-facing footer. Used to also
    print `run {run_id}` and the raw provider-request count ("LLM
    $0.10/2 provider requests"); both are engineering detail with no
    action attached to it on a phone alert, so 2026-09-17 dropped them.
    The run_id is still available wherever it actually matters (DB lookups,
    `send()`'s `link_url`/`symbols` args) — nothing depends on it being
    IN this printed text; only its presence in the rendered message
    changed.
    """
    bits: list[str] = []
    cost = snap.get("cost")
    if isinstance(cost, (int, float)):
        cost_text = f"${cost:.4f}" if cost < 0.01 else f"${cost:.2f}"
        bits.append(f"AI cost {cost_text}")
    bits.append(_fmt_elapsed(elapsed))
    lines.append("🧾 " + " · ".join(bits))


def _append_coverage_gaps(lines: list[str], result: dict) -> None:
    """Spec §11.1 guard 3 — two banners, never one merged count.

    A position with NO stop at all and a position whose stop is present but
    mis-sized are different conditions: the first has nothing standing watch,
    the second has an order covering most of it. Reporting them as a single
    "N gaps" line buried the worse condition inside the milder one. Shares
    `notifier._gap_is_uncovered` so the two feeds can never disagree about
    which bucket a gap is in.
    """
    from src.notifier import _gap_is_uncovered

    gaps = result.get("stop_coverage_gaps")
    if not isinstance(gaps, list) or not gaps:
        return
    rows = [row for row in gaps if isinstance(row, dict)]
    uncovered = [row for row in rows if _gap_is_uncovered(row)]
    partial = [row for row in rows if not _gap_is_uncovered(row)]
    if uncovered:
        names = ", ".join(str(row.get("symbol", "?")) for row in uncovered[:6])
        lines.append(f"🚨 NO STOP AT ALL: {len(uncovered)} · {names}")
    if partial:
        names = ", ".join(str(row.get("symbol", "?")) for row in partial[:6])
        lines.append(f"🚨 STOP MIS-SIZED: {len(partial)} · {names}")


def _append_done(lines: list[str], rows: list[dict], snap: dict, profiles: dict) -> None:
    """NEW LAYOUT item 3 — orders that actually reached the broker, one
    line each, the true fill state (never implying a fill that hasn't
    happened — `trades.fill_status` stays 'submitted' until it has)."""
    if not rows:
        return
    lines.append(_b("✅ DONE"))
    for row in rows:
        action = str(row.get("action", "?")).upper()
        symbol = str(row.get("symbol", "?")).upper()
        qty = _number(row.get("fill_qty")) or _number(row.get("qty"))
        price = _number(row.get("fill_price")) or _number(row.get("price"))
        fill_status = str(row.get("fill_status") or "").lower()
        state = "filled" if fill_status == "filled" else "placed, waiting to fill"
        qty_text = f"{qty:g}" if qty is not None else "?"
        price_text = f"${price:,.2f}" if price is not None and price > 0 else "?"
        stop = _symbol_stop(symbol, snap)
        stop_text = f" · stop ${stop:,.2f}" if stop is not None else ""
        lines.append(
            f"   • {action} {_ticker_co(symbol, profiles)} {qty_text} @ "
            f"{price_text} — {state}{stop_text}"
        )


def _append_blocked(lines: list[str], rows: list[dict], profiles: dict) -> None:
    """NEW LAYOUT item 4 — who actually stopped it, in plain words. See
    `_blocked_rows` for the three sources this merges."""
    if not rows:
        return
    lines.append(_b("❌ BLOCKED / FAILED"))
    for row in rows:
        reason = _clip(row.get("reason"), 300)
        lines.append(
            f"   • {row['action']} {_ticker_co(row['symbol'], profiles)} — "
            f"{row['who']}: {reason}"
        )


def _append_looked_at(lines: list[str], rows: list[dict], profiles: dict) -> None:
    """NEW LAYOUT item 5 — analyzed signals the PM/constructor passed on,
    one line each: ticker, company, rating/conviction, plain outcome."""
    if not rows:
        return
    lines.append(_b("👀 LOOKED AT, NO TRADE"))
    for row in rows:
        symbol = str(row.get("symbol", "?")).upper()
        rating = str(row.get("rating", "?")).upper()
        conviction = str(row.get("conviction", "?")).lower()
        lines.append(f"   • {_ticker_co(symbol, profiles)} {rating}/{conviction} — PM passed")


def _append_held(lines: list[str], symbols: list[str], profiles: dict) -> None:
    """NEW LAYOUT item 6 — every held ticker, once, with the correct count
    (the defect this fixes: a live message once said "7 hold(s)" over 6
    positions with only 2 actually listed)."""
    if not symbols:
        return
    lines.append(_b(f"HELD ({len(symbols)})"))
    for symbol in symbols:
        lines.append(f"   • {_ticker_co(symbol, profiles)}")


def _watch_rows(result: dict) -> list[dict]:
    """Positions already flagged by code the desk runs today — the
    STOP MIS-SIZED half of `_append_coverage_gaps` (the milder of its two
    banners; "NO STOP AT ALL" stays the loud top-of-message 🚨 banner it
    already is). No new threshold is invented here — only what
    `_gap_is_uncovered` already classifies."""
    from src.notifier import _gap_is_uncovered

    gaps = result.get("stop_coverage_gaps")
    if not isinstance(gaps, list):
        return []
    return [
        row for row in gaps
        if isinstance(row, dict) and not _gap_is_uncovered(row)
    ]


def _append_watch(lines: list[str], rows: list[dict], profiles: dict) -> None:
    if not rows:
        return
    lines.append(_b("⚠️ WATCH"))
    for row in rows:
        symbol = str(row.get("symbol", "?")).upper()
        lines.append(f"   • {_ticker_co(symbol, profiles)} — stop coverage is mis-sized")


def _format_decision_session(mode: str, result: dict, elapsed: float) -> str:
    run_id = result.get("run_id")
    snap = _read_run(run_id)
    status = str(result.get("status", "unknown"))

    done_rows = _done_rows(snap)
    blocked_rows = _blocked_rows(result, snap)
    acted = {row["symbol"] for row in done_rows} | {row["symbol"] for row in blocked_rows}
    looked_at_rows = _looked_at_rows(snap, None, acted)
    profiles = _lookup_company_profiles(
        _all_symbols(done_rows, blocked_rows, looked_at_rows)
    )

    outcome = _outcome_word(status, len(done_rows), len(blocked_rows))
    lines = [f"{_status_emoji(status)} {mode.upper()} · {fmt_time_12h(et_now())} · {outcome}"]

    _new_block(lines, _append_coverage_gaps, result)

    data_status = result.get("data_status") or {}
    if isinstance(data_status, dict):
        from src import evidence_gate
        degraded = [
            name for name, value in data_status.items()
            if evidence_gate.counts_as_degraded(value)
        ]
        if degraded:
            _new_section(lines, f"⚠️ Data degraded: {', '.join(sorted(degraded))}")

    _new_block(lines, _append_market, snap)
    _new_block(lines, _append_book, snap)
    _new_block(lines, _append_done, done_rows, snap, profiles)
    _new_block(lines, _append_blocked, blocked_rows, profiles)
    _new_block(lines, _append_looked_at, looked_at_rows, profiles)

    detail_lines: list[str] = []
    _new_block(detail_lines, _append_signals, snap)
    _new_block(detail_lines, _append_pm, snap, may_glue=True)
    _new_block(detail_lines, _append_risk, snap)
    _new_block(detail_lines, _append_gate_and_execution, result, snap)
    _wrap_details(lines, detail_lines)

    _new_block(lines, _append_footer, snap, elapsed)
    return "\n".join(lines)


def _held_symbols(snap: dict[str, Any]) -> list[str]:
    """Every symbol actually held, read from broker-truth `positions` —
    NOT the reviewer's self-reported `actions` list, which is LLM output
    and can under- or over-count (the defect this fixes: a live message
    once said "7 hold(s)" over 6 real positions, with only 2 ever named).
    Ordered exactly as `_append_book` already ranks them (by position
    size), excludes cash-sweep vehicles the same way."""
    positions = [row for row in (snap.get("positions") or []) if isinstance(row, dict)]
    return [
        str(row.get("symbol", "")).upper() for row in positions
        if str(row.get("symbol", "")).upper() not in _SWEEP_SYMBOLS
    ]


def _format_position_review(mode: str, result: dict, elapsed: float) -> str:
    run_id = result.get("run_id")
    snap = _read_run(run_id)
    status = str(result.get("status", "unknown"))
    review = result.get("review") if isinstance(result.get("review"), dict) else {}

    done_rows = _done_rows(snap)
    blocked_rows = _blocked_rows(result, snap)
    held_symbols = _held_symbols(snap)
    watch_rows = _watch_rows(result)
    profiles = _lookup_company_profiles(
        _all_symbols(done_rows, blocked_rows, held_symbols, watch_rows)
    )

    outcome = _outcome_word(status, len(done_rows), len(blocked_rows))
    lines = [
        f"{_status_emoji(status)} {mode.upper()} REVIEW · "
        f"{fmt_time_12h(et_now())} · {outcome}"
    ]

    # Owner request 2026-09-17: today's + total P&L, always shown (never
    # silently dropped the way the old "Session P&L" line was on every
    # midday/close message — `run_position_review` now always sets these).
    _new_section(lines, *_pnl_section_lines(result))

    def _render_halt_banner(lines: list[str]) -> None:
        if status == "emergency_sold":
            # Historical runs only — nothing emits this any more (item 32).
            lines.append("🚨 DAILY-LOSS CIRCUIT BREAKER — autonomous liquidation triggered")
        if status == "daily_loss_halted":
            lines.append(
                "🛑 DAILY-LOSS CIRCUIT BREAKER — NEW RISK HALTED. Nothing sold; "
                "every position kept."
            )
            unprotected = result.get("unprotected_at_halt") or []
            if unprotected:
                lines.append(
                    "⚠️ NOT verifiably stop-covered at the halt: "
                    + ", ".join(str(s) for s in unprotected[:8])
                )

    _new_block(lines, _render_halt_banner)
    _new_block(lines, _append_coverage_gaps, result)

    positions = result.get("positions")
    risk_level = review.get("risk_level")

    def _render_review_summary(lines: list[str]) -> None:
        bits = []
        if positions is not None:
            bits.append(f"{positions} position(s)")
        if risk_level:
            bits.append(f"risk {risk_level}")
        if bits:
            lines.append("📍 Review: " + " · ".join(bits))

    _new_block(lines, _render_review_summary)

    _new_block(lines, _append_done, done_rows, snap, profiles)
    _new_block(lines, _append_blocked, blocked_rows, profiles)
    _new_block(lines, _append_held, held_symbols, profiles)
    _new_block(lines, _append_watch, watch_rows, profiles)
    _new_block(lines, _append_book, snap)

    actions = [row for row in (review.get("actions") or []) if isinstance(row, dict)]
    actionable = [row for row in actions if str(row.get("action", "")).upper() != "HOLD"]
    holds = [row for row in actions if str(row.get("action", "")).upper() == "HOLD"]

    detail_lines: list[str] = []
    overall = _clip(review.get("overall_assessment"), 650)
    if overall:
        detail_lines.append(f"🧠 Reviewer: {overall}")

    def _render_decisions(lines: list[str]) -> None:
        if not actions:
            return
        lines.append(f"🎯 Decisions: {len(actionable)} action(s) · {len(holds)} hold(s)")
        # Uncapped — same reasoning as `_signal_rows`: a header count must
        # never claim more than the bullets beneath it actually show.
        for row in actionable:
            action = str(row.get("action", "?")).upper()
            symbol = str(row.get("symbol", "?")).upper()
            stop = _number(row.get("new_stop_price"))
            stop_text = f" → stop ${stop:,.2f}" if stop is not None else ""
            reason = _clip(row.get("reason"), 420)
            text = f"   • {action} {symbol}{stop_text}"
            if reason:
                text += f" — {reason}"
            lines.append(text)
        for row in holds:
            symbol = str(row.get("symbol", "?")).upper()
            reason = _clip(row.get("reason"), 420)
            text = f"   • HOLD {symbol}"
            if reason:
                text += f" — {reason}"
            lines.append(text)

    _new_block(detail_lines, _render_decisions, may_glue=True)

    def _render_gate_and_outcome(lines: list[str]) -> None:
        _append_gate_and_execution(lines, result, snap, explain_no_trade=False)
        _, real = _execution_rows(snap)
        if not real:
            if actions and holds and not actionable:
                lines.append("⏸️ NO ACTION — reviewer explicitly held the book")
            elif not actions and positions == 0:
                lines.append("⏸️ NO ACTION — no market-risk positions required review")
            elif not actions and status == "reviewed":
                lines.append("⏸️ NO ACTION — review completed with no broker action")

    _new_block(detail_lines, _render_gate_and_outcome)
    _wrap_details(lines, detail_lines)

    _new_block(lines, _append_footer, snap, elapsed)
    return "\n".join(lines)


def _fmt_pnl_line(label: str, pnl: float | None, ret: float | None) -> str:
    """'📈 Today's P&L: +$12.34 (+0.10%)' — a missing figure renders as the
    honest 'not available', never a fabricated 0. If BOTH are unknown the
    parenthetical is dropped too ('not available (not available)' reads as
    a bug, not an honest gap)."""
    if pnl is None and ret is None:
        return f"{label} not available"
    pnl_text = _fmt_signed_money(pnl) if pnl is not None else "not available"
    ret_text = f"{ret:+.2f}%" if ret is not None else "not available"
    return f"{label} {pnl_text} ({ret_text})"


def _pnl_section_lines(result: dict) -> list[str]:
    """The two-line P&L block every trader-feed message shows in the spot
    the old, undated 'Session P&L' line used to sit (owner request,
    2026-09-17): he did not know whether "session" meant this run, this
    calendar day, or something else — it actually meant this run's/tick's
    OWN process, i.e. it was silently absent on every midday/close message
    (`run_position_review` never set `daily_pnl` at all) and, when it did
    show on an intraday tick, was really just today's account change under
    an unexplained label.

    'Today's P&L' is the broker's own day-over-day account change
    (today's total value vs. its `last_equity`, i.e. official prior-close
    equity) — the SAME basis `run_evening`'s already-trusted 'Daily P&L'
    line uses (src/notifier.py), just not yet 4pm-close-verified mid-day.

    'Total P&L' is deliberately labelled with the date it starts from
    rather than called "total" bare: the 2026-09-02 book-wide liquidation
    archived every earlier record (docs/INCIDENT_HISTORY.md), so a total
    spanning that boundary would be meaningless — seeing "total" would
    read as "since the account began", which it is not. See
    `TradingPipeline._total_pnl_since_reset` for exactly which recorded
    value the baseline comes from (never reconstructed).
    """
    today_pnl = _number(result.get("daily_pnl"))
    today_ret = _number(result.get("daily_return_pct"))
    total_pnl = _number(result.get("total_pnl"))
    total_ret = _number(result.get("total_return_pct"))
    since = result.get("total_pnl_since")
    total_label = f"📊 Total P&L since {since}:" if since else "📊 Total P&L:"
    return [
        _fmt_pnl_line("📈 Today's P&L:", today_pnl, today_ret),
        _fmt_pnl_line(total_label, total_pnl, total_ret),
    ]


def _format_intraday(outer: dict, nested: dict, elapsed: float) -> str:
    run_id = nested.get("run_id") or outer.get("run_id")
    snap = _read_run(run_id)
    status = str(nested.get("status", "unknown"))

    candidates = [str(symbol).upper() for symbol in (nested.get("candidates") or []) if symbol]
    done_rows = _done_rows(snap)
    blocked_rows = _blocked_rows(nested, snap)
    acted = {row["symbol"] for row in done_rows} | {row["symbol"] for row in blocked_rows}
    looked_at_rows = _looked_at_rows(snap, candidates, acted)
    profiles = _lookup_company_profiles(
        _all_symbols(done_rows, blocked_rows, looked_at_rows)
    )

    outcome = _outcome_word(status, len(done_rows), len(blocked_rows))
    lines = [f"⚡ INTRADAY OPPORTUNITY · {fmt_time_12h(et_now())} · {outcome}"]

    def _render_status_banner(lines: list[str]) -> None:
        if status == "paid_analysis_suspended":
            lines.append(
                "🛑 SUSPENDED: paid opportunity discovery is suspended by the "
                "cost circuit; the deterministic intraday loss check completed "
                "normally."
            )
            if nested.get("error"):
                lines.append(f"Trigger: {_clip(nested.get('error'), 900)}")
        elif status == "intraday_analysis_error":
            lines.append(
                f"🛑 FAILED: PM analysis failed "
                f"({nested.get('failure_status') or 'unknown'}); this was not "
                "a deliberate no-trade decision."
            )
            if nested.get("error"):
                lines.append(f"Error: {_clip(nested.get('error'), 900)}")
        elif status == "evidence_gate_skip":
            lost = nested.get("lost_seats") or []
            seats = ", ".join(str(s) for s in lost) or "a research seat"
            lines.append(
                f"🟡 DECISION SKIPPED: {seats} never returned an answer, so this "
                "tick declined to decide rather than guess. Nothing was traded "
                "and the Portfolio Manager was not paid for."
            )
            if nested.get("reason"):
                lines.append(_clip(nested.get("reason"), 900))
        elif status == "intraday_scan_crashed":
            # Operator-honesty fix: this used to be indistinguishable from a
            # healthy tick that ran and found nothing — the scan raised, the
            # caller swallowed the exception and set scan_result to None, and no
            # `intraday_scan` key ever reached this formatter. Now the crash
            # attaches a dict with this status, so it renders through the same
            # nested path `paid_analysis_suspended` / `intraday_analysis_error`
            # already use, instead of silently reading as "Status: ok".
            lines.append(
                f"🛑 CRASHED: intraday opportunity scan crashed "
                f"({nested.get('error_type') or 'unknown'}); the deterministic "
                "intraday loss check above completed normally."
            )
            if nested.get("error"):
                lines.append(f"Error: {_clip(nested.get('error'), 900)}")

    _new_block(lines, _render_status_banner)

    _new_section(lines, *_pnl_section_lines(outer))

    _new_block(lines, _append_done, done_rows, snap, profiles)
    _new_block(lines, _append_blocked, blocked_rows, profiles)
    _new_block(lines, _append_looked_at, looked_at_rows, profiles)

    detail_lines: list[str] = []
    _new_block(detail_lines, _append_signals, snap, candidates=candidates)
    _new_block(detail_lines, _append_pm, snap, may_glue=True)
    _new_block(detail_lines, _append_risk, snap)
    # `nested`, not `outer`: on the intraday path the traded-order evidence
    # (and the run_id it's keyed by) lives in the `intraday_scan` sub-dict.
    _new_block(detail_lines, _append_gate_and_execution, nested, snap)
    _wrap_details(lines, detail_lines)

    _new_block(lines, _append_footer, snap, elapsed)
    return "\n".join(lines)


# === intra_check cadence (2026-09-17) ===
#
# Owner requirement: "I want messages every hour. I want oversight and
# transparency of what is being done." Ticks still run every 30 minutes,
# unchanged — but a tick with nothing actionable (no order, trade, skip,
# fill, stop/coverage problem, or error) sends NO message at all, same as
# before AND now including the ":30 INTRADAY OPPORTUNITY ... NO TRADE"
# message, which used to always send. Silence on a quiet half-hour is fine;
# silence for a whole clock hour is not. The top-of-hour tick (":00", plus
# the 9:30 session-open tick, which is the first tick of the day and has no
# ":00" before it) is the guaranteed pulse: it ALWAYS sends one message,
# built from DB records covering the last ~hour — including the :30 tick's
# analysis, which ran under a different run_id than this tick's own — so
# the owner never goes more than a clock hour without a message, even on a
# fully quiet stretch. Anything actionable still sends immediately at any
# tick, exactly as before; if that already happened this hour, the
# top-of-hour tick still sends its own summary, as a second message body.
_HOUR_WINDOW_MINUTES = 70  # 60 minutes + a buffer for scheduler jitter

# 2026-09-17 regression, fixed same night: PR #454 moved intra_check's own
# systemd timer off the shared `*:0/30` tick onto `*:15,45` (to stop it
# firing in the same wall-clock second as morning/midday/close/evening —
# see scripts/systemd/quant-agent-intra_check.timer for the full story),
# but `_is_hourly_checkpoint` below still hard-coded `minute == 0` as "the
# top of the hour". intra_check is the ONLY session that runs this check
# (it is the one that ticks all day, 09:30-16:00 ET) and it no longer ever
# lands on minute 0 — so the owner's guaranteed once-per-hour message
# silently stopped firing on a quiet day. Never caught because nothing
# tied the checkpoint minute to the timer's actual cadence.
#
# Fix: the "top of the hour" is not minute 0, it is whatever minute
# intra_check's OWN timer first fires on each hour — read from the unit
# file itself (scripts/systemd/quant-agent-intra_check.timer) rather than
# duplicated here as a second hard-coded number, so the next cadence move
# carries the guarantee with it instead of quietly breaking it again. See
# tests/test_trader_feed.py::test_hourly_checkpoint_minute_matches_intra_check_timer_cadence
# for the guard that fails the build if this ever falls out of sync with
# the unit file.
_INTRA_CHECK_TIMER_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "systemd" / "quant-agent-intra_check.timer"
)

# A bare `*:MM` or `*:MM,MM,...` OnCalendar spec — the only form intra_check's
# timer has ever used and the only form this parses. Anything else (a day
# restriction, a `/` step syntax, a full calendar expression, ...) is a
# cadence change this code cannot safely interpret, and it must say so
# loudly rather than guess.
_ONCALENDAR_PER_HOUR_RE = re.compile(r"\*:(\d{1,2}(?:,\d{1,2})*)")


def _intra_check_tick_minutes() -> tuple[int, ...]:
    """The minutes-past-the-hour intra_check actually fires on, read from
    its own systemd timer unit. Raises rather than guessing if the file is
    missing or its `OnCalendar=` line isn't the simple per-hour form this
    understands — a silent fallback here is exactly the class of bug this
    function exists to prevent.
    """
    try:
        text = _INTRA_CHECK_TIMER_PATH.read_text()
    except OSError as exc:
        raise RuntimeError(
            f"cannot read intra_check's own timer unit at "
            f"{_INTRA_CHECK_TIMER_PATH} to derive the hourly-checkpoint "
            f"minute: {exc}"
        ) from exc

    spec = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("OnCalendar="):
            spec = line[len("OnCalendar="):].strip()
            break
    if spec is None:
        raise RuntimeError(
            f"{_INTRA_CHECK_TIMER_PATH} has no OnCalendar= line; cannot "
            "derive the hourly-checkpoint minute"
        )

    match = _ONCALENDAR_PER_HOUR_RE.fullmatch(spec)
    if not match:
        raise RuntimeError(
            f"{_INTRA_CHECK_TIMER_PATH}: OnCalendar={spec!r} is not a "
            "simple '*:MM' / '*:MM,MM' per-hour cadence — "
            "_is_hourly_checkpoint doesn't know how to read this and won't "
            "guess. Update the parsing deliberately if the cadence syntax "
            "has genuinely changed."
        )
    return tuple(sorted({int(m) for m in match.group(1).split(",")}))


def _hourly_checkpoint_minute() -> int:
    """The minute-past-the-hour that owes the clock hour its guaranteed
    message: the FIRST tick intra_check makes each hour, under whatever
    cadence its timer is actually running."""
    return _intra_check_tick_minutes()[0]


def _is_hourly_checkpoint(now=None) -> bool:
    """True at the tick that owes this trading hour its guaranteed message.

    The first tick of each hour (see `_hourly_checkpoint_minute`) owns
    that hour. The 9:30 session-open tick is the exception: it is the
    first tick of the day, so there is no earlier tick this hour to have
    covered the 9:00-9:30 gap (during which the market was closed anyway)
    — 9:30 stands in for it.
    """
    now = now or et_now()
    return now.minute == _hourly_checkpoint_minute() or (now.hour == 9 and now.minute == 30)


def _intraday_tick_actionable(result: dict, nested: dict | None, snap: dict[str, Any]) -> bool:
    """Whether THIS intra_check tick has something the owner needs to see
    now, rather than folded into the next top-of-hour summary — an order,
    a trade, an execution skip, a stop-coverage problem, or a genuine
    error/interruption in paid analysis. Read-only classification; a
    mis-read here costs at most one message's timing, never a trading
    decision.

    `skips` is read from `snap` (the same `_read_run` DB snapshot every
    other renderer here uses), not from `nested["execution_skips"]` — the
    intraday scan's own result dict never sets that key (checked against
    `_intraday_opportunity_scan_body` in src/pipeline.py: it returns only
    status/candidates/orders/run_id), so a skip would otherwise never be
    seen as actionable here.
    """
    if _actionable_coverage_gaps(result.get("stop_coverage_gaps")):
        return True
    if not isinstance(nested, dict):
        return False
    status = str(nested.get("status") or "")
    if status in (
        "intraday_executed", "intraday_analysis_error",
        "intraday_scan_crashed", "paid_analysis_suspended",
        "evidence_gate_skip",
    ):
        return True
    if status == "intraday_no_trades":
        # PM/risk reached a real decision point with nothing to execute —
        # actionable only if execution actually had something to skip
        # (insufficient cash, etc.); a clean "nothing to do" is quiet.
        return bool(snap.get("skips"))
    return False  # disabled / lock_contended / no_opportunity — quiet


def _format_intra_check(result: dict, elapsed_seconds: float) -> str | None:
    nested = result.get("intraday_scan")
    run_id = (nested.get("run_id") if isinstance(nested, dict) else None) or result.get("run_id")
    snap = _read_run(run_id)
    actionable = _intraday_tick_actionable(result, nested, snap)

    own_message: str | None = None
    if actionable:
        nested_status = str(nested.get("status") or "") if isinstance(nested, dict) else ""
        if isinstance(nested, dict) and nested_status not in _INTRADAY_SILENT_STATUSES:
            own_message = _format_intraday(result, nested, elapsed_seconds)
        else:
            # Actionable for a reason `_format_intraday` doesn't render
            # (an outer-level stop-coverage gap while the scan itself was
            # disabled/contended/found nothing) — the base formatter's own
            # `_append_intra_check_body` already renders that banner.
            own_message = _base_format_session_result(
                "intra_check", result, elapsed_seconds, error=None,
            )

    if not _is_hourly_checkpoint():
        return own_message  # None here means: quiet tick, no send.

    summary = _format_hourly_desk_check(result, nested, elapsed_seconds)
    if own_message:
        return own_message + "\n\n" + summary
    return summary


def _movers_scanned_text(nested: dict | None) -> str:
    """Honest one-line account of whether the paid intraday scan even ran
    this tick — 'no arbitrary numbers': there is no mover COUNT to report
    for the common no-opportunity/disabled/contended outcomes (the
    pipeline's own status dicts don't carry one), so this says what is
    actually known instead of fabricating a figure.
    """
    if not isinstance(nested, dict):
        return "not run this tick"
    status = str(nested.get("status") or "")
    if status == "intraday_scan_no_opportunity":
        return "ran, no qualifying movers"
    if status == "intraday_scan_disabled":
        return "disabled"
    if status == "intraday_scan_lock_contended":
        return "skipped (lock contended)"
    return "ran this tick"


def _read_hour_evidence(hour_window_minutes: int = _HOUR_WINDOW_MINUTES) -> list[dict]:
    """Every tech-analyst analysis logged in roughly the last hour, read by
    TIME rather than by this tick's own `run_id` — the top-of-hour summary
    has to include the :30 tick's analysis too, and that ran under a
    different run_id. One row per symbol (most recent), so a symbol
    analyzed at both ticks (cooldown normally prevents this) is not shown
    twice. Read-only, fail-soft: any DB problem yields an empty list rather
    than blocking the guaranteed hourly message.
    """
    rows: dict[str, dict] = {}
    if not Path(_DB_PATH).exists():
        return []
    conn = None
    try:
        uri = f"file:{Path(_DB_PATH).resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=1000")
        for row in conn.execute(
            "SELECT symbol, evidence_json FROM specialist_evidence "
            "WHERE agent_name = 'tech_analyst' AND kind = 'analysis' "
            "AND timestamp >= datetime('now', ?) ORDER BY timestamp",
            (f"-{hour_window_minutes} minutes",),
        ).fetchall():
            try:
                data = json.loads(row["evidence_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            symbol = str(data.get("symbol") or row["symbol"] or "").upper()
            if symbol:
                rows[symbol] = data
    except Exception as exc:  # noqa: BLE001
        logger.warning("hourly desk check: evidence read failed: %s", exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return list(rows.values())


def _read_hour_trade_count(hour_window_minutes: int = _HOUR_WINDOW_MINUTES) -> int:
    """Count of real (non-HOLD) broker trades in roughly the last hour —
    read-only, fail-soft: any DB problem reads as 0 rather than blocking
    the guaranteed hourly message (a real trade still sent its own
    immediate message regardless of this count)."""
    if not Path(_DB_PATH).exists():
        return 0
    conn = None
    try:
        uri = f"file:{Path(_DB_PATH).resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.execute("PRAGMA busy_timeout=1000")
        row = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE timestamp >= datetime('now', ?) "
            "AND UPPER(action) != 'HOLD'",
            (f"-{hour_window_minutes} minutes",),
        ).fetchone()
        return int(row[0]) if row else 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("hourly desk check: trade count read failed: %s", exc)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _format_hourly_desk_check(
    result: dict,
    nested: dict | None,
    elapsed: float,
    *,
    snap: dict[str, Any] | None = None,
    trade_count: int | None = None,
    period_line: str = "Covering the last hour",
    coverage_text: str | None = None,
    scanned_text: str | None = None,
) -> str:
    """The guaranteed top-of-hour message: what happened across BOTH ticks
    of the last hour, in plain English, even when neither one had anything
    that needed the owner's attention on its own.

    Every keyword argument defaults to exactly what the scheduled
    top-of-hour path has always done; they exist so the on-demand desk
    status (`format_desk_status` below, `scripts/desk_status.py`) can drive
    this same formatter from live broker truth and — crucially — say
    "not available" for the two fields an on-demand read genuinely cannot
    know, rather than borrowing a scheduled tick's answer or printing a
    reassuring zero.
    """
    run_id = result.get("run_id")
    if snap is None:
        snap = _read_run(run_id)
    if trade_count is None:
        trade_count = _read_hour_trade_count()
    outcome = "TRADED" if trade_count else "NO CHANGE"
    lines = [
        f"🕐 DESK CHECK · {fmt_time_12h(et_now())} · {outcome}",
        period_line,
    ]

    if trade_count == 0:
        _new_section(lines, "⏸️ No action this hour")
    else:
        _new_section(
            lines,
            f"⚡ {trade_count} order(s) this hour — see the alert(s) already sent",
        )

    _new_section(lines, *_pnl_section_lines(result))

    positions = [row for row in (snap.get("positions") or []) if isinstance(row, dict)]
    risk_positions = [
        row for row in positions
        if str(row.get("symbol", "")).upper() not in _SWEEP_SYMBOLS
    ]
    _new_section(lines, f"💼 Positions held: {len(risk_positions)}")

    hour_rows = _read_hour_evidence()
    hour_symbols = [
        str(row.get("symbol", "")).upper() for row in hour_rows if row.get("symbol")
    ]
    profiles = _lookup_company_profiles(hour_symbols)

    _cov_start = len(lines)
    _append_coverage_gaps(lines, result)
    _cov_added = len(lines) > _cov_start
    _seal_section(lines, _cov_start)
    if not _cov_added:
        _new_section(lines, coverage_text or "🛡️ Stop coverage: OK")

    def _render_hour_signals(lines: list[str]) -> None:
        lines.append(f"🔎 Signals this hour: {len(hour_rows)} analyzed")
        priority = {"strong_buy": 0, "strong_sell": 0, "buy": 1, "sell": 1, "neutral": 2}
        ordered = sorted(
            hour_rows,
            key=lambda row: (
                priority.get(str(row.get("rating", "")).lower(), 3),
                str(row.get("symbol", "")),
            ),
        )
        # Uncapped, same reasoning as `_signal_rows`: the header just above
        # states the real count — the bullets below it must match, not
        # silently truncate to a smaller number. Company name inline
        # (`profiles`) since this is the only per-symbol listing in this
        # message — there is no separate DONE/LOOKED-AT section to have
        # already introduced it.
        for row in ordered:
            lines.append(_signal_row_line(row, profiles=profiles))

    detail_lines: list[str] = []
    if hour_rows:
        _new_block(detail_lines, _render_hour_signals)
    else:
        _new_section(
            detail_lines,
            f"🔎 Movers scanned: {scanned_text or _movers_scanned_text(nested)}",
        )
    _wrap_details(lines, detail_lines)

    _new_block(lines, _append_footer, snap, elapsed)
    return "\n".join(lines)


# === on-demand desk status (2026-09-17) ===
#
# Owner request: "send me the desk status now", at any time, without
# running a trading session. This deliberately holds NO message format of
# its own — it assembles read-only inputs and hands them to
# `_format_hourly_desk_check` above, the same function the scheduled
# top-of-hour message uses. Every future change to that formatter
# (wording, P&L line, sections) reaches this command with no edit here.
#
# Read-only, in the strong sense: it places and cancels nothing, calls no
# model, writes nothing to the database, and takes no session lock or
# once-per-day stamp. The scheduled sessions are unaffected by it.
_ON_DEMAND_PERIOD_LINE = "Covering the last hour · status requested on demand"
# Stop coverage is audited (and auto-repaired) by the scheduled sessions,
# which is a WRITING action — this command must not run it. So the answer
# is genuinely unknown here and says so, rather than printing the
# reassuring "OK" that the scheduled path prints when it has actually
# looked.
_ON_DEMAND_COVERAGE_TEXT = (
    "🛡️ Stop coverage: not available — the coverage audit runs with the "
    "scheduled sessions, not on demand"
)
_ON_DEMAND_SCANNED_TEXT = (
    "not available — the movers scan runs with the scheduled sessions, "
    "not on demand"
)


def _position_rows_from_broker(positions: Any) -> list[dict[str, Any]]:
    """Broker `Position` objects (or plain dicts) → the same row shape
    `_read_run` puts in `snapshot["positions"]`, so the shared formatter
    cannot tell the difference. Shorts (negative qty) are kept, matching
    that query's `qty != 0`."""
    rows: list[dict[str, Any]] = []
    for position in positions or []:
        def _get(field: str) -> Any:
            if isinstance(position, dict):
                return position.get(field)
            return getattr(position, field, None)

        qty = _number(_get("qty"))
        if qty is None or qty == 0:
            continue
        rows.append(
            {
                "symbol": str(_get("symbol") or "").upper(),
                "qty": qty,
                "avg_entry": _number(_get("avg_entry")),
                "current_price": _number(_get("current_price")),
                "market_value": _number(_get("market_value")),
                "unrealized_pnl": _number(_get("unrealized_pnl")),
            }
        )
    rows.sort(key=lambda row: abs(row.get("market_value") or 0.0), reverse=True)
    return rows


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
