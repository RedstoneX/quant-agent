"""The rehearsal report, written for the person who owns the money.

The owner is not a developer and has asked repeatedly for output he can read.
A rehearsal that produces a stack trace, a status enum, or a JSON blob has
failed at its job even when the harness worked perfectly. So every code this
module receives — session status, risk rule name, execution skip reason,
circuit trigger code — is translated into an English sentence that says what
happened and what it means for the day's trading.

The register is the one the brief asked for:

    morning session: 3 trades proposed, 2 executed, 1 rejected because the
    stop was too close to the entry

Anything the harness could not determine is stated as not determined. A
rehearsal that quietly presents a modelled fill as a real one would be worse
than no rehearsal, so the limits of the exercise are printed with the result,
every time, not tucked into a README.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

# --------------------------------------------------------------- phrasebook

# How each terminal session status reads to someone who wants to know whether
# the morning worked.
STATUS_PLAIN = {
    "executed": "The session ran all the way through and submitted orders.",
    "no_orders": (
        "The session ran all the way through, but in the end nothing was "
        "submitted."
    ),
    "no_trades": (
        "The session ran all the way through. The portfolio manager looked at "
        "the book and decided to propose no trades."
    ),
    "no_data": (
        "None of the research analysts produced anything usable, so no trades "
        "were even considered."
    ),
    "market_holiday": "The market was closed, so no session ran.",
    "broker_error": (
        "The session could not read the account from the broker and stopped "
        "before doing anything."
    ),
    "emergency_sold": (
        "The account had already fallen through its daily loss limit, so "
        "everything was sold and no new trades were considered."
    ),
    "paid_analysis_suspended": (
        "The session was stopped by the spending circuit before it could "
        "finish. Safety work (protective stops already at the broker, loss "
        "protection, order reconciliation) still ran; new analysis did not."
    ),
    "pm_agent_failure": (
        "The portfolio manager replied, but its answer could not be read, so "
        "there was no plan to act on."
    ),
    "agent_failure": (
        "The risk manager replied, but its answer could not be read, so "
        "nothing was approved."
    ),
    "rejected": "The risk manager rejected the whole plan.",
    "symbol_block": (
        "Every proposed trade was in a symbol the system is not permitted to "
        "trade, so nothing went through."
    ),
    "hard_risk_block": (
        "Every proposed trade broke a hard risk limit, so nothing went "
        "through."
    ),
    "buys_unfunded": (
        "Trades were approved, but there was not enough settled cash to pay "
        "for any of them, so nothing was submitted."
    ),
    # The five statuses below are real terminal outcomes of run_midday /
    # run_close / run_intra_check / run_evening — confirmed both by reading
    # src/pipeline.py (run_position_review's "reviewed", run_evening's
    # "analyzed", run_intra_check's "ok") and by production's OWN
    # notifier/feed code already treating them as healthy completions, not
    # failures: `src/trader_feed.py` groups "reviewed", "intraday_no_trades",
    # "no_trades" and "ok" together, and `src/notifier.py` groups "executed",
    # "analyzed", "reviewed", "preprocessed" and "reflected" together as the
    # statuses that get a normal notification. Before this fix, every one of
    # them was reported as VERDICT: FAIL here (via `_verdict`'s "healthy" set
    # below) purely because report.py's status vocabulary hadn't kept up with
    # `src/pipeline.py`'s — a plain midday/close position review or an
    # uneventful intra_check reads as a broken rehearsal even though nothing
    # went wrong. Reproduced against real production history (2026-08-29):
    # `midday`, `close` and `intra_check` rehearsals all completed cleanly and
    # were all still marked FAIL before this fix.
    "reviewed": (
        "The session ran all the way through and reviewed the open "
        "positions (or there were none to review)."
    ),
    "ok": (
        "The session ran its check and found nothing that required action."
    ),
    "analyzed": (
        "The session ran all the way through the evening review and "
        "analysis."
    ),
    "intraday_no_trades": (
        "The intra-session check ran its full analysis and decided to "
        "propose no trades."
    ),
    "intraday_executed": (
        "The intra-session check ran and submitted orders."
    ),
}

# Why an approved BUY died at the last moment, in the execution stage.
SKIP_PLAIN = {
    "no_price": "no current price was available for it",
    "stale_entry": (
        "the price had moved too far from the price the plan was built on"
    ),
    "qty_zero": "the position size worked out to less than one share",
    "slippage_gated": (
        "the gap between the buy and sell price was too wide — buying would "
        "have cost more than the plan allowed"
    ),
    "geometry_rr": (
        "the stop was too close to the entry for the possible gain to justify "
        "the risk"
    ),
    "insufficient_cash": "there was not enough settled cash to pay for it",
    "daily_loss_recheck": (
        "the account crossed its daily loss limit while the session was "
        "running, so buying stopped"
    ),
    "broker_rejected": "the broker refused the order",
    "broker_submit_exception": (
        "the order was sent but the broker never confirmed it, so it was left "
        "for the next session to reconcile"
    ),
}

# Deterministic risk rules that drop a trade before the risk manager sees it.
RULE_PLAIN = {
    "max_position_pct": "it would have made one position too large a share of the account",
    "max_total_position_pct": "it would have pushed total invested money above the ceiling",
    "max_sector_pct": "it would have concentrated too much money in one sector",
    "max_daily_loss_pct": "the account had already lost too much for one day",
    "require_stop_loss": "it had no protective stop attached",
    "cash_only": "it would have required borrowing, and this account never borrows",
    "correlation_cluster": (
        "it was too similar to positions already held — they would all move "
        "together"
    ),
    "drawdown_buy_cap": "the account is in a drawdown, so buying is capped",
    "macro_exposure_deviation": (
        "it moved the account too far from the exposure the macro view called for"
    ),
    "data_degraded": (
        "some of the data feeding the decision was missing or stale"
    ),
    "correlation_coverage_gap": (
        "there was not enough price history to check whether the positions "
        "move together"
    ),
    "hard_risk": "it broke a hard risk limit",
    "symbol_guard": "the symbol is not one this system is permitted to trade",
    "drawdown_buy_halved": "the size was halved because the account is in a drawdown",
}

# Why the spending circuit refused a call before the agent ever ran.
CIRCUIT_PLAIN = {
    "projected_session_cost_limit": (
        "this one call was estimated to cost more than the session's remaining "
        "spending allowance"
    ),
    "projected_daily_cost_limit": (
        "this one call was estimated to cost more than the day's remaining "
        "spending allowance"
    ),
    "session_retry_limit": "the session had already used its allowed number of calls",
    "cost_circuit_not_attached": "the spending circuit was not running",
    "pricing_preflight": (
        "current model pricing could not be confirmed, so no paid call could "
        "be budgeted safely"
    ),
}

AGENT_PLAIN = {
    "portfolio_manager": "Portfolio Manager (decides what to buy and sell)",
    "risk_manager": "Risk Manager (vetoes or trims the plan)",
    "tech_analyst": "Technical Analyst",
    "news_analyst": "News Analyst",
    "macro_analyst": "Macro Analyst",
    "earnings_analyst": "Earnings Analyst",
    "smart_money_analyst": "Insider-Filing Analyst",
    "position_reviewer": "Position Reviewer",
    "evening_analyst": "Evening Analyst",
}


def plain_agent(name: str) -> str:
    base = (name or "").rsplit("_morning", 1)[0].rsplit("_preprocess", 1)[0]
    return AGENT_PLAIN.get(base, AGENT_PLAIN.get(name, name))


# ------------------------------------------------------------------ model


@dataclass
class RehearsalReport:
    """Everything one rehearsal found, ready to render."""

    session: str
    rehearsed_date: str
    run_id: str
    source_run_id: str | None
    status: str
    completed: bool = False
    verdict: str = "FAIL"

    candidates: int = 0
    proposed: int = 0
    executed: int = 0
    rejections: list[dict] = field(default_factory=list)

    agents_ran: list[dict] = field(default_factory=list)
    blocked_agents: list[dict] = field(default_factory=list)

    orders_recorded: list[dict] = field(default_factory=list)
    isolation_checks: list[str] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    network_attempts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    fill_model: str = "immediate"
    provider_cost_usd: float = 0.0
    duration_s: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "session": self.session,
            "rehearsed_date": self.rehearsed_date,
            "run_id": self.run_id,
            "source_run_id": self.source_run_id,
            "status": self.status,
            "verdict": self.verdict,
            "completed": self.completed,
            "candidates": self.candidates,
            "proposed": self.proposed,
            "executed": self.executed,
            "rejections": self.rejections,
            "agents_ran": self.agents_ran,
            "blocked_agents": self.blocked_agents,
            "orders_recorded": self.orders_recorded,
            "isolation_checks": self.isolation_checks,
            "findings": self.findings,
            "unavailable": self.unavailable,
            "network_attempts": self.network_attempts,
            "notes": self.notes,
            "fill_model": self.fill_model,
            "provider_cost_usd": self.provider_cost_usd,
            "duration_s": round(self.duration_s, 2),
            "error": self.error,
        }

    # ----------------------------------------------------------- rendering

    def headline(self) -> str:
        """The one-line register the brief asked for."""
        bits = [f"{self.proposed} trade(s) proposed", f"{self.executed} executed"]
        if self.rejections:
            first = self.rejections[0]
            more = (
                f" (+{len(self.rejections) - 1} more)"
                if len(self.rejections) > 1 else ""
            )
            bits.append(
                f"{len(self.rejections)} rejected because "
                f"{first['plain']}{more}"
            )
        return f"{self.session} session: " + ", ".join(bits)

    def render(self) -> str:
        lines: list[str] = []
        add = lines.append

        title = f"REHEARSAL — {self.session} session of {self.rehearsed_date}"
        add(title)
        add("=" * len(title))
        add("")
        add(f"VERDICT: {self.verdict}")
        add("")
        add(self.headline())
        add("")

        add("WHAT HAPPENED")
        add("-------------")
        explanation = STATUS_PLAIN.get(
            self.status, f"The session ended with status '{self.status}'."
        )
        for line in _wrap(explanation):
            add("  " + line)
        if self.error:
            add("")
            for line in _wrap(f"Reported reason: {self.error}"):
                add("  " + line)
        add("")

        add("BY THE NUMBERS")
        add("--------------")
        add(f"  Trade ideas the analysts put forward ....... {self.candidates}")
        add(f"  Orders the portfolio manager proposed ...... {self.proposed}")
        add(f"  Orders that reached the broker ............. {self.executed}")
        add(f"  Orders stopped along the way ............... {len(self.rejections)}")
        add(f"  Money spent on model calls ................. "
            f"${self.provider_cost_usd:.2f} (nothing was sent to a provider)")
        add(f"  Time taken ................................. {self.duration_s:.1f}s")
        add("")

        add("WAS ANY AGENT STOPPED BEFORE IT COULD RUN?")
        add("------------------------------------------")
        if not self.blocked_agents:
            add("  No. Every agent the session needed was allowed to run.")
        for blocked in self.blocked_agents:
            add(f"  YES — {blocked['agent_plain']}.")
            for line in _wrap(blocked["plain"]):
                add("      " + line)
            if blocked.get("detail"):
                for line in _wrap(f"In the system's own words: {blocked['detail']}"):
                    add("      " + line)
        add("")

        add("WHAT WAS STOPPED, AND WHY")
        add("-------------------------")
        if not self.rejections:
            add("  Nothing was stopped.")
        for item in self.rejections:
            where = item.get("where", "")
            symbol = item.get("symbol") or "the whole plan"
            add(f"  {symbol} — {item['plain']}")
            if where:
                add(f"      (stopped at: {where})")
            if item.get("detail"):
                for line in _wrap(item["detail"], width=64):
                    add("      " + line)
        add("")

        add("WHICH AGENTS RAN (all answers replayed from recorded history)")
        add("------------------------------------------------------------")
        if not self.agents_ran:
            add("  None.")
        for agent in self.agents_ran:
            match = agent.get("similarity")
            match_text = (
                f"{match * 100:.0f}% match to the recorded question"
                if match is not None else "no match score"
            )
            add(f"  {agent['agent_plain']:<48} recorded {agent.get('recorded_at', '?')}"
                f"  [{match_text}]")
        add("")

        if self.orders_recorded:
            add("ORDERS THIS SESSION WOULD HAVE PLACED (none were sent)")
            add("-----------------------------------------------------")
            for order in self.orders_recorded:
                price = order.get("limit_price")
                price_text = f"@ ${price:,.2f}" if price else "at market"
                add(f"  {order['side'].upper():<5} {order['symbol']:<8} "
                    f"{order['qty']:>10} {price_text}")
            add("")

        if self.findings:
            add("THINGS THE HARNESS WANTS YOU TO KNOW")
            add("------------------------------------")
            for finding in self.findings:
                add(f"  * {finding.get('agent', 'session')}: {finding['detail']}")
            add("")

        add("WHAT THIS REHEARSAL COULD NOT TELL YOU")
        add("--------------------------------------")
        for line in self._limits():
            for wrapped in _wrap(line):
                add("  " + wrapped)
        add("")

        add("HOW WE KNOW NOTHING REAL WAS TOUCHED")
        add("------------------------------------")
        for check in self.isolation_checks:
            add(f"  [ok] {check}")
        add("")

        if self.notes:
            add("SETUP NOTES")
            add("-----------")
            for note in self.notes:
                for line in _wrap(note):
                    add("  " + line)
            add("")

        return "\n".join(lines)

    def _limits(self) -> list[str]:
        limits = []
        if self.fill_model == "immediate":
            limits.append(
                "Whether those orders would actually have filled. The rehearsal "
                "assumes every order fills instantly at the price asked. Real "
                "orders partial-fill, slip, or never fill at all."
            )
        else:
            limits.append(
                "Whether those orders would have filled. This rehearsal "
                "deliberately assumed none of them did."
            )
        limits.append(
            "Whether the broker would have accepted them. Buying power, "
            "pattern-day-trading rules, halted symbols and minimum order sizes "
            "are all decided on the broker's side and were never asked."
        )
        limits.append(
            "What the market was actually doing. Prices come from the last "
            "values the system recorded, not from this morning's tape, and no "
            "price history was available at all."
        )
        limits.append(
            "Whether today is really a trading day. The exchange calendar is a "
            "live lookup; the rehearsal assumes the date it was asked to "
            "rehearse is a session unless it falls on a weekend."
        )
        if self.unavailable:
            shown = ", ".join(self.unavailable[:6])
            more = f" and {len(self.unavailable) - 6} more" if len(self.unavailable) > 6 else ""
            limits.append(
                f"Anything that needed live market data. The rehearsal was "
                f"asked for {shown}{more}, and had none."
            )
        if self.network_attempts:
            limits.append(
                "Something in the session tried to reach the network and was "
                f"blocked ({'; '.join(self.network_attempts[:4])}). Whatever it "
                "would have fetched is missing from this result."
            )
        return limits


def _wrap(text: str, width: int = 72) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width=width) or [""]


# ------------------------------------------------------------- collection


def collect(
    *,
    session: str,
    rehearsed_date: str,
    run_id: str,
    source_run_id: str | None,
    result: dict,
    db_path: str,
    library,
    trading_stub,
    isolation_checks: list[str],
    unavailable: list[str],
    network_attempts: list[str],
    notes: list[str],
    fill_model: str,
    duration_s: float,
    error: str | None = None,
) -> RehearsalReport:
    """Turn a finished rehearsal into a report, reading the sandbox for truth.

    Deliberately reads the evidence the pipeline itself wrote — `agent_logs`,
    `specialist_evidence`, `llm_circuit_events` — rather than trusting a
    summary the harness assembled. If the pipeline did not record something,
    the report does not claim it.
    """
    status = str(result.get("status", "unknown")) if result else "did_not_finish"
    report = RehearsalReport(
        session=session,
        rehearsed_date=rehearsed_date,
        run_id=run_id,
        source_run_id=source_run_id,
        status=status,
        completed=bool(result),
        isolation_checks=isolation_checks,
        unavailable=unavailable,
        network_attempts=network_attempts,
        notes=notes,
        fill_model=fill_model,
        duration_s=duration_s,
        error=(error or result.get("error") if result else error),
    )

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        _collect_agents(conn, run_id, report, library)
        _collect_blocked(conn, run_id, report)
        _collect_rejections(conn, run_id, report, result or {})
        _collect_counts(conn, run_id, report, result or {})
        report.provider_cost_usd = float(
            conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM agent_logs WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0] or 0.0
        )
    finally:
        conn.close()

    report.orders_recorded = [o.as_plain() for o in getattr(trading_stub, "submitted", [])]
    report.executed = len([
        o for o in report.orders_recorded if o["side"] in ("buy", "sell")
        and "stop" not in (o.get("type") or "")
    ])
    report.findings = list(getattr(library, "findings", []))
    report.verdict = _verdict(report)
    return report


def _collect_agents(conn, run_id: str, report: RehearsalReport, library) -> None:
    matches = {m["row_id"]: m for m in getattr(library, "matches", [])}
    by_agent: dict[str, dict] = {}
    for match in matches.values():
        by_agent.setdefault(_base_agent(match["agent"]), match)
    rows = conn.execute(
        "SELECT agent_name, timestamp, status FROM agent_logs "
        "WHERE run_id = ? ORDER BY id", (run_id,),
    ).fetchall()
    for row in rows:
        name = str(row["agent_name"])
        match = by_agent.get(_base_agent(name), {})
        report.agents_ran.append({
            "agent": name,
            "agent_plain": plain_agent(name),
            "recorded_at": match.get("recorded_at", "—"),
            "similarity": match.get("similarity"),
            "status": row["status"],
        })


def _base_agent(name: str) -> str:
    base = name or ""
    for suffix in ("_morning", "_midday", "_close", "_evening", "_preprocess",
                   "_intra_check"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def _collect_blocked(conn, run_id: str, report: RehearsalReport) -> None:
    """Which agent, if any, the spending circuit refused before it ran."""
    rows = conn.execute(
        "SELECT trigger_code, detail, agent_name, event_type FROM llm_circuit_events "
        "WHERE run_id = ? ORDER BY id", (run_id,),
    ).fetchall()
    for row in rows:
        if str(row["event_type"]) not in ("quota_held", "suspended", "unavailable"):
            continue
        code = str(row["trigger_code"] or "")
        agent = str(row["agent_name"] or "an agent")
        report.blocked_agents.append({
            "agent": agent,
            "agent_plain": plain_agent(agent),
            "trigger_code": code,
            "plain": (
                "It was never called. The spending circuit refused it because "
                + CIRCUIT_PLAIN.get(code, f"of a limit named '{code}'")
                + "."
            ),
            "detail": str(row["detail"] or ""),
        })


def _collect_rejections(conn, run_id: str, report: RehearsalReport, result: dict) -> None:
    rows = conn.execute(
        "SELECT agent_name, kind, symbol, evidence_json FROM specialist_evidence "
        "WHERE run_id = ? ORDER BY id", (run_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["evidence_json"] or "{}")
        except (ValueError, TypeError):
            continue
        kind = str(row["kind"])
        symbol = row["symbol"]
        if kind == "execution_skip":
            reason = str(payload.get("reason", ""))
            report.rejections.append({
                "symbol": symbol,
                "where": "just before the order was sent",
                "reason": reason,
                "plain": SKIP_PLAIN.get(reason, f"of a check named '{reason}'"),
                "detail": str(payload.get("detail", "")),
            })
        elif kind == "pipeline_event" and payload.get("outcome") in ("blocked", "rejected", "failed"):
            reason = str(payload.get("reason", ""))
            stage = str(payload.get("stage", ""))
            report.rejections.append({
                "symbol": symbol,
                "where": _stage_plain(stage),
                "reason": reason,
                "plain": RULE_PLAIN.get(reason, _fallback_reason(reason)),
                "detail": str(payload.get("detail", "")),
            })
    if result.get("status") == "rejected" and not report.rejections:
        report.rejections.append({
            "symbol": None,
            "where": "the risk manager's review",
            "reason": "risk_manager_veto",
            "plain": "the risk manager vetoed the entire plan",
            "detail": str(result.get("reason", "")),
        })


def _fallback_reason(reason: str) -> str:
    text = (reason or "").strip()
    if not text:
        return "a check that gave no reason"
    if len(text) > 90 or " " in text:
        return text
    return f"of a check named '{text}'"


def _stage_plain(stage: str) -> str:
    return {
        "deterministic_gate": "the automatic risk limits, before the risk manager saw it",
        "risk": "the risk manager's review",
        "order": "the broker",
    }.get(stage, stage)


def _collect_counts(conn, run_id: str, report: RehearsalReport, result: dict) -> None:
    row = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE run_id = ? AND action IN ('BUY', 'SELL')",
        (run_id,),
    ).fetchone()
    report.proposed = int(row[0] or 0)
    holds = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE run_id = ? AND action = 'HOLD'", (run_id,),
    ).fetchone()
    report.candidates = report.proposed + int(holds[0] or 0)
    orders = result.get("orders") or []
    if orders and not report.proposed:
        report.proposed = len(orders)


def _verdict(report: RehearsalReport) -> str:
    """PASS only when the session reached a truthful, complete conclusion.

    A session that stops because the market is shut, or because the portfolio
    manager genuinely proposed nothing, is a working session. A session that
    is cut off by the spending circuit, loses an agent, or cannot read its own
    plan is not — those are the mornings that arrive broken.
    """
    if report.error and not report.status:
        return "FAIL"
    if report.blocked_agents:
        return "FAIL"
    if any(f["kind"] == "missing_recorded_response" for f in report.findings):
        return "FAIL"
    # "reviewed" / "ok" / "analyzed" / "intraday_no_trades" / "intraday_executed"
    # are the normal completions of run_midday, run_close, run_intra_check and
    # run_evening — see the matching STATUS_PLAIN entries above for how this was
    # confirmed against src/pipeline.py and against production's own
    # trader_feed.py / notifier.py status groupings.
    healthy = {
        "executed", "no_orders", "no_trades", "market_holiday",
        "reviewed", "ok", "analyzed", "intraday_no_trades", "intraday_executed",
    }
    return "PASS" if report.status in healthy else "FAIL"
