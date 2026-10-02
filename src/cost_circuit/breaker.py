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
from src.cost_circuit.breaker_latch import _BreakerLatchMixin
from src.cost_circuit.breaker_retry import _BreakerRetryMixin
from src.cost_circuit.breaker_session import _BreakerSessionMixin
from src.cost_circuit.breaker_notify import _BreakerNotifyMixin
from src.cost_circuit.breaker_admission import _BreakerAdmissionMixin
from src.cost_circuit.breaker_settlement import _BreakerSettlementMixin
from src.cost_circuit.breaker_operator import _BreakerOperatorMixin
from src.cost_circuit.parts.alert_formats import AlertFormats
from src.cost_circuit.parts.circuit_state import CircuitState
from src.cost_circuit.parts.episode_wording import EpisodeWording
from src.cost_circuit.parts.quota_holds import QuotaHolds

logger = logging.getLogger(__name__)


class LLMCostCircuitBreaker(_BreakerLatchMixin, _BreakerRetryMixin, _BreakerSessionMixin, _BreakerNotifyMixin, _BreakerAdmissionMixin, _BreakerSettlementMixin, _BreakerOperatorMixin):
    """Mandatory cost/retry breaker shared by every agent in one pipeline.

    Four pieces are HELD, not inherited: `AlertFormats`, `EpisodeWording`,
    `CircuitState` and `QuotaHolds` are built once in `_hold_parts` and every
    same-named method below delegates to that instance. The seven remaining
    bases are still per-call shims (see src/cost_circuit/breaker_*.py).
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
        self._hold_parts()
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
        self._hold_parts()
        self._session_context = ContextVar(
            f"qamc_cost_session_{id(self)}", default=(run_id, mode)
        )
        self._infrastructure_lock = threading.Lock()
        self._infrastructure_error = None
        self._unavailable_sentinel = None
        self.mark_unavailable(
            error,
            run_id=run_id,
            mode=mode,
            agent_name=agent_name,
            attempts=attempts,
        )
        return self

    # --- Held parts (composition). Built once per breaker; `config` and the
    # latch path never change after construction, and `_context` is the
    # session shim's bound method, which reads the ContextVar live.
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

