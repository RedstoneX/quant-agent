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
    _actionable_coverage_gaps,
    _append_company_identities,
    _clip_text,
    _DB_PATH as _NOTIFIER_DB_PATH,
    _fmt_signed_money,
    _new_block,
    _new_section,
    _seal_section,
    format_session_result as _base_format_session_result,
    humanize_status,
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


def _append_identities(
    lines: list[str],
    run_id: str | None,
    result: dict | None,
    extra_symbols: list[str] | None = None,
) -> None:
    """`who:` block for the rich trader-feed formatters below — the same
    `extract_alert_symbols` source already used to decide which tickers get
    a tap-through link, plus (2026-09-17) `extra_symbols` — the symbols a
    formatter's "🔎 Signals" section is about to show as bullets, via
    `_signal_symbols`. A no-trade intraday tick has no order/trade/skip
    evidence for `extract_alert_symbols` to find, so before this its
    analyzed-but-not-traded candidates (e.g. "VST", "AVGO") got no identity
    line at all — the operator saw a bare ticker with no idea which company
    it was. Both lists feed the SAME `src.notifier._append_company_identities`
    call (the ONE place that turns symbols into identity text; see its
    docstring — do not add a second lookup, extend the symbol list instead),
    which already dedupes, so a symbol present in both never renders twice.
    Deliberately called LAST by every formatter below, after the footer:
    `TelegramNotifier._build_payload`'s length-budget fallback truncates
    from the tail of the message when it must, so whatever is appended last
    is the first thing a length-pressured alert drops — and identity lines
    are the least important content here, never the order list, the PM/risk
    rationale, or the footer.

    Wrapped locally (not left to the `format_session_result` dispatcher's
    own try/except) because that outer handler's fallback on any exception
    is the OLD, plainer base formatter for the WHOLE message — losing every
    section this module adds, not just the identity garnish. A failure here
    must cost only the `who:` block.
    """
    try:
        symbols = extract_alert_symbols(run_id, result)
        for sym in extra_symbols or []:
            sym = str(sym or "").strip().upper()
            if sym and sym not in symbols:
                symbols.append(sym)
        _append_company_identities(lines, symbols)
    except Exception as exc:  # noqa: BLE001 — identities are a garnish, never worth the alert
        logger.warning("trader-feed: company identities failed: %s", exc)


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
    `candidates` when given, priority-ordered, capped at 4 — shared by
    `_append_signals` (the live per-run listing) and the hourly desk-check
    summary so both name exactly the symbols they actually show, never more
    and never a second, divergent ordering.
    """
    tech = [row for row in (snap.get("tech") or []) if isinstance(row, dict)]
    if candidates:
        wanted = {str(symbol).upper() for symbol in candidates}
        tech = [row for row in tech if str(row.get("symbol", "")).upper() in wanted]
    priority = {"strong_buy": 0, "strong_sell": 0, "buy": 1, "sell": 1, "neutral": 2}
    ordered = sorted(
        tech,
        key=lambda row: (
            priority.get(str(row.get("rating", "")).lower(), 3),
            str(row.get("symbol", "")),
        ),
    )
    return ordered[:4]


def _signal_row_line(row: dict) -> str:
    """One '   • SYM: RATING/conviction · R/R x.xx — reason' bullet, the
    one place that renders a tech-analysis row this way — shared by
    `_append_signals` and the hourly desk-check summary."""
    sym = str(row.get("symbol", "?")).upper()
    rating = str(row.get("rating", "?")).upper()
    conviction = str(row.get("conviction", "?")).lower()
    rr = row.get("risk_reward")
    rr_text = f" · R/R {rr:g}" if isinstance(rr, (int, float)) else ""
    reason = _clip(row.get("reasoning"), 420)
    text = f"   • {sym}: {rating}/{conviction}{rr_text}"
    if reason:
        text += f" — {reason}"
    return text


def _signal_symbols(snap: dict[str, Any], candidates: list[str] | None = None) -> list[str]:
    """Upper-cased symbols the signals section actually renders as bullets
    — fed into `_append_identities` so the `who:` block can name them too,
    without a second CompanyProfileStore lookup anywhere (see
    `src.notifier._append_company_identities`'s docstring: it must stay the
    ONLY place that turns a symbol list into identity text)."""
    return [
        str(row.get("symbol", "")).upper()
        for row in _signal_rows(snap, candidates)
        if row.get("symbol")
    ]


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


def _format_decision_session(mode: str, result: dict, elapsed: float) -> str:
    run_id = result.get("run_id")
    snap = _read_run(run_id)
    status = str(result.get("status", "unknown"))
    lines = [
        f"{_status_emoji(status)} {mode.upper()} · {et_now().strftime('%H:%M ET')}",
        f"Status: {humanize_status(status)}",
    ]

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
    _new_block(lines, _append_signals, snap)
    _new_block(lines, _append_pm, snap, may_glue=True)
    _new_block(lines, _append_risk, snap)
    _new_block(lines, _append_gate_and_execution, result, snap)
    _new_block(lines, _append_footer, snap, elapsed)
    _new_block(lines, _append_identities, run_id, result, _signal_symbols(snap))
    return "\n".join(lines)


def _format_position_review(mode: str, result: dict, elapsed: float) -> str:
    run_id = result.get("run_id")
    snap = _read_run(run_id)
    status = str(result.get("status", "unknown"))
    review = result.get("review") if isinstance(result.get("review"), dict) else {}
    lines = [
        f"{_status_emoji(status)} {mode.upper()} REVIEW · {et_now().strftime('%H:%M ET')}",
        f"Status: {humanize_status(status)}",
    ]

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

        overall = _clip(review.get("overall_assessment"), 650)
        if overall:
            lines.append(f"🧠 Reviewer: {overall}")

    _new_block(lines, _render_review_summary)

    actions = [row for row in (review.get("actions") or []) if isinstance(row, dict)]
    actionable = [row for row in actions if str(row.get("action", "")).upper() != "HOLD"]
    holds = [row for row in actions if str(row.get("action", "")).upper() == "HOLD"]

    def _render_decisions(lines: list[str]) -> None:
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

    _new_block(lines, _render_decisions)

    _new_block(lines, _append_book, snap)

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

    _new_block(lines, _render_gate_and_outcome)
    _new_block(lines, _append_footer, snap, elapsed)
    _new_block(lines, _append_identities, run_id, result)
    return "\n".join(lines)


def _fmt_pnl_line(label: str, pnl: float | None, ret: float | None) -> str:
    """'📈 Session P&L: +$12.34 (+0.10%)' — shared by the per-tick intraday
    formatter and the hourly desk-check summary."""
    pnl_text = _fmt_signed_money(pnl) if pnl is not None else "n/a"
    ret_text = f"{ret:+.2f}%" if ret is not None else "n/a"
    return f"{label} {pnl_text} ({ret_text})"


def _format_intraday(outer: dict, nested: dict, elapsed: float) -> str:
    run_id = nested.get("run_id") or outer.get("run_id")
    snap = _read_run(run_id)
    status = str(nested.get("status", "unknown"))
    lines = [
        f"⚡ INTRADAY OPPORTUNITY · {et_now().strftime('%H:%M ET')}",
        f"Status: {humanize_status(status)}",
    ]

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

    pnl = _number(outer.get("daily_pnl"))
    ret = _number(outer.get("daily_return_pct"))
    if pnl is not None or ret is not None:
        _new_section(lines, _fmt_pnl_line("📈 Session P&L:", pnl, ret))

    candidates = [str(symbol).upper() for symbol in (nested.get("candidates") or []) if symbol]
    _new_block(lines, _append_signals, snap, candidates=candidates)
    _new_block(lines, _append_pm, snap, may_glue=True)
    _new_block(lines, _append_risk, snap)
    _new_block(lines, _append_gate_and_execution, nested, snap)
    _new_block(lines, _append_footer, snap, elapsed)
    # `nested`, not `outer`: on the intraday path the traded-order evidence
    # (and the run_id it's keyed by) lives in the `intraday_scan` sub-dict —
    # same source `_append_gate_and_execution` above already reads.
    _new_block(lines, _append_identities, run_id, nested, _signal_symbols(snap, candidates))
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


def _is_hourly_checkpoint(now=None) -> bool:
    """True at the tick that owes this trading hour its guaranteed message.

    The :00 tick owns every ordinary hour. The 9:30 session-open tick is
    the exception: it is the first tick of the day, so there is no :00
    tick before it to have covered the 9:00-9:30 gap (during which the
    market was closed anyway) — 9:30 stands in for it.
    """
    now = now or et_now()
    return now.minute == 0 or (now.hour == 9 and now.minute == 30)


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


def _format_hourly_desk_check(result: dict, nested: dict | None, elapsed: float) -> str:
    """The guaranteed top-of-hour message: what happened across BOTH ticks
    of the last hour, in plain English, even when neither one had anything
    that needed the owner's attention on its own.
    """
    run_id = result.get("run_id")
    snap = _read_run(run_id)
    lines = [f"🕐 DESK CHECK · {et_now().strftime('%H:%M ET')}", "Covering the last hour"]

    trade_count = _read_hour_trade_count()
    if trade_count == 0:
        _new_section(lines, "⏸️ No action this hour")
    else:
        _new_section(
            lines,
            f"⚡ {trade_count} order(s) this hour — see the alert(s) already sent",
        )

    pnl = _number(result.get("daily_pnl"))
    ret = _number(result.get("daily_return_pct"))
    if pnl is not None or ret is not None:
        _new_section(lines, _fmt_pnl_line("📈 Session P&L:", pnl, ret))

    positions = [row for row in (snap.get("positions") or []) if isinstance(row, dict)]
    risk_positions = [
        row for row in positions
        if str(row.get("symbol", "")).upper() not in _SWEEP_SYMBOLS
    ]
    _new_section(lines, f"💼 Positions held: {len(risk_positions)}")

    hour_rows = _read_hour_evidence()

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
        for row in ordered[:8]:
            lines.append(_signal_row_line(row))

    if hour_rows:
        _new_block(lines, _render_hour_signals)
    else:
        _new_section(lines, f"🔎 Movers scanned: {_movers_scanned_text(nested)}")

    _cov_start = len(lines)
    _append_coverage_gaps(lines, result)
    _cov_added = len(lines) > _cov_start
    _seal_section(lines, _cov_start)
    if not _cov_added:
        _new_section(lines, "🛡️ Stop coverage: OK")

    _new_block(lines, _append_footer, snap, elapsed)
    hour_symbols = [
        str(row.get("symbol", "")).upper() for row in hour_rows if row.get("symbol")
    ]
    _new_block(lines, _append_identities, run_id, result, hour_symbols)
    return "\n".join(lines)
