"""The intra-check session and the intraday opportunity scan.

Step 8 of `docs/PIPELINE_SPLIT_PLAN.md` (board item 210), clusters R and W.
Moved verbatim out of `src/pipeline.py` as a mixin, so `TradingPipeline` keeps
every one of these as its own attribute and every test that patches, calls or
reads the source of them is untouched.

Two things live here and they are one thing: the FREE intra-check session
(its safety preamble, the paid-scan slot, the single-scan process lock and the
snapshot-health tracking) and the PAID intraday opportunity scan it gates
(mover candidates, the held-technical refresh, the ATR move context and the
scan body that turns all of that into orders through the execution stage).
`tests/test_invariants.py` already reads the source of four of these methods as
one unit, which is the clearest statement that they are a single seam.

`inspect.getsource` on these methods keeps working: it resolves through the
function object's own `__code__.co_filename`, which is now this file, not
through `TradingPipeline`'s defining module.

Names resolved HERE rather than in `src.pipeline` after the move (plan S5,
silent-behaviour risk 1): `compute_indicators`, `session_date_key`,
`agent_log_kwargs`, `seat_acceptance_kwargs`, `RunContext`,
`PaidAnalysisSuspended`, `_persist_evidence` and `_record_pipeline_event`.
A test that PATCHES one of them on `src.pipeline` no longer reaches this
module's code and must patch it here instead.

Nothing here may import `src.pipeline`: this module is one of its bases.
"""

import contextlib
import logging
from pathlib import Path

from src.agents.base import agent_log_kwargs, seat_acceptance_kwargs
from src.cost_circuit import PaidAnalysisSuspended
from src.intraday_scan_outcome import failed_scan_result
from src.data.technical import compute_indicators
from src.pipeline_context import RunContext
from src.sentinel.guarded_site import record_site as _site
from src.pipeline_stages import _persist_evidence, _record_pipeline_event
from src.trading_calendar import session_date_key

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class IntradayMixin:
    """Intra-check session (cluster R) and intraday opportunity scan (cluster W)."""

    def run_intra_safety(self) -> dict:
        """The FREE safety preamble, on its own schedule (board item 177).

        Fill reconcile, stop-out reconcile, protection-restore drain and
        repeg drain cost no model spend and protect live capital. Until
        2026-10-01 they existed ONLY as the opening block of
        ``_run_intra_check_body``, so they were welded to the *paid*
        intraday tick: cutting the paid cadence would silently have cut
        the loss-protection latency with it. That coupling was the defect
        item 177 names.

        The body is unchanged and lives in ``_run_intra_safety_preamble``.
        Both entry points call that one method, so this is strictly
        ADDITIVE: the paid tick still runs the preamble exactly as before,
        and the standalone ``intra_safety`` mode gives it a second,
        independent chance every tick. There is no new window in which
        protection is not restored — the only change is that one more
        caller can reach the same idempotent work.

        Both callers take the same advisory ``_intraday_scan_process_lock``
        and the same ``_blocking_owner_session`` check (board item 127), so
        two of them firing together cannot race: whichever acquires the
        lock does the work and the other defers, which is the behaviour the
        lock was built for.

        No LLM calls, and deliberately NO cost session — this path can
        never spend, and activating one would put empty rows into the very
        ``llm_budget_sessions`` measurement item 177 reads.
        """
        ctx = RunContext.start("intra_safety")
        run_id = ctx.run_id
        logger.info("=== Intra safety preamble (free): %s ===", run_id)

        if not self._is_trading_day():
            logger.info("Intra safety skipped: market closed for non-trading day")
            return {"status": "market_holiday", "run_id": run_id}

        halt = self._kill_switch_halt_result(run_id)
        if halt is not None:
            return halt

        coverage_gaps, preamble_deferred = self._run_intra_safety_preamble(run_id)
        self._intra_preamble_deferred = preamble_deferred
        return {
            "status": "deferred" if preamble_deferred else "ok",
            "run_id": run_id,
            "stop_coverage_gaps": coverage_gaps,
            "preamble_deferred": preamble_deferred,
        }

    def _run_intra_safety_preamble(self, run_id: str) -> tuple[list[dict], str]:
        """Run the free broker-truth safety work; return (gaps, deferred_reason).

        Called by BOTH ``_run_intra_check_body`` (the paid tick) and
        ``run_intra_safety`` (the standalone free tick). Idempotent and
        fail-soft throughout; an empty deferred reason means it ran.
        """
        coverage_gaps: list[dict] = []
        preamble_deferred = ""
        with self._intraday_scan_process_lock() as preamble_lock:
            if not preamble_lock:
                preamble_deferred = (
                    "another desk process holds the broker-write lock"
                )
            else:
                blocking = self._blocking_owner_session()
                if blocking == "unreadable":
                    preamble_deferred = (
                        "the active-session owner file could not be read "
                        "(fail closed)"
                    )
                elif blocking is not None:
                    preamble_deferred = (
                        f"a live {blocking} session owns the desk and runs "
                        "this same reconcile itself"
                    )
            if preamble_deferred:
                logger.warning(
                    "Intra check: broker-writing preamble DEFERRED this tick — "
                    "%s. No drain, repair, release or reconcile ran; the next "
                    "tick re-reads the broker.", preamble_deferred,
                )
            else:
                # Drain orphaned protection-restore intents — intra runs every
                # 30 min so this is the most frequent recovery opportunity for
                # bails that landed during morning. Codex r8 #2.
                drained = self._drain_pending_protection_restores()
                self._drain_pending_repegs()
                # Broker-truth coverage audit + auto-repair every tick (audit round
                # 2): an entry that fills after place_entry_protection's wait, or a
                # repair that failed once, otherwise stayed naked until the NEXT
                # session — hours. On the intra cadence the naked window is ≤30 min.
                # Read-only when coverage is fine; ~1 broker call per held long.
                # Spec §11.1 guard 3: the return value used to be DISCARDED here, so
                # the 30-minute sweep — the tightest cadence this audit runs on, and
                # the one the fractional decision leans on — was the one caller whose
                # findings never reached the operator's feed at all. Carried into the
                # result dict now, exactly as every other session already does.
                try:
                    coverage_gaps = self._reconcile_stop_coverage()
                except Exception as exc:  # noqa: BLE001
                    _site(self, "coverage_reconcile", exc, log=logger)
                else:
                    _site(self, "coverage_reconcile", log=logger)
                # Sweep retired (owner mandate 2026-09-17): release any held vehicle.
                self._release_retired_cash_park(run_id)
                self._reconcile_orphan_pending_submits()  # audit F4
                # 2026-09-17 AMD incident: AMD filled at $549.11 but the trades
                # table still read 'submitted' half an hour later. The stop-coverage
                # and stop-out reconcilers below only watch protective/broker-
                # initiated exits — neither one asks the broker about the fate of an
                # order THIS pipeline submitted (a BUY/SELL/REDUCE/etc still marked
                # 'submitted' in the trades table). `run_morning` and the midday/
                # close review both call `_reconcile_fills` for exactly that reason;
                # this tick — the one that runs every ~30 minutes and is therefore
                # the tightest window available to close that gap between sessions
                # — never did. The live fill-notification websocket never
                # authenticates on this host (placeholder credential, frozen pending
                # an owner decision — see broker.py), so in production this always
                # resolves through `_reconcile_fills`'s own bounded REST lookup
                # (`broker.get_order_fill_info`), never the socket. Unscoped
                # (no run_id) so a still-'submitted' row from ANY earlier session
                # today is picked up, not just ones this tick itself created.
                #
                # Item 173(2): this runs BEFORE the stop-out reconcile below,
                # not after. A SELL this pipeline submitted but hasn't yet
                # reconciled leaves the ledger believing the position is still
                # open (get_symbols_with_open_ledger_qty ignores 'submitted'
                # rows) while the broker has already reduced it — a positive
                # gap. The stop-out reconciler can't explain that gap either,
                # because the submitted SELL's broker_order_id is already in
                # get_known_broker_order_ids, so its fill is filtered out of
                # new_fills — and it pages a false CRITICAL "records disagree
                # with broker". Reconciling fills first flips that SELL to
                # executed, the gap closes, and the stop-out check stays quiet.
                try:
                    self._reconcile_fills()
                except Exception as exc:  # noqa: BLE001
                    _site(self, "fill_reconcile", exc, log=logger)
                else:
                    _site(self, "fill_reconcile", log=logger)
                # Broker-truth EXIT audit (2026-08-28 ONDS/CCJ). intra_check fires
                # every ~30 min, so this is the tightest window this reconciler
                # runs on — a stop that fires mid-session is written back within
                # one tick instead of sitting unrecorded until the next scheduled
                # session hours later.
                reco = None
                try:
                    reco = self._reconcile_stop_out_fills(run_id)
                except Exception as exc:  # noqa: BLE001
                    _site(self, "stop_out_reconcile", exc, log=logger)
                else:
                    _site(self, "stop_out_reconcile", log=logger)
                # Item 101: surface a broker-made stop-out / re-protection to
                # owner — intra is the tightest cadence, so this is where a
                # mid-session stop-out reaches him fastest.
                self._surface_reconcile_outcomes(reco, drained, run_id=run_id)

        return coverage_gaps, preamble_deferred

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
            _site(self, "report_persist", exc)
        else:
            _site(self, "report_persist")

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
            _site(self, "broker_query", e, log=logger)
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
            _site(self, "intraday_scan", e, log=logger)
            scan_result = failed_scan_result(e, run_id)
        if scan_result is not None:
            result["intraday_scan"] = scan_result
            if scan_result.get("status") == "intraday_executed":
                # Scan went through ExecutionStage; refresh the
                # local table from broker truth (the start-of-tick
                # snapshot above is now stale).
                self._sync_positions_from_broker()
        return result

    def _recently_intraday_evaluated(self, symbol: str, cooldown_hours: float) -> bool:
        """True when the explicit evaluation ledger says this symbol ran.

        Trades are not an evaluation ledger: PM parse failures, RM rejects,
        no-target decisions, and pre-execution errors create no trade row and
        previously bypassed cooldown, repeatedly buying the same analysis.
        """
        try:
            rows = self.db.get_recent_intraday_evaluations(
                symbol, cooldown_hours=cooldown_hours,
            )
        except Exception as e:  # noqa: BLE001
            _site(self, "cooldown_ledger", e, context={"symbol": symbol}, log=logger)
            return True
        if isinstance(rows, list):
            return bool(rows)

        # Compatibility for lightweight test doubles and rolling upgrades in
        # which an older DB facade has not exposed the new ledger method yet.
        # Production Database always returns a real list above.
        try:
            legacy_rows = self.db.get_trades(symbol=symbol, limit=10)
        except Exception as exc:  # noqa: BLE001
            _site(self, "cooldown_legacy_trades", exc, context={"symbol": symbol})
            return True
        from datetime import datetime as _dt, timedelta, timezone
        cutoff = _dt.now(timezone.utc) - timedelta(hours=cooldown_hours)
        for row in legacy_rows if isinstance(legacy_rows, list) else []:
            if not str(row.get("run_id") or "").startswith("intra_check-"):
                continue
            try:
                ts = str(row.get("timestamp") or "")
                when = (_dt.fromisoformat(ts.replace("Z", "+00:00")) if "T" in ts
                        else _dt.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if when >= cutoff:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _blocking_owner_session(self) -> str | None:
        """Live wrapper owner mode that must not overlap paid discovery.

        Returns the other session's mode when it is alive, ``"unreadable"``
        when the owner file exists but cannot be trusted (fail closed), or
        None when paid discovery may run. ``intra_check`` never blocks
        itself. A dead pid or a vanished file is None — morning that
        already finished must not sleep the 09:30 scan until 10:00.
        """
        import os
        import time as _time

        owner_path = Path.home() / ".cache" / "quant-agent" / "active-session.lock" / "owner"
        if not owner_path.exists():
            return None
        try:
            parts = owner_path.read_text().strip().split()
            owner_mode = parts[0]
            owner_ts = int(parts[2])
            owner_pid = int(parts[3])
            age = _time.time() - owner_ts
            alive = True
            try:
                os.kill(owner_pid, 0)
            except OSError:
                alive = False
            if owner_mode == "intra_check":
                return None
            if alive and 0 <= age <= 1800:
                return owner_mode
            return None
        except (OSError, ValueError, IndexError):
            return "unreadable"

    def _intra_window_remaining_s(self) -> float:
        """Seconds left in the intra_check ET window. Calendar-bound, not invented."""
        from src.trading_calendar import SESSION_WINDOWS, _minute_of_day, et_now
        _start, end = SESSION_WINDOWS["intra_check"]
        now = et_now()
        remaining_min = end - _minute_of_day(now)
        return max(0.0, remaining_min * 60.0)

    def _await_paid_scan_slot(self, run_id: str) -> bool:
        """Wait for morning/midday/close to finish rather than skip the tick.

        Returns True when paid discovery must still be skipped (lock still
        held at window end, morning was the session waited on — see below —
        or the owner file unreadable). Returns False when the slot is free.

        Morning shares the 09:30 ``SESSION_WINDOWS`` start with this
        ``intra_check`` fire, so waiting for morning then running paid
        discovery on the SAME tick is still the 09:30 open, not a real
        INTRADAY look (item 121 — measured leftover at 09:37). Sets
        ``self._paid_scan_waited_for = "morning"`` in that case so the
        caller skips this tick instead of scanning; the first true paid
        INTRADAY look is the next existing half-hour fire, which sees
        morning's lock already released and runs immediately with no
        invented offset. Midday/close are a different cadence than this
        fire, so waiting for either and then scanning on release is still
        correct.
        """
        import time as _time

        first = True
        self._paid_scan_waited = False
        self._paid_scan_waited_for = None
        last_blocking = None
        while True:
            blocking = self._blocking_owner_session()
            if blocking is None:
                if not first:
                    if last_blocking == "morning":
                        self._paid_scan_waited_for = "morning"
                        logger.info(
                            "Intraday scan: morning released the owner lock; "
                            "this fire shares the 09:30 open with morning, "
                            "so paid discovery stays skipped this tick — "
                            "the next existing half-hour fire is the first "
                            "true INTRADAY look",
                        )
                        return True
                    self._paid_scan_waited = True
                    logger.info(
                        "Intraday scan: other session released the owner lock; "
                        "running paid discovery on this tick instead of "
                        "waiting for the next 30-minute fire",
                    )
                return False
            last_blocking = blocking
            if blocking == "unreadable":
                logger.warning(
                    "Intraday scan: could not validate active-session owner — "
                    "skipping paid discovery fail-closed",
                )
                return True
            remaining = self._intra_window_remaining_s()
            if remaining <= 0:
                logger.info(
                    "Intraday scan: %s still holds the owner lock at window "
                    "end; paid discovery cannot run this tick", blocking,
                )
                return True
            if first:
                logger.info(
                    "Intraday scan: wrapper reports active %s session; waiting "
                    "for it to finish instead of skipping this tick", blocking,
                )
                first = False
            _time.sleep(min(1.0, remaining))

    def _another_session_recently_active(self, run_id: str,
                                         within_minutes: float = 15.0) -> bool:
        """True when a DIFFERENT session currently owns the trading process.

        The 15-minute trade-row heuristic slept the 09:30 and 13:00 scans
        after morning/midday had already written fills — the owner lock is
        the in-flight signal. `within_minutes` is kept for callers but no
        longer gates a finished session.
        """
        blocking = self._blocking_owner_session()
        if blocking == "unreadable":
            return True
        return blocking is not None

    @contextlib.contextmanager
    def _intraday_scan_process_lock(self):
        """Non-blocking process-level mutex for the intraday scan.

        Yields True when this process holds the lock, False otherwise.

        Why (independent review finding, 2026-08-19): the owner-lock
        `_another_session_recently_active` guard sees a concurrent
        morning/midday/close only while that wrapper still owns the
        process. Two `intra_check` processes launched at nearly the same
        instant would both pass it — and could then size BUYs against the
        same pre-fill snapshot, breaching `max_position_pct`.

        In practice `scripts/run_if_et_window.sh` makes that impossible:
        ticks are 1800s apart and the wrapper hard-kills a run at
        `timeout --kill-after=30 1200` (~1230s), so a tick is always dead
        before the next fires. But that guarantee lives in a deployment
        config this code cannot read (the production systemd units are not
        in-repo), and it would silently disappear if the interval were ever
        shortened. A trading safety property should not depend on an
        unverifiable assumption, so this closes the class outright.

        Deliberately NOT a new service/daemon/timer — a plain advisory
        `flock` on a local file, the same idea as the wrapper's existing
        `mkdir`-based session lock. Since 2026-09-19 (board item 127) it
        also guards `intra_check`'s broker-writing preamble, and the
        standalone coverage sweep's repair pass takes the same file
        (`src.coverage_watchdog.repair_lock`). Loss protection keeps its
        exemption and never touches this. The lock is released on process exit even if we
        are SIGKILLed, so a killed run cannot wedge it.
        """
        import fcntl

        fh = None
        acquired = False
        try:
            lock_path = Path(self.config.storage.db_path).parent / ".intraday_scan.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(lock_path, "w")
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                logger.info(
                    "Intraday scan: another process already holds the scan "
                    "lock — skipping this tick (no concurrent position sizing)",
                )
        except Exception as e:  # noqa: BLE001 — unknowable lock state must not scan
            _site(self, "scan_lock", e, log=logger)
        try:
            # Keep the yield outside the acquisition exception handler.  An
            # exception raised by the protected scan body is injected here by
            # contextlib and must propagate to run_intra_check (not be mistaken
            # for a lock failure and replaced by "generator didn't stop after
            # throw()").
            yield acquired
        finally:
            if fh is not None:
                try:
                    fh.close()   # releases the flock
                except Exception as exc:  # noqa: BLE001
                    _site(self, "scan_lock_release", exc)

    def _track_intraday_snapshot_ok(self, symbol: str) -> None:
        """Reset a symbol's consecutive-miss streak. Never raises — a
        monitoring bug must not be able to break the scan it watches."""
        try:
            self.db.record_intraday_symbol_snapshot_result(symbol, ok=True)
        except Exception:
            logger.warning(
                "intraday snapshot health: failed to record OK for %s", symbol,
                exc_info=True,
            )

    def _track_intraday_snapshot_miss(self, symbol: str) -> None:
        """Record a missed snapshot for `symbol` and alert the owner once
        it has failed 3 consecutive ticks (~90 min) — see
        `Database.record_intraday_symbol_snapshot_result`'s docstring for
        the threshold/cooldown reasoning. Never raises."""
        try:
            result = self.db.record_intraday_symbol_snapshot_result(symbol, ok=False)
        except Exception:
            logger.warning(
                "intraday snapshot health: failed to record miss for %s", symbol,
                exc_info=True,
            )
            return
        if not result.get("should_alert"):
            return
        try:
            from src import notifier as _notifier

            misses = result.get("consecutive_misses", 0)
            _notifier.send_owner_alert(
                "INTRADAY SNAPSHOT UNAVAILABLE\n"
                f"{symbol} has failed to return snapshot data for "
                f"{misses} consecutive scans (~{misses * 30} min). It is being "
                "silently excluded from intraday move detection until this "
                "resolves — check whether the ticker is still valid/tradable "
                "on Alpaca. Will not re-alert on this symbol for 24h.", category=_notifier.CATEGORY_OPERATIONAL,
            )
        except Exception:
            logger.warning(
                "intraday snapshot health: alert failed for %s", symbol,
                exc_info=True,
            )

    def _run_intraday_opportunity_scan(self, ctx: RunContext) -> dict:
        """Concurrency-guarded wrapper around the scan body.

        2026-08-31 visibility fix: every path through this wrapper and the
        body it delegates to now returns an explicit result dict — never a
        bare None — so run_intra_check's `intraday_scan` key distinguishes
        the three everyday reasons a tick adds no new activity from EACH
        OTHER and from a crash. PR #163 (2026-08-30) made a crashed scan
        visible as "intraday_scan_crashed" but left these three still
        collapsed onto the identical absent-key shape:

          - "intraday_scan_disabled": the feature is off in config.
          - "intraday_scan_lock_contended": another scan already owns this
            window — either this process's own advisory flock (see
            `_intraday_scan_process_lock`) or a morning/midday/close
            wrapper that still holds the owner lock at the end of this
            tick's wait (`_await_paid_scan_slot`).
          - "intraday_scan_open_overlap": morning released the owner lock
            on this same 09:30-shared tick — still the open, not a real
            INTRADAY look (item 121; see `_intraday_open_overlap_skip`).
          - "intraday_scan_no_opportunity": the scan ran and found nothing
            worth escalating (see `_intraday_opportunity_scan_body`'s
            early-return points).

        All three are HEALTHY completions — see ops/rehearsal/report.py's
        STATUS_PLAIN entries and `_verdict`'s healthy set, which is where
        "intraday_scan_crashed" is deliberately NOT included.
        """
        cfg = getattr(self.config, "intraday_scan", None)
        if cfg is None or not getattr(cfg, "enabled", False):
            return {"status": "intraday_scan_disabled", "run_id": ctx.run_id}
        with self._intraday_scan_process_lock() as acquired:
            if not acquired:
                return {"status": "intraday_scan_lock_contended", "run_id": ctx.run_id}
            return self._intraday_opportunity_scan_body(ctx)

    def _intraday_held_tech_symbols(self, ctx: RunContext) -> list[str]:
        """Investable holdings that need current-run Technical on this scan.

        Morning's Tech pre-filter already includes every held name
        (`_has_actionable_signal_fn`). The intraday scan used to send only
        names that moved past the threshold, so a quiet hold the PM can
        still increase had no current-run Technical: grounding failed the
        whole paid decision (`pm_grounding_error`, "increase lacks a
        current-run Technical analysis"). Missing specialist data is a
        defect in the producing step — this list is that step. Cash-park
        vehicles have no thesis and stay out.
        """
        parked: set[str] = set()
        sweeper = self._sweeper()
        investable = list(ctx.positions or [])
        if sweeper is not None:
            investable, _parked = sweeper.split_positions(investable)
        retired = self._retired_cash_park_symbol()
        if isinstance(retired, str) and retired.strip():
            parked.add(retired.strip().upper())
        seen: set[str] = set()
        out: list[str] = []
        for pos in investable:
            if not getattr(pos, "qty", 0):
                continue
            symbol = str(getattr(pos, "symbol", "") or "").strip().upper()
            if not symbol or symbol in seen or symbol in parked:
                continue
            seen.add(symbol)
            out.append(symbol)
        return out

    def _intraday_scan_mover_candidates(
        self, ctx: RunContext,
    ) -> tuple[list[tuple[str, float]], dict]:
        """Cheap snapshot of who moved. No paid calls.

        Runs before the owner-lock wait so a contended morning/midday
        cannot vanish the mover list. A skip after wait names these
        symbols in a durable reason instead of dropping them silently.
        """
        from src.data.live_price import (
            NO_PRICE_AT_ALL, NO_SNAPSHOT, resolve_live_price,
        )

        cfg = self.config.intraday_scan
        universe = list(self.config.trading.universe)
        snapshots = self.broker.get_intraday_snapshots(universe) or {}
        if not snapshots:
            return [], {}
        candidates: list[tuple[str, float]] = []
        for symbol in universe:
            snap = snapshots.get(symbol) or {}
            # item 120: the move that buys a PAID look has to be today's.
            # This read `last_price` straight, so a name still carrying a
            # prior session's print measured a move that did not happen
            # today. Same resolver as the morning Tech pass, so "today" is
            # decided in one place.
            resolved = resolve_live_price(snap)
            last = resolved.price
            prev = snap.get("prev_close")
            # The miss counter pages the owner after three consecutive
            # scans with "check whether the ticker is still valid/tradable
            # on Alpaca". It exists to tell a BROKEN ticker from a quiet
            # one (`src/storage/db.py`), so a thin name that simply has not
            # printed today must NOT feed it — item 120's own filing names
            # two IEX-thin names in exactly that state, and paging on them
            # would be a false alarm. A symbol the feed returned nothing
            # for at all is still a miss.
            fed_nothing = resolved.unavailable in (NO_SNAPSHOT, NO_PRICE_AT_ALL)
            if not isinstance(prev, (int, float)) or (
                last is None and fed_nothing
            ):
                self._track_intraday_snapshot_miss(symbol)
                continue
            self._track_intraday_snapshot_ok(symbol)
            if last is None:
                # Quiet, not broken: the feed answered, the name has no
                # today print. It cannot have moved today, so it buys no
                # paid look — and it does not page anybody either.
                continue
            if prev <= 0:
                continue
            move_pct = abs(last - prev) / prev * 100.0
            if move_pct < cfg.move_threshold_pct:
                continue
            if self._recently_intraday_evaluated(symbol, cfg.cooldown_hours):
                continue
            candidates.append((symbol, move_pct))
        candidates.sort(key=lambda t: -t[1])
        return candidates, snapshots

    @staticmethod
    def _intraday_move_in_atr(
        move_pct: float, atr_14: float | None, prev_close: float | None,
    ) -> tuple[float | None, float | None]:
        """The trigger's move expressed in the NAME'S OWN daily range.

        Board item 177, the trigger third. `move_threshold_pct` is a flat
        3% applied to every symbol alike, and the number ledger's open
        question against it asks what move size *relative to the name's own
        ATR* marks a development worth re-reading. That question cannot be
        answered from the desk's record, because the record never held the
        denominator: `intraday_evaluations.detail` stored `move_pct=` and
        nothing else, so 253 recorded selections (2026-09-02 -> 2026-09-25)
        say how far a name moved and never how far that name normally
        moves. Measured on those 253 rows, the flat threshold does not
        discriminate at all — the median move of a selection that produced
        a BUY/SHORT is 3.50% against 3.67% for one that produced nothing,
        and the 5-7% band produced zero orders from 51 selections — so
        re-picking the flat number in either direction has no basis, and
        the ATR-relative form has no data yet. This records the
        denominator, on bars the scan already paid to fetch, changing no
        behaviour: the threshold, the cap and the cooldown all still
        decide exactly what they decided before.

        Returns (atr_pct_of_prev_close, move_in_atr_multiples), either of
        which is None when the inputs cannot support it.
        """
        if not isinstance(atr_14, (int, float)) or atr_14 <= 0:
            return None, None
        if not isinstance(prev_close, (int, float)) or prev_close <= 0:
            return None, None
        atr_pct = float(atr_14) / float(prev_close) * 100.0
        if atr_pct <= 0:
            return None, None
        return atr_pct, float(move_pct) / atr_pct

    def _record_intraday_trigger_atr_context(
        self, ctx: RunContext, symbol: str, mover_symbols: set[str],
        move_by_symbol: dict, snapshots: dict, indicators,
    ) -> None:
        """Stamp the ATR denominator onto a mover's existing ledger row.

        Upsert on (symbol, run_id), so this updates the row
        `record_intraday_evaluation` already wrote at selection time rather
        than adding one: no new row, no change to the cooldown the row
        enforces, no extra market or model call. Best-effort — a
        measurement must never cost the scan that carries it.
        """
        upper = symbol.upper()
        if upper not in mover_symbols:
            return
        move_pct = move_by_symbol.get(symbol, move_by_symbol.get(upper))
        if not isinstance(move_pct, (int, float)):
            return
        snap = snapshots.get(symbol) or snapshots.get(upper) or {}
        atr_pct, move_atr = self._intraday_move_in_atr(
            float(move_pct), getattr(indicators, "atr_14", None),
            snap.get("prev_close"),
        )
        detail = f"move_pct={float(move_pct):.4f}"
        if atr_pct is None or move_atr is None:
            detail += ";atr_pct=unreadable;move_atr=unreadable"
        else:
            detail += f";atr_pct={atr_pct:.4f};move_atr={move_atr:.4f}"
        try:
            self.db.record_intraday_evaluation(
                symbol=upper, run_id=ctx.run_id, status="selected",
                detail=detail,
            )
        except Exception as exc:  # noqa: BLE001 — measurement, never the scan
            _site(self, "trigger_atr_context", exc, context={"symbol": upper})

    def _intraday_paid_scan_skip(self, ctx: RunContext, movers: list[str]) -> dict:
        """Durable skip: lock still held, movers named, no silent drop."""
        blocking = self._blocking_owner_session() or "owner_lock"
        named = ",".join(movers) if movers else "none"
        reason = (
            f"paid discovery skipped: {blocking} still held; movers={named}"
        )
        logger.warning("Intraday scan: %s", reason)
        for symbol in movers:
            try:
                _record_pipeline_event(
                    self, ctx, symbol, "opportunity", "skipped",
                    "intraday_scan_lock_contended", detail=reason,
                )
            except Exception as exc:  # noqa: BLE001
                _site(self, "skip_reason_lock_contended", exc, context={"symbol": symbol})
        return {
            "status": "intraday_scan_lock_contended",
            "run_id": ctx.run_id,
            "reason": reason,
            "movers": list(movers),
        }

    def _intraday_open_overlap_skip(self, ctx: RunContext, movers: list[str]) -> dict:
        """Morning released the lock on this same 09:30-shared tick.

        Not a lock contention (morning is no longer holding it) and not a
        real INTRADAY opportunity — running paid discovery here would be
        the measured 09:37 leftover (item 121): the SAME open, sold to the
        owner a second time under a different label. Skip; the next
        existing half-hour fire, which sees no lock at all, runs normally.
        """
        named = ",".join(movers) if movers else "none"
        reason = (
            "paid discovery skipped: this fire shares the 09:30 open with "
            f"morning; movers={named}"
        )
        logger.info("Intraday scan: %s", reason)
        for symbol in movers:
            try:
                _record_pipeline_event(
                    self, ctx, symbol, "opportunity", "skipped",
                    "intraday_scan_open_overlap", detail=reason,
                )
            except Exception as exc:  # noqa: BLE001
                _site(self, "skip_reason_open_overlap", exc, context={"symbol": symbol})
        return {
            "status": "intraday_scan_open_overlap",
            "run_id": ctx.run_id,
            "reason": reason,
            "movers": list(movers),
        }

    def _intraday_opportunity_scan_body(self, ctx: RunContext) -> dict:
        """Bounded intraday opportunity discovery (2026-08-19 fix).

        Runs on the existing intra_check cadence — no new systemd timer,
        no full morning research stack. One cheap bulk current-session
        snapshot call flags symbols that moved materially since the last
        close; those movers (capped, cooldown-deduped against repeat churn)
        PLUS currently held investable names get real daily bars/indicators
        and a real tech_analyst call. Held names join the batch so an
        increase on a quiet hold has current-run Technical and can ground;
        they do not consume the mover cap or the mover cooldown. Then the
        SAME DecisionStage -> RiskStage -> ExecutionStage chain morning
        uses — no separate/duplicated decision logic, so PM's sizing rules,
        RM's veto authority and the deterministic gate all apply exactly
        as they do in the morning run. Bullish AND bearish setups both
        surface: the universe already includes the approved inverse ETFs
        (SH/SDS/PSQ/SQQQ), so a broad-market decline shows up as a
        qualifying move in those symbols the same way a rally shows up in
        a long candidate — no separate bearish code path needed.

        Returns a status dict at every early-exit point — never a bare
        None (2026-08-31 visibility fix; see `_run_intraday_opportunity_scan`
        for the full rationale). "intraday_scan_open_overlap" when morning
        released the owner lock on this same 09:30-shared tick (item 121 —
        still the open, not INTRADAY); "intraday_scan_lock_contended" when
        `_await_paid_scan_slot` cannot free the owner lock before this
        tick's calendar window ends; "intraday_scan_no_opportunity" for
        every other early return (no
        snapshots, no qualifying moves, no ledgerable symbols, no usable
        bars). A tech seat that is fully LOST (every submitted symbol
        failed, or the batch call raised) is NOT folded into that status —
        item 20 (board) — it is classified `data_status["tech"]="failed"`
        and routed through the same evidence-gate skip + unsuppressible
        alert morning uses. Past that point, a real result dict
        mirroring the shape callers of run_morning already expect
        (status/orders/run_id). Best-effort: any failure degrades to a
        status dict, never raises (the caller also wraps this
        defensively) — a scan miss costs a possible trade; a scan crash
        must never cost the loss-protection check that already ran this
        tick.

        The enabled-check and the process-level lock live in the
        `_run_intraday_opportunity_scan` wrapper; this is the body.
        """
        cfg = self.config.intraday_scan

        # Identify movers first (cheap snapshot) so a morning/midday owner
        # lock cannot vanish paid discovery. Then wait. If the lock is
        # still held at window end, skip with a durable reason that names
        # the movers — never sleep the scan away, never drop them silently.
        candidates, snapshots = self._intraday_scan_mover_candidates(ctx)
        mover_names = [s for s, _ in candidates[: cfg.max_candidates_per_scan]]
        if self._await_paid_scan_slot(ctx.run_id):
            if getattr(self, "_paid_scan_waited_for", None) == "morning":
                return self._intraday_open_overlap_skip(ctx, mover_names)
            return self._intraday_paid_scan_skip(ctx, mover_names)
        # The 09:30/13:00 wait must not size against the pre-fill snapshot
        # taken before morning finished. Refresh after the lock releases.
        if getattr(self, "_paid_scan_waited", False):
            try:
                account, positions, _ = self._refresh_account_state()
                ctx.account = account
                ctx.positions = positions
                ctx.cash = account["cash"]
                ctx.deployable_cash = self._compute_deployable_cash(
                    ctx.cash, positions,
                )
                ctx.total_value = account.get("portfolio_value", ctx.total_value)
                self._sync_positions_from_broker(positions)
            except Exception as exc:  # noqa: BLE001
                _site(self, "post_wait_refresh", exc, log=logger)
                return {"status": "intraday_scan_no_opportunity", "run_id": ctx.run_id}
            candidates, snapshots = self._intraday_scan_mover_candidates(ctx)

        if not snapshots:
            return {"status": "intraday_scan_no_opportunity", "run_id": ctx.run_id}

        if not candidates:
            return {"status": "intraday_scan_no_opportunity", "run_id": ctx.run_id}

        # Largest moves first, capped — bounded per-tick cost regardless of
        # how many symbols move on a broad market day; not a scan of
        # everything, a check of the few things that moved most.
        symbols = [s for s, _ in candidates[: cfg.max_candidates_per_scan]]
        logger.info(
            "Intraday scan: %d symbol(s) moved >= %.1f%% since last close "
            "and are outside the %.1fh cooldown: %s",
            len(symbols), cfg.move_threshold_pct, cfg.cooldown_hours, symbols,
        )
        move_by_symbol = dict(candidates)
        ledgered_symbols: list[str] = []
        for symbol in symbols:
            # Persist before any paid call. Every outcome—including a model
            # failure or no target—now consumes the configured cooldown.
            try:
                self.db.record_intraday_evaluation(
                    symbol=symbol, run_id=ctx.run_id, status="selected",
                    detail=f"move_pct={move_by_symbol[symbol]:.4f}",
                )
            except Exception as exc:
                _site(self, "evaluation_ledger", exc, context={"symbol": symbol}, log=logger)
                continue
            ledgered_symbols.append(symbol)
            _record_pipeline_event(
                self, ctx, symbol, "opportunity", "discovered",
                "intraday_move_threshold",
                move_pct=move_by_symbol[symbol],
                threshold_pct=cfg.move_threshold_pct,
            )
        symbols = ledgered_symbols
        if not symbols:
            return {"status": "intraday_scan_no_opportunity", "run_id": ctx.run_id}

        # Produce Technical for quiet holds on the same paid call. Discovery
        # stays mover-capped; coverage for names the PM can increase does
        # not compete with that cap and does not consume mover cooldown.
        # Dropping an ungrounded hold is not the product for missing Tech.
        held_for_tech = [
            s for s in self._intraday_held_tech_symbols(ctx)
            if s not in {x.upper() for x in symbols}
        ]
        if held_for_tech:
            logger.info(
                "Intraday scan: producing Technical for %d held name(s) "
                "the mover list did not cover: %s",
                len(held_for_tech), held_for_tech,
            )
        tech_symbols = list(symbols) + held_for_tech
        # Item 177: the mover set, so the ATR context below is stamped only
        # on names the flat `move_threshold_pct` trigger actually selected —
        # held-book coverage never went through that trigger and must not be
        # mixed into the measurement that will answer for it.
        symbols_set = {s.upper() for s in symbols}

        symbols_data = []
        symbols_bars: dict[str, list] = {}
        for symbol in tech_symbols:
            try:
                bars = self.market.get_ohlcv(symbol, self.config.trading.lookback_days)
            except Exception as e:  # noqa: BLE001
                _site(self, "bar_fetch", e, context={"symbol": symbol}, log=logger)
                _record_pipeline_event(
                    self, ctx, symbol, "specialist", "failed",
                    "market_data_exception", detail=str(e),
                    specialist="tech_analyst",
                )
                continue
            if not bars:
                _record_pipeline_event(
                    self, ctx, symbol, "specialist", "failed",
                    "market_data_unavailable", specialist="tech_analyst",
                )
                continue
            indicators = compute_indicators(symbol, bars)
            self._record_intraday_trigger_atr_context(
                ctx, symbol, symbols_set, move_by_symbol, snapshots, indicators,
            )
            symbols_data.append({"symbol": symbol, "bars": bars, "indicators": indicators})
            symbols_bars[symbol] = bars
        if not symbols_data:
            return {"status": "intraday_scan_no_opportunity", "run_id": ctx.run_id}
        ctx.symbols_bars = symbols_bars

        prior_macro_state: dict = {}
        try:
            prior_macro_state = self.macro_store.load_last_state() or {}
        except Exception as e:  # noqa: BLE001
            _site(self, "macro_state_load", e)
        prior_ratings: dict = {}
        try:
            prior_ratings = self.tech_store.load()
        except Exception as e:  # noqa: BLE001
            _site(self, "tech_store_load", e)
        else:
            _site(self, "tech_store_load")

        # Truthful current-session evidence for exactly the names being
        # analyzed (2026-08-19): the scan detects on live prices, so Tech
        # must see those same live prices — not just daily bars ending at
        # yesterday's close, which is what triggered the scan being
        # invisible to the analyst that had to judge it. Rendered by
        # `build_user_message` as an explicit INCOMPLETE-session block,
        # never as a completed daily bar. Held names already in the
        # universe snapshot are included; a hold outside that snapshot
        # still gets bars, just no live-session block.
        # item 120: resolved through the SAME freshness rule the morning
        # pass uses. This used to hand Tech the raw snapshot, so a name
        # whose last trade was a prior session's could be rendered to the
        # intraday seat as "CURRENT SESSION (TODAY)".
        intraday_context, _missing, _stale, _rescued = self._resolve_live_context(
            snapshots, [s for s in tech_symbols if s in snapshots],
        )
        if _missing or _stale:
            logger.warning(
                "Intraday scan: no today print for %d symbol(s) handed to Tech "
                "(no price: %s; no today print: %s) — labelled as a lost price "
                "seat, never replaced by a prior session's number",
                len(_missing) + len(_stale), _missing[:10], _stale[:10],
            )
        self._require_paid_analysis("intraday_tech_analyst")
        try:
            analyses_map, ta_result = self.tech_analyst.analyze_batch(
                symbols_data,
                prior_ratings=prior_ratings,
                valuations={},
                intraday_context=intraday_context,
                prior_macro_regime=prior_macro_state.get("regime"),
                prior_macro_outlook=prior_macro_state.get("equity_outlook"),
            )
        except PaidAnalysisSuspended:
            raise
        except Exception as e:  # noqa: BLE001 — mirrors morning's tech
            # try/except (pipeline_stages.py): a bare call here had no
            # guard at all, so a batch-level raise (provider outage,
            # unparseable response) crashed the whole intraday tick
            # instead of being recorded as a LOST tech seat like every
            # other failure mode this scan already handles.
            _site(self, "tech_batch", e, log=logger)
            analyses_map, ta_result = {}, None
        # analyses_map carries every candidate symbol as a key (2026-08-19
        # Tech batch-response symbol-loss fix) — None marks a symbol
        # tech_analyst could not resolve even after its own bounded retry.
        # Filter before treating entries as real analyses.
        analyses = [a for a in analyses_map.values() if a is not None]
        failed_count = len(analyses_map) - len(analyses)
        if failed_count:
            logger.warning(
                "Intraday scan: %d/%d candidate symbol(s) failed to resolve "
                "even after retry: %s", failed_count, len(analyses_map),
                sorted(sym for sym, a in analyses_map.items() if a is None),
            )
        if ta_result:
            try:
                self.db.insert_agent_log(
                    **seat_acceptance_kwargs("failed" if not analyses else None),
                    agent_name="tech_analyst", run_id=ctx.run_id,
                    input_summary=(
                        f"Intraday scan batch: {len(analyses)}/{len(analyses_map)} "
                        f"symbols analyzed" + (f", {failed_count} failed" if failed_count else "")
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
            except Exception as e:  # noqa: BLE001
                _site(self, "tech_agent_log", e)
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
        if analyses:
            try:
                self.tech_store.update(analyses)
                ages = self.tech_store.compute_ages([a.symbol for a in analyses])
                for analysis in analyses:
                    if analysis.symbol in ages:
                        analysis.signal_age_days = ages[analysis.symbol]
            except Exception as e:  # noqa: BLE001
                _site(self, "tech_store_update", e)

        # Item 20 (board): deliberately no early "no analyses" return here.
        # `symbols_data` was already confirmed non-empty above, so zero
        # usable analyses at this point is a LOST tech seat, not a quiet
        # tick — it must fall through to the shared `data_status`/gate path
        # below (classification just before `ctx.data_status`), not return
        # "intraday_scan_no_opportunity" indistinguishably from a real
        # empty candidate set.

        # Same shared chain morning uses — no separate PM/RM/gate logic.
        #
        # Macro/news/earnings are still NOT re-fetched this tick — that is the
        # expensive research stack this scan exists to avoid rerunning, and
        # the saving is the whole point. But "not re-run" was previously
        # implemented as "not shown", and those are different things. This
        # session was handing the Portfolio Manager a technical-only view
        # while THIS MORNING'S macro regime and news sat on disk, already
        # paid for. The PM was blindfolded, not economical: `intra_check`
        # measured at $0.222/run against `morning`'s $0.221 over the 10 days
        # to 2026-08-27, ~99% of it the PM call, deciding on a fraction of
        # the evidence.
        #
        # So: carry the morning's results forward, and label them as carried.
        # The grounding property that mattered is preserved — nothing is
        # presented as having run this tick — while the PM stops reasoning
        # about an intraday move with no idea what regime it is happening in.
        ctx.analyses = analyses
        carried_macro = self._carry_forward_macro()
        carried_news = self._carry_forward_news(ctx)
        carried_earnings = self._carry_forward_earnings(ctx)
        carried_insider = self._carry_forward_insider(ctx)
        # Item 20 (board): three-way, matching morning's classification.
        # `symbols_data` was non-empty going in, so `analyses` empty here
        # means every submitted symbol failed (or the batch call raised,
        # caught above) — a LOST seat, not an ordinary quiet tick. A
        # partial batch (some resolved) stays REPORTED, exactly as before —
        # this must not start blocking intraday trading on one bad symbol.
        if analyses:
            tech_status = "partial" if failed_count else "ok"
        else:
            tech_status = "failed"
            logger.error(
                "Intraday scan: tech seat LOST — %d/%d submitted symbol(s) "
                "resolved to a usable analysis this tick",
                len(analyses), len(analyses_map),
            )
        ctx.data_status = {
            "tech": tech_status,
            # Status comes from the kind+event helpers, not from payload
            # truthiness. Same-session GOOD reuse is `carried_from_morning`
            # (PR #430). Cross-day GOOD macro is `remembered`. Empty/failed
            # carry still refuses BEFORE the Portfolio Manager. Earnings
            # without a provider on this object stays the intentional skip.
            "macro": carried_macro.status,
            "news": carried_news.status,
            "earnings": carried_earnings.status,
            "smart_money": carried_insider.status,
        }
        ctx.macro_analysis = carried_macro.payload
        ctx.news_intel = carried_news.payload
        ctx.earnings_results = list(carried_earnings.payload or [])
        ctx.smart_money_findings = list(carried_insider.payload or [])
        self._heal_lost_research_seats(ctx)

        # Same owner rule as morning: a decision on incomplete evidence is
        # fabricated. Applied here now that empty/failed carry-forward is
        # distinguishable from the intentional earnings skip.
        gate_skip = self._evidence_gate_skip(
            ctx, ctx.run_id, session=ctx.session,
        )
        if gate_skip is not None:
            gate_skip["candidates"] = symbols
            return gate_skip

        self.decision_stage.run(ctx)
        if not ctx.portfolio_decision:
            logger.error(
                "Intraday scan: PM failed (%s): %s",
                ctx.analysis_failure_status, ctx.analysis_failure_error,
            )
            return {
                "status": "intraday_analysis_error",
                "failure_status": ctx.analysis_failure_status or "pm_agent_failure",
                "error": ctx.analysis_failure_error or "no valid PM decision",
                "candidates": symbols,
                "run_id": ctx.run_id,
            }
        if not ctx.portfolio_decision.decisions:
            logger.info("Intraday scan: PM produced no actionable decisions")
            return {
                "status": "intraday_no_trades", "candidates": symbols,
                "run_id": ctx.run_id,
            }

        early_exit = self.risk_stage.run(ctx)
        if early_exit is not None:
            early_exit["candidates"] = symbols
            stop_updates = getattr(
                getattr(self, "broker", None), "stop_trade_updates", None,
            )
            if callable(stop_updates):
                try:
                    stop_updates()
                except Exception as exc:  # noqa: BLE001
                    _site(self, "stop_updates", exc, log=logger)
            return early_exit

        orders = self.execution_stage.run(ctx)
        return {
            "status": "intraday_executed" if orders else "intraday_no_trades",
            "candidates": symbols, "orders": orders, "run_id": ctx.run_id,
        }
