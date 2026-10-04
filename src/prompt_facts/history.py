"""src.prompt_facts.history -- position history, weekly narrative, macro trajectory and active state changes.

Bodies moved verbatim from src/pipeline_prompt_facts.py (`PromptFactsMixin`), which keeps
same-named thin shims built per call. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no pipeline behind it.
"""

import logging

from src.trading_calendar import et_today

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class PromptHistory:
    """Position history, weekly narrative, macro trajectory and active state changes; standalone, built from explicit collaborators."""

    def __init__(
        self, *,
        db=None,
        tech_store=None,
        macro_store=None,
        news_store=None,
    ) -> None:
        self.db = db
        self.tech_store = tech_store
        self.macro_store = macro_store
        self.news_store = news_store

    def _build_position_history(self, positions) -> dict[str, dict]:
        """L2 memory: for each held symbol, entry context + Tech rating trajectory.

        PM uses this to anchor 'when did I buy + why' and recognize when a fresh
        setup has been maturing vs stuck vs invalidated.
        """
        from datetime import date as _date
        from src.execution.stop_records import recorded_initial_stop
        out: dict[str, dict] = {}
        today = et_today()
        for p in positions:
            sym = p.symbol
            entry = None
            try:
                try:
                    qty = float(getattr(p, "qty", 0) or 0)
                except (TypeError, ValueError):
                    qty = 0.0
                opening = "SHORT" if qty < 0 else "BUY"
                entry = self.db.get_symbol_last_buy(sym, action=opening)
            except Exception as e:
                logger.warning("position_history: last_buy lookup failed for %s: %s", sym, e)

            entry_date_str = None
            days_held: int | None = None
            if entry and entry.get("timestamp"):
                try:
                    ts = entry["timestamp"]
                    entry_date = _date.fromisoformat(ts[:10]) if isinstance(ts, str) else None
                    if entry_date is not None:
                        entry_date_str = str(entry_date)
                        days_held = max(0, (today - entry_date).days)
                except (ValueError, TypeError):
                    pass

            try:
                tech_history = self.tech_store.get_history(sym, days=7)
            except Exception as e:
                logger.warning("position_history: tech history failed for %s: %s", sym, e)
                tech_history = []

            out[sym] = {
                "entry_date": entry_date_str,
                "entry_price": entry.get("price") if entry else None,
                "entry_reasoning": (entry.get("reasoning") or "")[:280] if entry else "",
                # Real, untruncated falsifier condition — see
                # TradeDecision.thesis_invalid_if in models.py. Carried
                # ALONGSIDE entry_reasoning (never a replacement for it):
                # the embedded "(invalid if: ...)"/"(thesis_invalid_if: ...)"
                # text above is truncated at 280 chars here (and 500 chars
                # upstream in the constructor), which could silently cut off
                # a long condition. This column is None for legacy rows
                # written before the trades.thesis_invalid_if column existed.
                "thesis_invalid_if": entry.get("thesis_invalid_if") if entry else None,
                "days_held": days_held,
                "tech_history": tech_history,
                # The stop recorded at entry — RiskStage's structural
                # holding-discipline check (spec item 25) reads this to find
                # the level backing it. Deliberately the frozen entry-time
                # stop (`initial_stop_loss`), not the live `stop_loss` a
                # trail may have since written back, and not the live
                # broker stop: same "the bet that was actually made"
                # reasoning `_build_position_facts.initial_stop` documents.
                "stop_loss": (
                    recorded_initial_stop(entry) or None
                ) if entry else None,
            }
        return out

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

    def _build_macro_trajectory(self) -> str:
        """L3b memory: last 7 days of macro regime / confidence / equity outlook.

        No invested target: macro stopped setting one with the owner's
        fully-invested mandate (2026-09-17). Older snapshots still carry
        `position_guidance.target_invested_pct`; it is deliberately not read.
        """
        try:
            history = self.macro_store.load_history(days=7)
        except Exception as e:
            logger.warning("macro_trajectory: load_history failed: %s", e)
            history = []
        if not history:
            return ""
        lines = []
        for snap in history:
            d = snap.get("date", "?")
            regime = snap.get("regime", "?")
            conf = snap.get("confidence", "?")
            outlook = snap.get("equity_outlook") or "?"
            lines.append(f"- {d}: {regime} ({conf}) → outlook {outlook}")
        return "\n".join(lines)

    def _build_active_state_changes(self) -> str:
        """L3c memory: HIGH-conviction state_changes from the last 14 days, deduped."""
        try:
            changes = self.news_store.recent_state_changes(lookback_days=14, limit=8)
        except Exception as e:
            logger.warning("active_state_changes: news_store failed: %s", e)
            changes = []
        if not changes:
            return ""
        lines = []
        for ch in changes:
            d = ch.get("first_seen_date", "?")
            event = (ch.get("event") or "")[:160]
            symbols = ch.get("affected_symbols") or []
            # Phase 13 catalyst-gate fix: render each symbol's direction
            # inline as `SYMBOL(direction)` so `PortfolioManagerAgent.
            # _state_change_symbols_by_date` can parse it back out — this
            # is a round-trip over a format this repo owns end to end
            # (same discipline as the rest of this block). A symbol with
            # no recorded `symbol_direction` (older persisted reports
            # predating this field, or the news analyst genuinely
            # omitting one) renders as `(unknown)`, which the PM-side
            # parser treats as not qualifying — fail closed, never an
            # upgrade to "assume it's good news."
            directions = ch.get("symbol_direction") or {}
            if symbols:
                syms = ", ".join(
                    f"{s.strip().upper()}({directions.get(s.strip().upper(), 'unknown')})"
                    for s in symbols[:6]
                )
            else:
                syms = "—"
            lines.append(f"- [{d}] {event} → {syms}")
        return "\n".join(lines)
