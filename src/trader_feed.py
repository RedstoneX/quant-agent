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
import sqlite3
from pathlib import Path
from typing import Any

from src.notifier import (
    _clip_text,
    _DB_PATH as _NOTIFIER_DB_PATH,
    format_session_result as _base_format_session_result,
)
from src.trading_calendar import et_now

logger = logging.getLogger(__name__)

_DB_PATH = _NOTIFIER_DB_PATH
_SWEEP_SYMBOLS = frozenset({"SGOV", "BIL"})
_BASE_ONLY_STATUSES = frozenset(
    {"market_holiday", "early_close", "broker_error", "analysis_error", "fetch_error"}
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
            nested = result.get("intraday_scan")
            if isinstance(nested, dict):
                nested_status = str(nested.get("status") or "")
                if nested_status in _INTRADAY_SILENT_STATUSES:
                    return _base_format_session_result(
                        mode, result, elapsed_seconds, error=None,
                    )
                return _format_intraday(result, nested, elapsed_seconds)
            # Preserves the existing policy: ordinary ~30-minute OK ticks are silent.
            return _base_format_session_result(mode, result, elapsed_seconds, error=None)

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
    if "error" in status or status in {"failed", "emergency_sold"}:
        return "🔴"
    return "⚪"


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
    priority = {"strong_buy": 0, "strong_sell": 0, "buy": 1, "sell": 1, "neutral": 2}
    ordered = sorted(
        tech,
        key=lambda row: (
            priority.get(str(row.get("rating", "")).lower(), 3),
            str(row.get("symbol", "")),
        ),
    )
    for row in ordered[:4]:
        sym = str(row.get("symbol", "?")).upper()
        rating = str(row.get("rating", "?")).upper()
        conviction = str(row.get("conviction", "?")).lower()
        rr = row.get("risk_reward")
        rr_text = f" · R/R {rr:g}" if isinstance(rr, (int, float)) else ""
        reason = _clip(row.get("reasoning"), 420)
        text = f"   • {sym}: {rating}/{conviction}{rr_text}"
        if reason:
            text += f" — {reason}"
        lines.append(text)


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

    portfolio_view = reasoning.get("portfolio_view") if isinstance(reasoning, dict) else None
    if portfolio_view:
        lines.append(f"   View: {_clip(portfolio_view, 550)}")
    elif pm_summary and str(pm_summary).lower() != "no trades":
        lines.append(f"   View: {_clip(pm_summary, 550)}")


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
    lines.append(f"🛡️ Risk: {label} · {category}{scale_text} · {len(mods)} mod(s)")
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


def _append_footer(lines: list[str], run_id: str | None, snap: dict[str, Any], elapsed: float) -> None:
    bits: list[str] = []
    if run_id and run_id != "?":
        bits.append(f"run {run_id}")
    cost = snap.get("cost")
    calls = int(snap.get("calls") or 0)
    if isinstance(cost, (int, float)):
        cost_text = f"${cost:.4f}" if cost < 0.01 else f"${cost:.2f}"
        bits.append(f"LLM {cost_text}/{calls} provider request{'s' if calls != 1 else ''}")
    bits.append(_fmt_elapsed(elapsed))
    lines.append("🧾 " + " · ".join(bits))


def _format_decision_session(mode: str, result: dict, elapsed: float) -> str:
    run_id = result.get("run_id")
    snap = _read_run(run_id)
    status = str(result.get("status", "unknown"))
    lines = [f"{_status_emoji(status)} {mode.upper()} · {et_now().strftime('%H:%M ET')}", f"Status: {status}"]

    gaps = result.get("stop_coverage_gaps")
    if isinstance(gaps, list) and gaps:
        symbols = ", ".join(str(row.get("symbol", "?")) for row in gaps[:6] if isinstance(row, dict))
        lines.append(f"🚨 STOP-COVERAGE GAP: {len(gaps)} · {symbols}")

    data_status = result.get("data_status") or {}
    if isinstance(data_status, dict):
        degraded = [name for name, value in data_status.items() if value not in ("ok", "empty")]
        if degraded:
            lines.append(f"⚠️ Data degraded: {', '.join(sorted(degraded))}")

    _append_market(lines, snap)
    _append_book(lines, snap)
    _append_signals(lines, snap)
    _append_pm(lines, snap)
    _append_risk(lines, snap)
    _append_gate_and_execution(lines, result, snap)
    _append_footer(lines, run_id, snap, elapsed)
    return "\n".join(lines)


def _format_position_review(mode: str, result: dict, elapsed: float) -> str:
    run_id = result.get("run_id")
    snap = _read_run(run_id)
    status = str(result.get("status", "unknown"))
    review = result.get("review") if isinstance(result.get("review"), dict) else {}
    lines = [f"{_status_emoji(status)} {mode.upper()} REVIEW · {et_now().strftime('%H:%M ET')}", f"Status: {status}"]

    if status == "emergency_sold":
        lines.append("🚨 DAILY-LOSS CIRCUIT BREAKER — autonomous liquidation triggered")

    gaps = result.get("stop_coverage_gaps")
    if isinstance(gaps, list) and gaps:
        symbols = ", ".join(str(row.get("symbol", "?")) for row in gaps[:6] if isinstance(row, dict))
        lines.append(f"🚨 STOP-COVERAGE GAP: {len(gaps)} · {symbols}")

    positions = result.get("positions")
    risk_level = review.get("risk_level")
    bits = []
    if positions is not None:
        bits.append(f"{positions} position(s)")
    if risk_level:
        bits.append(f"risk {risk_level}")
    if bits:
        lines.append("📍 Review: " + " · ".join(bits))

    overall = _clip(review.get("overall_assessment"), 650)
    if overall:
        lines.append(f"🧠 Reviewer: {overall}")

    actions = [row for row in (review.get("actions") or []) if isinstance(row, dict)]
    actionable = [row for row in actions if str(row.get("action", "")).upper() != "HOLD"]
    holds = [row for row in actions if str(row.get("action", "")).upper() == "HOLD"]
    if actions:
        lines.append(f"🎯 Decisions: {len(actionable)} action(s) · {len(holds)} hold(s)")
        for row in actionable[:5]:
            action = str(row.get("action", "?")).upper()
            symbol = str(row.get("symbol", "?")).upper()
            stop = _number(row.get("new_stop_price"))
            stop_text = f" → stop ${stop:,.2f}" if stop is not None else ""
            reason = _clip(row.get("reason"), 420)
            text = f"   • {action} {symbol}{stop_text}"
            if reason:
                text += f" — {reason}"
            lines.append(text)
        for row in holds[:2]:
            symbol = str(row.get("symbol", "?")).upper()
            reason = _clip(row.get("reason"), 420)
            text = f"   • HOLD {symbol}"
            if reason:
                text += f" — {reason}"
            lines.append(text)

    _append_book(lines, snap)
    _append_gate_and_execution(lines, result, snap, explain_no_trade=False)
    _, real = _execution_rows(snap)
    if not real:
        if actions and holds and not actionable:
            lines.append("⏸️ NO ACTION — reviewer explicitly held the book")
        elif not actions and positions == 0:
            lines.append("⏸️ NO ACTION — no market-risk positions required review")
        elif not actions and status == "reviewed":
            lines.append("⏸️ NO ACTION — review completed with no broker action")

    _append_footer(lines, run_id, snap, elapsed)
    return "\n".join(lines)


def _format_intraday(outer: dict, nested: dict, elapsed: float) -> str:
    run_id = nested.get("run_id") or outer.get("run_id")
    snap = _read_run(run_id)
    status = str(nested.get("status", "unknown"))
    lines = [f"⚡ INTRADAY OPPORTUNITY · {et_now().strftime('%H:%M ET')}", f"Status: {status}"]
    if status == "paid_analysis_suspended":
        lines.append(
            "🔴 Paid opportunity discovery is suspended by the cost circuit; "
            "the deterministic intraday loss check completed normally."
        )
        if nested.get("error"):
            lines.append(f"Trigger: {_clip(nested.get('error'), 900)}")
    elif status == "intraday_analysis_error":
        lines.append(
            f"🔴 PM analysis failed ({nested.get('failure_status') or 'unknown'}); "
            "this was not a deliberate no-trade decision."
        )
        if nested.get("error"):
            lines.append(f"Error: {_clip(nested.get('error'), 900)}")
    elif status == "intraday_scan_crashed":
        # Operator-honesty fix: this used to be indistinguishable from a
        # healthy tick that ran and found nothing — the scan raised, the
        # caller swallowed the exception and set scan_result to None, and no
        # `intraday_scan` key ever reached this formatter. Now the crash
        # attaches a dict with this status, so it renders through the same
        # nested path `paid_analysis_suspended` / `intraday_analysis_error`
        # already use, instead of silently reading as "Status: ok".
        lines.append(
            f"🔴 Intraday opportunity scan crashed ({nested.get('error_type') or 'unknown'}); "
            "the deterministic intraday loss check above completed normally."
        )
        if nested.get("error"):
            lines.append(f"Error: {_clip(nested.get('error'), 900)}")

    pnl = _number(outer.get("daily_pnl"))
    ret = _number(outer.get("daily_return_pct"))
    if pnl is not None or ret is not None:
        pnl_text = f"${pnl:+,.2f}" if pnl is not None else "n/a"
        ret_text = f"{ret:+.2f}%" if ret is not None else "n/a"
        lines.append(f"📈 Session P&L: {pnl_text} ({ret_text})")

    candidates = [str(symbol).upper() for symbol in (nested.get("candidates") or []) if symbol]
    _append_signals(lines, snap, candidates=candidates)
    _append_pm(lines, snap)
    _append_risk(lines, snap)
    _append_gate_and_execution(lines, nested, snap)
    _append_footer(lines, run_id, snap, elapsed)
    return "\n".join(lines)
