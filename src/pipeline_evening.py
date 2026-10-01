"""The evening session, the proximity checks and the quarterly meta reflection.

Step 9 of `docs/PIPELINE_SPLIT_PLAN.md` (board item 210), the last of the class
moves. Clusters X and Y moved verbatim out of `src/pipeline.py` as a mixin, so
`TradingPipeline` keeps every one of these as its own attribute and every test
that patches or calls them is untouched.

What is here: the evening session wrapper and its 612-line body, the durable
evening report record, the stop- and earnings-proximity checks the evening
digest reports, the expected-sessions-missing check, and the quarterly meta
reflection plus the `run_daily` wrapper that may trigger it.

The module-level names the moved bodies read are imported below rather than
reached through `src.pipeline`, which this module may not import: it is one of
its bases. A test that PATCHES one of those names on `src.pipeline` therefore
no longer reaches this module's code and must patch it here instead (plan S5,
silent-behaviour risk 1). Measured on the day of the move: no test patches any
of them on `src.pipeline`.

Nothing here may import `src.pipeline`: this module is one of its bases.
"""

import json as _json
import logging
import math
import uuid
from pathlib import Path

from src.agents.base import AgentResult, agent_log_kwargs, seat_acceptance_kwargs
from src.cost_circuit import PaidAnalysisSuspended
from src.data.macro import MacroCoverage
from src.pipeline_context import RunContext
from src.trading_calendar import et_today, session_date_key

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class EveningMixin:
    """The evening session, proximity checks and quarterly meta reflection.

    A mixin of `TradingPipeline`; `self` is always a `TradingPipeline`.
    """

    def run_evening(self) -> dict:
        """The evening session, plus the durable record of its own output.

        The body below is unchanged; this wrapper exists so that EVERY
        return path (holiday short-circuit, paid-analysis suspension, the
        error payloads and the full report) lands in `evening_reports`
        before the result reaches the notifier. Previously the run handed
        stop_coverage_gaps / stop_proximity / earnings_proximity /
        total_pnl / risk_capital_dollars to the Telegram formatter and
        then dropped them: only daily_pnl and insights survived, so last
        night's report could not be re-read without paying for a fresh
        run. Persistence is fail-soft — a storage problem must cost the
        audit record, never the evening push.
        """
        result = self._run_evening_body()
        if isinstance(result, dict) and result.get("status") != "market_holiday":
            screen = self._run_universe_screen(result.get("run_id") or "evening")
            if screen is not None:
                result["universe_screen"] = screen
        self._persist_evening_report(result)
        return result

    def _persist_evening_report(self, result: dict) -> None:
        """Write the evening result dict verbatim, keyed by trading day.

        No field is defaulted or filled in: a value the run could not
        compute is stored absent/None so that a re-render says
        "not available" rather than showing a fabricated zero.
        """
        if not isinstance(result, dict):
            return
        try:
            self.db.save_evening_report(
                date=session_date_key(),
                run_id=result.get("run_id"),
                payload=result,
            )
        except Exception as exc:  # noqa: BLE001 — never break the push
            logger.warning(
                "evening report persistence failed (non-fatal): %s", exc,
            )

    def _run_evening_body(self) -> dict:
        ctx = RunContext.start("evening")
        run_id = ctx.run_id
        logger.info("=== Evening report: %s ===", run_id)

        if not self._is_trading_day():
            logger.info("Evening run skipped: market closed for non-trading day")
            return {"status": "market_holiday", "analysis": None, "run_id": run_id}

        self._activate_cost_session(run_id, "evening")

        # Drain orphaned protection-restore intents — last chance before
        # the trading day ends. If close-session bailed and the SELL has
        # since gone terminal, recover coverage now rather than carrying
        # a naked position overnight. Codex r8 #2.
        drained = self._drain_pending_protection_restores()
        self._drain_pending_repegs()
        self._reconcile_orphan_pending_submits()  # audit F4
        # Broker-truth coverage audit — last check before carrying positions
        # overnight (independent of the WAL).
        coverage_gaps = self._reconcile_stop_coverage()
        # Sweep submitted orders so canceled/expired orders do not get
        # narrated as real trades, and partial terminal fills are reflected
        # in the trade list before the evening prompt is built.
        #
        # Item 173(2): this runs BEFORE the stop-out reconcile below, not
        # after. A SELL this session submitted but hasn't yet reconciled
        # leaves the ledger believing the position is still open
        # (get_symbols_with_open_ledger_qty ignores 'submitted' rows) while
        # the broker has already reduced it — a positive gap the stop-out
        # reconciler can't explain, because the submitted SELL's
        # broker_order_id is already in get_known_broker_order_ids so its
        # fill is filtered out of new_fills, and it pages a false CRITICAL
        # "records disagree with broker". Reconciling fills first flips that
        # SELL to executed, the gap closes, and the stop-out check stays quiet.
        self._reconcile_fills()
        # Broker-truth EXIT audit (2026-08-28 ONDS/CCJ) — last chance before
        # the daily P&L snapshot below is computed, so a same-day stop-out
        # is reflected in tonight's report rather than showing up as an
        # unexplained gap the next time someone looks at realized_pnl.
        reco = None
        try:
            reco = self._reconcile_stop_out_fills(run_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("evening stop-out reconcile failed (non-fatal): %s", exc)
        # Item 101: surface a broker-made stop-out / re-protection to owner.
        self._surface_reconcile_outcomes(reco, drained, run_id=run_id)

        # 1. Record daily PnL — use Alpaca's last_equity (previous trading-day close)
        # as the baseline. This correctly handles weekends/holidays (Alpaca updates
        # last_equity only on trading days) and doesn't depend on whether yesterday's
        # evening run actually persisted a snapshot to our own DB.
        account = self.broker.get_account()
        positions = self.broker.get_positions()
        total_value = account["portfolio_value"]
        last_equity = account.get("last_equity", total_value)
        today_str = session_date_key()  # ET trading-day key — stable across host TZ

        if last_equity > 0:
            daily_pnl = total_value - last_equity
            daily_return_pct = daily_pnl / last_equity * 100
        else:
            daily_pnl = 0.0
            daily_return_pct = 0.0
        ctx.account = account
        ctx.positions = positions
        ctx.total_value = total_value
        ctx.last_equity = last_equity
        ctx.daily_pnl = daily_pnl
        # Sync the full broker book (before the LLM-view split below).
        # Evening's Telegram snapshot reads this table, not the in-memory list.
        self._sync_positions_from_broker(ctx.positions)

        # LLM view: hide the cash-sweep vehicle from evening's position
        # narratives (facts / thesis-health / missed-ops held-set) — parked
        # T-bills have no thesis to review. ctx keeps broker truth.
        sweeper = self._sweeper()
        if sweeper is not None:
            positions, _parked = sweeper.split_positions(positions)

        # Phase 6 (§6.3b): today's P&L expressed against capital actually AT
        # RISK, not just total equity — reuses the same audit §1.3 heat
        # calculation (`_build_portfolio_heat` -> `src.risk.metrics.
        # portfolio_heat`) the risk-manager prompt already trusts, rather
        # than recomputing it. None (not 0.0) on a failed build, so the
        # notifier can say "unknown" instead of a fabricated number.
        try:
            risk_heat = self._build_portfolio_heat(positions, total_value)
            risk_capital_dollars = (
                risk_heat.budget_risk_dollars if risk_heat is not None else None
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("evening: risk-capital heat build failed: %s", e)
            risk_capital_dollars = None


        # Phase 4 #5: daily_pnl write is deferred to the atomic
        # save_evening_snapshot() below, along with insights. Doing both in
        # one transaction means a crash between them doesn't leave next
        # morning reading a P&L number with no insights narrative attached.
        # Fallback: if the evening LLM fails (analysis is None), we still
        # save the daily_pnl alone below to preserve the P&L audit trail.

        # This boundary is intentionally after broker protection/fill
        # reconciliation and the deterministic P&L snapshot, but before the
        # first paid news/model request. A latched breaker still persists the
        # P&L audit row and returns a truthful suspended status.
        try:
            self._require_paid_analysis("evening_news")
        except PaidAnalysisSuspended as exc:
            equity_close = None
            try:
                closes = self.broker.get_recent_daily_closes(lookback_days=10)
                if closes and closes[-1][0] == today_str:
                    equity_close = closes[-1][1]
            except Exception as close_exc:  # noqa: BLE001
                logger.warning("suspended evening: 4pm close fetch failed: %s", close_exc)
            self.db.insert_daily_pnl(
                date=today_str,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                equity_close=equity_close,
            )
            payload = self._paid_suspended_payload(run_id, error=exc)
            payload.update(
                analysis=None,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                equity_close=equity_close,
                stop_coverage_gaps=coverage_gaps,
            )
            return payload

        # 2. News + Earnings update — capture end-of-day developments
        try:
            # Same held-symbols-only scope as run_position_review — see the
            # comment there. No separate candidate list exists pre-fetch in
            # this path. `positions` here was already sweeper-split above,
            # so _news_held_symbols' own split is a no-op; called anyway to
            # keep this call site identical to the other two.
            evening_news, evening_news_coverage = self._run_news_update(
                run_id, session="evening",
                held_symbols=self._news_held_symbols(positions),
            )
        except PaidAnalysisSuspended as exc:
            self.db.insert_daily_pnl(
                date=today_str, total_value=total_value,
                daily_pnl=daily_pnl, daily_return_pct=daily_return_pct,
            )
            payload = self._paid_suspended_payload(run_id, error=exc)
            payload.update(
                analysis=None, total_value=total_value, daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                stop_coverage_gaps=coverage_gaps,
            )
            return payload
        if evening_news_coverage is not None and evening_news_coverage.status != "ok":
            # Same gap noted in run_position_review: evening has no
            # data_status mechanism of its own to carry this further, so at
            # minimum it does not disappear into a log-only "ok".
            logger.warning("evening: %s", evening_news_coverage.describe())
        if evening_news:
            logger.info("Evening news: %s", evening_news.pm_briefing[:200])
        try:
            _, evening_earnings = self._load_earnings_analyses(run_id, session="evening", ctx=ctx)
        except Exception as e:  # noqa: BLE001 — evening proceeds without earnings
            logger.error("evening: earnings load failed (continuing without): %s", e)
            evening_earnings = []

        try:
            self._require_paid_analysis("evening_analyst")
        except PaidAnalysisSuspended as exc:
            self.db.insert_daily_pnl(
                date=today_str, total_value=total_value,
                daily_pnl=daily_pnl, daily_return_pct=daily_return_pct,
            )
            payload = self._paid_suspended_payload(run_id, error=exc)
            payload.update(
                analysis=None, total_value=total_value, daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                stop_coverage_gaps=coverage_gaps,
            )
            return payload

        # 3. LLM evening analysis — daily review and tomorrow outlook
        macro_summary = self.macro.get_macro_summary()
        evening_macro_coverage = self.macro.last_coverage
        if isinstance(evening_macro_coverage, MacroCoverage) and evening_macro_coverage.status != "ok":
            # Same gap noted in run_position_review / the news coverage
            # check above: evening has no data_status mechanism of its own
            # to carry this further, so at minimum it does not disappear
            # into a log-only "ok".
            logger.warning("evening: %s", evening_macro_coverage.describe())
        # Sweep churn (SWEEP_BUY/SWEEP_SELL) is cash parking, not a trading
        # decision — narrating it to the evening analyst would feed the
        # learning loops noise (review finding). Fetch extra rows so the
        # filter doesn't shrink the real-trade view.
        today_trades = [
            self._actualize_trade_row(t)
            for t in self.db.get_trades(limit=30, today_only=True, executed_only=True)
            if (t.get("action") or "") not in ("SWEEP_BUY", "SWEEP_SELL")
        ][:20]
        # Feed yesterday's insights back so evening can grade its own prior outlook
        # against today's reality — enables calibration over time.
        prior_outlook = self.db.get_latest_insights(before_date=today_str)
        # SELL decisions from the last 2 days + each symbol's move since sell.
        # Evening grades each one {correct|premature|wrong} — the feedback loop
        # on selling discipline.
        recent_sells = self._build_recent_sells_for_grading(
            lookback_days=2,
            symbols_bars=ctx.symbols_bars,  # empty for evening (no tech fetch) — OK, we use broker price
        )
        # v2: mirror SELL grading with BUY grading. Entry quality feedback loop.
        recent_buys = self._build_recent_buys_for_grading(
            lookback_days=5, symbols_bars=ctx.symbols_bars,
        )
        # v2: meta-calibration — evening sees its own recent tomorrow_bias vs
        # actual outcomes so it can detect "I've been too bullish 7/10 days".
        outlook_calibration = self._build_recent_outlook_calibration(lookback=10)
        # v2: share the PM's 7-day narrative + 14-day active state-change
        # memory so evening doesn't drift from or repeat its own previous
        # language unchecked.
        weekly_narrative = self._build_weekly_narrative()
        active_state_changes = self._build_active_state_changes()

        # Phase-1 evening-upgrade: deterministic "what did we miss" digest.
        # Python pre-computes the signal-state context so the LLM's classification
        # has to cite observable evidence rather than retro-rationalize price.
        held_set = {p.symbol for p in positions}
        try:
            missed_ops_snapshots = self._build_missed_opportunities_digest(
                lookback_days=5, move_threshold_pct=8.0, top_n=15,
                current_position_symbols=held_set,
            )
        except Exception as e:
            logger.warning("missed_ops digest failed (proceeding without it): %s", e)
            missed_ops_snapshots = []

        # Value-lens upgrade (2026-04): per-position 8-week fundamentals
        # evolution — feeds the new thesis_health_review reasoning step.
        try:
            thesis_health_context = self._build_thesis_health_context(positions)
        except Exception as e:
            logger.warning(
                "thesis_health_context failed (proceeding without it): %s", e,
            )
            thesis_health_context = {}

        # Replay/shadow mechanism (2026-04 — P2 follow-up): persist the
        # full evening-analyst input set so a candidate prompt can be
        # re-scored on the same frozen inputs later via
        # `scripts/replay_evening.py`. Doesn't affect the live run;
        # failure here is non-fatal and only logged.
        try:
            self._persist_evening_replay_inputs(
                date_iso=today_str,
                run_id=run_id,
                positions=positions,
                macro_summary=macro_summary,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                today_trades=today_trades,
                prior_outlook=prior_outlook,
                recent_sells=recent_sells,
                recent_buys=recent_buys,
                news_intel=evening_news,
                earnings_analyses=evening_earnings,
                weekly_narrative=weekly_narrative,
                active_state_changes=active_state_changes,
                outlook_calibration=outlook_calibration,
                missed_ops_snapshots=missed_ops_snapshots,
                thesis_health_context=thesis_health_context,
            )
        except Exception as e:
            logger.warning("evening replay input persistence failed: %s", e)

        analysis = None
        analysis_error = False
        try:
            analysis, ev_result = self.evening_analyst.analyze(
                positions=positions,
                macro_summary=macro_summary,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                today_trades=today_trades,
                prior_outlook=prior_outlook,
                recent_sells=recent_sells,
                recent_buys=recent_buys,
                news_intel=evening_news,
                earnings_analyses=evening_earnings,
                weekly_narrative=weekly_narrative,
                active_state_changes=active_state_changes,
                outlook_calibration=outlook_calibration,
                missed_ops_snapshots=missed_ops_snapshots,
                thesis_health_context=thesis_health_context,
            )
        except PaidAnalysisSuspended as exc:
            self.db.insert_daily_pnl(
                date=today_str, total_value=total_value,
                daily_pnl=daily_pnl, daily_return_pct=daily_return_pct,
            )
            payload = self._paid_suspended_payload(run_id, error=exc)
            payload.update(
                analysis=None, total_value=total_value, daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                stop_coverage_gaps=coverage_gaps,
            )
            return payload
        except Exception as e:
            from src.agents.base import AgentResult, resolve_provider

            analysis_error = True
            logger.error("Evening analyst failed: %s", e, exc_info=True)
            # No call ever completed, so `actual_provider`/model stay unknown
            # (not fabricated) — but WHAT was requested is known regardless
            # of the exception, so record that much for attribution.
            _requested_model = self.config.llm.evening_analyst_model
            _requested_provider = resolve_provider(
                _requested_model, self.config.llm.evening_analyst_provider,
            )
            ev_result = AgentResult(
                raw_text=f"[exception] {e}",
                tokens_used=0,
                model=self.config.llm.evening_analyst_model,
                user_message="",
                requested_model=_requested_model,
                requested_provider=_requested_provider,
                provider_requests=0,
            )

        _ev_log_kwargs = agent_log_kwargs(ev_result)
        if analysis_error:
            # agent_log_kwargs() derives "fallback"/"success" from
            # used_fallback, which is False here (no call ever completed) —
            # override so a hard failure isn't misreported as a success.
            _ev_log_kwargs["status"] = "failed"
        elif analysis is None:
            _ev_log_kwargs["status"] = "evening_parse_error"
        self.db.insert_agent_log(
            **seat_acceptance_kwargs(_ev_log_kwargs.get("status") if _ev_log_kwargs.get("status") in ("failed", "evening_parse_error") else None),
            agent_name="evening_analyst", run_id=run_id,
            input_summary=f"${total_value:.0f} total, PnL ${daily_pnl:.2f}",
            input_message=ev_result.user_message,
            output_summary=(
                analysis.daily_summary
                if analysis
                else ("analysis_error" if analysis_error else "parse_error")
            ),
            full_response=ev_result.raw_text,
            model=ev_result.model,
            tokens_used=ev_result.tokens_used,
            input_tokens=ev_result.input_tokens,
            output_tokens=ev_result.output_tokens,
            cost_usd=ev_result.cost_usd,
            **_ev_log_kwargs,
        )

        # True close-to-close ("4pm-to-4pm") P&L. account.last_equity is the
        # PRIOR day's close (stale at the 20:00 ET evening run), and
        # total_value here is the 8pm after-hours value — neither gives today's
        # official 4pm close. Alpaca portfolio_history (extended_hours=False)
        # does: its latest 1D point is today's regular-session close. We report
        # the clean close-to-close P&L when available and store today's close
        # for the audit trail; on any gap we fall back to the real-time diff.
        equity_close = None
        pnl_4pm = None
        pnl_4pm_pct = None
        try:
            closes = self.broker.get_recent_daily_closes(lookback_days=10)
            if closes and closes[-1][0] == today_str:
                equity_close = closes[-1][1]
                prev_close = closes[-2][1] if len(closes) >= 2 else None
                # Guard > 0: a negative prior close (corrupted data / underwater
                # account) would flip the sign of the return %; leave pnl_4pm
                # None so the headline falls back to the real-time path.
                if prev_close and prev_close > 0:
                    pnl_4pm = equity_close - prev_close
                    pnl_4pm_pct = pnl_4pm / prev_close * 100
            elif closes:
                logger.info(
                    "4pm snapshot: portfolio_history latest date %s != today %s "
                    "(API lag?) — evening uses the real-time P&L fallback",
                    closes[-1][0], today_str,
                )
            # Self-heal: when portfolio_history is a day behind at the
            # 20:00 ET evening run (the "API lag?" branch above), that
            # evening's equity_close landed NULL — but by a LATER evening
            # the API has caught up on those dates, which are still inside
            # this lookback window. Backfill any still-NULL rows now.
            # today_str is excluded because today's row is owned by the
            # branches above + save_evening_snapshot below: when today's
            # bar is present the first branch already uses it as the
            # official close, and when it's absent there is nothing to
            # backfill yet.
            for d, close_val in closes:
                if d == today_str:
                    continue
                # Mirror the `prev_close > 0` guard above: Alpaca
                # portfolio_history can emit 0.0 (pre-funding / account
                # reset) or non-finite points, and a backfilled value is
                # permanent (the fill targets NULL-only rows, so a bad
                # write can never be corrected by a later run) — never
                # freeze a corrupt equity in. NaN must be caught here
                # anyway: sqlite binds it as NULL, which would make
                # backfill report success while storing nothing.
                if not (math.isfinite(close_val) and close_val > 0):
                    logger.warning(
                        "equity_close backfill skipped for %s: suspect "
                        "equity value %r", d, close_val,
                    )
                    continue
                try:
                    if self.db.backfill_equity_close(d, close_val):
                        logger.info(
                            "equity_close backfilled for %s = %.2f (API lag self-heal)",
                            d, close_val,
                        )
                except Exception as exc:
                    logger.warning("equity_close backfill failed for %s: %s", d, exc)
        except Exception as e:
            logger.warning("4pm snapshot fetch failed: %s — using real-time P&L", e)

        # Save daily_pnl + insights atomically (Phase 4 #5). If the LLM
        # failed (analysis is None), still record the P&L number so the
        # audit trail is complete — just with empty insights fields.
        if analysis:
            self.db.save_evening_snapshot(
                date=today_str,
                total_value=total_value, daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                equity_close=equity_close,
                tomorrow_outlook=analysis.tomorrow_outlook,
                lessons=analysis.lessons,
                suggested_actions=analysis.suggested_actions,
                risk_rating=analysis.risk_rating,
                tomorrow_bias=analysis.tomorrow_bias,
                tomorrow_conviction=analysis.tomorrow_conviction,
                tomorrow_key_risks=analysis.tomorrow_key_risks,
                sell_decisions_assessment=analysis.sell_decisions_assessment,
                # v2: persist structured grades so next-day position_reviewer
                # can aggregate counts into its "lean patient" bias.
                sell_grades=analysis.sell_grades,
                buy_grades=analysis.buy_grades,
                # Phase-1 upgrade: per-day missed opportunities feed PM's L3d
                # memory next morning and the quarterly meta-reflector's
                # theme_coverage_report.
                missed_opportunities=analysis.missed_opportunities,
                # Defect (d) fix: these four were produced by the LLM every
                # night and declared on EveningReport, but had no parameter
                # here — dropped before ever reaching disk.
                # thesis_updates/selection_rules/discipline_notes feed
                # tomorrow's portfolio_manager (see build_user_message).
                thesis_updates=analysis.thesis_updates,
                selection_rules=analysis.selection_rules,
                discipline_notes=analysis.discipline_notes,
                previous_outlook_assessment=analysis.previous_outlook_assessment,
            )
        else:
            # LLM failed — keep at least the P&L number for daily audit.
            self.db.insert_daily_pnl(
                date=today_str,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_return_pct=daily_return_pct,
                equity_close=equity_close,
            )

        # Conviction ledger (spec §9.5) — score on close. Every position
        # chain that went flat today is credited to the seats that took a
        # side on it: aligned with the direction taken scores +R, opposed
        # scores -R, weighted by the conviction that seat declared. Runs
        # HERE, in evening housekeeping, deliberately: it reads closed
        # `trades` rows and writes forensic evidence rows, touches no broker
        # and no open position, and is idempotent (a position already scored
        # is skipped), so it can never influence or delay an execution path.
        # Advisory only — nothing in the trading chain reads what it writes.
        try:
            ledger = self.db.resolve_conviction_ledger()
            if ledger.get("scored_positions"):
                logger.info(
                    "Conviction ledger: scored %d newly closed position(s) into "
                    "%d seat credit(s) (%d already scored, %d unscorable without "
                    "an entry stop, %d with no recorded stances)",
                    ledger["scored_positions"], ledger["credits_written"],
                    ledger["skipped_already_scored"], ledger["skipped_no_r"],
                    ledger["skipped_no_stances"],
                )
        except Exception as e:
            logger.warning("Conviction ledger resolution failed: %s", e)

        # Housekeeping: drop agent_logs older than 2 years (full_response bloats the DB
        # but 730 days supports quarter-over-quarter learning), and trades older than
        # 5 years (keep a long audit tail but bound it).
        try:
            pruned = self.db.prune_agent_logs(keep_days=730)
            if pruned:
                logger.info("Pruned %d old agent_log rows", pruned)
        except Exception as e:
            logger.warning("Agent log prune failed: %s", e)
        try:
            pruned_t = self.db.prune_trades(keep_days=365 * 5)
            if pruned_t:
                logger.info("Pruned %d trades older than 5 years", pruned_t)
        except Exception as e:
            logger.warning("Trades prune failed: %s", e)
        # Stage 4 (QAMC): specialist_evidence is forensic display detail for
        # the same agent calls agent_logs already prunes — same 730-day
        # retention, same never-block-housekeeping discipline.
        try:
            pruned_se = self.db.prune_specialist_evidence(keep_days=730)
            if pruned_se:
                logger.info("Pruned %d old specialist_evidence rows", pruned_se)
        except Exception as e:
            logger.warning("specialist_evidence prune failed: %s", e)
        # Stale orphaned protection-restore rows accumulate when a
        # sell_order_id becomes unqueryable (broker GC) or position
        # gets liquidated by another path. Drain can't make progress on
        # them; 30d cutoff bounds the operational noise.
        try:
            pruned_p = self.db.prune_pending_protection_restores(keep_days=30)
            if pruned_p:
                logger.info("Pruned %d stale pending_protection_restores rows", pruned_p)
        except Exception as e:
            logger.warning("pending_protection_restores prune failed: %s", e)
        try:
            pruned_rp = self.db.prune_pending_repegs(keep_days=30)
            if pruned_rp:
                logger.info("Pruned %d stale pending_repegs rows", pruned_rp)
        except Exception as e:
            logger.warning("pending_repegs prune failed: %s", e)
        # File-store housekeeping: the news dated dirs + narrative backups grow
        # unbounded (the DB side prunes; the file-stores didn't). Nothing reads
        # news artifacts older than ~14 days, so 1000d is very safe headroom.
        try:
            pruned_n = self.news_store.prune(keep_days=1000)
            if pruned_n:
                logger.info("Pruned %d dated news artifact(s)", pruned_n)
        except Exception as e:
            logger.warning("news file-store prune failed: %s", e)
        try:
            pruned_e = self.earnings_provider.prune(keep_days=1000)
            if pruned_e:
                logger.info("Pruned %d old raw earnings filing(s)", pruned_e)
        except Exception as e:
            logger.warning("earnings file-store prune failed: %s", e)

        logger.info("Evening: value=$%.2f, PnL=$%.2f (%.2f%%), risk=%s",
                     total_value, daily_pnl, daily_return_pct,
                     analysis.risk_rating if analysis else "error")
        if analysis:
            logger.info("Summary: %s", analysis.daily_summary)
            logger.info("Tomorrow: %s", analysis.tomorrow_outlook)
        # Evening is the last chance to reconcile today's orders before the
        # next trading day. Sweep everything still marked submitted.
        self._reconcile_fills()
        self._sync_positions_from_broker()

        meta_result = self._maybe_run_quarterly_meta()
        missing_sessions = self._expected_sessions_missing_today()
        # Owner-facing evening report (2026-09-18): today's P&L alone never
        # answered "am I up since the desk restarted". The same
        # `_total_pnl_since_reset` the trader-feed messages already use is
        # read here so the evening message can lead with BOTH figures on the
        # identical basis, rather than computing a second "total" of its own.
        total_pnl, total_return_pct, total_pnl_since = (
            self._total_pnl_since_reset(total_value)
        )
        if missing_sessions:
            logger.warning(
                "Dead-man's check: expected session(s) left no agent_logs "
                "today: %s", ", ".join(missing_sessions),
            )
        return {
            "status": (
                "analyzed" if analysis is not None else
                ("evening_analysis_error" if analysis_error else "evening_parse_error")
            ),
            "total_value": total_value,
            "daily_pnl": daily_pnl,
            "daily_return_pct": daily_return_pct,
            "analysis": analysis.model_dump() if analysis else None,
            "run_id": run_id,
            "auto_meta": meta_result,
            # Observability: surface a silently-missing session so the
            # notifier can raise deterministic escalation (not just LLM).
            "missing_sessions": missing_sessions,
            "stop_coverage_gaps": coverage_gaps,
            # True 4pm-to-4pm headline P&L (None → notifier falls back to the
            # real-time total_value/daily_pnl figures).
            "equity_close": equity_close,
            "pnl_4pm": pnl_4pm,
            "pnl_4pm_pct": pnl_4pm_pct,
            # Phase 6 (§6.3b) — capital actually at risk (sum of
            # (entry-stop) x shares across open positions), for the
            # notifier's "P&L vs risk capital" line. None on a failed heat
            # build; 0.0 for a genuinely flat/fully-released book — the
            # notifier tells those two apart.
            "risk_capital_dollars": risk_capital_dollars,
            # Dated total P&L (see `_total_pnl_since_reset` for why it is
            # dated rather than called "since inception").
            "total_pnl": total_pnl,
            "total_return_pct": total_return_pct,
            "total_pnl_since": total_pnl_since,
            # Two end-of-day facts the desk knew and never told the owner:
            # which holdings sit within one ordinary day's move of their stop,
            # and which report earnings imminently. Both fail soft to [].
            "stop_proximity": self._evening_stop_proximity(positions),
            "earnings_proximity": self._evening_earnings_proximity(positions),
        }

    def _evening_stop_proximity(self, positions) -> list[dict]:
        """Held positions whose live stop is less than one ordinary day's
        move away — the evening report's "close to its stop" line.

        "Close" is read off the instrument, never picked: the yardstick is
        the symbol's own ATR(14) (`_atr_for_symbol`, the same measure the
        trailing-stop noise band uses). A position is listed when the gap
        between the last price and the live broker stop is smaller than one
        ATR, i.e. a single ordinary session could reach it. No percentage
        threshold is invented anywhere in this method.

        A symbol whose stop or ATR cannot be read is returned with
        ``status='unknown'`` rather than dropped: silently omitting it would
        render as "nothing is near its stop", which is not what was
        measured. Never raises — any failure degrades to [].
        """
        rows: list[dict] = []
        try:
            park = (self._sweep_symbol() or "").strip().upper()
            for p in positions or ():
                symbol = str(getattr(p, "symbol", "") or "").strip().upper()
                if not symbol or (park and symbol == park):
                    continue
                try:
                    qty = float(getattr(p, "qty", 0) or 0)
                    price = float(getattr(p, "current_price", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if qty == 0 or not (math.isfinite(price) and price > 0):
                    continue
                try:
                    stop = self.broker.get_current_stop_price(symbol)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "evening stop-proximity: stop read failed for %s: %s",
                        symbol, exc,
                    )
                    stop = None
                atr = self._atr_for_symbol(symbol)
                if stop is None or atr is None or not (stop > 0):
                    rows.append({"symbol": symbol, "status": "unknown"})
                    continue
                # A long is stopped from BELOW, a short from ABOVE. The
                # distance is the same arithmetic either way.
                gap = (price - stop) if qty > 0 else (stop - price)
                if gap < 0:
                    # PRICE IS THROUGH THE STOP and the broker order is
                    # still open, so it has not filled. This used to be
                    # clamped to 0.0 and reported as `status='near'`, which
                    # merged two different facts into one row: a stop that
                    # is merely TIGHT (an ordinary session could reach it)
                    # and a stop that has already been BLOWN THROUGH without
                    # filling (nothing is standing watch over those shares).
                    # The second is the state the stop-limit buffer trade-off
                    # produces on a gap — now only reachable on the stop-limit
                    # FALLBACK leg, since primary protective stops are
                    # stop-market and fill when elected — and it now reads as
                    # itself. The distance is reported as a positive number
                    # of dollars PAST the trigger, which is a different
                    # quantity from `gap` and carries a different name.
                    rows.append({
                        "symbol": symbol, "status": "through", "price": price,
                        "stop": float(stop), "through": -gap,
                        "atr": float(atr),
                    })
                    continue
                if gap < atr:
                    rows.append({
                        "symbol": symbol, "status": "near", "price": price,
                        "stop": float(stop), "gap": gap, "atr": float(atr),
                    })
        except Exception as exc:  # noqa: BLE001 — never break the evening push
            logger.warning("evening stop-proximity sweep failed: %s", exc)
            return []
        return rows

    def _evening_earnings_proximity(self, positions) -> list[dict]:
        """Next-earnings proximity for every held name, for the evening
        report's "reports earnings soon" line.

        Reuses `src.data.event_calendar.fetch_earnings_proximity` — already
        bounded per symbol and in aggregate by the same `config.event_risk`
        timeouts the morning research stage uses — rather than calling the
        unbounded provider method directly. A symbol whose date could not be
        fetched comes back labelled, never as "no earnings".

        Never raises; degrades to [].
        """
        try:
            symbols = self._news_held_symbols(positions)
            if not symbols or getattr(self, "market", None) is None:
                return []
            from src.data.event_calendar import fetch_earnings_proximity
            event_cfg = getattr(getattr(self, "config", None), "event_risk", None)
            rows = fetch_earnings_proximity(
                self.market, symbols,
                per_symbol_timeout_s=getattr(
                    event_cfg, "earnings_symbol_timeout_s", 8.0,
                ),
                total_deadline_s=getattr(event_cfg, "earnings_deadline_s", 20.0),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("evening earnings-proximity sweep failed: %s", exc)
            return []
        return [
            {
                "symbol": r.symbol,
                "sessions_away": r.sessions_away,
                "status": r.status,
            }
            for r in rows or []
        ]

    def _expected_sessions_missing_today(self) -> list[str]:
        """Best-effort internal dead-man's check: on a trading day, which of
        the market-day sessions that should have run by evening left NO
        agent_logs rows? Catches a session that silently never fired — the one
        failure mode push-on-completion observability structurally cannot see
        (a disabled timer, a stuck lock, ET-window math wrong on a half-day).

        Run from evening, which is already gated on `_is_trading_day`, so this
        never false-fires on a holiday. Does NOT cover total host death or
        evening itself not firing — that needs an EXTERNAL dead-man's switch
        (e.g. a healthchecks.io ping the wrapper hits on success). Best-effort:
        any failure returns [] so it can never break the evening push.
        """
        try:
            present = self.db.session_prefixes_logged_on()
        except Exception as exc:  # noqa: BLE001
            logger.warning("missing-session check: agent_logs read failed: %s", exc)
            return []
        # run_id prefix -> display name; morning's prefix is 'run'.
        expected = {"run": "morning", "midday": "midday", "close": "close"}
        missing = [name for prefix, name in expected.items() if prefix not in present]

        # RC5 (2026-07-16): "any run- row exists" cannot tell a completed
        # morning from one killed mid-flight — research rows land BEFORE the
        # kill, so 13 straight days of morning deaths passed this check and
        # the 🔴 banner never fired. Two sharper probes:
        if "morning" not in missing and "run" in present:
            # A legit PM-less completion (no_data, say) records a status
            # marker — skip both probes for it.
            try:
                from src import decision_checkpoint as _dc0
                legit_early_exit = _dc0.read_status("morning") is not None
            except Exception:  # noqa: BLE001
                legit_early_exit = False
            #  (a) research logged but the PM never ran → died during research.
            try:
                agents = self.db.agent_names_logged_on("run-")
                if (not legit_early_exit and agents
                        and "portfolio_manager" not in agents):
                    missing.append("morning (research ran, PM never did — killed mid-run?)")
            except Exception as exc:  # noqa: BLE001
                logger.warning("missing-session check: agent probe failed: %s", exc)
            #  (b) PM plan checkpointed but never consumed → killed before the
            #      RiskStage reviewed it (the observed 6/30-7/15 death mode).
            try:
                import json as _json
                from src import decision_checkpoint as _dc
                p = _dc.checkpoint_path("morning")
                if p.exists() and _json.loads(p.read_text()).get("consumed") is False:
                    missing.append(
                        "morning (PM plan never risk-reviewed — checkpoint unconsumed)"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("missing-session check: checkpoint probe failed: %s", exc)
        return missing

    def _maybe_run_quarterly_meta(self) -> dict | None:
        """Evening-time piggyback for the quarterly meta-reflection loop.

        There is no separate systemd timer for `meta`; the autonomous-
        evolution loop fires by checking the quarter-end gate inside
        evening. The pre-fix behavior was that `run_quarterly_meta_
        reflection()` had to be invoked by hand (`python main.py --mode
        meta`), so the entire 8-week-built loop never ran automatically.

        Wrapped in try/except so a meta failure can never fail the
        evening report. Evening's artifact is load-bearing for next
        morning's PM; meta is a once-a-quarter bonus.

        Returns None when not quarter-end, a result dict otherwise.
        """
        try:
            from src.trading_calendar import et_today
            today = et_today()
            try:
                is_last = self.broker.is_last_trading_day_of_quarter(on_date=today)
            except Exception as e:
                logger.warning("Evening: meta quarter-end check failed: %s", e)
                return None
            if not is_last:
                return None
            logger.info(
                "Evening: today is last trading day of quarter %d-Q%d — "
                "running auto meta-reflection",
                today.year, (today.month - 1) // 3 + 1,
            )
            return self.run_quarterly_meta_reflection(force=False)
        except Exception as e:
            logger.exception("Evening: meta-reflection piggyback failed: %s", e)
            return {"status": "auto_meta_error", "error": str(e)}

    def run_quarterly_meta_reflection(
        self,
        *,
        force: bool = False,
        period_end=None,
        lookback_days: int = 90,
        evolution_root: str = "data/evolution",
        prompts_dir: str | Path | None = None,
    ) -> dict:
        """Build the quarterly digest, run the meta-reflector, persist both.

        Cadence: normally this is a NOP unless today is the last trading day
        of the current quarter (`broker.is_last_trading_day_of_quarter`).
        Pass `force=True` to override — used by CLI `--mode meta --force`
        for ad-hoc runs and by tests.

        Output always includes `digest_path` (persisted) and, when the LLM
        succeeded, `reflection_path`. PR3 intentionally stops here — it
        does NOT edit any prompt files. PR4 will pick up reflection.json
        from disk and apply proposed_learnings through prompt_editor.
        """
        from src.evolution.quarterly_digest import (
            build_quarterly_digest,
            load_previous_digest,
            persist_digest,
        )
        from src.agents.meta_reflector import (
            load_previous_reflection,
            persist_reflection,
        )

        today = period_end or et_today()
        if not force:
            try:
                is_last = self.broker.is_last_trading_day_of_quarter(on_date=today)
            except Exception as exc:
                logger.warning(
                    "meta reflection skipped: quarter-end check failed (%s); "
                    "pass --force to override", exc,
                )
                return {"status": "skipped", "reason": "quarter_end_check_failed"}
            if not is_last:
                logger.info(
                    "meta reflection skipped: %s is not the last trading "
                    "day of the quarter. Pass --force to run anyway.",
                    today,
                )
                return {"status": "skipped", "reason": "not_quarter_end"}

        logger.info("=== Quarterly meta-reflection: %s ===", today)

        # 1. Build digest — deterministic facts layer.
        prev_digest = load_previous_digest(today, root_dir=evolution_root)
        digest = build_quarterly_digest(
            self.db, self.market,
            period_end=today, lookback_days=lookback_days,
            prev_digest=prev_digest,
            prompts_dir=prompts_dir,
        )
        digest_path = persist_digest(digest, root_dir=evolution_root)
        logger.info(
            "Quarterly digest built for %s: alpha=%s, total_real_misses=%s, "
            "total_wrong_buys=%s",
            digest["period"],
            (digest.get("period_performance") or {}).get("alpha_vs_spy_pct"),
            (digest.get("missed_themes") or {}).get("total_real_misses"),
            (digest.get("loss_patterns") or {}).get("total_wrong_buys"),
        )

        # Every invocation is a distinct paid session.  The period remains in
        # the artifacts/result, while a UUID suffix prevents forced reruns of
        # the same quarter from reusing SQLite counters under the run_id PK.
        meta_run_id = f"meta-{digest['period']}-{uuid.uuid4().hex[:8]}"
        self._activate_cost_session(meta_run_id, "meta")
        try:
            self._require_paid_analysis("meta_reflector")
        except PaidAnalysisSuspended as exc:
            payload = self._paid_suspended_payload(meta_run_id, error=exc)
            payload.update(
                period=digest["period"], digest_path=str(digest_path),
                reflection_path=None, reflection=None,
            )
            return payload

        # 2. Meta-reflector LLM — observe-only in PR3 (no prompt edits).
        # analyze() can raise on provider/network failures after retries. The
        # digest has already been persisted so we must degrade to the
        # digest_only path rather than let the exception abort the run
        # (operators lose the audit / status payload otherwise).
        prev_reflection = load_previous_reflection(today, root_dir=evolution_root)
        reflection = None
        ev_result = None
        try:
            reflection, ev_result = self.meta_reflector.analyze(
                digest=digest, prev_reflection=prev_reflection,
            )
        except PaidAnalysisSuspended as exc:
            payload = self._paid_suspended_payload(meta_run_id, error=exc)
            payload.update(
                period=digest["period"], digest_path=str(digest_path),
                reflection_path=None, reflection=None,
            )
            return payload
        except Exception as exc:
            logger.error(
                "meta_reflector.analyze raised; falling back to digest_only: %s",
                exc, exc_info=True,
            )

        # Always log the agent's raw output for audit, even on failure.
        if ev_result is not None:
            try:
                self.db.insert_agent_log(
                    agent_name="meta_reflector",
                    run_id=meta_run_id,
                    input_summary=(
                        f"{digest['period']} · "
                        f"alpha={(digest.get('period_performance') or {}).get('alpha_vs_spy_pct')}"
                    ),
                    input_message=ev_result.user_message,
                    output_summary=(
                        reflection.style_self_portrait[:200]
                        if reflection else "parse_error"
                    ),
                    full_response=ev_result.raw_text,
                    model=ev_result.model,
                    tokens_used=ev_result.tokens_used,
                    input_tokens=ev_result.input_tokens,
                    output_tokens=ev_result.output_tokens,
                    cost_usd=ev_result.cost_usd,
                    **agent_log_kwargs(ev_result),
                )
            except Exception as exc:
                logger.warning("meta_reflector agent_log insert failed: %s", exc)

        if reflection is None:
            logger.error("Meta-reflector returned no valid reflection; "
                         "digest persisted, reflection missing.")
            return {
                "status": "digest_only",
                "run_id": meta_run_id,
                "period": digest["period"],
                "digest_path": str(digest_path),
                "reflection_path": None,
                "reflection": None,
            }

        reflection_path = persist_reflection(reflection, root_dir=evolution_root)
        logger.info(
            "Quarterly meta-reflection complete: %s · %d proposed learnings",
            digest["period"], len(reflection.proposed_learnings),
        )

        # 3. Prompt editor — only runs when evolution.enabled. When off
        # (default until a deployment has reviewed a quarter or two of
        # reflection.json contents by hand), we return without touching any
        # prompt file. The editor itself short-circuits to a full-rejection
        # report; we still persist the attempt log for audit continuity.
        editor_report: dict | None = None
        try:
            from src.config import EvolutionConfig
            evolution_cfg = getattr(self.config, "evolution", None)
            if evolution_cfg is None:
                evolution_cfg = EvolutionConfig()
        except Exception:
            from src.config import EvolutionConfig
            evolution_cfg = EvolutionConfig()

        try:
            from src.evolution.prompt_editor import PromptEditor
            resolved_prompts_dir = (
                Path(prompts_dir) if prompts_dir is not None
                else Path(__file__).resolve().parent.parent / "config" / "prompts"
            )
            editor = PromptEditor(
                config=evolution_cfg,
                prompts_dir=resolved_prompts_dir,
                evolution_dir=evolution_root,
            )
            result_obj = editor.apply_reflection(reflection)
            editor_report = result_obj.to_dict()
            if result_obj.applied:
                logger.info(
                    "Prompt editor applied %d learning(s) across %d agent(s); "
                    "git_commit=%s",
                    len(result_obj.applied),
                    result_obj.agents_edited,
                    result_obj.git_commit,
                )
            elif result_obj.rejected:
                # Most common: evolution.enabled=false (observe-only). Log
                # at INFO so operators see why nothing was applied.
                logger.info(
                    "Prompt editor did not apply any learnings (%d rejected). "
                    "First reason: %s",
                    len(result_obj.rejected), result_obj.rejected[0].reason,
                )
        except Exception as exc:
            logger.error("Prompt editor invocation failed: %s", exc, exc_info=True)

        return {
            "status": "reflected",
            "run_id": meta_run_id,
            "period": digest["period"],
            "digest_path": str(digest_path),
            "reflection_path": str(reflection_path),
            "reflection": reflection.model_dump(),
            "proposed_learnings_count": len(reflection.proposed_learnings),
            "editor_report": editor_report,
        }

    def run_daily(self) -> dict:
        """Fetch full portfolio history from Alpaca, build a CSV, and send
        via Telegram. No LLM calls — pure data export. Runs on weekdays.

        Returns {"status": "sent", "rows": N, "filename": ...} on delivery,
        {"status": "skipped", ...} when Telegram is disabled (CSV built but no
        sink), {"status": "error", ...} on a real failure. The status must be
        honest: previously it reported "sent" even when the upload failed or
        the notifier was disabled, so the operator couldn't tell a delivered
        export from a silently-dropped one.
        """
        from src.notifier import build_daily_csv, TelegramNotifier
        from src.trading_calendar import et_today
        try:
            closes = self.broker.get_full_portfolio_history()
            if not closes:
                logger.warning("run_daily: no portfolio history returned")
                return {"status": "error", "error": "no data from portfolio_history"}
            csv_bytes = build_daily_csv(closes)
            date_str = et_today().strftime("%Y-%m-%d")
            filename = f"pnl_history_{date_str}.csv"
            caption = f"📊 P&L History export — {date_str} ({len(closes)} trading days)"
            notifier = TelegramNotifier()
            delivered = notifier.send_document(csv_bytes, filename, caption)
            base = {"rows": len(closes), "filename": filename}
            if delivered:
                logger.info("run_daily: sent %d rows as %s", len(closes), filename)
                return {"status": "sent", **base}
            if not notifier.enabled:
                # CSV built fine; Telegram simply isn't configured — not a
                # failure, just nowhere to deliver it.
                logger.info(
                    "run_daily: built %d-row CSV %s but Telegram is disabled",
                    len(closes), filename,
                )
                return {"status": "skipped", **base}
            # Enabled but the upload failed (network / API / rate limit).
            logger.error("run_daily: Telegram delivery failed for %s", filename)
            return {"status": "error", "error": "telegram delivery failed", **base}
        except Exception as exc:
            logger.error("run_daily failed: %s", exc, exc_info=True)
            return {"status": "error", "error": str(exc)}
