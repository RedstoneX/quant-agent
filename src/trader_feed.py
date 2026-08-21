"""Trader-oriented Telegram presentation for QAMC.

This module is observability-only. It never calls the broker, never mutates the
trading database, and never changes a trading decision. It turns already
persisted, validated decision evidence into a phone-friendly trader feed.

`src.notifier.py` remains the transport + legacy formatter. Modes that are not
trading-decision sessions fall straight through to that formatter unchanged.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from src.notifier import (
    _DB_PATH as _NOTIFIER_DB_PATH,
    format_session_result as _base_format_session_result,
)
from src.trading_calendar import et_now

logger = logging.getLogger(__name__)

_DB_PATH = _NOTIFIER_DB_PATH
_SWEEP_SYMBOLS = frozenset({"SGOV", "BIL"})
_TRADE_MODES = frozenset({"morning", "midday", "close", "once"})


def format_session_result(
    mode: str,
    result: dict | None,
    elapsed_seconds: float,
    error: BaseException | None = None,
) -> str | None:
    """Format a session for Telegram, enriching trading sessions only.

    Safety contract:
    - errors/non-dict results use the established formatter unchanged;
    - evening/earnings/meta/daily use the established formatter unchanged;
    - all enrichment is derived from the result dict + read-only SQLite;
    - any enrichment failure falls back to the established formatter.
    """
    if error is not None or not isinstance(result, dict):
        return _base_format_session_result(mode, result, elapsed_seconds, error=error)

    try:
        if mode == "intra_check":
            nested = result.get("intraday_scan")
            if isinstance(nested, dict):
                return _format_intraday(result, nested, elapsed_seconds)
            return _base_format_session_result(mode, result, elapsed_seconds, error=None)

        if mode in _TRADE_MODES:
            if mode in ("midday", "close") and isinstance(result.get("review"), dict):
                return _format_position_review(mode, result, elapsed_seconds)
            return _format_decision_session(mode, result, elapsed_seconds)

        return _base_format_session_result(mode, result, elapsed_seconds, error=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("trader-feed enrichment failed for %s: %s", mode, exc)
        return _base_format_session_result(mode, result, elapsed_seconds, error=None)


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m {secs}s"


def _clip(value: Any, n: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= n:
        return text
    return text[: max(0, n - 1)].rstrip() + "…"


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


def _read_run(run_id: str | None) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
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
        "cost": None,
        "calls": 0,
    }
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
                "fill_qty, fill_price FROM trades "
                "WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
            snapshot["trades"] = [dict(r) for r in rows]
        except sqlite3.DatabaseError:
            pass

        try:
            rows = conn.execute(
                "SELECT symbol, qty, avg_entry, current_price, market_value, "
                "unrealized_pnl FROM positions WHERE qty > 0 "
                "ORDER BY ABS(market_value) DESC",
            ).fetchall()
            snapshot["positions"] = [dict(r) for r in rows]
        except sqlite3.DatabaseError:
            pass

        try:
            rows = conn.execute(
                "SELECT cost_usd FROM agent_logs WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            snapshot["calls"] = len(rows)
            if rows and all(r[0] is not None for r in rows):
                snapshot["cost"] = sum(float(r[0]) for r in rows)
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
    regime = macro.get("regime")
    outlook = macro.get("equity_outlook")
    confidence = macro.get("confidence")
    guidance = macro.get("position_guidance") or {}
    target = guidance.get("target_invested_pct") if isinstance(guidance, dict) else None
    bits = [str(x) for x in (regime, outlook, confidence) if x]
    text = " / ".join(bits)
    if target is not None:
        text += f" · target exposure {target:g}%"
    if text:
        lines.append(f"📊 Market: {text}")


def _append_book(lines: list[str], snap: dict[str, Any]) -> None:
    positions = snap.get("positions") or []
    if not positions:
        return
    risk_rows = [r for r in positions if str(r.get("symbol", "")).upper() not in _SWEEP_SYMBOLS]
    sweep_rows = [r for r in positions if str(r.get("symbol", "")).upper() in _SWEEP_SYMBOLS]
    invested = sum(float(r.get("market_value") or 0) for r in risk_rows)
    parked = sum(float(r.get("market_value") or 0) for r in sweep_rows)
    text = f"💼 Book snapshot: {len(risk_rows)} risk pos · ${invested:,.0f} market exposure"
    if parked > 0:
        text += f" · ${parked:,.0f} T-bills"
    lines.append(text)


def _append_signals(lines: list[str], snap: dict[str, Any], candidates: list[str] | None = None) -> None:
    tech = [x for x in (snap.get("tech") or []) if isinstance(x, dict)]
    if candidates:
        wanted = {str(x).upper() for x in candidates}
        tech = [x for x in tech if str(x.get("symbol", "")).upper() in wanted]
    if not tech:
        if candidates:
            lines.append(f"🔎 Triggered: {', '.join(candidates[:5])}")
        return

    actionable = [
        x for x in tech
        if str(x.get("rating", "")).lower() not in ("", "neutral")
    ]
    lines.append(f"🔎 Signals: {len(tech)} analyzed · {len(actionable)} actionable")
    priority = {"strong_buy": 0, "strong_sell": 0, "buy": 1, "sell": 1, "neutral": 2}
    ordered = sorted(
        tech,
        key=lambda x: (
            priority.get(str(x.get("rating", "")).lower(), 3),
            str(x.get("symbol", "")),
        ),
    )
    for item in ordered[:5]:
        sym = str(item.get("symbol", "?")).upper()
        rating = str(item.get("rating", "?")).upper()
        conv = str(item.get("conviction", "?")).lower()
        rr = item.get("risk_reward")
        rr_text = f" · R/R {rr:g}" if isinstance(rr, (int, float)) else ""
        why = _clip(item.get("reasoning"), 120)
        line = f"   • {sym}: {rating}/{conv}{rr_text}"
        if why:
            line += f" — {why}"
        lines.append(line)


def _append_pm(lines: list[str], snap: dict[str, Any]) -> None:
    reasoning = snap.get("pm_reasoning")
    orders = [x for x in (snap.get("pm_orders") or []) if isinstance(x, dict)]
    targets = [x for x in (snap.get("pm_targets") or []) if isinstance(x, dict)]
    portfolio_view = reasoning.get("portfolio_view") if isinstance(reasoning, dict) else None

    if not (reasoning or orders or targets):
        return

    actionable = [x for x in orders if str(x.get("action", "")).upper() != "HOLD"]
    holds = sum(1 for x in orders if str(x.get("action", "")).upper() == "HOLD")
    if orders:
        lines.append(f"🧠 PM/Constructor: {len(actionable)} change(s) · {holds} hold(s)")
        for item in actionable[:5]:
            action = str(item.get("action", "?")).upper()
            sym = str(item.get("symbol", "?")).upper()
            alloc = item.get("allocation_pct")
            alloc_text = f" {alloc:g}%" if isinstance(alloc, (int, float)) else ""
            why = _clip(item.get("reasoning"), 120)
            text = f"   • {action} {sym}{alloc_text}"
            if why:
                text += f" — {why}"
            lines.append(text)
        hold_items = [
            x for x in orders if str(x.get("action", "")).upper() == "HOLD"
        ]
        for item in hold_items[:3]:
            sym = str(item.get("symbol", "?")).upper()
            why = _clip(item.get("reasoning"), 120)
            text = f"   • PASS {sym}"
            if why:
                text += f" — {why}"
            lines.append(text)
    elif targets:
        lines.append(f"🧠 PM: {len(targets)} target(s), no constructed order evidence")

    if portfolio_view:
        lines.append(f"   View: {_clip(portfolio_view, 180)}")


def _append_risk(lines: list[str], snap: dict[str, Any]) -> None:
    risk = snap.get("risk")
    if not isinstance(risk, dict):
        return
    approved = risk.get("approved")
    label = "APPROVED" if approved is True else "REJECTED" if approved is False else "UNKNOWN"
    category = risk.get("reason_category") or "?"
    scale = risk.get("scale_all_buys")
    scale_text = ""
    if isinstance(scale, (int, float)):
        scale_text = f" · buy size {scale * 100:.0f}%"
    mods = snap.get("risk_mods") or []
    lines.append(f"🛡️ Risk: {label} · {category}{scale_text} · {len(mods)} mod(s)")
    reasoning = _clip(risk.get("reasoning"), 180)
    if reasoning:
        lines.append(f"   {reasoning}")


def _append_gate_and_execution(
    lines: list[str],
    result: dict,
    snap: dict[str, Any],
) -> None:
    status = str(result.get("status", "unknown"))
    skips = [x for x in (result.get("execution_skips") or []) if isinstance(x, dict)]
    if not skips:
        skips = [x for x in (snap.get("skips") or []) if isinstance(x, dict)]

    if status in {"hard_risk_block", "symbol_block"}:
        lines.append(f"⚙️ Deterministic gate: BLOCKED — {_clip(result.get('reason'), 220)}")
    elif skips:
        lines.append(f"⚙️ Execution gate: {len(skips)} skip(s)")
        for item in skips[:5]:
            sym = str(item.get("symbol", "?")).upper()
            reason = str(item.get("reason", "?"))
            detail = _clip(item.get("detail"), 150)
            text = f"   • {sym}: {reason}"
            if detail:
                text += f" — {detail}"
            lines.append(text)

    trades = [x for x in (snap.get("trades") or []) if isinstance(x, dict)]
    sweep = [x for x in trades if str(x.get("action", "")).upper().startswith("SWEEP_")]
    real = [
        x for x in trades
        if str(x.get("action", "")).upper() not in {"HOLD"}
        and not str(x.get("action", "")).upper().startswith("SWEEP_")
    ]

    if sweep:
        funding_sells = [x for x in sweep if str(x.get("action", "")).upper() == "SWEEP_SELL"]
        parking_buys = [x for x in sweep if str(x.get("action", "")).upper() == "SWEEP_BUY"]
        bits = []
        if funding_sells:
            bits.append(f"{len(funding_sells)} T-bill funding sell")
        if parking_buys:
            bits.append(f"{len(parking_buys)} cash-park buy")
        lines.append("💵 Cash management: " + " · ".join(bits))

    if real:
        lines.append(f"⚡ Execution: {len(real)} market-risk action(s)")
        for trade in real[:8]:
            action = str(trade.get("action", "?")).upper()
            sym = str(trade.get("symbol", "?")).upper()
            qty = trade.get("fill_qty") or trade.get("qty")
            px = trade.get("fill_price") or trade.get("price")
            status_text = trade.get("fill_status") or "recorded"
            qty_text = f" {qty:g}" if isinstance(qty, (int, float)) else ""
            px_text = f" @ ${px:,.2f}" if isinstance(px, (int, float)) and px > 0 else ""
            lines.append(f"   • {action} {sym}{qty_text}{px_text} · {status_text}")
    else:
        _append_no_trade_reason(lines, result, snap, skips)


def _append_no_trade_reason(
    lines: list[str],
    result: dict,
    snap: dict[str, Any],
    skips: list[dict],
) -> None:
    status = str(result.get("status", "unknown"))
    risk = snap.get("risk")
    pm_orders = [x for x in (snap.get("pm_orders") or []) if isinstance(x, dict)]
    actionable = [x for x in pm_orders if str(x.get("action", "")).upper() != "HOLD"]

    if status == "rejected":
        reason = _clip(result.get("reason"), 220)
        lines.append(f"⏸️ NO TRADE — Risk vetoed the plan{': ' + reason if reason else ''}")
    elif status in {"hard_risk_block", "symbol_block"}:
        lines.append("⏸️ NO TRADE — deterministic eligibility blocked the proposed action(s)")
    elif status == "buys_unfunded" or skips:
        lines.append("⏸️ NO TRADE — decision(s) survived review but execution could not complete")
    elif isinstance(risk, dict) and risk.get("approved") is False:
        lines.append(f"⏸️ NO TRADE — Risk rejected: {_clip(risk.get('reasoning'), 200)}")
    elif pm_orders and not actionable:
        lines.append("⏸️ NO TRADE — PM/constructor produced HOLD only")
    elif snap.get("pm_reasoning") and not pm_orders:
        lines.append("⏸️ NO TRADE — PM produced no executable portfolio change")
    elif not snap.get("pm_reasoning"):
        lines.append("⏸️ NO TRADE — decision chain did not produce recorded PM evidence")
    else:
        lines.append("⏸️ NO TRADE — no market-risk order was submitted")


def _append_footer(lines: list[str], run_id: str | None, snap: dict[str, Any], elapsed: float) -> None:
    bits = []
    if run_id and run_id != "?":
        bits.append(f"run {run_id}")
    cost = snap.get("cost")
    calls = snap.get("calls") or 0
    if isinstance(cost, (int, float)):
        bits.append(f"LLM ${cost:.4f}" if cost < 0.01 else f"LLM ${cost:.2f}")
        bits[-1] += f"/{calls} call{'s' if calls != 1 else ''}"
    bits.append(_fmt_elapsed(elapsed))
    lines.append("🧾 " + " · ".join(bits))


def _format_decision_session(mode: str, result: dict, elapsed: float) -> str:
    run_id = result.get("run_id")
    snap = _read_run(run_id)
    status = str(result.get("status", "unknown"))
    timestamp = et_now().strftime("%H:%M ET")
    lines = [f"{_status_emoji(status)} {mode.upper()} · {timestamp}", f"Status: {status}"]

    gaps = result.get("stop_coverage_gaps")
    if isinstance(gaps, list) and gaps:
        syms = ", ".join(str(x.get("symbol", "?")) for x in gaps[:6] if isinstance(x, dict))
        lines.append(f"🚨 STOP-COVERAGE GAP: {len(gaps)} · {syms}")

    data_status = result.get("data_status") or {}
    if isinstance(data_status, dict):
        degraded = [k for k, v in data_status.items() if v not in ("ok", "empty")]
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
    timestamp = et_now().strftime("%H:%M ET")
    review = result.get("review") or {}
    lines = [f"{_status_emoji(status)} {mode.upper()} REVIEW · {timestamp}", f"Status: {status}"]

    gaps = result.get("stop_coverage_gaps")
    if isinstance(gaps, list) and gaps:
        syms = ", ".join(str(x.get("symbol", "?")) for x in gaps[:6] if isinstance(x, dict))
        lines.append(f"🚨 STOP-COVERAGE GAP: {len(gaps)} · {syms}")

    positions = result.get("positions")
    risk_level = review.get("risk_level")
    if positions is not None or risk_level:
        bits = []
        if positions is not None:
            bits.append(f"{positions} position(s)")
        if risk_level:
            bits.append(f"risk {risk_level}")
        lines.append("📍 Review: " + " · ".join(bits))

    overall = _clip(review.get("overall_assessment"), 220)
    if overall:
        lines.append(f"🧠 Reviewer: {overall}")

    actions = [x for x in (review.get("actions") or []) if isinstance(x, dict)]
    actionable = [x for x in actions if str(x.get("action", "")).upper() != "HOLD"]
    holds = len(actions) - len(actionable)
    if actions:
        lines.append(f"🎯 Decisions: {len(actionable)} action(s) · {holds} hold(s)")
        for action in actionable[:6]:
            act = str(action.get("action", "?")).upper()
            sym = str(action.get("symbol", "?")).upper()
            reason = _clip(action.get("reason"), 150)
            stop = action.get("new_stop_price")
            stop_text = f" → stop ${stop:,.2f}" if isinstance(stop, (int, float)) else ""
            text = f"   • {act} {sym}{stop_text}"
            if reason:
                text += f" — {reason}"
            lines.append(text)
        hold_items = [
            x for x in actions if str(x.get("action", "")).upper() == "HOLD"
        ]
        for action in hold_items[:3]:
            sym = str(action.get("symbol", "?")).upper()
            reason = _clip(action.get("reason"), 120)
            text = f"   • HOLD {sym}"
            if reason:
                text += f" — {reason}"
            lines.append(text)

    _append_book(lines, snap)
    _append_gate_and_execution(lines, result, snap)
    if not actionable and not [
        x for x in (snap.get("trades") or [])
        if str(x.get("action", "")).upper() not in {"HOLD", "SWEEP_BUY", "SWEEP_SELL"}
    ]:
        if actions and holds:
            lines.append("   ↳ Reviewer explicitly held the book; no position-management trade warranted")
        elif not actions and positions == 0:
            lines.append("   ↳ No market-risk positions required review")

    _append_footer(lines, run_id, snap, elapsed)
    return "\n".join(lines)


def _format_intraday(outer: dict, nested: dict, elapsed: float) -> str:
    run_id = nested.get("run_id") or outer.get("run_id")
    snap = _read_run(run_id)
    status = str(nested.get("status", "unknown"))
    timestamp = et_now().strftime("%H:%M ET")
    lines = [f"⚡ INTRADAY OPPORTUNITY · {timestamp}", f"Status: {status}"]

    pnl = outer.get("daily_pnl")
    ret = outer.get("daily_return_pct")
    if isinstance(pnl, (int, float)) or isinstance(ret, (int, float)):
        pnl_text = f"${pnl:+,.2f}" if isinstance(pnl, (int, float)) else "n/a"
        ret_text = f"{ret:+.2f}%" if isinstance(ret, (int, float)) else "n/a"
        lines.append(f"📈 Session P&L: {pnl_text} ({ret_text})")

    candidates = nested.get("candidates") or []
    candidates = [str(x).upper() for x in candidates if x]
    _append_signals(lines, snap, candidates=candidates)
    _append_pm(lines, snap)
    _append_risk(lines, snap)
    _append_gate_and_execution(lines, nested, snap)
    _append_footer(lines, run_id, snap, elapsed)
    return "\n".join(lines)
