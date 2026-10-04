"""src.intraday.session -- the FREE intra-check session: the check itself, its body and its report.

Bodies moved verbatim from src/pipeline_intraday.py (`IntradayMixin`), which keeps
same-named thin shims built per call. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no pipeline behind it. Host
attributes a body both reads and ASSIGNS go through `state` (a get/set view the shim
hands in), never a construction-time copy.
"""

import logging

from src.cost_circuit import PaidAnalysisSuspended
from src.intraday_scan_outcome import failed_scan_result
from src.pipeline_context import RunContext
from src.trading_calendar import session_date_key

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class IntradaySession:
    """The FREE intra-check session: the check itself, its body and its report. Standalone, built from explicit collaborators."""

    def __init__(
        self, *,
        db=None,
        broker=None,
        is_trading_day=None,
        kill_switch_halt_result=None,
        run_intra_safety_preamble=None,
        attach_evidence_freshness=None,
        attach_pnl=None,
        activate_cost_session=None,
        compute_deployable_cash=None,
        record_account_snapshot=None,
        run_intraday_opportunity_scan=None,
        sync_positions_from_broker=None,
        total_pnl_since_reset=None,
        persist_intra_check_report=None,
        run_intra_check_body=None,
        state=None,
    ) -> None:
        self.db = db
        self.broker = broker
        self._is_trading_day = is_trading_day
        self._kill_switch_halt_result = kill_switch_halt_result
        self._run_intra_safety_preamble = run_intra_safety_preamble
        self._attach_evidence_freshness = attach_evidence_freshness
        self._attach_pnl = attach_pnl
        self._activate_cost_session = activate_cost_session
        self._compute_deployable_cash = compute_deployable_cash
        self._record_account_snapshot = record_account_snapshot
        self._run_intraday_opportunity_scan = run_intraday_opportunity_scan
        self._sync_positions_from_broker = sync_positions_from_broker
        self._total_pnl_since_reset = total_pnl_since_reset
        if persist_intra_check_report is not None:
            self._persist_intra_check_report = persist_intra_check_report  # else: this part's own body
        if run_intra_check_body is not None:
            self._run_intra_check_body = run_intra_check_body  # else: this part's own body
        self._state = state

    @property
    def _intra_preamble_deferred(self):
        return self._state.get("_intra_preamble_deferred")

    @_intra_preamble_deferred.setter
    def _intra_preamble_deferred(self, value) -> None:
        self._state.set("_intra_preamble_deferred", value)

    @property
    def _last_account_snapshot(self):
        return self._state.get("_last_account_snapshot")

    @_last_account_snapshot.setter
    def _last_account_snapshot(self, value) -> None:
        self._state.set("_last_account_snapshot", value)

    @property
    def _last_evidence_freshness(self):
        return self._state.get("_last_evidence_freshness")

    @_last_evidence_freshness.setter
    def _last_evidence_freshness(self, value) -> None:
        self._state.set("_last_evidence_freshness", value)

    def run_intra_check(self) -> dict:
        """Intra-session circuit-breaker check, plus the durable record of
        its own output.

        Same gap as `run_morning`/`run_position_review` (2026-09-18 sweep):
        `stop_coverage_gaps` is computed from live broker state every tick
        and handed to the notifier with no other durable home. This wrapper
        persists every return path, keyed by run_id (not date — this fires
        roughly every 30 minutes, so a date-keyed row would keep only the
        last tick; see `Database.save_intra_check_report`). Fail-soft.
        """
        self._last_evidence_freshness = None
        self._last_account_snapshot = None
        self._intra_preamble_deferred = ""
        result = self._run_intra_check_body()
        if isinstance(result, dict) and self._intra_preamble_deferred:
            result["preamble_deferred"] = self._intra_preamble_deferred
        self._attach_pnl(result)
        self._attach_evidence_freshness(result)
        self._persist_intra_check_report(result)
        return result

    def _persist_intra_check_report(self, result: dict) -> None:
        if not isinstance(result, dict):
            return
        run_id = result.get("run_id")
        if not run_id:
            return
        try:
            self.db.save_intra_check_report(
                run_id=run_id, date=session_date_key(), payload=result,
            )
        except Exception as exc:  # noqa: BLE001 — never break the push
            logger.warning(
                "intra_check report persistence failed (non-fatal): %s", exc,
            )

    def _run_intra_check_body(self) -> dict:
        """Lightweight intra-session maintenance tick (no LLM calls).

        Scheduled between morning and midday (typically 12:00 ET). It
        reconciles fills, repairs stop coverage on anything found
        unprotected, reports the session snapshot, and runs the bounded
        intraday opportunity scan.

        **It no longer carries an account-level loss breaker.** That whole
        mechanism — a daily P&L vs loss-limit test that halted the desk —
        was removed 2026-09-20 on the owner's instruction (retired item 32,
        docs/INCIDENT_HISTORY.md). Loss protection is the per-position stop
        living at the broker, which does not depend on this tick running.
        Runs in ~5 seconds.
        """
        ctx = RunContext.start("intra_check")
        run_id = ctx.run_id
        logger.info("=== Intra-session risk check: %s ===", run_id)

        if not self._is_trading_day():
            logger.info("Intra check skipped: market closed for non-trading day")
            return {"status": "market_holiday", "run_id": run_id}

        halt = self._kill_switch_halt_result(run_id)
        if halt is not None:
            return halt

        self._activate_cost_session(run_id, "intra_check")

        # Board item 127 (2026-09-19). Every write below reaches the broker,
        # and `intra_check` is exempt from the wrapper's session lock (item
        # 128), so this whole preamble used to run with no lock at all. It
        # now runs only while this process holds the same advisory flock the
        # paid scan below takes (`_intraday_scan_process_lock`) — which the
        # standalone coverage sweep's repair pass also takes — and only while
        # no morning/midday/close session owns the desk
        # (`_blocking_owner_session`, the check the paid scan already uses).
        # A live session runs this same preamble itself near the start of its
        # own run, and it may be in the middle of cancelling stops to sell; a
        # stop added here in that window is the worst pairing item 127 names.
        # Deferring skips only this tick's preamble, and the next tick
        # re-reads the broker. This used to add "the loss check below still
        # runs every tick" as the rest of the safety argument; there is no
        # loss check any more (2026-09-20, retired item 32), so the
        # argument for deferring now rests entirely on the next tick
        # re-reading. Board item 127 is open on that exposure.
        # Board item 177 (2026-10-01): the free safety work below now also
        # has its own entry point (`run_intra_safety`) and its own systemd
        # unit, so it no longer depends on this paid tick running. The paid
        # tick still calls it, unchanged, so nothing here got less reliable.
        coverage_gaps, preamble_deferred = self._run_intra_safety_preamble(run_id)
        self._intra_preamble_deferred = preamble_deferred

        try:
            account = self.broker.get_account()
            positions = self.broker.get_positions()
        except Exception as e:
            logger.error("Intra check: broker query failed: %s", e)
            return {"status": "broker_error", "run_id": run_id, "error": str(e),
                    "stop_coverage_gaps": coverage_gaps}

        total_value = account["portfolio_value"]
        last_equity = account.get("last_equity", total_value)
        daily_pnl = total_value - last_equity
        self._record_account_snapshot(total_value, last_equity)
        ctx.account = account
        ctx.positions = positions
        ctx.cash = account["cash"]
        ctx.deployable_cash = self._compute_deployable_cash(ctx.cash, ctx.positions)
        ctx.total_value = total_value
        ctx.last_equity = last_equity
        ctx.daily_pnl = daily_pnl
        self._sync_positions_from_broker(positions)
        daily_return_pct = (daily_pnl / last_equity * 100) if last_equity > 0 else 0
        total_pnl, total_return_pct, total_pnl_since = (
            self._total_pnl_since_reset(total_value)
        )
        logger.info(
            "Intra snapshot: equity=$%.2f, last_close=$%.2f, pnl=$%.2f (%.2f%%), positions=%d",
            total_value, last_equity, daily_pnl, daily_return_pct, len(positions),
        )

        result = {
            "status": "ok",
            "daily_pnl": daily_pnl,
            "daily_return_pct": daily_return_pct,
            "total_pnl": total_pnl,
            "total_return_pct": total_return_pct,
            "total_pnl_since": total_pnl_since,
            "positions": len(positions),
            "run_id": run_id,
            "stop_coverage_gaps": coverage_gaps,
        }
        # 2026-08-19 intraday opportunity-discovery fix: bounded new-
        # opportunity scan.
        try:
            scan_result = self._run_intraday_opportunity_scan(ctx)
        except PaidAnalysisSuspended as exc:
            scan_result = {
                "status": "paid_analysis_suspended",
                "run_id": run_id,
                "error": str(exc),
                "suspended": "intraday opportunity discovery only",
                "preserved": "fill reconciliation and stop-coverage repair",
            }
        except Exception as e:  # noqa: BLE001 — never let the scan
            # turn a routine intra_check tick into a failed run.
            # Operator-honesty fix: a crash used to set scan_result to
            # None, which is exactly what a healthy "ran, nothing to
            # do" tick also produces — no `intraday_scan` key, session
            # status stays "ok". The Telegram feed and the rehearsal
            # rig were both blind to the difference. Attaching a
            # dict (mirroring the `paid_analysis_suspended` shape
            # above) makes the crash visible through the same nested
            # path, while the tick itself still completes normally.
            scan_result = failed_scan_result(e, run_id)
        if scan_result is not None:
            result["intraday_scan"] = scan_result
            if scan_result.get("status") == "intraday_executed":
                # Scan went through ExecutionStage; refresh the
                # local table from broker truth (the start-of-tick
                # snapshot above is now stale).
                self._sync_positions_from_broker()
        return result
