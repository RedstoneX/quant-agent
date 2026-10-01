"""Prompt-fact family: what each seat said last time: the logged agent responses of the risk, PM and calling seat, and the week's narrative, read back from the DB.

Step 10 (second half) of `docs/ARCHITECTURE.md` §4, board item 210: one of
the six fact families split out of `PromptFacts`. Method bodies are the
former `PromptFacts` bodies byte for byte; this class takes ONLY the
2 collaborators those bodies read. Nothing here may import
`src.pipeline` or `src.pipeline_prompt_facts`.
"""

from src.trading_calendar import et_today, session_date_key
from src.prompt_facts_ports import _ABSENT, bind_ports, logger


class SeatMemoryFacts:
    """What each seat said last time: the logged agent responses of the risk, PM and calling seat, and the week's narrative, read back from the DB."""

    def __init__(
        self,
        *,
        db=_ABSENT,
        parse_logged_agent_response=_ABSENT,
    ) -> None:
        bind_ports(self, {
            "db": db,
            "_parse_logged_agent_response": parse_logged_agent_response,
        })

    def _build_weekly_narrative(self) -> str:
        """L3a memory: last 7 evenings' daily_summary + daily_pnl, compact."""
        try:
            insights = self.db.get_recent_insights(limit=7)
        except Exception as e:
            logger.warning("weekly_narrative: insights fetch failed: %s", e)
            insights = []
        if not insights:
            return ""
        try:
            pnl_rows = self.db.get_daily_pnl(limit=14)
        except Exception:
            pnl_rows = []
        pnl_by_date = {r["date"]: r for r in pnl_rows}
        lines = []
        # insights come newest-first; display oldest→newest so the "arc" reads naturally
        for row in reversed(insights):
            d = row.get("date", "?")
            summary = (row.get("tomorrow_outlook") or row.get("lessons") or "").strip()
            if len(summary) > 220:
                summary = summary[:217] + "..."
            pnl = pnl_by_date.get(d) or {}
            ret = pnl.get("daily_return_pct")
            ret_str = f"{ret:+.2f}%" if isinstance(ret, (int, float)) else "n/a"
            risk = row.get("risk_rating", "?")
            lines.append(f"- {d}: {ret_str} ({risk}) — {summary}")
        return "\n".join(lines)

    def _build_rm_recent_verdicts(self, limit: int = 5) -> str:
        """How RM has been judging PM's output over the last N sessions.

        PM reading this lets it self-calibrate: if RM has been scaling BUYs
        down for several runs in a row, PM has been oversizing — pull base
        allocations down before RM has to do it again.
        """
        try:
            rows = self.db.get_recent_agent_outputs(
                agent_name="risk_manager", limit=limit,
                before_date=session_date_key(),
            )
        except Exception as e:
            logger.warning("rm_recent_verdicts: DB fetch failed: %s", e)
            return ""
        if not rows:
            return ""
        lines = []
        for row in reversed(rows):  # oldest→newest
            ts = (row.get("timestamp") or "")[:10]
            data = self._parse_logged_agent_response(row)
            if not isinstance(data, dict):
                # PM's L5 layer reads RM history to self-calibrate.
                # Silently dropping a corrupt full_response row makes PM
                # see fewer verdicts than the DB actually contains and
                # the operator never knows. Surface it so a recurring
                # corruption pattern shows up in logs.
                logger.warning(
                    "rm_recent_verdicts: JSON parse failed for row %s: %s",
                    ts or "?", "no decision object found",
                )
                continue
            approved = data.get("approved")
            mods = data.get("modifications") or []
            scale = data.get("scale_all_buys", 1.0)
            try:
                scale = float(scale) if scale is not None else 1.0
            except (TypeError, ValueError):
                scale = 1.0
            verdict = "APPROVED" if approved else "REJECTED"
            category = (data.get("reason_category") or "clean").strip()
            extras: list[str] = [f"cat={category}"]
            if scale < 1.0:
                extras.append(f"scale_all_buys={scale:.2f}")
            if mods:
                mod_syms = sorted({m.get("symbol", "?") for m in mods if isinstance(m, dict)})
                if mod_syms:
                    extras.append(f"mods on {', '.join(mod_syms)}")
            # Phase 10.1 — a per-symbol refusal is the sharpest feedback this
            # loop can carry: `reason_category` alone tells PM the plan had an
            # R/R problem, this tells it which NAME died for it. Rendered as
            # plain text from the stored verdict, tolerant of any shape,
            # because a display line must never raise on a historical row.
            rejected = data.get("rejected_symbols") or []
            if isinstance(rejected, list):
                rej_syms = sorted({
                    (r.get("symbol") if isinstance(r, dict) else r)
                    for r in rejected
                    if isinstance(r, (dict, str))
                } - {None, ""})
                if rej_syms:
                    extras.append(f"refused {', '.join(str(s) for s in rej_syms)}")
            tag = f" [{'; '.join(extras)}]"
            reason = (data.get("reasoning") or "")[:140].strip().replace("\n", " ")
            lines.append(f"- {ts}: {verdict}{tag} — {reason}")
        return "\n".join(lines)

    def _build_pm_recent_decisions(self, limit: int = 3) -> str:
        """PM's own last N decision sets — used to spot flip-flopping against itself."""
        try:
            rows = self.db.get_recent_agent_outputs(
                agent_name="portfolio_manager", limit=limit,
                before_date=session_date_key(),
            )
        except Exception as e:
            logger.warning("pm_recent_decisions: DB fetch failed: %s", e)
            return ""
        if not rows:
            return ""
        lines = []
        for row in reversed(rows):  # oldest→newest
            ts = (row.get("timestamp") or "")[:10]
            data = self._parse_logged_agent_response(row)
            if not isinstance(data, dict):
                # PM's L6 layer reads its own recent decision history to
                # spot flip-flops. A silent skip on JSON corruption hides
                # the gap; same fix as L5 / L3d / L3f builders.
                logger.warning(
                    "pm_recent_decisions: JSON parse failed for row %s: %s",
                    ts or "?", "no decision object found",
                )
                continue
            # Phase 2: new schema emits `targets` (target weights + thesis);
            # older logs in the DB carry `decisions` (legacy TradeDecision).
            # Parse whichever is present so PM reads a unified history.
            targets = data.get("targets") or []
            decisions = data.get("decisions") or []
            summary_parts: list[str] = []
            if targets:
                for t in targets[:8]:
                    if not isinstance(t, dict):
                        continue
                    sym = t.get("symbol", "?")
                    # The live schema sizes a target by `risk_allocation_pct`;
                    # `target_weight_pct` is the legacy notional field older
                    # logs carry. Reading only the legacy one rendered every
                    # recent size as "?", and a flip-flop check that cannot
                    # see the size is not a check. Tag the unit — 1% of risk
                    # and 1% of notional are not the same number.
                    risk = t.get("risk_allocation_pct")
                    weight = t.get("target_weight_pct")
                    if risk is not None:
                        w = f"{risk}%r"
                    elif weight is not None:
                        w = f"{weight}%w"
                    else:
                        w = "?"
                    conv = (t.get("conviction") or "?")[0]
                    summary_parts.append(f"{sym}→{w}({conv})")
            elif decisions:
                for d in decisions[:8]:
                    if not isinstance(d, dict):
                        continue
                    act = d.get("action", "?")
                    sym = d.get("symbol", "?")
                    alloc = d.get("allocation_pct", "?")
                    summary_parts.append(f"{act} {sym} {alloc}%")
            if not summary_parts:
                lines.append(f"- {ts}: (no trades that day)")
                continue
            rc = data.get("reasoning_chain") or {}
            sizing = (rc.get("sizing_logic") or "")[:160].strip().replace("\n", " ")
            continuity = (rc.get("continuity_check") or "")[:160].strip().replace("\n", " ")
            line = f"- {ts}: {'; '.join(summary_parts)}"
            if sizing:
                line += f"\n    sizing: {sizing}"
            if continuity:
                line += f"\n    continuity: {continuity}"
            lines.append(line)
        return "\n".join(lines)

    def _build_own_recent_decisions(self, limit: int = 3) -> str:
        """Pull last N position_reviewer sessions from agent_logs.

        Anti-flip-flop memory: shows the reviewer its own previous 3 sessions'
        actions per symbol so it can't silently reverse itself within hours
        without a named trigger. Complement to PM's `_build_pm_recent_decisions`.
        """
        try:
            # No before_date cutoff (audit round 2): the 15:30 close session
            # must see the 13:00 midday row — this anti-flip-flop memory says
            # "don't reverse yourself WITHIN HOURS", and the ET-midnight
            # cutoff excluded exactly those rows. The current session's own
            # row is inserted AFTER this builder runs, so no self-read.
            rows = self.db.get_recent_agent_outputs(
                agent_name="position_reviewer", limit=limit,
            )
        except Exception as e:
            logger.warning("own_recent_decisions: DB fetch failed: %s", e)
            return ""
        if not rows:
            return ""
        lines: list[str] = []
        for row in reversed(rows):  # oldest → newest
            ts = (row.get("timestamp") or "")[:16]
            data = self._parse_logged_agent_response(row)
            if not isinstance(data, dict):
                continue
            actions = data.get("actions") or []
            if not isinstance(actions, list):
                continue
            action_bits = []
            for a in actions:
                if not isinstance(a, dict):
                    continue
                sym = a.get("symbol", "?")
                act = a.get("action", "?")
                if act == "HOLD":
                    continue  # only surface actionable past decisions
                action_bits.append(f"{sym}:{act}")
            if action_bits:
                lines.append(f"- {ts}: {', '.join(action_bits[:8])}")
        return "\n".join(lines)
