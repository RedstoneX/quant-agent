"""src.cost_circuit.parts.infra_retry -- InfraRetry for the cost circuit (infra-fault retry/backoff, mark_unavailable, raise_if_unavailable).

Bodies moved verbatim from the former src/cost_circuit/breaker_retry.py (now held by LLMCostCircuitBreaker) (originally src/cost_circuit.py).
Every collaborator is an explicit keyword-only constructor argument.
"""

from __future__ import annotations
import logging
import random
import sqlite3
import time
from typing import Any, Callable, TypeVar
from src.cost_circuit.classification import _T
from src.cost_circuit.refusal import OptionalPaidAnalysisRetrySkipped, PaidAnalysisSuspended
from src.cost_circuit.alert_ledger import UnavailableLLMCostCircuit

logger = logging.getLogger(__name__)


class InfraRetry:
    def __init__(
        self,
        *,
        config,
        context,
        notifier,
        infrastructure_lock,
        emergency_latch_path,
        emergency_lock_path,
        best_effort_emergency_snapshot,
        write_emergency_latch,
        sync_emergency_latch,
        read_unavailable_sentinel,
        write_unavailable_sentinel,
        read_infrastructure_error,
        write_infrastructure_error,
        infra_retry_backoff_s=None,
        mark_unavailable=None,
    ) -> None:
        self.config = config
        self._context = context
        self.notifier = notifier
        self._infrastructure_lock = infrastructure_lock
        self._emergency_latch_path = emergency_latch_path
        self._emergency_lock_path = emergency_lock_path
        self._best_effort_emergency_snapshot = best_effort_emergency_snapshot
        self._write_emergency_latch = write_emergency_latch
        self._sync_emergency_latch = sync_emergency_latch
        self.read_unavailable_sentinel = read_unavailable_sentinel
        self.write_unavailable_sentinel = write_unavailable_sentinel
        self.read_infrastructure_error = read_infrastructure_error
        self.write_infrastructure_error = write_infrastructure_error
        # The bodies below are themselves moved; a caller passes one only when it was
        # swapped on the host (see the shim), otherwise this object's own run.
        if infra_retry_backoff_s is not None:
            self._infra_retry_backoff_s = infra_retry_backoff_s
        if mark_unavailable is not None:
            self.mark_unavailable = mark_unavailable

    @property
    def _unavailable_sentinel(self):
        """Read live off the host: the bodies read it after calls that install it."""
        return self.read_unavailable_sentinel()

    @_unavailable_sentinel.setter
    def _unavailable_sentinel(self, value) -> None:
        """Written through to the host under the same lock the body already holds."""
        self.write_unavailable_sentinel(value)

    @property
    def _infrastructure_error(self):
        """Read live off the host: the bodies read it after calls that install it."""
        return self.read_infrastructure_error()

    @_infrastructure_error.setter
    def _infrastructure_error(self, value) -> None:
        """Written through to the host under the same lock the body already holds."""
        self.write_infrastructure_error(value)

    def _infra_retry_backoff_s(self, attempt: int) -> float:
        """Exponential backoff with jitter for one retried infra operation.

        Same shape as `MacroDataProvider._next_backoff` (src/data/macro.py):
        doubles from `infra_fault_retry_backoff_base_s`, capped at
        `infra_fault_retry_backoff_max_s`, plus uniform jitter up to
        `infra_fault_retry_backoff_jitter_s` so concurrent processes hitting
        the same transient fault don't retry in lockstep.
        """

        base = max(0.0, float(getattr(self.config, "infra_fault_retry_backoff_base_s", 2.0)))
        cap = max(base, float(getattr(self.config, "infra_fault_retry_backoff_max_s", 8.0)))
        jitter = max(0.0, float(getattr(self.config, "infra_fault_retry_backoff_jitter_s", 1.0)))
        delay = min(base * (2**attempt), cap)
        return delay + (random.uniform(0, jitter) if jitter > 0 else 0.0)

    def _run_with_infra_retry(
        self,
        operation: Callable[[], _T],
        *,
        agent_name: str,
        run_id: str | None = None,
        mode: str | None = None,
    ) -> _T:
        """Retry a transient cost-circuit infrastructure fault with backoff.

        docs/WORK.md item 17(a): distinguishes "I cannot read the budget"
        (a transient I/O or DB-open failure raised by `operation` -- worth
        retrying) from two things it must NOT be confused with:

          * "I am over budget" -- a real, measured breach, which never
            raises here at all: `_trip_locked` records it directly in the
            `llm_circuit_state` row and returns normally. Its signal
            (`PaidAnalysisSuspended` / `OptionalPaidAnalysisRetrySkipped`,
            when it surfaces through `operation`, e.g. from `begin_call`)
            is control flow, not an infrastructure fault -- it passes
            straight through, unretried and unlatched-by-this-method,
            exactly as before.
          * "the ledger IS open and readable, and it is provably wrong" --
            `_validate_accounting_invariants` / `_reconcile_quota_holds_
            locked` raise a plain `RuntimeError` for a detected accounting
            corruption (mismatched settled totals, an inexact day that
            cannot re-arm a hold). That is a deterministic finding, not a
            flake: retrying it produces the exact same answer every time,
            and `scripts/cost_circuit.py` deliberately catches this raw
            exception at several commands to behave differently (`reset`
            tolerates it, `status`/`check` propagate it). Retrying and then
            converting it into the generic sentinel state would both waste
            time on a fault backoff can never fix and break that existing
            distinction. So only `sqlite3.Error` / `OSError` -- the actual
            "cannot open/read the database" shape -- are treated as the
            transient fault this method retries; every other exception
            (including these invariant `RuntimeError`s) passes straight
            through unretried, exactly as before this method existed.

        Only after `infra_fault_max_retries` extra attempts have ALL failed
        does this escalate to the durable file latch via `mark_unavailable`,
        then re-raise the last error so existing callers' own fail-closed
        handling (which every call site already had) is unchanged -- it
        just now only fires once persistence is confirmed, not on the
        first blip.
        """

        max_retries = max(0, int(getattr(self.config, "infra_fault_max_retries", 2)))
        last_exc: BaseException | None = None
        for attempt in range(max_retries + 1):
            try:
                return operation()
            except (PaidAnalysisSuspended, OptionalPaidAnalysisRetrySkipped):
                raise
            except (sqlite3.Error, OSError) as exc:
                last_exc = exc
                if attempt < max_retries:
                    backoff = self._infra_retry_backoff_s(attempt)
                    logger.warning(
                        "Cost-circuit infrastructure fault in %s (attempt %d/%d): %s -- retrying in %.1fs",
                        agent_name,
                        attempt + 1,
                        max_retries + 1,
                        exc,
                        backoff,
                    )
                    if backoff > 0:
                        time.sleep(backoff)
                    continue
                logger.critical(
                    "Cost-circuit infrastructure fault in %s persisted past "
                    "%d attempt(s); latching paid analysis closed: %s",
                    agent_name,
                    max_retries + 1,
                    exc,
                    exc_info=True,
                )
        assert last_exc is not None  # loop always returns or sets this
        self.mark_unavailable(
            last_exc,
            run_id=run_id,
            mode=mode,
            agent_name=agent_name,
        )
        raise last_exc

    def mark_unavailable(
        self,
        error: BaseException,
        *,
        run_id: str | None = None,
        mode: str | None = None,
        agent_name: str = "circuit_infrastructure",
        attempts: int | None = None,
    ) -> dict[str, Any]:
        """Permanently fail this process closed after accounting failure.

        Provider code calls this when a breaker DB operation itself raises.
        The shared object then blocks every agent in this process, and the
        no-DB sentinel provides the mandatory Telegram/local alert path.
        """

        current_run, current_mode = self._context()
        affected_run = run_id or current_run
        affected_mode = mode or current_mode
        durable_error: BaseException = error
        snapshot = self._best_effort_emergency_snapshot(affected_run)
        effective_attempts = attempts
        attempts_exact = False
        if snapshot.get("attempts") is not None:
            persisted_attempts = int(snapshot["attempts"])
            if effective_attempts is None or effective_attempts <= persisted_attempts:
                effective_attempts = persisted_attempts
                attempts_exact = bool(snapshot.get("attempts_exact"))
            else:
                # A local request crossed the boundary after the durable
                # snapshot; max() is the honest lower bound, not an exact sum.
                effective_attempts = max(effective_attempts, persisted_attempts)
        elif agent_name == "pricing_preflight" and effective_attempts == 0:
            # Pricing verification precedes every provider request.
            attempts_exact = True
        session_cost = snapshot.get("session_cost_usd")
        if session_cost is None and agent_name == "pricing_preflight" and effective_attempts == 0:
            session_cost = 0.0
        daily_cost = snapshot.get("daily_cost_usd")
        costs_exact = bool(
            session_cost is not None
            and daily_cost is not None
            and snapshot.get("daily_costs_exact", False)
            and (snapshot.get("session_costs_exact", True) if snapshot.get("session_cost_usd") is not None else True)
        )
        try:
            self._write_emergency_latch(
                error,
                run_id=affected_run,
                mode=affected_mode,
                agent_name=agent_name,
                attempts=effective_attempts,
                attempts_exact=attempts_exact,
                session_cost_usd=session_cost,
                daily_cost_usd=daily_cost,
                costs_exact=costs_exact,
            )
        except Exception as latch_exc:
            # Keep this process stopped even if both persistence mechanisms
            # are impaired, and make the alert explicit that cross-process
            # durability could not be guaranteed.
            durable_error = RuntimeError(
                f"{type(error).__name__}: {str(error)[:300]}; durable emergency "
                f"latch write also failed: {type(latch_exc).__name__}: "
                f"{str(latch_exc)[:240]}"
            )
            logger.critical("Cost-circuit emergency latch write failed", exc_info=True)

        with self._infrastructure_lock:
            if self._infrastructure_error is None:
                self._infrastructure_error = durable_error
                self._unavailable_sentinel = UnavailableLLMCostCircuit(
                    durable_error,
                    notifier=self.notifier,
                    run_id=affected_run,
                    mode=affected_mode,
                    agent_name=agent_name,
                    attempts=effective_attempts,
                    attempts_exact=attempts_exact,
                    session_cost_usd=session_cost,
                    daily_cost_usd=daily_cost,
                    costs_exact=costs_exact,
                    emergency_latch_path=self._emergency_latch_path,
                    emergency_lock_path=self._emergency_lock_path,
                )
            sentinel = self._unavailable_sentinel
        return sentinel.activate_session(
            affected_run,
            affected_mode,
        )

    def _raise_if_unavailable(self, agent_name: str) -> None:
        self._sync_emergency_latch()
        with self._infrastructure_lock:
            sentinel = self._unavailable_sentinel
        if sentinel is not None:
            sentinel.require_paid_analysis(agent_name)
