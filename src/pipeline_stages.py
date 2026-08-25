"""Pipeline stages — explicit, composable, per-responsibility units.

Phase 4 #1 of the architecture work. `TradingPipeline` was a 2600-line
god object whose three `run_*` methods each did data-fetching, LLM
orchestration, risk filtering, order execution, and audit logging
inline. Nothing could be tested in isolation; nothing could be reused
across sessions.

Here we extract the logical phases into stand-alone stages that take a
`RunContext` (explicit shared state), read/write specific fields on it,
and return it (or an early-exit dict) for the next stage.

Morning composes four stages:
  1. MorningResearchStage — parallel macro/news/tech/earnings fan-out
  2. DecisionStage         — L2..L8 memory + PM + Constructor
  3. RiskStage             — hard filter + correlation + RM review + mods
  4. ExecutionStage        — HOLD audit → SELLs → wait fills → BUYs

Midday and evening are *themselves* single-stage workflows (account
snapshot → review/report → log). They have no internal sub-pipeline
to compose, so they stay as TradingPipeline methods rather than being
wrapped in an artificial "stage of one".

Dependency injection pattern: research stage takes each provider/agent
by hand (demonstrates the pure form). Decision/Risk/Execution each take
a `pipeline` reference for the large surface of helpers they share with
TradingPipeline (_build_* memory layers, _filter_* risk helpers,
_order_accepted, _full_sell_qty, etc.). The pragmatic tradeoff: no
tangled re-plumbing of 15+ helpers just to say "zero coupling." Those
helpers are the right extraction boundary for a later phase.
"""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from src.agents.base import agent_log_kwargs
from src.data.technical import compute_indicators
from src.models import NewsIntelligenceReport, TechAnalysisResult, TechnicalIndicators
from src.pipeline_context import RunContext

if TYPE_CHECKING:
    from src.agents.earnings_analyst import EarningsAnalystAgent
    from src.agents.macro_analyst import MacroAnalystAgent
    from src.agents.news_analyst import NewsAnalystAgent
    from src.agents.tech_analyst import TechAnalystAgent
    from src.config import AppConfig
    from src.data.earnings import EarningsDataProvider
    from src.data.macro import MacroDataProvider
    from src.data.macro_store import MacroStore
    from src.data.market import MarketDataProvider
    from src.data.news import NewsDataProvider
    from src.data.news_store import NewsStore
    from src.data.tech_store import TechStore
    from src.models import TradeDecision
    from src.pipeline import TradingPipeline
    from src.storage.db import Database

logger = logging.getLogger(__name__)


def _persist_evidence(db: "Database", *, run_id: str, agent_name: str, kind: str,
                       scope: str, evidence_json: str, symbol: str | None = None,
                       decision_id: str | None = None) -> None:
    """Best-effort Stage 4 structured-evidence write — NEVER raises.

    Wraps `Database.insert_specialist_evidence` so every call site below can
    call this unconditionally without its own try/except. A failure here
    (disk full, lock contention, whatever) is a forensic-display gap, not a
    reason to mark research/decision data degraded or interrupt the
    pipeline — see docs/architecture/MISSION_CONTROL_API.md and
    .claude/rules/trading-core.md's "Logging/forensic persistence failure
    must never relax a deterministic block" rule.
    """
    try:
        db.insert_specialist_evidence(
            run_id=run_id, agent_name=agent_name, kind=kind, scope=scope,
            evidence_json=evidence_json, symbol=symbol, decision_id=decision_id,
        )
    except Exception as e:
        logger.warning(
            "Failed to persist Stage 4 specialist evidence (agent=%s kind=%s "
            "scope=%s symbol=%s): %s", agent_name, kind, scope, symbol, e,
        )


def _record_execution_skip(pipeline, ctx, symbol: str, reason: str,
                           detail: str) -> None:
    """Durable record of a deterministic BUY skip in the execution phase.

    Every skip path in the BUY loop used to be a log-only `continue`: the
    DB, funnel, Mission Control and the evening reflection all read a
    session whose approved BUYs were dropped here as a deliberate no-trade
    (2026-08-19: three risk-approved BUYs skipped as unfunded; the evening
    analyst concluded the system needed "proactive idea generation").
    Appends to ctx.execution_skips (drives the run's final status) and
    persists an `execution_skip` evidence row (drives the funnel/journal).
    Best-effort by construction — persistence failure never affects the
    skip decision itself (trading-core rule).
    """
    ctx.execution_skips.append(
        {"symbol": symbol, "reason": reason, "detail": detail},
    )
    import json as _json
    _persist_evidence(
        pipeline.db, run_id=ctx.run_id, agent_name="execution",
        kind="execution_skip", scope="symbol", symbol=symbol,
        decision_id=ctx.decision_id,
        evidence_json=_json.dumps(
            {"symbol": symbol, "reason": reason, "detail": detail},
        ),
    )


def _record_pipeline_event(pipeline, ctx, symbol: str | None, stage: str,
                           outcome: str, reason: str = "", **details) -> None:
    """Append one typed lifecycle fact to the existing evidence stream."""
    import json as _json
    payload = {"stage": stage, "outcome": outcome, "reason": reason, **details}
    _persist_evidence(
        pipeline.db, run_id=ctx.run_id, agent_name="pipeline",
        kind="pipeline_event", scope="symbol" if symbol else "run",
        symbol=symbol, decision_id=ctx.decision_id,
        evidence_json=_json.dumps(payload, sort_keys=True),
    )


def _apply_scale_all_buys(decisions, verdict) -> tuple[list, float]:
    """Apply RiskVerdict.scale_all_buys to BUY decisions.

    `scale_all_buys` is documented in config/prompts/risk_manager.md as
    a portfolio-level sizing knob with a ge=0.0 le=1.0 range — 0.0 is
    an explicit "kill all BUYs" veto. The pre-fix code did
    ``getattr(...) or 1.0`` which silently collapsed 0.0 to 1.0 because
    0.0 is falsy in Python, disabling the veto. Treat None/missing as
    1.0 (no scaling), but pass 0.0 through so the scaling branch zeros
    every BUY allocation.

    Returns ``(scaled_decisions, scale)`` so the caller can use the
    coerced scale for follow-up filters (re-running hard risk if the
    scale dropped allocations into different buckets).
    """
    scale_raw = getattr(verdict, "scale_all_buys", 1.0)
    scale = 1.0 if scale_raw is None else float(scale_raw)
    if scale >= 1.0 or scale < 0.0:
        return list(decisions), scale

    scaled: list = []
    for d in decisions:
        if d.action == "BUY":
            new_alloc = max(0.0, min(100.0, d.allocation_pct * scale))
            if new_alloc <= 0:
                logger.info(
                    "scale_all_buys=%.2f drops %s (alloc 0 after scaling)",
                    scale, d.symbol,
                )
                continue
            try:
                scaled.append(d.model_copy(update={"allocation_pct": new_alloc}))
                logger.info(
                    "scale_all_buys=%.2f: %s %.2f%% → %.2f%%",
                    scale, d.symbol, d.allocation_pct, new_alloc,
                )
            except Exception as e:
                logger.warning(
                    "scale_all_buys copy failed for %s: %s — keeping original",
                    d.symbol, e,
                )
                scaled.append(d)
        else:
            scaled.append(d)
    return scaled, scale


class MorningResearchStage:
    """Parallel data + LLM fan-out at morning open.

    Produces on ctx:
      macro_summary, macro_analysis, news_intel, analyses, earnings_results,
      symbols_bars, valuations, data_status

    Uses a ThreadPoolExecutor for the 4 parallel calls (same as the old
    inline implementation). Failures are isolated so one bad branch
    doesn't abort the rest.
    """

    def __init__(
        self,
        *,
        config: "AppConfig",
        db: "Database",
        market: "MarketDataProvider",
        macro: "MacroDataProvider",
        news_provider: "NewsDataProvider",
        news_store: "NewsStore",
        macro_store: "MacroStore",
        tech_store: "TechStore",
        earnings_provider: "EarningsDataProvider",
        macro_analyst: "MacroAnalystAgent",
        news_analyst: "NewsAnalystAgent",
        tech_analyst: "TechAnalystAgent",
        earnings_analyst: "EarningsAnalystAgent",
        has_actionable_signal_fn,
        run_news_update_fn,
        load_earnings_analyses_fn,
    ):
        self.config = config
        self.db = db
        self.market = market
        self.macro = macro
        self.news_provider = news_provider
        self.news_store = news_store
        self.macro_store = macro_store
        self.tech_store = tech_store
        self.earnings_provider = earnings_provider
        self.macro_analyst = macro_analyst
        self.news_analyst = news_analyst
        self.tech_analyst = tech_analyst
        self.earnings_analyst = earnings_analyst
        # Injected callables so we don't duplicate pre-filter / news / earnings
        # orchestration logic. Those still live on TradingPipeline for now
        # because they touch shared state we haven't finished extracting.
        self._has_actionable_signal = has_actionable_signal_fn
        self._run_news_update = run_news_update_fn
        self._load_earnings_analyses = load_earnings_analyses_fn

    def run(self, ctx: RunContext) -> RunContext:
        logger.info("=== Stage: MorningResearch ===")
        data_status: dict[str, str] = {}
        try:
            prior_macro_state = self.macro_store.load_last_state() or {}
        except Exception as e:
            logger.warning("Failed to load prior macro state: %s", e)
            prior_macro_state = {}
        try:
            news_narrative = self.news_store.load_macro_narrative()
        except Exception as e:
            logger.warning("Failed to load macro news narrative: %s", e)
            news_narrative = None

        def _run_macro():
            macro_summary = self.macro.get_macro_summary()
            logger.info(
                "Macro data: VIX=%s, HY OAS=%sbps, CPI core YoY=%s, UNRATE=%s",
                macro_summary.get("vix", {}).get("current"),
                macro_summary.get("credit_spread", {}).get("current_bps"),
                macro_summary.get("inflation", {}).get("core_cpi_yoy"),
                macro_summary.get("unemployment", {}).get("current"),
            )
            analysis, result = self.macro_analyst.analyze(
                macro_summary=macro_summary,
                universe=self.config.trading.universe,
                last_state=prior_macro_state,
                news_narrative=news_narrative,
            )
            if analysis:
                try:
                    self.macro_store.save_last_state(analysis.model_dump())
                except Exception as e:
                    logger.warning("Failed to persist macro last state: %s", e)
            return macro_summary, analysis, result

        def _run_news():
            return self._run_news_update(ctx.run_id, session="morning")

        def _run_tech():
            all_symbols_data = []
            symbols_bars: dict[str, list] = {}
            for symbol in self.config.trading.universe:
                bars = self.market.get_ohlcv(symbol, self.config.trading.lookback_days)
                if not bars:
                    logger.warning("No data for %s, skipping", symbol)
                    continue
                indicators = compute_indicators(symbol, bars)
                all_symbols_data.append({"symbol": symbol, "bars": bars, "indicators": indicators})
                symbols_bars[symbol] = bars
            ctx.symbols_bars = symbols_bars
            symbols_data = [
                s for s in all_symbols_data
                if self._has_actionable_signal(s["indicators"], s["symbol"], s["bars"], ctx.positions)
            ]
            logger.info(
                "Tech pre-filter: %d/%d symbols have actionable signals",
                len(symbols_data), len(all_symbols_data),
            )
            for candidate in symbols_data:
                _record_pipeline_event(
                    self, ctx, candidate["symbol"], "opportunity",
                    "discovered", "actionable_technical_prefilter",
                )
            if not symbols_data:
                return {}, None
            prior_ratings = self.tech_store.load()
            valuations: dict[str, dict] = {}
            for s in symbols_data:
                sym = s.get("symbol")
                if sym:
                    try:
                        valuations[sym] = self.market.get_valuation_metrics(sym)
                    except Exception as e:
                        logger.warning("valuation fetch crashed for %s: %s", sym, e)
            ctx.valuations = valuations
            # analyses_map is guaranteed to carry every symbol in
            # symbols_data as a key (2026-08-19 Tech batch-response
            # symbol-loss fix) — a TechAnalysisResult on success, or None
            # for a symbol that failed to resolve even after tech_analyst's
            # own bounded retry. Filter before touching real analyses;
            # `analyses_map` itself (None values intact) is still returned
            # so the caller can see and report the failed count instead of
            # it silently vanishing.
            analyses_map, ta_res = self.tech_analyst.analyze_batch(
                symbols_data,
                prior_ratings=prior_ratings,
                valuations=valuations,
                prior_macro_regime=prior_macro_state.get("regime"),
                prior_macro_outlook=prior_macro_state.get("equity_outlook"),
            )
            resolved = [a for a in analyses_map.values() if a is not None]
            if resolved:
                try:
                    self.tech_store.update(resolved)
                except Exception as e:
                    logger.warning("TechStore.update failed: %s", e)
                ages = self.tech_store.compute_ages([a.symbol for a in resolved])
                for analysis in resolved:
                    if analysis.symbol in ages:
                        analysis.signal_age_days = ages[analysis.symbol]
            return analyses_map, ta_res

        def _load_earnings():
            return self._load_earnings_analyses(ctx.run_id, session="morning", ctx=ctx)

        logger.info("Starting parallel: macro + news + tech + earnings")
        with ThreadPoolExecutor(max_workers=4) as ex:
            macro_future = ex.submit(_run_macro)
            news_future = ex.submit(_run_news)
            tech_future = ex.submit(_run_tech)
            earnings_future = ex.submit(_load_earnings)

        # Macro
        try:
            macro_summary, macro_analysis, ma_result = macro_future.result()
            # audit round 2: commit the analysis to ctx BEFORE the agent_logs
            # write — a DB lock/timeout on the log write used to discard a
            # fully successful macro run (ctx fields were assigned after it).
            ctx.macro_summary = macro_summary
            ctx.macro_analysis = macro_analysis
            self.db.insert_agent_log(
                agent_name="macro_analyst", run_id=ctx.run_id,
                input_summary=f"VIX={macro_summary.get('vix', {}).get('current')}",
                input_message=ma_result.user_message,
                output_summary=(
                    f"regime={macro_analysis.regime}, outlook={macro_analysis.equity_outlook}"
                    if macro_analysis else "parse_error"
                ),
                full_response=ma_result.raw_text,
                model=ma_result.model,
                tokens_used=ma_result.tokens_used,
                input_tokens=ma_result.input_tokens,
                output_tokens=ma_result.output_tokens,
                cost_usd=ma_result.cost_usd,
                **agent_log_kwargs(ma_result),
            )
            ctx.macro_summary = macro_summary
            ctx.macro_analysis = macro_analysis
            if macro_analysis:
                logger.info(
                    "Macro analysis: regime=%s, outlook=%s, target_invested=%s%%",
                    macro_analysis.regime, macro_analysis.equity_outlook,
                    macro_analysis.position_guidance.target_invested_pct,
                )
                data_status["macro"] = "ok"
                _persist_evidence(
                    self.db, run_id=ctx.run_id, agent_name="macro_analyst",
                    kind="analysis", scope="run",
                    evidence_json=macro_analysis.model_dump_json(),
                )
            else:
                data_status["macro"] = "parse_error"
        except Exception as e:
            logger.error("Macro analyst failed: %s. Continuing without macro.", e)
            data_status["macro"] = "failed"

        # News
        news_intel: NewsIntelligenceReport | None = None
        try:
            news_intel = news_future.result()
            if news_intel:
                logger.info("News briefing: %s", news_intel.pm_briefing[:200])
                data_status["news"] = "ok"
                _persist_evidence(
                    self.db, run_id=ctx.run_id, agent_name="news_analyst",
                    kind="analysis", scope="run",
                    evidence_json=news_intel.model_dump_json(),
                )
            else:
                data_status["news"] = "parse_error"
        except Exception as e:
            logger.error("News analyst failed: %s. Continuing without news.", e)
            data_status["news"] = "failed"
        ctx.news_intel = news_intel

        # Tech
        analyses: list[TechAnalysisResult] = []
        try:
            analyses_map, ta_result = tech_future.result()
            # analyses_map carries every pre-filtered symbol as a key
            # (2026-08-19 Tech batch-response symbol-loss fix); None marks
            # a symbol tech_analyst could not resolve even after its own
            # bounded retry. Filter before building the real analyses
            # list, and surface the failed count explicitly rather than
            # letting it disappear into a plain "ok".
            analyses = [a for a in analyses_map.values() if a is not None]
            failed_count = len(analyses_map) - len(analyses)
            if not analyses_map:
                data_status["tech"] = "empty"
            elif failed_count == 0:
                data_status["tech"] = "ok"
            elif analyses:
                data_status["tech"] = "partial"
                logger.warning(
                    "Tech batch partial: %d/%d symbols resolved, %d failed "
                    "even after retry — proceeding with the resolved subset",
                    len(analyses), len(analyses_map), failed_count,
                )
            else:
                data_status["tech"] = "failed"
                logger.error(
                    "Tech batch: all %d submitted symbol(s) failed even after retry",
                    len(analyses_map),
                )
            if ta_result:
                self.db.insert_agent_log(
                    agent_name="tech_analyst", run_id=ctx.run_id,
                    input_summary=(
                        f"Batch: {len(analyses)}/{len(analyses_map)} symbols "
                        f"analyzed" + (f", {failed_count} failed" if failed_count else "")
                    ),
                    input_message=ta_result.user_message,
                    output_summary=", ".join(f"{a.symbol}:{a.rating}" for a in analyses),
                    full_response=ta_result.raw_text,
                    model=ta_result.model,
                    tokens_used=ta_result.tokens_used,
                    input_tokens=ta_result.input_tokens,
                    output_tokens=ta_result.output_tokens,
                    cost_usd=ta_result.cost_usd,
                    **agent_log_kwargs(ta_result),
                )
                for analysis in analyses:
                    _persist_evidence(
                        self.db, run_id=ctx.run_id, agent_name="tech_analyst",
                        kind="analysis", scope="symbol", symbol=analysis.symbol,
                        evidence_json=analysis.model_dump_json(),
                    )
                    _record_pipeline_event(
                        self, ctx, analysis.symbol, "specialist", "evaluated",
                        "technical_analysis_validated",
                        specialist="tech_analyst", rating=analysis.rating,
                    )
                for symbol, analysis in analyses_map.items():
                    if analysis is None:
                        _record_pipeline_event(
                            self, ctx, symbol, "specialist", "failed",
                            "technical_analysis_unresolved_after_retry",
                            specialist="tech_analyst",
                        )
            logger.info("Technical analysis complete: %d symbols in 1 LLM call", len(analyses))
        except Exception as e:
            logger.error("Tech analyst failed: %s. Continuing without technical data.", e)
            data_status["tech"] = "failed"
        ctx.analyses = analyses

        # Earnings
        earnings_results = []
        try:
            _, earnings_results = earnings_future.result()
            data_status["earnings"] = "ok"
            import json as _json
            for item in earnings_results:
                analysis = item.get("analysis") if isinstance(item, dict) else None
                symbol = item.get("symbol") if isinstance(item, dict) else None
                if analysis and symbol:
                    # `analysis` is already validated_model.model_dump() —
                    # see EarningsAnalystAgent._analyze_new/_load_analysis —
                    # never re-derived from raw filing text here.
                    _persist_evidence(
                        self.db, run_id=ctx.run_id, agent_name="earnings_analyst",
                        kind="analysis", scope="symbol", symbol=symbol,
                        evidence_json=_json.dumps(analysis),
                    )
        except Exception as e:
            logger.error("Earnings check failed: %s. Continuing without earnings.", e)
            data_status["earnings"] = "failed"
        ctx.earnings_results = earnings_results

        ctx.data_status = data_status
        # Single grep-able summary line. Each agent's failure already logs
        # at ERROR individually, but a downstream operator scanning the
        # journal for "why did morning trade zero today?" wants one row
        # listing all degraded inputs side-by-side. The 2+ failure
        # advisory in RiskStage handles the runtime defensive response;
        # this log handles the postmortem readability.
        degraded = [k for k, v in data_status.items() if v not in ("ok", "empty")]
        if degraded:
            logger.error(
                "Morning research degraded: %s | full status=%s",
                ",".join(sorted(degraded)), data_status,
            )
        return ctx


class DecisionStage:
    """Build PM memory layers → call PM → run Constructor.

    Reads:  ctx.positions, ctx.analyses, ctx.news_intel, ctx.earnings_results,
            ctx.macro_analysis, ctx.total_value, ctx.cash, ctx.last_equity
    Writes: ctx.portfolio_decision (with .targets AND .decisions populated),
            ctx.facts
    """

    def __init__(self, *, pipeline: "TradingPipeline"):
        self._pipeline = pipeline

    def run(self, ctx: RunContext) -> RunContext:
        from src.trading_calendar import session_date_key

        pipeline = self._pipeline
        run_id = ctx.run_id
        positions = ctx.positions
        analyses = ctx.analyses
        news_intel = ctx.news_intel
        earnings_results = ctx.earnings_results
        macro_analysis = ctx.macro_analysis
        total_value = ctx.total_value
        # PM sizes against `ctx.deployable_cash` = raw cash + convertible
        # sweep value (see `_compute_deployable_cash` for the verified
        # Alpaca field semantics: a filled SGOV sale credits `cash`
        # immediately — T+1 gates only withdrawal/transfer). The sweep
        # detail is rendered informationally via `reserve_balance`;
        # execution's raw-cash recheck after the funding sale remains the
        # final authority on what a BUY can actually spend.
        cash = ctx.deployable_cash
        last_equity = ctx.last_equity

        # isinstance guard: stage tests stub `pipeline` with MagicMock, whose
        # auto-attrs would otherwise duck-type as an enabled sweeper.
        from src.execution.cash_sweep import CashSweeper
        sweeper = getattr(pipeline, "_sweeper", None)
        sweeper = sweeper() if callable(sweeper) else None
        reserve_balance = 0.0
        if isinstance(sweeper, CashSweeper):
            positions, parked = sweeper.split_positions(positions)
            if parked is not None:
                reserve_balance = sweeper.parked_value(ctx.positions)

        yesterday_insights = pipeline.db.get_latest_insights(before_date=session_date_key())
        recent_performance = pipeline._compute_recent_performance(last_equity)
        if yesterday_insights:
            logger.info(
                "Loaded yesterday's insights (risk=%s): %s",
                yesterday_insights.get("risk_rating", "?"),
                yesterday_insights.get("tomorrow_outlook", "")[:100],
            )

        position_history = pipeline._build_position_history(positions)
        # Publish both to ctx so RiskStage audits PM against the SAME holding
        # ages and drawdown state PM sized from, instead of a second snapshot
        # taken minutes later (2026-08-13 agent audit).
        ctx.position_history = position_history
        ctx.recent_performance = recent_performance
        weekly_narrative = pipeline._build_weekly_narrative()
        macro_trajectory = pipeline._build_macro_trajectory()
        active_state_changes = pipeline._build_active_state_changes()
        rm_recent_verdicts = pipeline._build_rm_recent_verdicts()
        pm_recent_decisions = pipeline._build_pm_recent_decisions()
        projected_portfolio = pipeline._build_projected_portfolio(
            positions, analyses, total_value,
        )
        calibration_note = pipeline._build_calibration_note()
        macro_tech_alignment = pipeline._build_macro_tech_alignment(macro_analysis, analyses)
        # Phase-1 evening-upgrade feedback: surface recurring missed themes
        # (L3d) and repeat loss patterns (L3f) that evening classified over
        # the last 14 days. Empty strings when no recurring pattern found.
        recent_missed_lessons = pipeline._build_recent_missed_lessons()
        recent_loss_pits = pipeline._build_recent_loss_pits()
        pm_facts = pipeline._build_pm_facts(
            positions=positions, analyses=analyses,
            total_value=total_value, cash=cash,
            recent_performance=recent_performance,
            macro_analysis=macro_analysis,
        )
        ctx.facts = pm_facts

        portfolio_decision, pm_result = pipeline.portfolio_manager.decide(
            analyses=analyses,
            positions=positions,
            macro_analysis=(macro_analysis.model_dump() if macro_analysis else None),
            cash_balance=cash,
            reserve_balance=reserve_balance,
            total_value=total_value,
            news_intel=news_intel,
            earnings_analyses=earnings_results,
            yesterday_insights=yesterday_insights,
            recent_performance=recent_performance,
            position_history=position_history,
            weekly_narrative=weekly_narrative,
            macro_trajectory=macro_trajectory,
            active_state_changes=active_state_changes,
            rm_recent_verdicts=rm_recent_verdicts,
            pm_recent_decisions=pm_recent_decisions,
            projected_portfolio=projected_portfolio,
            calibration_note=calibration_note,
            macro_tech_alignment=macro_tech_alignment,
            recent_missed_lessons=recent_missed_lessons,
            recent_loss_pits=recent_loss_pits,
            facts=pm_facts,
            allow_margin=bool(getattr(pipeline.config.risk, "allow_margin", False)),
        )

        if portfolio_decision and portfolio_decision.reasoning_chain:
            rc = portfolio_decision.reasoning_chain
            # All NINE fields. This line logged seven, and the two it omitted
            # were the two the schema lets default to "" — so the operator-
            # facing log could not distinguish "PM red-teamed its book" from
            # "PM skipped the step" (2026-08-13 agent audit).
            logger.info(
                "PM Reasoning Chain:\n  Macro: %s\n  News: %s\n  Earnings: %s\n  "
                "Conflicts: %s\n  Sizing: %s\n  Balance: %s\n  Cash: %s\n  "
                "Continuity: %s\n  Pre-mortem: %s",
                rc.macro_filter[:120], rc.news_check[:120], rc.earnings_check[:120],
                rc.signal_conflicts[:120], rc.sizing_logic[:120],
                rc.portfolio_balance[:120], rc.cash_target[:120],
                rc.continuity_check[:120] or "[MISSING]",
                rc.premortem_check[:120] or "[MISSING]",
            )

        # Stage 1 (QAMC correlation plumbing): one id per PM call, generated
        # independently of run_id (not reused verbatim) so it stays correct
        # even if a future change ever calls decide() more than once per
        # run. Threaded to the risk_manager agent_logs row (RiskStage) and
        # every trades row this run's decisions produce (ExecutionStage).
        decision_id = f"{run_id}-dec-{uuid.uuid4().hex[:6]}"
        ctx.decision_id = decision_id

        pm_log_kwargs = agent_log_kwargs(pm_result)
        if portfolio_decision is None:
            pm_log_kwargs["status"] = "agent_failure"
        pipeline.db.insert_agent_log(
            agent_name="portfolio_manager", run_id=run_id,
            input_summary=f"{len(analyses)} analyses, ${total_value:.0f} total",
            input_message=pm_result.user_message,
            output_summary=(
                portfolio_decision.portfolio_view
                if portfolio_decision else "agent_failure: no valid PM decision"
            ),
            full_response=pm_result.raw_text,
            model=pm_result.model,
            tokens_used=pm_result.tokens_used,
            input_tokens=pm_result.input_tokens,
            output_tokens=pm_result.output_tokens,
            cost_usd=pm_result.cost_usd,
            decision_id=decision_id,
            **pm_log_kwargs,
        )

        if not portfolio_decision:
            _record_pipeline_event(
                pipeline, ctx, None, "portfolio_manager", "failed",
                "no_valid_grounded_decision",
            )
            _persist_evidence(
                pipeline.db, run_id=run_id, agent_name="portfolio_manager",
                kind="agent_failure", scope="run", decision_id=decision_id,
                evidence_json=(
                    '{"failure":"no_valid_grounded_decision",'
                    '"stage":"portfolio_manager","decision":null}'
                ),
            )
            ctx.portfolio_decision = None
            return ctx

        import json as _json
        _persist_evidence(
            pipeline.db, run_id=run_id, agent_name="portfolio_manager",
            kind="reasoning", scope="run", decision_id=decision_id,
            evidence_json=_json.dumps({
                "portfolio_view": portfolio_decision.portfolio_view,
                "reasoning_chain": portfolio_decision.reasoning_chain.model_dump(),
            }),
        )
        for target in portfolio_decision.targets:
            _persist_evidence(
                pipeline.db, run_id=run_id, agent_name="portfolio_manager",
                kind="target", scope="symbol", symbol=target.symbol,
                decision_id=decision_id, evidence_json=target.model_dump_json(),
            )
        target_symbols = {target.symbol for target in portfolio_decision.targets}
        for analysis in analyses:
            if analysis.symbol not in target_symbols:
                _record_pipeline_event(
                    pipeline, ctx, analysis.symbol, "portfolio_manager", "omitted",
                    "candidate_not_selected_for_target",
                )

        price_map = {p.symbol: p.current_price for p in positions}
        for target in portfolio_decision.targets:
            sym = target.symbol.strip().upper()
            if sym in price_map:
                continue
            try:
                live = pipeline.broker.get_latest_price(sym)
            except Exception as e:
                logger.warning("Constructor price lookup failed for %s: %s", sym, e)
                continue
            if live and live > 0:
                price_map[sym] = live
        portfolio_decision.decisions = pipeline.portfolio_constructor.construct_orders(
            targets=portfolio_decision.targets,
            positions=positions,
            analyses=analyses,
            total_value=total_value,
            price_map=price_map,
        )
        logger.info(
            "Constructor: %d targets → %d decisions (%d BUY, %d SELL, %d HOLD)",
            len(portfolio_decision.targets),
            len(portfolio_decision.decisions),
            sum(1 for d in portfolio_decision.decisions if d.action == "BUY"),
            sum(1 for d in portfolio_decision.decisions if d.action == "SELL"),
            sum(1 for d in portfolio_decision.decisions if d.action == "HOLD"),
        )
        # "Proposed" evidence — the constructor's concrete order BEFORE the
        # AI Risk Manager reviews/modifies it (RiskStage persists the
        # post-review verdict/modifications separately). Together these let
        # the UI show a proposed-vs-executed delta per symbol without
        # re-deriving it from raw agent_logs text.
        for decision in portfolio_decision.decisions:
            _persist_evidence(
                pipeline.db, run_id=run_id, agent_name="portfolio_manager",
                kind="proposed_order", scope="symbol", symbol=decision.symbol,
                decision_id=decision_id, evidence_json=decision.model_dump_json(),
            )
            _record_pipeline_event(
                pipeline, ctx, decision.symbol, "portfolio_manager", "proposed",
                "constructor_created_order", action=decision.action,
            )
        ctx.portfolio_decision = portfolio_decision
        return ctx


class RiskStage:
    """Hard filter → earnings cap → correlation → RM review → mods → re-filter.

    Reads:  ctx.portfolio_decision, ctx.positions, ctx.total_value,
            ctx.last_equity, ctx.earnings_results, ctx.macro_analysis,
            ctx.analyses, ctx.symbols_bars, ctx.data_status, ctx.news_intel,
            ctx.macro_summary

    Writes: ctx.portfolio_decision.decisions (filtered/capped/scaled),
            ctx.correlation_matrix, ctx.daily_pnl, ctx.macro_target_pct

    Returns an early-exit dict (symbol_block / hard_risk_block / rejected)
    or None when the pipeline should proceed to execution.
    """

    def __init__(self, *, pipeline: "TradingPipeline"):
        self._pipeline = pipeline

    def run(self, ctx: RunContext) -> dict | None:
        pipeline = self._pipeline
        run_id = ctx.run_id
        portfolio_decision = ctx.portfolio_decision
        positions = ctx.positions
        total_value = ctx.total_value
        last_equity = ctx.last_equity
        earnings_results = ctx.earnings_results
        macro_analysis = ctx.macro_analysis
        analyses = ctx.analyses
        news_intel = ctx.news_intel
        data_status = ctx.data_status

        # Cash-sweep view — same contract as DecisionStage: the RiskManager
        # must never see parked T-bills as an 84%-of-book "position" (review
        # finding: PM and RM otherwise get contradictory views of the same
        # dollars in the same run). IMPORTANT: only the LLM-facing uses (RM
        # prompt, correlation pool, has_book_to_check) take the scrubbed
        # list — the hard filter keeps RAW positions because it still needs
        # to find the vehicle in the list to exclude it from net-exposure /
        # cluster math (it no longer credits any cash from it — see the
        # 2026-08-19 SGOV/deployable-liquidity forensic note below).
        from src.execution.cash_sweep import CashSweeper
        sweeper = getattr(pipeline, "_sweeper", None)
        sweeper = sweeper() if callable(sweeper) else None
        rm_positions = positions
        if isinstance(sweeper, CashSweeper):
            rm_positions, _parked = sweeper.split_positions(positions)

        # Symbol guard
        before_symbol_guard = list(portfolio_decision.decisions)
        portfolio_decision.decisions, symbol_blocked_reasons = pipeline._filter_supported_symbols(
            portfolio_decision.decisions, analyses, positions,
        )
        if symbol_blocked_reasons:
            reasons = "; ".join(dict.fromkeys(symbol_blocked_reasons))
            logger.warning("SYMBOL GUARD BLOCK: %s", reasons)
            allowed_ids = {id(d) for d in portfolio_decision.decisions}
            for decision in before_symbol_guard:
                if id(decision) not in allowed_ids:
                    _record_pipeline_event(
                        pipeline, ctx, decision.symbol, "deterministic_gate",
                        "blocked", "symbol_guard", detail=reasons,
                    )
            if not portfolio_decision.decisions:
                return {"status": "symbol_block", "orders": [], "reason": reasons}
            logger.info(
                "Allowing %d supported orders through after symbol guard filter",
                len(portfolio_decision.decisions),
            )

        # Pass the book so the cap measures the RESULTING weight, not just the
        # add: allocation_pct here is the constructor's delta, so a name already
        # at 15% with an unread filing could otherwise be topped up to 20%.
        # rm_positions (sweep-vehicle-free) is the right basis — parked T-bills
        # are cash and never carry an earnings filing.
        portfolio_decision.decisions = pipeline._clamp_queued_earnings_buys(
            portfolio_decision.decisions, earnings_results,
            positions=rm_positions, total_value=total_value,
        )

        daily_pnl = total_value - last_equity
        ctx.daily_pnl = daily_pnl
        macro_target_pct = None
        if macro_analysis:
            macro_target_pct = macro_analysis.position_guidance.target_invested_pct
        ctx.macro_target_pct = macro_target_pct

        correlation_matrix = None
        try:
            from src.data.correlation import build_correlation_matrix
            pool_bars = dict(ctx.symbols_bars)
            for p in rm_positions:
                if p.symbol not in pool_bars:
                    pool_bars[p.symbol] = pipeline.market.get_ohlcv(
                        p.symbol, pipeline.config.trading.lookback_days,
                    ) or []
            correlation_matrix = build_correlation_matrix(pool_bars)
        except Exception as e:
            logger.warning("Failed to build correlation matrix: %s (continuing without)", e)
        ctx.correlation_matrix = correlation_matrix or {}

        before_hard_gate = list(portfolio_decision.decisions)
        portfolio_decision.decisions, rule_violations, blocked_reasons = (
            pipeline._filter_hard_risk_decisions(
                portfolio_decision.decisions,
                positions, total_value, daily_pnl,
                baseline=last_equity,
                macro_target_invested_pct=macro_target_pct,
                correlation_matrix=correlation_matrix,
                cash=ctx.deployable_cash,
            )
        )
        if blocked_reasons:
            reasons = "; ".join(dict.fromkeys(blocked_reasons))
            logger.warning("HARD RISK BLOCK (BUY blocked): %s", reasons)
            allowed_ids = {id(d) for d in portfolio_decision.decisions}
            for decision in before_hard_gate:
                if id(decision) not in allowed_ids:
                    _record_pipeline_event(
                        pipeline, ctx, decision.symbol, "deterministic_gate",
                        "blocked", "hard_risk", detail=reasons,
                    )
            if not portfolio_decision.decisions:
                pipeline._persist_hard_risk_block(ctx, reasons, stage="pre_rm")
                return {"status": "hard_risk_block", "orders": [], "reason": reasons}
            logger.info(
                "Allowing %d non-blocked orders through after hard risk filter",
                len(portfolio_decision.decisions),
            )

        degraded = [k for k, v in data_status.items() if v not in ("ok", "empty")]
        if len(degraded) >= 2:
            from src.risk.rules import RiskViolation as _RV
            rule_violations.append(_RV(
                rule="data_degraded",
                message=(
                    f"Upstream data sources degraded: {', '.join(sorted(degraded))} "
                    f"(status: {data_status}). Decisions may be built on incomplete input — "
                    f"RM should consider scale_all_buys < 1.0."
                ),
                value=float(len(degraded)),
                limit=1.0,
            ))
            logger.warning("Morning data degradation: %s", data_status)

        has_book_to_check = len(rm_positions) >= 2 or any(
            d.action == "BUY" for d in portfolio_decision.decisions
        )
        if (not correlation_matrix) and has_book_to_check:
            from src.risk.rules import RiskViolation as _RV
            rule_violations.append(_RV(
                rule="correlation_coverage_gap",
                message=(
                    "Correlation matrix is empty (insufficient bar data this run). "
                    "The cluster-concentration advisory is DISABLED. Consider "
                    "scale_all_buys < 1.0 until coverage returns, especially for "
                    "thematic names (AI, semis, energy)."
                ),
                value=0.0,
                limit=2.0,
            ))
            logger.warning(
                "Correlation matrix empty — cluster risk check disabled for this run "
                "(positions=%d, buy_candidates=%d)",
                len(positions),
                sum(1 for d in portfolio_decision.decisions if d.action == "BUY"),
            )

        # 2026-08-13 agent audit — "premortem/observability". `premortem_check`
        # and `continuity_check` are MANDATORY in portfolio_manager.md but
        # default to "" in ReasoningChain, so PM skipping the two disconfirming
        # steps produced a clean parse, a clean log line and a clean verdict.
        # The step could vanish and nothing in the system would say so.
        #
        # The schema stays permissive on purpose (pre-2026-06 logs carry
        # neither field and replay must keep parsing them — see
        # src/models.py::ReasoningChain), so the observability lands here as an
        # ADVISORY, the same non-blocking seam `data_degraded` and
        # `correlation_coverage_gap` already use. No order is blocked by it;
        # RM's prompt requires every advisory to be answered in the matching
        # reasoning_chain field, which is what makes the omission visible.
        rc_now = portfolio_decision.reasoning_chain
        if rc_now is not None:
            missing_audit_steps = [
                name for name, value in (
                    ("premortem_check", rc_now.premortem_check),
                    ("continuity_check", rc_now.continuity_check),
                )
                if not (value or "").strip()
            ]
            if missing_audit_steps:
                from src.risk.rules import RiskViolation as _RV
                rule_violations.append(_RV(
                    rule="pm_audit_step_missing",
                    message=(
                        f"PM returned no {' and no '.join(missing_audit_steps)} — "
                        f"mandatory in its prompt, optional in the schema, so this "
                        f"raised no parse error. The disconfirming/red-team step of "
                        f"today's plan was NOT performed. Weigh the plan as unaudited "
                        f"in that respect and address it in "
                        f"`reasoning_chain.overall`."
                    ),
                    value=float(len(missing_audit_steps)),
                    limit=0.0,
                ))
                logger.warning(
                    "PM reasoning chain missing mandatory audit step(s): %s "
                    "(run_id=%s) — surfaced to RM as a pm_audit_step_missing advisory",
                    ", ".join(missing_audit_steps), run_id,
                )

        # 2026-08-19 SGOV/deployable-liquidity forensic: RM used to be told
        # `cash + parked SGOV value`, the same overstated figure PM saw —
        # RM's cash_only / sizing_sanity audit was therefore auditing PM
        # against a number neither of them could actually spend same-day.
        # RM now gets `ctx.deployable_cash` (settled, non-margin) plus the
        # parked reserve separately/informationally via `reserve_balance`.
        rm_cash = ctx.deployable_cash
        rm_reserve_balance = 0.0
        if isinstance(sweeper, CashSweeper):
            rm_reserve_balance = sweeper.parked_value(ctx.positions)

        # Holding ages + system-drawdown state for the RM audit (2026-08-13
        # agent audit). Normally DecisionStage already published both. On the
        # RC2 resume lane it never ran, so rebuild rather than let RM silently
        # lose the evidence for two of the rules its prompt makes it own. Both
        # builders are local DB reads with no LLM call and no broker call; a
        # failure degrades to "not provided" in the prompt, never to a wrong
        # value that reads as "no drawdown".
        rm_position_history = ctx.position_history
        rm_recent_performance = ctx.recent_performance
        if not rm_position_history:
            try:
                rm_position_history = pipeline._build_position_history(rm_positions)
                ctx.position_history = rm_position_history
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "RiskStage: position history rebuild failed — RM will see "
                    "holding ages as unknown: %s", e,
                )
                rm_position_history = {}
        if not rm_recent_performance:
            try:
                rm_recent_performance = pipeline._compute_recent_performance(last_equity)
                ctx.recent_performance = rm_recent_performance
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "RiskStage: recent-performance rebuild failed — RM cannot "
                    "audit the drawdown-halve rule this run: %s", e,
                )
                rm_recent_performance = {}

        verdict, rm_result = pipeline.risk_manager.review(
            portfolio_decision=portfolio_decision,
            positions=rm_positions,
            macro_summary=ctx.macro_summary,
            rule_violations=rule_violations,
            tech_analyses=analyses,
            news_intel=news_intel,
            earnings_analyses=earnings_results,
            # audit round 2: the veto layer's rr_audit / sizing_sanity steps
            # ran blind — no equity, no cash, no weights.
            total_value=total_value,
            cash=rm_cash,
            reserve_balance=rm_reserve_balance,
            position_history=rm_position_history,
            recent_performance=rm_recent_performance,
        )

        rm_log_kwargs = agent_log_kwargs(rm_result)
        if verdict is None:
            rm_log_kwargs["status"] = "agent_failure"
        pipeline.db.insert_agent_log(
            agent_name="risk_manager", run_id=run_id,
            input_summary=f"{len(portfolio_decision.decisions)} trades, {len(rule_violations)} violations",
            input_message=rm_result.user_message,
            output_summary=f"Approved: {verdict.approved if verdict else 'error'}",
            full_response=rm_result.raw_text,
            model=rm_result.model,
            tokens_used=rm_result.tokens_used,
            input_tokens=rm_result.input_tokens,
            output_tokens=rm_result.output_tokens,
            cost_usd=rm_result.cost_usd,
            decision_id=ctx.decision_id,
            **rm_log_kwargs,
        )

        if verdict:
            _persist_evidence(
                pipeline.db, run_id=run_id, agent_name="risk_manager",
                kind="verdict", scope="run", decision_id=ctx.decision_id,
                evidence_json=verdict.model_dump_json(),
            )
            for mod in verdict.modifications:
                _persist_evidence(
                    pipeline.db, run_id=run_id, agent_name="risk_manager",
                    kind="modification", scope="symbol", symbol=mod.symbol,
                    decision_id=ctx.decision_id, evidence_json=mod.model_dump_json(),
                )

        if verdict is None:
            logger.error(
                "Risk manager AGENT FAILURE: output remained unparseable after "
                "bounded repair; no trading verdict exists",
            )
            for decision in portfolio_decision.decisions:
                _record_pipeline_event(
                    pipeline, ctx, decision.symbol, "risk", "failed",
                    "risk_manager_unparseable_output",
                )
            _persist_evidence(
                pipeline.db, run_id=run_id, agent_name="risk_manager",
                kind="agent_failure", scope="run", decision_id=ctx.decision_id,
                evidence_json=(
                    '{"failure":"unparseable_output",'
                    '"stage":"risk_manager","verdict":null}'
                ),
            )
            return {
                "status": "agent_failure", "orders": [],
                "reason": "risk_manager_unparseable_output",
            }

        if not verdict.approved:
            logger.info(
                "Risk manager REJECTED trades: %s",
                verdict.reasoning,
            )
            for decision in portfolio_decision.decisions:
                _record_pipeline_event(
                    pipeline, ctx, decision.symbol, "risk", "rejected",
                    verdict.reasoning,
                )
            return {
                "status": "rejected", "orders": [],
                "reason": verdict.reasoning,
            }

        if verdict.modifications:
            portfolio_decision.decisions = pipeline._apply_risk_modifications(
                portfolio_decision.decisions, verdict.modifications,
            )

        portfolio_decision.decisions, scale = _apply_scale_all_buys(
            portfolio_decision.decisions, verdict,
        )

        if verdict.modifications or scale < 1.0:
            portfolio_decision.decisions, _, blocked_reasons = (
                pipeline._filter_hard_risk_decisions(
                    portfolio_decision.decisions,
                    positions, total_value, daily_pnl,
                    baseline=last_equity,
                    macro_target_invested_pct=macro_target_pct,
                    correlation_matrix=correlation_matrix,
                    cash=ctx.deployable_cash,
                )
            )
            if blocked_reasons:
                reasons = "; ".join(dict.fromkeys(blocked_reasons))
                logger.warning("HARD RISK BLOCK AFTER MODIFICATIONS: %s", reasons)
                if not portfolio_decision.decisions:
                    pipeline._persist_hard_risk_block(ctx, reasons, stage="post_rm_modifications")
                    return {"status": "hard_risk_block", "orders": [], "reason": reasons}

        modified_symbols = {mod.symbol for mod in verdict.modifications}
        for decision in portfolio_decision.decisions:
            _record_pipeline_event(
                pipeline, ctx, decision.symbol, "risk",
                "modified" if decision.symbol in modified_symbols or scale < 1.0 else "approved",
                "risk_manager_verdict",
            )
            _record_pipeline_event(
                pipeline, ctx, decision.symbol, "deterministic_gate", "allowed",
                "post_risk_checks_passed",
            )
        return None


class ExecutionStage:
    """Record HOLDs → submit SELLs → wait → refresh → submit BUYs.

    Reads:  ctx.portfolio_decision.decisions, ctx.positions, ctx.cash,
            ctx.total_value, ctx.symbols_bars
    Writes: ctx.orders, and on SELL refresh: ctx.positions / .cash / .total_value
    """

    def __init__(self, *, pipeline: "TradingPipeline"):
        self._pipeline = pipeline

    def run(self, ctx: RunContext) -> list[dict]:
        pipeline = self._pipeline
        run_id = ctx.run_id
        # Stage 1 (QAMC correlation plumbing): links every trades row this
        # run produces back to the PM proposal / RM review that led to it.
        # None on any run that never reached a successful PM call (e.g. an
        # early-exit before DecisionStage) — trades rows from such a run
        # simply carry no decision_id, which is correct, not a bug.
        decision_id = ctx.decision_id
        positions = ctx.positions
        total_value = ctx.total_value
        cash = ctx.cash
        portfolio_decision = ctx.portfolio_decision

        orders: list[dict] = []
        sell_decisions = [d for d in portfolio_decision.decisions if d.action == "SELL"]
        buy_decisions = [d for d in portfolio_decision.decisions if d.action == "BUY"]
        hold_decisions = [d for d in portfolio_decision.decisions if d.action == "HOLD"]

        for d in hold_decisions:
            try:
                pipeline.db.insert_trade(
                    symbol=d.symbol, action="HOLD", qty=0.0, price=0.0,
                    reasoning=d.reasoning, run_id=run_id,
                    decision_id=decision_id,
                )
            except Exception as e:
                logger.warning("Failed to record HOLD decision for %s: %s", d.symbol, e)

        sell_order_ids: list[str] = []
        pending_protections: list[dict] = []
        for decision in sell_decisions:
            try:
                existing = [p for p in positions if p.symbol == decision.symbol]
                if not existing or existing[0].qty <= 0:
                    continue
                if decision.allocation_pct == 0:
                    logger.warning(
                        "Skipping SELL %s with allocation_pct=0 (ambiguous — use 100 for full exit)",
                        decision.symbol,
                    )
                    continue
                if 0 < decision.allocation_pct < 100:
                    sell_fraction = decision.allocation_pct / 100
                    qty = existing[0].qty * sell_fraction
                    if float(existing[0].qty).is_integer():
                        qty = max(1.0, float(int(qty)))
                    if qty <= 0:
                        continue
                    if qty >= existing[0].qty:
                        qty = pipeline._full_sell_qty(existing[0].qty)
                        if qty is None:
                            continue
                        action_label = "SELL"
                    else:
                        action_label = f"PARTIAL_SELL({decision.allocation_pct:.0f}%)"
                else:
                    qty = pipeline._full_sell_qty(existing[0].qty)
                    if qty is None:
                        continue
                    action_label = "SELL"
                sell_price = existing[0].current_price
                sell_limit = round(sell_price * 0.995, 2)
                position_qty = existing[0].qty
                # Single protected-sell discipline (cancel-WAL → submit →
                # accept → restore-on-failure) lives in one helper so this path
                # can't skip a step; defer reprotect/restore to the post-sell
                # wait below, which resolves the actual fill_qty.
                sale = pipeline._submit_protected_sell(
                    symbol=decision.symbol, qty=qty, limit_price=sell_limit,
                    reference_price=existing[0].current_price,
                    position_qty_before_sell=position_qty, label=action_label,
                )
                if sale is None:
                    continue
                order, prot = sale
                pending_protections.append(prot)
                orders.append(order)
                sell_order_ids.append(order["id"])
                pipeline.db.insert_trade(
                    symbol=decision.symbol, action=action_label, qty=qty,
                    price=sell_price, reasoning=decision.reasoning, run_id=run_id,
                    broker_order_id=order.get("id"),
                    fill_status="submitted",
                    decision_id=decision_id,
                )
                _record_pipeline_event(
                    pipeline, ctx, decision.symbol, "order", "submitted",
                    "broker_accepted", broker_order_id=order.get("id"), qty=qty,
                    limit_price=sell_limit, side="sell",
                )
                logger.info(
                    "Executed: %s %s %s @ limit $%.2f",
                    action_label.lower(), pipeline._format_qty(qty), decision.symbol, sell_limit,
                )
            except Exception as e:
                logger.error("Order failed for %s %s: %s", decision.action, decision.symbol, e)

        for order_id in sell_order_ids:
            # ExecutionStage was the lone SELL path missing this guard
            # — every other SELL path (force_delever / midday_emergency /
            # midday_llm / intra_check / take_profit) wraps the wait in
            # try/except. An uncaught exception here (broker 5xx, DNS
            # blip mid-poll) would propagate past the finalize loop
            # below. The audit F1 write-ahead row already covers a hard
            # process kill; this try/except additionally keeps the
            # in-process finalize path alive so coverage is rebuilt now
            # rather than waiting for the next session's drain.
            try:
                status = pipeline.broker.wait_for_order_terminal(order_id)
            except Exception as e:
                logger.warning(
                    "ExecutionStage: wait_for_order_terminal failed for %s: %s "
                    "— treating as unknown status so finalize still runs",
                    order_id, e,
                )
                status = None
            if status != "filled":
                logger.warning(
                    "Sell order %s did not fill before buy phase (status=%s); buys will use current cash only",
                    order_id, status or "unknown",
                )

        # Now that wait_for_order_terminal has returned for every sell,
        # the broker's fill_info is final. Reprotect on actual residual
        # (filled successfully) or restore originals (no-fill terminal).
        # wait=False: the sell_order_ids loop above already blocked until each
        # order reached terminal (it also gates the buy phase), so the orders
        # are terminal here — re-waiting would be a redundant no-op.
        pipeline._finalize_pending_protections(
            pending_protections, context="ExecutionStage", wait=False,
        )

        if sell_decisions:
            account, positions, price_map = pipeline._refresh_account_state()
            cash = account["cash"]
            total_value = account["portfolio_value"]
            ctx.positions = positions
            ctx.cash = cash
            ctx.deployable_cash = pipeline._compute_deployable_cash(cash, positions)
            ctx.total_value = total_value
            logger.info(
                "Post-sell refresh: $%.2f total, $%.2f cash, %d positions",
                total_value, cash, len(positions),
            )
        else:
            price_map = {p.symbol: p.current_price for p in positions}

        # Daily-loss re-check before BUYs. The initial circuit breaker ran
        # ~10 min ago (before LLM research); the tape may have gapped
        # through the limit while PM/RM was thinking, especially relevant
        # now that intra_check fires concurrently per #46. We block BUYs
        # (no new risk during a confirmed breach) but let any pending SELLs
        # stay — they reduced exposure already. intra's next tick handles
        # full emergency liquidation; morning's job here is just to not
        # add to the hole. Refresh first when sells didn't fire so the
        # check uses fresh portfolio_value, not the stale research-stage
        # snapshot.
        if buy_decisions:
            if not sell_decisions:
                # Take the FRESH price_map too (2026-07-16 audit): it was
                # discarded into `_`, leaving `price_map` at research-time
                # position prices from 5-10 minutes earlier. For an ADD to a
                # held name that stale price is what the 5% entry-staleness
                # guard compares the LLM's entry against, and what sizes the
                # order — so the guard could pass a genuinely stale entry (or
                # reject a good one) on exactly the fast-moving tape where it
                # matters. New symbols were unaffected (they miss the map and
                # fall through to a live quote).
                account, positions, fresh_prices = pipeline._refresh_account_state()
                cash = account["cash"]
                total_value = account["portfolio_value"]
                ctx.positions = positions
                ctx.cash = cash
                ctx.deployable_cash = pipeline._compute_deployable_cash(cash, positions)
                ctx.total_value = total_value
                price_map = {**price_map, **fresh_prices}
            daily_pnl_now = total_value - ctx.last_equity
            loss_violation_now = pipeline.risk_engine.check_daily_loss(
                ctx.last_equity, daily_pnl_now,
            )
            if loss_violation_now:
                logger.warning(
                    "ExecutionStage daily-loss re-check: %s — blocking "
                    "%d BUY(s); intra will liquidate on next tick",
                    loss_violation_now.message, len(buy_decisions),
                )
                for d in buy_decisions:
                    _record_execution_skip(
                        pipeline, ctx, d.symbol, "daily_loss_recheck",
                        loss_violation_now.message,
                    )
                buy_decisions = []

        # Run the cheap deterministic entry-viability checks BEFORE selling
        # SGOV. Production evidence showed the sweep funding names that were
        # guaranteed to die moments later on stale-entry / no-price / qty-zero
        # checks, creating avoidable sell/re-park churn. The full checks remain
        # in the submit loop below; this preflight only removes names whose
        # failure is already knowable and computes the actual whole-share
        # notional that funding should cover.
        fundable_notional: dict[str, float] = {}
        preflight_survivors = []
        for decision in buy_decisions:
            market_price = price_map.get(decision.symbol)
            if not isinstance(market_price, (int, float)) or market_price <= 0:
                live_price = pipeline.broker.get_latest_price(decision.symbol)
                if isinstance(live_price, (int, float)) and live_price > 0:
                    market_price = live_price
                    price_map[decision.symbol] = live_price
            if not isinstance(market_price, (int, float)) or market_price <= 0:
                bars = ctx.symbols_bars.get(decision.symbol) or []
                if bars:
                    last_close = float(bars[-1].close)
                    if last_close > 0:
                        market_price = last_close
                        price_map[decision.symbol] = last_close
            if not isinstance(market_price, (int, float)) or market_price <= 0:
                _record_execution_skip(
                    pipeline, ctx, decision.symbol, "no_price",
                    "no verifiable price reference (broker + bars unavailable)",
                )
                continue
            if decision.entry_price > 0:
                deviation = abs(decision.entry_price - market_price) / market_price
                if deviation > 0.05:
                    _record_execution_skip(
                        pipeline, ctx, decision.symbol, "stale_entry",
                        f"entry ${decision.entry_price:.2f} is "
                        f"{deviation * 100:.1f}% from market "
                        f"${market_price:.2f} (threshold 5%)",
                    )
                    continue
            preflight_price = max(market_price, decision.entry_price or 0)
            preflight_qty = int(
                (total_value * decision.allocation_pct / 100) / preflight_price
            )
            if preflight_qty <= 0:
                _record_execution_skip(
                    pipeline, ctx, decision.symbol, "qty_zero",
                    f"allocation {decision.allocation_pct:.2f}% at "
                    f"${preflight_price:.2f} rounds to zero shares",
                )
                continue
            fundable_notional[decision.symbol] = preflight_qty * preflight_price
            preflight_survivors.append(decision)
        buy_decisions = preflight_survivors

        # Cash-sweep funding: PM/RM/the hard gate size BUYs against
        # `deployable_cash` (raw cash + convertible sweep value), so on any
        # session with meaningful BUYs this sale IS load-bearing — the raw
        # cash on hand is typically just the reserve. `fund_buys` sells
        # enough of the vehicle to cover the planned notional, then waits
        # for the fill and CONFIRMS the observed rise in broker cash (a
        # filled sale credits `cash` immediately; the 2026-08-19 loss of a
        # fully-approved plan was a 51s fill outliving a 15s wait, not
        # settlement — see cash_sweep._FUND_TERMINAL_TIMEOUT_S). Whatever
        # it confirms, `available_cash` below governs: a BUY the sale
        # didn't actually fund is safely skipped.
        # isinstance guard: stage tests stub `pipeline` with MagicMock.
        if buy_decisions:
            from src.execution.cash_sweep import CashSweeper
            sweeper = getattr(pipeline, "_sweeper", None)
            sweeper = sweeper() if callable(sweeper) else None
            if not isinstance(sweeper, CashSweeper):
                sweeper = None
            if sweeper is not None:
                planned_notional = sum(
                    fundable_notional.get(d.symbol, 0.0) for d in buy_decisions
                )
                for d in buy_decisions:
                    _record_pipeline_event(
                        pipeline, ctx, d.symbol, "funding", "attempted",
                        "cash_sweep_funding", planned_notional=planned_notional,
                    )
                try:
                    freed = sweeper.fund_buys(ctx, planned_notional)
                except Exception as e:
                    logger.warning("cash sweep: fund_buys failed (BUYs will "
                                   "use raw cash only): %s", e)
                    freed = 0.0
                    for d in buy_decisions:
                        _record_pipeline_event(
                            pipeline, ctx, d.symbol, "funding", "failed",
                            "cash_sweep_exception", detail=str(e),
                        )
                if freed > 0:
                    positions = ctx.positions
                    cash = ctx.cash
                    total_value = ctx.total_value
                    for d in buy_decisions:
                        _record_pipeline_event(
                            pipeline, ctx, d.symbol, "funding", "funded",
                            "cash_sweep_confirmed", freed_cash=freed,
                        )
                elif buy_decisions:
                    for d in buy_decisions:
                        _record_pipeline_event(
                            pipeline, ctx, d.symbol, "funding", "no_additional_cash",
                            "cash_sweep_released_zero", raw_cash=cash,
                        )
            else:
                for d in buy_decisions:
                    _record_pipeline_event(
                        pipeline, ctx, d.symbol, "funding", "not_required",
                        "cash_sweep_disabled", raw_cash=cash,
                    )

        available_cash = cash
        pending_entry_stops: list[dict] = []
        for decision in buy_decisions:
            if decision.action != "BUY":
                continue
            try:
                market_price = price_map.get(decision.symbol)
                if not market_price or market_price <= 0:
                    live_price = pipeline.broker.get_latest_price(decision.symbol)
                    if live_price and live_price > 0:
                        market_price = live_price
                        price_map[decision.symbol] = live_price
                if not market_price or market_price <= 0:
                    bars = ctx.symbols_bars.get(decision.symbol) or []
                    if bars:
                        last_close = float(bars[-1].close)
                        if last_close > 0:
                            logger.info(
                                "Using last-bar close $%.2f as price reference for %s "
                                "(broker pricing unavailable)",
                                last_close, decision.symbol,
                            )
                            market_price = last_close

                limit_price = None
                sizing_price = None
                if decision.entry_price > 0:
                    limit_price = decision.entry_price

                if market_price and market_price > 0:
                    if limit_price is not None:
                        deviation = abs(limit_price - market_price) / market_price
                        if deviation > 0.05:
                            # Previously fell back to market order here — that
                            # silently absorbed up to 10% slippage against the
                            # LLM's stated entry. Now we skip: if entry_price
                            # is stale by >5%, the stop_loss computed against
                            # that entry is also stale, and the whole R/R math
                            # is bogus. Better to wait for next session.
                            logger.warning(
                                "BUY %s skipped: LLM entry_price $%.2f is %.1f%% "
                                "away from market $%.2f (threshold 5%%). Stop/R/R "
                                "computed against stale entry would be unsafe.",
                                decision.symbol, decision.entry_price,
                                deviation * 100, market_price,
                            )
                            _record_execution_skip(
                                pipeline, ctx, decision.symbol, "stale_entry",
                                f"entry ${decision.entry_price:.2f} is "
                                f"{deviation * 100:.1f}% from market "
                                f"${market_price:.2f} (threshold 5%)",
                            )
                            continue
                        elif limit_price < market_price:
                            logger.info(
                                "Adjusting limit price for %s: $%.2f → $%.2f (raised to market)",
                                decision.symbol, limit_price, market_price,
                            )
                            limit_price = market_price
                            sizing_price = market_price
                        else:
                            sizing_price = max(market_price, limit_price)
                    else:
                        sizing_price = market_price
                else:
                    logger.error(
                        "BUY %s skipped: no verifiable price reference "
                        "(broker + bars both unavailable). "
                        "LLM proposed entry $%.2f but cannot be validated.",
                        decision.symbol, decision.entry_price,
                    )
                    _record_execution_skip(
                        pipeline, ctx, decision.symbol, "no_price",
                        "no verifiable price reference (broker + bars "
                        "unavailable)",
                    )
                    continue

                # Liquid-equity execution policy: cross the displayed offer
                # with a limit (never a market order), padded by 5 bps for a
                # moving quote but hard-capped 25 bps above the verified
                # reference. A wider spread therefore remains price-protected
                # and may expire after the bounded entry window instead of
                # paying through an abnormal book. If quote data is degraded,
                # retain the validated last/PM limit and the same bounded wait.
                try:
                    quote = pipeline.broker.get_latest_quote(decision.symbol)
                except Exception as e:  # noqa: BLE001
                    logger.warning("BUY %s quote lookup failed: %s", decision.symbol, e)
                    quote = None
                ask = quote.get("ask_price") if isinstance(quote, dict) else None
                if isinstance(ask, (int, float)) and ask > 0:
                    offer_limit = min(ask * 1.0005, market_price * 1.0025)
                    offer_limit = round(offer_limit, 2 if offer_limit >= 1 else 4)
                    if limit_price is None or abs(limit_price - offer_limit) > 0.000001:
                        logger.info(
                            "BUY %s marketable-limit policy: prior $%s → $%.4f "
                            "(ask $%.4f, 25bp protection cap)",
                            decision.symbol,
                            f"{limit_price:.4f}" if limit_price is not None else "none",
                            offer_limit, ask,
                        )
                    limit_price = offer_limit
                    sizing_price = max(sizing_price or 0, offer_limit)

                # RC1: code-enforced ATR stop-distance floor at entry. The
                # P1 prompt rule ("fresh-entry stops never tighter than
                # 1×ATR") is advisory — LLM output still occasionally lands
                # stops inside one day's range, which converts routine
                # volatility into a same-week exit. Widen to 1×ATR(14) from
                # bars already fetched by research; qty_by_risk below sizes
                # against the wider distance, so per-trade $ risk is
                # unchanged. No bars → no floor (behavior identical).
                stop_price = decision.stop_loss
                if stop_price > 0 and sizing_price > stop_price:
                    try:
                        bars = ctx.symbols_bars.get(decision.symbol) or []
                        atr14 = None
                        if len(bars) >= 15:
                            from src.data.technical import compute_indicators
                            atr14 = compute_indicators(decision.symbol, bars).atr_14
                        if atr14 and atr14 > 0 and (sizing_price - stop_price) < atr14:
                            widened = round(sizing_price - atr14, 2)
                            logger.warning(
                                "BUY %s: stop $%.2f is %.2f×ATR from entry "
                                "$%.2f — widening to $%.2f (1×ATR14=$%.2f "
                                "floor; qty sizing compensates)",
                                decision.symbol, stop_price,
                                (sizing_price - stop_price) / atr14,
                                sizing_price, widened, atr14,
                            )
                            stop_price = widened
                    except Exception as e:
                        logger.warning("ATR stop floor skipped for %s: %s",
                                       decision.symbol, e)

                # R/R re-check whenever EXECUTION changed the geometry the RM
                # audited — either the stop was ATR-widened OR the limit was
                # raised to market (audit round 2: the raise-to-market path
                # GROWS the stop distance, dodging the ATR gate, yet shrinks
                # reward against the unchanged target — the one case the old
                # nested check could never see). If the honest geometry
                # collapses below a sane floor, the setup RM approved never
                # existed — skip rather than execute a trade nobody reviewed.
                geometry_changed = (
                    stop_price != decision.stop_loss
                    or (decision.entry_price > 0 and sizing_price > decision.entry_price)
                )
                if (geometry_changed and decision.take_profit > 0
                        and stop_price > 0 and sizing_price > stop_price):
                    reward = decision.take_profit - sizing_price
                    risk = sizing_price - stop_price
                    if risk > 0 and reward / risk < 1.2:
                        logger.warning(
                            "BUY %s skipped: executed geometry makes R/R %.2f "
                            "(<1.2) — RM approved entry $%.2f / stop $%.2f, "
                            "execution moved it to $%.2f / $%.2f.",
                            decision.symbol, reward / risk,
                            decision.entry_price, decision.stop_loss,
                            sizing_price, stop_price,
                        )
                        _record_execution_skip(
                            pipeline, ctx, decision.symbol, "geometry_rr",
                            f"executed geometry R/R {reward / risk:.2f} < 1.2 "
                            f"(RM approved ${decision.entry_price:.2f}/"
                            f"${decision.stop_loss:.2f}, execution moved to "
                            f"${sizing_price:.2f}/${stop_price:.2f})",
                        )
                        continue

                qty_by_alloc = int((total_value * decision.allocation_pct / 100) / sizing_price)
                qty_by_risk = None
                RISK_BUDGET_PCT = 0.5
                if stop_price > 0 and sizing_price > stop_price:
                    risk_per_share = sizing_price - stop_price
                    if risk_per_share > 0:
                        risk_dollars = total_value * RISK_BUDGET_PCT / 100
                        qty_by_risk = int(risk_dollars / risk_per_share)
                if qty_by_risk is not None and qty_by_risk < qty_by_alloc:
                    logger.info(
                        "Vol-adjusted sizing for %s: qty_by_alloc=%d → qty_by_risk=%d "
                        "(risk %.2f/share, budget $%.0f = %.1f%% of equity)",
                        decision.symbol, qty_by_alloc, qty_by_risk,
                        sizing_price - stop_price,
                        total_value * RISK_BUDGET_PCT / 100, RISK_BUDGET_PCT,
                    )
                    qty = qty_by_risk
                else:
                    qty = qty_by_alloc
                if qty <= 0:
                    logger.warning("Calculated qty=0 for %s, skipping", decision.symbol)
                    _record_execution_skip(
                        pipeline, ctx, decision.symbol, "qty_zero",
                        f"allocation {decision.allocation_pct:.2f}% at "
                        f"${sizing_price:.2f} rounds to zero shares",
                    )
                    continue

                estimated_cost = qty * sizing_price
                if estimated_cost > available_cash:
                    affordable_qty = int(available_cash / sizing_price)
                    if affordable_qty <= 0:
                        logger.warning(
                            "Skipping BUY %s: estimated cost $%.2f exceeds available cash $%.2f after sell phase",
                            decision.symbol, estimated_cost, available_cash,
                        )
                        _record_execution_skip(
                            pipeline, ctx, decision.symbol, "insufficient_cash",
                            f"estimated cost ${estimated_cost:.2f} exceeds "
                            f"available cash ${available_cash:.2f}",
                        )
                        continue
                    logger.warning(
                        "Resizing BUY %s from %d to %d share(s): confirmed cash "
                        "$%.2f only partially covers the approved order",
                        decision.symbol, qty, affordable_qty, available_cash,
                    )
                    qty = min(qty, affordable_qty)
                    estimated_cost = qty * sizing_price
                    _record_pipeline_event(
                        pipeline, ctx, decision.symbol, "funding", "resized",
                        "confirmed_cash_partially_funded_order",
                        approved_qty=qty_by_risk if qty_by_risk is not None and qty_by_risk < qty_by_alloc else qty_by_alloc,
                        resized_qty=qty,
                    )

                # Write-ahead intent: insert a pending row BEFORE calling
                # the broker. Closes the BUY-side phantom-fill window the
                # audit surfaced — pre-fix, submit_order could return
                # successfully and a SIGKILL before db.insert_trade left
                # the broker with an accepted order and the DB with no
                # row. _reconcile_fills queries by broker_order_id, so
                # there was no recovery path for the phantom. With the
                # pending row pre-inserted, even a crash mid-submit
                # leaves a fill_status='pending_submit' row the operator
                # (or a periodic cleanup) can reconcile against the
                # broker's order list.
                executed_price = limit_price if limit_price is not None else sizing_price
                pending_row_id = pipeline.db.insert_trade(
                    symbol=decision.symbol, action="BUY", qty=qty,
                    price=executed_price, reasoning=decision.reasoning, run_id=run_id,
                    stop_loss=stop_price, take_profit=decision.take_profit,
                    broker_order_id=None,
                    fill_status="pending_submit",
                    decision_id=decision_id,
                )

                try:
                    order = pipeline.broker.submit_order(
                        symbol=decision.symbol, qty=qty, side="buy",
                        limit_price=limit_price,
                        stop_loss_price=stop_price if stop_price > 0 else None,
                        reference_price=market_price,
                    )
                except Exception as e:
                    # Submit raised — broker may or may not have the
                    # order. Leave the row as 'pending_submit' so the
                    # next session's orphan sweep
                    # (_reconcile_orphan_pending_submits) can match it
                    # against broker activity by symbol + qty + time
                    # window. Audit 2026-05-27: a prior version called
                    # mark_trade_submit_failed here, but
                    # get_orphaned_pending_submits filters only
                    # fill_status='pending_submit' — flipping it to
                    # submit_failed silently HID the row from the
                    # recovery path it was supposed to be flagged for.
                    _record_pipeline_event(
                        pipeline, ctx, decision.symbol, "order", "submit_unknown",
                        "broker_submit_exception", detail=str(e),
                        trade_row_id=pending_row_id,
                    )
                    raise

                if not pipeline._order_accepted(order, decision.symbol, "buy"):
                    # Broker explicitly rejected (status != accepted/filled).
                    # Mark the pending row failed so it doesn't poison
                    # calibration as a "submitted" trade we never tracked.
                    # Distinct from the submit-raised case: here we KNOW
                    # the broker rejected, so there's no orphan to sweep.
                    pipeline.db.mark_trade_submit_failed(pending_row_id)
                    _record_pipeline_event(
                        pipeline, ctx, decision.symbol, "order", "rejected",
                        "broker_rejected", trade_row_id=pending_row_id, qty=qty,
                    )
                    _record_execution_skip(
                        pipeline, ctx, decision.symbol, "broker_rejected",
                        f"broker rejected buy {qty} @ "
                        f"{'limit $%.2f' % limit_price if limit_price else 'market'}",
                    )
                    continue

                # Submit accepted — finalize the pending row with the
                # broker's order_id and flip to 'submitted'.
                pipeline.db.confirm_trade_submitted(
                    pending_row_id, broker_order_id=order.get("id"),
                )
                _record_pipeline_event(
                    pipeline, ctx, decision.symbol, "order", "submitted",
                    "broker_accepted", broker_order_id=order.get("id"), qty=qty,
                    limit_price=executed_price,
                )
                if isinstance(order, dict):
                    order.setdefault("action", "BUY")  # audit F5
                orders.append(order)
                available_cash -= estimated_cost
                order_type = "limit" if limit_price is not None else "market"
                logger.info(
                    "Executed: buy %d %s @ %s $%.2f",
                    qty, decision.symbol, order_type, executed_price,
                )
                # The entry still owes a protective stop: it is placed as a
                # separate GTC order AFTER the fill, because an OTO leg would
                # inherit the parent's DAY tif and be expired by the broker at
                # 16:00 ET the same day (2026-07-16 audit — positions were
                # naked every night). Deferred until all BUYs are submitted so
                # the fill waits don't serialize the submission burst.
                if isinstance(order, dict) and order.get("pending_stop_price"):
                    pending_entry_stops.append({
                        "symbol": decision.symbol,
                        "order_id": order.get("id"),
                        "stop_price": order["pending_stop_price"],
                        "qty": qty,
                    })
            except Exception as e:
                logger.error("Order failed for %s %s: %s", decision.action, decision.symbol, e)

        # Protect every filled entry (GTC stop-limit keyed to the ACTUAL fill).
        for spec in pending_entry_stops:
            if not spec.get("order_id"):
                continue
            try:
                protection = pipeline.broker.place_entry_protection(
                    symbol=spec["symbol"], order_id=spec["order_id"],
                    stop_price=spec["stop_price"], requested_qty=spec["qty"],
                )
                _record_pipeline_event(
                    pipeline, ctx, spec["symbol"], "protection",
                    "placed" if protection else "not_placed",
                    "protective_stop_result",
                    entry_order_id=spec["order_id"], stop_price=spec["stop_price"],
                    protective_order_id=(protection or {}).get("id") if isinstance(protection, dict) else None,
                )
            except Exception as e:  # noqa: BLE001 — never abort the session here
                logger.error(
                    "entry protection raised for %s: %s — position may be "
                    "unprotected until the next coverage reconcile",
                    spec["symbol"], e,
                )
                _record_pipeline_event(
                    pipeline, ctx, spec["symbol"], "protection", "failed",
                    "protective_stop_exception", detail=str(e),
                    entry_order_id=spec["order_id"],
                )

        ctx.orders = orders
        return orders
