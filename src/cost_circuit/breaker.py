"""src.cost_circuit.breaker -- moved verbatim from src/cost_circuit.py; see the package docstring."""
from __future__ import annotations
import logging
import sqlite3
import threading
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, TypeVar
from src.cost_circuit.clock import _ClockPinnedConnection, _REAL_NOW_UTC, _now_utc
from src.cost_circuit.alert_ledger import UnavailableLLMCostCircuit, _durable_alert_surface_ok
from src.cost_circuit.parts.admission import Admission
from src.cost_circuit.parts.alert_formats import AlertFormats
from src.cost_circuit.parts.emergency_latch import EmergencyLatch
from src.cost_circuit.parts.circuit_state import CircuitState
from src.cost_circuit.parts.episode_wording import EpisodeWording
from src.cost_circuit.parts.infra_retry import InfraRetry
from src.cost_circuit.parts.operator_controls import OperatorControls
from src.cost_circuit.parts.owner_notify import OwnerNotify
from src.cost_circuit.parts.quota_holds import QuotaHolds
from src.cost_circuit.parts.session_lifecycle import SessionLifecycle
from src.cost_circuit.parts.settlement import Settlement

logger = logging.getLogger(__name__)


class LLMCostCircuitBreaker:
    """Mandatory cost/retry breaker shared by every agent in one pipeline.

    Composition, not inheritance: all eleven cost-circuit parts are HELD.
    `_hold_parts` builds one instance of each once per breaker and every
    same-named method below delegates to that instance. Collaborators that
    can change after construction (`notifier`, `_connect`, the sentinel and
    the infrastructure error) reach the parts live, never as a snapshot.
    """

    def __init__(self, db_path: str, config: Any, notifier: Any | None = None):
        self._memory_keeper: sqlite3.Connection | None = None
        self._emergency_latch_path: Path | None = None
        self._emergency_lock_path: Path | None = None
        if db_path == ":memory:":
            # Separate sqlite ``:memory:`` connections are separate databases.
            # Tests construct the pipeline this way, while the breaker needs a
            # short-lived connection per atomic transaction.  A named shared
            # memory DB plus a keeper preserves those semantics without
            # weakening production's file-backed cross-process behavior.
            self.db_path = f"file:qamc-cost-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._memory_keeper = sqlite3.connect(self.db_path, uri=True)
        else:
            self.db_path = str(Path(db_path))
            # SQLite is normally the durable source of truth.  This sidecar is
            # the independent fail-closed path for the one case where SQLite
            # itself fails while a paid request is in flight.  A new systemd
            # process must see that failure immediately instead of treating a
            # recovered DB connection as permission to spend again.
            db_file = Path(self.db_path)
            self._emergency_latch_path = db_file.with_name(
                f"{db_file.name}.llm-circuit-unavailable"
            )
            self._emergency_lock_path = db_file.with_name(
                f"{db_file.name}.llm-circuit.lock"
            )
        self.config = config
        if notifier is None:
            from src.notifier import TelegramNotifier
            notifier = TelegramNotifier()
        self.notifier = notifier
        # ContextVar keeps overlapping APScheduler job threads isolated.  The
        # morning research ThreadPool explicitly copies this context into its
        # workers (pipeline_stages.py); a mutable process-global tuple allowed
        # an intra tick to relabel an in-flight morning request.
        self._session_context: ContextVar[tuple[str, str]] = ContextVar(
            f"qamc_cost_session_{id(self)}", default=("unscoped", "unknown")
        )
        self._infrastructure_lock = threading.Lock()
        self._infrastructure_error: BaseException | None = None
        self._unavailable_sentinel: UnavailableLLMCostCircuit | None = None
        self._hold_parts()
        self._sync_emergency_latch()
        if self._unavailable_sentinel is not None:
            return
        if getattr(self.config, "require_telegram_alerts", True) is True:
            transport_enabled = getattr(self.notifier, "enabled", True) is True
            durable_ok = _durable_alert_surface_ok(self._emergency_latch_path)
            if not transport_enabled and not durable_ok:
                # Genuine case only: nowhere to deliver AND nowhere to record.
                self.mark_unavailable(
                    RuntimeError(
                        "mandatory cost-circuit alerts can be neither delivered "
                        "nor durably recorded: the notification transport is "
                        "disabled and no durable latch record can be written"
                    )
                )
                return
            if not transport_enabled:
                logger.warning(
                    "Cost-circuit notification transport is disabled; mandatory "
                    "alerts will be recorded durably at %s and surfaced through "
                    "the API/dashboard decision-path health instead. Paid "
                    "analysis is NOT suspended for this reason.",
                    self._emergency_latch_path,
                )
        try:
            self._run_with_infra_retry(
                self._initialize, agent_name="circuit_startup",
            )
        except Exception as exc:
            self.mark_unavailable(exc, agent_name="circuit_startup")

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "enabled", True))

    @classmethod
    def fail_closed(
        cls,
        db_path: str,
        config: Any,
        error: BaseException,
        *,
        notifier: Any | None = None,
        run_id: str = "unscoped",
        mode: str = "unknown",
        agent_name: str = "circuit_startup",
        attempts: int | None = None,
    ) -> "LLMCostCircuitBreaker":
        """Build a durable sentinel even when normal construction explodes."""

        self = cls.__new__(cls)
        self._memory_keeper = None
        self.db_path = str(db_path)
        if db_path == ":memory:":
            # There is no durable filesystem location for a true in-memory
            # database.  The in-process sentinel below still fails closed;
            # importantly, do not create literal ``:memory:*`` sidecars.
            self._emergency_latch_path = None
            self._emergency_lock_path = None
        else:
            self.db_path = str(Path(db_path))
            db_file = Path(self.db_path)
            self._emergency_latch_path = db_file.with_name(
                f"{db_file.name}.llm-circuit-unavailable"
            )
            self._emergency_lock_path = db_file.with_name(
                f"{db_file.name}.llm-circuit.lock"
            )
        self.config = config
        if notifier is None:
            try:
                from src.notifier import TelegramNotifier
                notifier = TelegramNotifier()
            except Exception:
                class _LocalOnlyNotifier:
                    enabled = False

                    @staticmethod
                    def send(_message: str) -> bool:
                        return False

                notifier = _LocalOnlyNotifier()
        self.notifier = notifier
        self._session_context = ContextVar(
            f"qamc_cost_session_{id(self)}", default=(run_id, mode)
        )
        self._infrastructure_lock = threading.Lock()
        self._infrastructure_error = None
        self._unavailable_sentinel = None
        self._hold_parts()
        self.mark_unavailable(
            error,
            run_id=run_id,
            mode=mode,
            agent_name=agent_name,
            attempts=attempts,
        )
        return self

    # --- Held parts (composition). Built once per breaker, after every slot
    # they read exists. `config`, the latch/lock paths, `_session_context` and
    # `_infrastructure_lock` never change after construction. Anything that
    # CAN change is handed in live: `notifier` through the write-through
    # property below, `_connect` through a late-bound lambda (tests swap it on
    # the instance), the sentinel and infrastructure error through getter /
    # setter pairs. A part is never handed the breaker's delegate for a body
    # it already owns (that would recurse); cross-part calls go through the
    # breaker's delegates so every part sees the held instance.
    @property
    def notifier(self) -> Any:
        return self._notifier

    @notifier.setter
    def notifier(self, value: Any) -> None:
        self._notifier = value
        for part in (
            getattr(self, "_emergency_latch", None),
            getattr(self, "_infra_retry", None),
            getattr(self, "_owner_notify", None),
        ):
            if part is not None:
                part.notifier = value

    def _hold_parts(self) -> None:
        self._alert_formats = AlertFormats()
        self._episode_wording = EpisodeWording(config=self.config)
        self._circuit_state = CircuitState(
            config=self.config,
            context=self._context,
            emergency_latch_path=self._emergency_latch_path,
        )
        self._quota_holds = QuotaHolds(
            config=self.config,
            auto_clear_transient_latch_locked=self._circuit_state._auto_clear_transient_latch_locked,
            scope_key=self._circuit_state._scope_key,
            state_row=self._circuit_state._state_row,
        )
        connect = lambda: self._connect()  # noqa: E731 -- late-bound: tests swap `_connect`
        read_sentinel = lambda: self._unavailable_sentinel  # noqa: E731
        write_sentinel = lambda value: setattr(self, "_unavailable_sentinel", value)  # noqa: E731
        read_infra_error = lambda: self._infrastructure_error  # noqa: E731
        write_infra_error = lambda value: setattr(self, "_infrastructure_error", value)  # noqa: E731
        self._emergency_latch = EmergencyLatch(
            connect=connect,
            notifier=self.notifier,
            infrastructure_lock=self._infrastructure_lock,
            emergency_latch_path=self._emergency_latch_path,
            emergency_lock_path=self._emergency_lock_path,
            read_unavailable_sentinel=read_sentinel,
            write_unavailable_sentinel=write_sentinel,
            read_infrastructure_error=read_infra_error,
            write_infrastructure_error=write_infra_error,
        )
        self._infra_retry = InfraRetry(
            config=self.config,
            context=self._context,
            notifier=self.notifier,
            infrastructure_lock=self._infrastructure_lock,
            emergency_latch_path=self._emergency_latch_path,
            emergency_lock_path=self._emergency_lock_path,
            best_effort_emergency_snapshot=self._best_effort_emergency_snapshot,
            write_emergency_latch=self._write_emergency_latch,
            sync_emergency_latch=self._sync_emergency_latch,
            read_unavailable_sentinel=read_sentinel,
            write_unavailable_sentinel=write_sentinel,
            read_infrastructure_error=read_infra_error,
            write_infrastructure_error=write_infra_error,
        )
        self._session_lifecycle = SessionLifecycle(
            enabled=self.enabled,
            connect=connect,
            session_context=self._session_context,
            infrastructure_lock=self._infrastructure_lock,
            sync_emergency_latch=self._sync_emergency_latch,
            reconcile_quota_holds_locked=self._reconcile_quota_holds_locked,
            run_with_infra_retry=self._run_with_infra_retry,
            notify_if_needed=self._notify_if_needed,
            enforce_current_limits=self.enforce_current_limits,
            status=self.status,
            read_unavailable_sentinel=read_sentinel,
        )
        self._owner_notify = OwnerNotify(
            enabled=self.enabled,
            infrastructure_lock=self._infrastructure_lock,
            read_unavailable_sentinel=read_sentinel,
            connect=connect,
            refresh_latched_snapshot_locked=self._refresh_latched_snapshot_locked,
            state_row=self._state_row,
            notifier=self.notifier,
            episode_already_paged_locked=self._episode_already_paged_locked,
            suspension_still_inside_self_clear_window_locked=self._suspension_still_inside_self_clear_window_locked,
            record_suspension_deferral_locked=self._record_suspension_deferral_locked,
            episode_facts_locked=self._episode_facts_locked,
            format_alert=self.format_alert,
            format_quota_alert=self.format_quota_alert,
            format_recovery_alert=self.format_recovery_alert,
            format_auto_reset_alert=self.format_auto_reset_alert,
        )
        self._admission = Admission(
            config=self.config,
            enabled=self.enabled,
            connect=connect,
            context=self._context,
            infrastructure_lock=self._infrastructure_lock,
            effective_state_locked=self._effective_state_locked,
            notify_if_needed=self._notify_if_needed,
            raise_if_unavailable=self._raise_if_unavailable,
            reconcile_quota_holds_locked=self._reconcile_quota_holds_locked,
            run_with_infra_retry=self._run_with_infra_retry,
            seed_today=self._seed_today,
            sync_emergency_latch=self._sync_emergency_latch,
            totals=self._totals,
            trip_locked=self._trip_locked,
            status=self.status,
            read_unavailable_sentinel=read_sentinel,
        )
        self._settlement = Settlement(
            config=self.config,
            enabled=self.enabled,
            connect=connect,
            infrastructure_lock=self._infrastructure_lock,
            raise_if_unavailable=self._raise_if_unavailable,
            seed_today=self._seed_today,
            reconcile_quota_holds_locked=self._reconcile_quota_holds_locked,
            effective_state_locked=self._effective_state_locked,
            notify_if_needed=self._notify_if_needed,
            totals=self._totals,
            enforce_settled_limits_locked=self._enforce_settled_limits_locked,
            trip_locked=self._trip_locked,
            refresh_latched_snapshot_locked=self._refresh_latched_snapshot_locked,
            sync_emergency_latch=self._sync_emergency_latch,
            read_unavailable_sentinel=read_sentinel,
        )
        self._operator_controls = OperatorControls(
            enabled=self.enabled,
            context=self._context,
            connect=connect,
            infrastructure_lock=self._infrastructure_lock,
            emergency_latch_path=self._emergency_latch_path,
            sync_emergency_latch=self._sync_emergency_latch,
            seed_today=self._seed_today,
            reconcile_quota_holds_locked=self._reconcile_quota_holds_locked,
            effective_state_locked=self._effective_state_locked,
            totals=self._totals,
            notify_if_needed=self._notify_if_needed,
            state_row=self._state_row,
            emergency_file_lock=self._emergency_file_lock,
            notify_auto_resets_if_needed=self._notify_auto_resets_if_needed,
            read_unavailable_sentinel=read_sentinel,
            write_unavailable_sentinel=write_sentinel,
            read_infrastructure_error=read_infra_error,
            write_infrastructure_error=write_infra_error,
        )

    # EmergencyLatch.
    def _best_effort_emergency_snapshot(self, *args, **kwargs):
        return self._emergency_latch._best_effort_emergency_snapshot(*args, **kwargs)

    def _read_emergency_latch(self, *args, **kwargs):
        return self._emergency_latch._read_emergency_latch(*args, **kwargs)

    def _sync_emergency_latch(self, *args, **kwargs):
        return self._emergency_latch._sync_emergency_latch(*args, **kwargs)

    _safe_optional_int = staticmethod(EmergencyLatch._safe_optional_int)
    _safe_optional_float = staticmethod(EmergencyLatch._safe_optional_float)

    def _emergency_file_lock(self, *args, **kwargs):
        return self._emergency_latch._emergency_file_lock(*args, **kwargs)

    def _write_emergency_latch(self, *args, **kwargs):
        return self._emergency_latch._write_emergency_latch(*args, **kwargs)

    # InfraRetry.
    def _infra_retry_backoff_s(self, *args, **kwargs):
        return self._infra_retry._infra_retry_backoff_s(*args, **kwargs)

    def _run_with_infra_retry(self, *args, **kwargs):
        return self._infra_retry._run_with_infra_retry(*args, **kwargs)

    def mark_unavailable(self, *args, **kwargs):
        return self._infra_retry.mark_unavailable(*args, **kwargs)

    def _raise_if_unavailable(self, *args, **kwargs):
        return self._infra_retry._raise_if_unavailable(*args, **kwargs)

    # SessionLifecycle.
    def _initialize(self, *args, **kwargs):
        return self._session_lifecycle._initialize(*args, **kwargs)

    def _validate_accounting_invariants(self, *args, **kwargs):
        return self._session_lifecycle._validate_accounting_invariants(*args, **kwargs)

    def _seed_today(self, *args, **kwargs):
        return self._session_lifecycle._seed_today(*args, **kwargs)

    def activate_session(self, *args, **kwargs):
        return self._session_lifecycle.activate_session(*args, **kwargs)

    def set_session_context(self, *args, **kwargs):
        return self._session_lifecycle.set_session_context(*args, **kwargs)

    def _context(self, *args, **kwargs):
        return self._session_lifecycle._context(*args, **kwargs)

    # OwnerNotify.
    def _notify_if_needed(self, *args, **kwargs):
        return self._owner_notify._notify_if_needed(*args, **kwargs)

    def _notify_quota_holds_if_needed(self, *args, **kwargs):
        return self._owner_notify._notify_quota_holds_if_needed(*args, **kwargs)

    def _notify_quota_recoveries_if_needed(self, *args, **kwargs):
        return self._owner_notify._notify_quota_recoveries_if_needed(*args, **kwargs)

    def _notify_auto_resets_if_needed(self, *args, **kwargs):
        return self._owner_notify._notify_auto_resets_if_needed(*args, **kwargs)

    # Admission.
    def _enforce_settled_limits_locked(self, *args, **kwargs):
        return self._admission._enforce_settled_limits_locked(*args, **kwargs)

    def enforce_current_limits(self, *args, **kwargs):
        return self._admission.enforce_current_limits(*args, **kwargs)

    def require_paid_analysis(self, *args, **kwargs):
        return self._admission.require_paid_analysis(*args, **kwargs)

    def begin_call(self, *args, **kwargs):
        return self._admission.begin_call(*args, **kwargs)

    # Settlement.
    def before_provider_attempt(self, *args, **kwargs):
        return self._settlement.before_provider_attempt(*args, **kwargs)

    def complete_call(self, *args, **kwargs):
        return self._settlement.complete_call(*args, **kwargs)

    def fail_call(self, *args, **kwargs):
        return self._settlement.fail_call(*args, **kwargs)

    # OperatorControls.
    def status(self, *args, **kwargs):
        return self._operator_controls.status(*args, **kwargs)

    def reset(self, *args, **kwargs):
        return self._operator_controls.reset(*args, **kwargs)

    # AlertFormats -- four pure formatters, the part's own functions.
    format_auto_reset_alert = staticmethod(AlertFormats.format_auto_reset_alert)
    format_quota_alert = staticmethod(AlertFormats.format_quota_alert)
    format_recovery_alert = staticmethod(AlertFormats.format_recovery_alert)
    format_alert = staticmethod(AlertFormats.format_alert)

    # EpisodeWording.
    def _self_clear_window_minutes(self):
        return self._episode_wording._self_clear_window_minutes()

    def _suspension_still_inside_self_clear_window_locked(self, *args, **kwargs):
        return self._episode_wording._suspension_still_inside_self_clear_window_locked(*args, **kwargs)

    def _episode_already_paged_locked(self, *args, **kwargs):
        return self._episode_wording._episode_already_paged_locked(*args, **kwargs)

    def _record_suspension_deferral_locked(self, *args, **kwargs):
        return self._episode_wording._record_suspension_deferral_locked(*args, **kwargs)

    def _episode_facts_locked(self, *args, **kwargs):
        return self._episode_wording._episode_facts_locked(*args, **kwargs)

    _format_episode_line = staticmethod(EpisodeWording._format_episode_line)
    _format_episode_summary = staticmethod(EpisodeWording._format_episode_summary)

    # CircuitState.
    _totals = staticmethod(CircuitState._totals)

    def _state_row(self, conn):
        return self._circuit_state._state_row(conn)

    _scope_key = staticmethod(CircuitState._scope_key)

    def _active_quota_hold_locked(self, *args, **kwargs):
        return self._circuit_state._active_quota_hold_locked(*args, **kwargs)

    def _effective_state_locked(self, *args, **kwargs):
        return self._circuit_state._effective_state_locked(*args, **kwargs)

    def _auto_clear_transient_latch_locked(self, *args, **kwargs):
        return self._circuit_state._auto_clear_transient_latch_locked(*args, **kwargs)

    # QuotaHolds.
    def _reconcile_quota_holds_locked(self, *args, **kwargs):
        return self._quota_holds._reconcile_quota_holds_locked(*args, **kwargs)

    def _hold_quota_locked(self, *args, **kwargs):
        return self._quota_holds._hold_quota_locked(*args, **kwargs)

    def _refresh_latched_snapshot_locked(self, conn):
        return self._quota_holds._refresh_latched_snapshot_locked(conn)

    def _trip_locked(self, *args, **kwargs):
        return self._quota_holds._trip_locked(*args, **kwargs)

    def _connect(self) -> Any:
        conn = sqlite3.connect(
            self.db_path, timeout=10.0, uri=self.db_path.startswith("file:qamc-cost-")
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        # Production takes this branch: the clock is the real one, so the
        # bare connection is returned and SQLite's `'now'` is SQLite's own
        # clock, unchanged. Only a caller that supplied a clock gets the
        # wrapper, and then both clocks are that one instant.
        if _now_utc is not _REAL_NOW_UTC:
            return _ClockPinnedConnection(
                conn, _now_utc().strftime("%Y-%m-%d %H:%M:%S")
            )
        return conn

