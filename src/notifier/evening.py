"""Evening P&L block and evening message body.

Moved verbatim from the former src/notifier.py; the package re-exports it.
"""

from __future__ import annotations

from src.notifier.base import (
    _clip_text,
)
from src.notifier.sections import (
    _fmt_signed_money,
    _new_block,
    _seal_section,
)
from src.notifier.markup import (
    _attr_or_key,
)
from src.notifier.gaps import (
    _append_coverage_gap_banner,
)
from src.notifier.snapshots import (
    _append_position_snapshot,
)


def _evening_pnl_block(result: dict) -> list[str]:
    """The evening message's own P&L lines — Daily P&L (4pm-correct where
    available), equity, and the same day's return against capital actually
    at risk.

    Lifted OUT of `_append_evening_body` unchanged on 2026-09-18 so it can
    lead the message rather than sit below the escalation banners and the
    cost lines (owner: P&L directly under the heading, every message). Not
    one figure, basis or fallback was altered in the move — this is the
    same arithmetic in a different place, which is why the existing
    4pm-vs-real-time regression tests still pin it.

    Deliberately NOT `trader_feed._pnl_section_lines`: that renders the
    real-time `daily_pnl`, and the evening message must show the official
    close-to-close figure. Using the shared one here would leak exactly the
    after-hours number the 4pm path exists to keep out.
    """
    lines: list[str] = []
    # Daily P&L summary — the headline of the evening push. Operator wants to
    # know "did I make money today" without grepping logs.
    #
    # Prefer the TRUE close-to-close ("4pm-to-4pm") P&L the pipeline computed
    # from Alpaca portfolio_history (pnl_4pm / equity_close = today's official
    # regular-session close). That's clean of after-hours drift AND free of the
    # off-by-one trap of differencing account.last_equity (which is the PRIOR
    # day's close). Fall back to the real-time prior-close→now diff when the
    # 4pm figures aren't available (API gap / legacy result dicts).
    daily_pnl = result.get("daily_pnl")
    total_value = result.get("total_value")
    pnl_4pm = result.get("pnl_4pm")
    equity_close = result.get("equity_close")

    _fmt_pnl = _fmt_signed_money

    # Phase 6 (§6.3b) — the SAME day's P&L expressed against capital
    # actually at risk, not just against total equity. "Risk capital" here
    # is `sum((entry - stop) x shares)` across open positions — audit §1.3's
    # `budget_risk_dollars` from `src.risk.metrics.portfolio_heat`, reused
    # (not recomputed) via `TradingPipeline._build_portfolio_heat` and
    # threaded through evening's result dict as `risk_capital_dollars`.
    # Equity tells you how the whole book did; this tells you how the
    # capital that was actually exposed today did — a much bigger number on
    # a day the book was mostly in cash or mostly stopped-out to breakeven.
    risk_capital = result.get("risk_capital_dollars")

    def _append_risk_capital_line(pnl: float | None) -> None:
        if risk_capital is None:
            return  # heat build failed or wasn't available — say nothing, not a guess
        if risk_capital <= 0:
            # A flat book (or a book where every stop has trailed past
            # entry, releasing all risk) — not a divide-by-zero, and NOT a
            # fabricated 0%: there was no capital at risk to measure P&L
            # against today.
            lines.append("   vs risk capital: n/a — no capital currently at risk (flat book)")
            return
        if pnl is None:
            return
        risk_pct = pnl / risk_capital * 100
        risk_str = f"+{risk_pct:.2f}%" if pnl >= 0 else f"{risk_pct:.2f}%"
        lines.append(f"   vs risk capital: {risk_str}  (${risk_capital:,.2f} at risk)")

    if pnl_4pm is not None and equity_close is not None:
        # baseline = prior official close = equity_close - pnl_4pm.
        baseline = equity_close - pnl_4pm
        if baseline > 0:
            r = pnl_4pm / baseline * 100
            ret_str = f"+{r:.2f}%" if pnl_4pm >= 0 else f"{r:.2f}%"
        else:
            ret_str = "n/a"
        lines.append(f"💰 Daily P&L: {_fmt_pnl(pnl_4pm)} ({ret_str})  ·  4pm close")
        lines.append(f"   Equity: ${equity_close:,.2f}")
        _append_risk_capital_line(pnl_4pm)
    elif daily_pnl is not None and total_value is not None:
        # Fallback: real-time diff (prior close → 8pm, includes after-hours).
        # Return is P&L over PRIOR-day equity (= total_value − daily_pnl); using
        # current equity would understate losses (denominator includes the draw).
        prior_equity = total_value - daily_pnl
        if prior_equity > 0:
            ret_pct = (daily_pnl / prior_equity) * 100
            ret_str = f"+{ret_pct:.2f}%" if daily_pnl >= 0 else f"{ret_pct:.2f}%"
        else:
            # prior_equity <= 0 → return % undefined; "0.00%" would mislead.
            ret_str = "n/a"
        lines.append(f"💰 Daily P&L: {_fmt_pnl(daily_pnl)} ({ret_str})")
        lines.append(f"   Equity: ${total_value:,.2f}")
        _append_risk_capital_line(daily_pnl)

    return lines


def _append_evening_body(lines: list[str], result: dict) -> None:
    # === Escalation banners (first thing read, before Daily P&L) ===
    analysis = result.get("analysis")

    def _render_escalation_banners(lines: list[str]) -> None:
        # (0) Dead-man's check: a market-day session that left zero agent_logs
        # today silently never ran (disabled timer, stuck lock, half-day window
        # math). morning missing is unambiguous → 🛑; midday/close can be
        # legitimately skipped on some early-close days → softer ⚠️.
        missing = result.get("missing_sessions")
        if isinstance(missing, list) and missing:
            # Prefix match: the sharpened probes emit decorated entries like
            # "morning (PM plan never risk-reviewed — checkpoint unconsumed)" —
            # they carry the diagnosis and must hit the hard banner too.
            hard = [m for m in missing if m == "morning" or str(m).startswith("morning (")]
            for m in hard:
                detail = m if m != "morning" else ("morning — no agent activity logged; check the timer/scheduler")
                lines.append(f"🛑 INCOMPLETE: MORNING SESSION TODAY — {detail}")
            soft = [m for m in missing if m not in hard]
            if soft:
                lines.append(f"⚠️ no activity logged today for: {', '.join(soft)}")

        # (0b) Broker-truth stop-coverage gap (last check before overnight).
        _append_coverage_gap_banner(lines, result)

        # (1) LLM-graded escalation — evening's contract maps thesis_trajectory=
        # broken / macro_warning_ignored loss patterns to risk_rating >= elevated.
        risk_for_banner = _attr_or_key(analysis, "risk_rating")
        if isinstance(risk_for_banner, str) and risk_for_banner.lower() in ("elevated", "high"):
            lines.append(f"🚨 OPERATOR ATTENTION — risk_rating={risk_for_banner}")

        # A (2) used to sit here: a deterministic banner raised when the
        # day's loss reached 80% of the account-level daily-loss circuit
        # breaker. That breaker was removed 2026-09-20 on the owner's
        # instruction (retired item 32, docs/INCIDENT_HISTORY.md), so there
        # is no limit left to measure the day against.

    _new_block(lines, _render_escalation_banners)

    # The evening P&L block is NOT rendered here any more: owner, 2026-09-18,
    # "all the P&L information has to go at the very top of every telegram
    # alert, right after the first line, which is really the heading." It now
    # renders from `_evening_pnl_block` above the escalation banners and above
    # the cost lines — see `_pnl_lines_for`. Nothing about the figures changed.

    # Suggested actions — surfaced HIGH in the message (right after the
    # headline P&L) so the tail-clip truncation in send() can never eat
    # them. On exactly the high-risk days where these are populated the
    # message is longest, and these are the lines most worth reading.
    # Only shown when risk_rating is elevated/high. (The P&L history
    # text table that used to follow was replaced by the daily CSV
    # export — PR #99.)
    _actions_start = len(lines)
    risk_for_actions = _attr_or_key(analysis, "risk_rating")
    if isinstance(risk_for_actions, str) and risk_for_actions.lower() in ("elevated", "high"):
        actions = _attr_or_key(analysis, "suggested_actions") or []
        if isinstance(actions, list) and actions:
            lines.append("⚡ Suggested actions:")
            for act in actions[:5]:
                if not isinstance(act, str):
                    continue
                # 500, not 200 — this is exactly the field the operator
                # complained about: a per-symbol call like "CRM: strong
                # heavy accumulation volume, add on any weakness..." was
                # being cut off mid-sentence at 200 chars with no ellipsis.
                lines.append(f"   • {_clip_text(act, 500)}")
    _seal_section(lines, _actions_start)

    # Position snapshot: total invested + cash + top winners/losers.
    # Helper queries the live DB so this works regardless of how the
    # evening result dict is constructed.
    # `total_value` used to be a local of the P&L block that moved to
    # `_evening_pnl_block` (2026-09-18); read it back from the same key the
    # block reads, so the snapshot's denominator is unchanged.
    _new_block(lines, _append_position_snapshot, result.get("total_value"))

    _tomorrow_start = len(lines)
    analysis = result.get("analysis")
    risk = _attr_or_key(analysis, "risk_rating")
    bias = _attr_or_key(analysis, "tomorrow_bias")
    conv = _attr_or_key(analysis, "tomorrow_conviction")
    if risk or bias or conv:
        bits = []
        if risk:
            bits.append(f"risk={risk}")
        if bias:
            bits.append(f"bias={bias}")
        if conv:
            bits.append(f"conv={conv}")
        lines.append("🔮 Tomorrow: " + "  ".join(bits))
    outlook = _attr_or_key(analysis, "tomorrow_outlook") or ""
    if outlook:
        lines.append(f"   {_clip_text(outlook, 1000)}")
    _seal_section(lines, _tomorrow_start)

    # Auto-meta piggyback (Round 2 enabled this; Round 6 adds the
    # dry-run staging hint). When today is the last trading day of a
    # quarter, run_evening invokes run_quarterly_meta_reflection and
    # stuffs the result into `result['auto_meta']`. Surface dry-run
    # proposals so the operator knows to review proposed_edits.json
    # before next quarter.
    _meta_start = len(lines)
    auto_meta = result.get("auto_meta")
    if isinstance(auto_meta, dict):
        # audit round 2 (#15/#19): the producer
        # (run_quarterly_meta_reflection) never emits top-level
        # "applied"/"rejected" ints — the counts exist only as LISTS nested
        # inside editor_report (ApplicationReport.to_dict). The old flat
        # .get("applied", 0)/.get("rejected", 0) reads always yielded 0/0,
        # so both hint branches were dead code and the once-a-quarter
        # "review proposed_edits.json" operator prompt never fired (the
        # 2026-06-30 quarter end went through this dead path). Stage-only
        # proposals surface as "rejected" entries whose reason carries
        # "dry_run" — count those separately for accurate wording.
        report = auto_meta.get("editor_report") or {}
        applied = len(report.get("applied") or [])
        rej_list = report.get("rejected") or []
        rejected = len(rej_list)
        staged = sum(1 for r in rej_list if isinstance(r, dict) and "dry_run" in str(r.get("reason", "")))
        proposed = int(auto_meta.get("proposed_learnings_count") or 0)
        period = auto_meta.get("period", "?")
        status = auto_meta.get("status", "?")
        if status == "auto_meta_error":
            err = _clip_text(str(auto_meta.get("error", "?")), 600)
            lines.append(f"🧪 meta {period}: ERROR — {err}")
        elif status == "digest_only":
            # LLM reflection step failed after the digest was written —
            # the learning loop is broken until next quarter.
            lines.append(f"🧪 meta {period}: digest written but LLM reflection FAILED — check logs")
        elif applied > 0:
            lines.append(f"🧪 meta {period}: applied {applied} learning(s); rejected {rejected}")
        elif staged > 0:
            # Dry-run staged proposals (none actually applied).
            lines.append(
                f"🧪 meta {period}: {staged} proposal(s) staged "
                f"(dry-run — see data/evolution/{period}/proposed_edits.json)"
            )
        elif rejected > 0:
            # Live/off mode with everything rejected by guardrails or the
            # enabled=false short-circuit — still worth one line.
            lines.append(f"🧪 meta {period}: 0 applied / {rejected} rejected (see data/evolution/edits.jsonl)")
        elif proposed > 0:
            # editor_report missing (editor crashed) but the reflection
            # carried proposals — surface the review hint rather than
            # nothing (idx 19 fallback).
            lines.append(
                f"🧪 meta {period}: {proposed} proposal(s) generated but prompt-editor report missing — check logs"
            )
        # status='skipped' (not quarter-end) → no line, normal evening.
    _seal_section(lines, _meta_start)
