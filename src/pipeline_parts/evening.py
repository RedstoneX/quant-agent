import logging

from src.pipeline_stages import (
    record_stage,
)
from src.sessions.evening_session import EveningSession
from src.sessions.evening_stop_proximity_session import EveningStopProximitySession
from src.sessions.expected_sessions_session import ExpectedSessionsMissingSession
from src.trading_calendar import session_date_key

logger = logging.getLogger(__name__)


def _persist_evening_report(pipeline, result: dict) -> None:
    """Write the evening result dict verbatim, keyed by trading day.

    No field is defaulted or filled in: a value the run could not
    compute is stored absent/None so that a re-render says
    "not available" rather than showing a fabricated zero.
    """
    if not isinstance(result, dict):
        return
    try:
        pipeline.db.save_evening_report(
            date=session_date_key(),
            run_id=result.get("run_id"),
            payload=result,
        )
    except Exception as exc:  # noqa: BLE001 — never break the push
        logger.warning(
            "evening report persistence failed (non-fatal): %s", exc,
        )


def _run_evening_body(pipeline) -> dict:
    """Thin shim: builds the standalone session and runs it (body moved to src/sessions/evening_session.py)."""
    return EveningSession(
        activate_cost_session=pipeline._collab("_activate_cost_session"),
        actualize_trade_row=pipeline._collab("_actualize_trade_row"),
        build_active_state_changes=pipeline._collab("_build_active_state_changes"),
        build_missed_opportunities_digest=pipeline._collab("_build_missed_opportunities_digest"),
        build_portfolio_heat=pipeline._collab("_build_portfolio_heat"),
        build_recent_buys_for_grading=pipeline._collab("_build_recent_buys_for_grading"),
        build_recent_outlook_calibration=pipeline._collab("_build_recent_outlook_calibration"),
        build_recent_sells_for_grading=pipeline._collab("_build_recent_sells_for_grading"),
        build_thesis_health_context=pipeline._collab("_build_thesis_health_context"),
        build_weekly_narrative=pipeline._collab("_build_weekly_narrative"),
        drain_pending_protection_restores=pipeline._collab("_drain_pending_protection_restores"),
        drain_pending_repegs=pipeline._collab("_drain_pending_repegs"),
        evening_earnings_proximity=pipeline._collab("_evening_earnings_proximity"),
        evening_stop_proximity=pipeline._collab("_evening_stop_proximity"),
        expected_sessions_missing_today=pipeline._collab("_expected_sessions_missing_today"),
        is_trading_day=pipeline._collab("_is_trading_day"),
        load_earnings_analyses=pipeline._collab("_load_earnings_analyses"),
        maybe_run_quarterly_meta=pipeline._collab("_maybe_run_quarterly_meta"),
        news_held_symbols=pipeline._collab("_news_held_symbols"),
        paid_suspended_payload=pipeline._collab("_paid_suspended_payload"),
        persist_evening_replay_inputs=pipeline._collab("_persist_evening_replay_inputs"),
        reconcile_fills=pipeline._collab("_reconcile_fills"),
        reconcile_orphan_pending_submits=pipeline._collab("_reconcile_orphan_pending_submits"),
        reconcile_stop_coverage=pipeline._collab("_reconcile_stop_coverage"),
        reconcile_stop_out_fills=pipeline._collab("_reconcile_stop_out_fills"),
        require_paid_analysis=pipeline._collab("_require_paid_analysis"),
        run_news_update=pipeline._collab("_run_news_update"),
        surface_reconcile_outcomes=pipeline._collab("_surface_reconcile_outcomes"),
        sweeper=pipeline._collab("_sweeper"),
        sync_positions_from_broker=pipeline._collab("_sync_positions_from_broker"),
        total_pnl_since_reset=pipeline._collab("_total_pnl_since_reset"),
        broker=pipeline._collab("broker"),
        config=pipeline._collab("config"),
        db=pipeline._collab("db"),
        earnings_provider=pipeline._collab("earnings_provider"),
        evening_analyst=pipeline._collab("evening_analyst"),
        macro=pipeline._collab("macro"),
        news_store=pipeline._collab("news_store"),
    ).run()


def _evening_stop_proximity(pipeline, positions) -> list[dict]:
    """Thin shim: builds the standalone session and runs it (body moved to src/sessions/evening_stop_proximity_session.py)."""
    from src.execution.stop_read import read_stop
    return EveningStopProximitySession(
        stop_reader=read_stop, db=pipeline.db, atr_for_symbol=pipeline._collab("_atr_for_symbol"),
        sweep_symbol=pipeline._collab("_sweep_symbol"),
        broker=pipeline._collab("broker"),
    ).run(positions)


def _evening_earnings_proximity(pipeline, positions) -> list[dict]:
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
        symbols = pipeline._news_held_symbols(positions)
        if not symbols or getattr(pipeline, "market", None) is None:
            return []
        from src.data.event_calendar import fetch_earnings_proximity
        event_cfg = getattr(getattr(pipeline, "config", None), "event_risk", None)
        rows = fetch_earnings_proximity(
            pipeline.market, symbols,
            per_symbol_timeout_s=getattr(
                event_cfg, "earnings_symbol_timeout_s", 8.0,
            ),
            total_deadline_s=getattr(event_cfg, "earnings_deadline_s", 20.0),
        )
    except Exception as exc:  # noqa: BLE001
        record_stage(pipeline, "evening_earnings_proximity", exc)
        return []
    return [
        {
            "symbol": r.symbol,
            "sessions_away": r.sessions_away,
            "status": r.status,
        }
        for r in rows or []
    ]


def _expected_sessions_missing_today(pipeline) -> list[str]:
    """Thin shim: builds the standalone session and runs it (body moved to src/sessions/expected_sessions_session.py)."""
    return ExpectedSessionsMissingSession(
        db=pipeline._collab("db"),
    ).run()


def _maybe_run_quarterly_meta(pipeline) -> dict | None:
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
            is_last = pipeline.broker.is_last_trading_day_of_quarter(on_date=today)
        except Exception as e:
            record_stage(pipeline, "meta_quarter_end_check", e)
            return None
        if not is_last:
            return None
        logger.info(
            "Evening: today is last trading day of quarter %d-Q%d — "
            "running auto meta-reflection",
            today.year, (today.month - 1) // 3 + 1,
        )
        return pipeline.run_quarterly_meta_reflection(force=False)
    except Exception as e:
        logger.exception("Evening: meta-reflection piggyback failed: %s", e)
        return {"status": "auto_meta_error", "error": str(e)}


def run_evening(pipeline) -> dict:
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
    result = pipeline._run_evening_body()
    if isinstance(result, dict) and result.get("status") != "market_holiday":
        if (screen := pipeline.admission._run_universe_screen(
                result.get("run_id") or "evening")) is not None:
            result["universe_screen"] = screen
    pipeline._persist_evening_report(result)
    return result
