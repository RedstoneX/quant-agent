"""Wiring for `LLMCostCircuitBreaker`: builds the eleven held parts.

Construction only -- no behaviour lives here. `hold_parts` reads the slots a
built breaker already has (`config`, the latch/lock paths, `_session_context`,
`_infrastructure_lock`, the sentinel and error slots) and hands each part its
collaborators. Anything that can change after construction is handed in live:
`notifier` through the breaker's write-through property, `_connect` through a
late-bound lambda (tests swap it on the instance), the sentinel and the
infrastructure error through getter / setter pairs. A part is never handed the
breaker's delegate for a body it already owns (that would recurse); cross-part
calls go through the breaker's delegates so every part sees the held instance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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

if TYPE_CHECKING:  # pragma: no cover
    from src.cost_circuit.breaker import LLMCostCircuitBreaker


def hold_parts(breaker: "LLMCostCircuitBreaker") -> None:
    """Build one instance of each part onto `breaker`; body moved verbatim
    from the former `LLMCostCircuitBreaker._hold_parts` (`self` -> `breaker`)."""
    breaker._alert_formats = AlertFormats()
    breaker._episode_wording = EpisodeWording(config=breaker.config)
    breaker._circuit_state = CircuitState(
        config=breaker.config,
        context=breaker._context,
        emergency_latch_path=breaker._emergency_latch_path,
    )
    breaker._quota_holds = QuotaHolds(
        config=breaker.config,
        auto_clear_transient_latch_locked=breaker._circuit_state._auto_clear_transient_latch_locked,
        scope_key=breaker._circuit_state._scope_key,
        state_row=breaker._circuit_state._state_row,
    )
    connect = lambda: breaker._connect()  # noqa: E731 -- late-bound: tests swap `_connect`
    read_sentinel = lambda: breaker._unavailable_sentinel  # noqa: E731
    write_sentinel = lambda value: setattr(breaker, "_unavailable_sentinel", value)  # noqa: E731
    read_infra_error = lambda: breaker._infrastructure_error  # noqa: E731
    write_infra_error = lambda value: setattr(breaker, "_infrastructure_error", value)  # noqa: E731
    breaker._emergency_latch = EmergencyLatch(
        connect=connect,
        notifier=breaker.notifier,
        infrastructure_lock=breaker._infrastructure_lock,
        emergency_latch_path=breaker._emergency_latch_path,
        emergency_lock_path=breaker._emergency_lock_path,
        read_unavailable_sentinel=read_sentinel,
        write_unavailable_sentinel=write_sentinel,
        read_infrastructure_error=read_infra_error,
        write_infrastructure_error=write_infra_error,
    )
    breaker._infra_retry = InfraRetry(
        config=breaker.config,
        context=breaker._context,
        notifier=breaker.notifier,
        infrastructure_lock=breaker._infrastructure_lock,
        emergency_latch_path=breaker._emergency_latch_path,
        emergency_lock_path=breaker._emergency_lock_path,
        best_effort_emergency_snapshot=breaker._best_effort_emergency_snapshot,
        write_emergency_latch=breaker._write_emergency_latch,
        sync_emergency_latch=breaker._sync_emergency_latch,
        read_unavailable_sentinel=read_sentinel,
        write_unavailable_sentinel=write_sentinel,
        read_infrastructure_error=read_infra_error,
        write_infrastructure_error=write_infra_error,
    )
    breaker._session_lifecycle = SessionLifecycle(
        enabled=breaker.enabled,
        connect=connect,
        session_context=breaker._session_context,
        infrastructure_lock=breaker._infrastructure_lock,
        sync_emergency_latch=breaker._sync_emergency_latch,
        reconcile_quota_holds_locked=breaker._reconcile_quota_holds_locked,
        run_with_infra_retry=breaker._run_with_infra_retry,
        notify_if_needed=breaker._notify_if_needed,
        enforce_current_limits=breaker.enforce_current_limits,
        status=breaker.status,
        read_unavailable_sentinel=read_sentinel,
    )
    breaker._owner_notify = OwnerNotify(
        enabled=breaker.enabled,
        infrastructure_lock=breaker._infrastructure_lock,
        read_unavailable_sentinel=read_sentinel,
        connect=connect,
        refresh_latched_snapshot_locked=breaker._refresh_latched_snapshot_locked,
        state_row=breaker._state_row,
        notifier=breaker.notifier,
        episode_already_paged_locked=breaker._episode_already_paged_locked,
        suspension_still_inside_self_clear_window_locked=breaker._suspension_still_inside_self_clear_window_locked,
        record_suspension_deferral_locked=breaker._record_suspension_deferral_locked,
        episode_facts_locked=breaker._episode_facts_locked,
        format_alert=breaker.format_alert,
        format_quota_alert=breaker.format_quota_alert,
        format_recovery_alert=breaker.format_recovery_alert,
        format_auto_reset_alert=breaker.format_auto_reset_alert,
    )
    breaker._admission = Admission(
        config=breaker.config,
        enabled=breaker.enabled,
        connect=connect,
        context=breaker._context,
        infrastructure_lock=breaker._infrastructure_lock,
        effective_state_locked=breaker._effective_state_locked,
        notify_if_needed=breaker._notify_if_needed,
        raise_if_unavailable=breaker._raise_if_unavailable,
        reconcile_quota_holds_locked=breaker._reconcile_quota_holds_locked,
        run_with_infra_retry=breaker._run_with_infra_retry,
        seed_today=breaker._seed_today,
        sync_emergency_latch=breaker._sync_emergency_latch,
        totals=breaker._totals,
        trip_locked=breaker._trip_locked,
        status=breaker.status,
        read_unavailable_sentinel=read_sentinel,
    )
    breaker._settlement = Settlement(
        config=breaker.config,
        enabled=breaker.enabled,
        connect=connect,
        infrastructure_lock=breaker._infrastructure_lock,
        raise_if_unavailable=breaker._raise_if_unavailable,
        seed_today=breaker._seed_today,
        reconcile_quota_holds_locked=breaker._reconcile_quota_holds_locked,
        effective_state_locked=breaker._effective_state_locked,
        notify_if_needed=breaker._notify_if_needed,
        totals=breaker._totals,
        enforce_settled_limits_locked=breaker._enforce_settled_limits_locked,
        trip_locked=breaker._trip_locked,
        refresh_latched_snapshot_locked=breaker._refresh_latched_snapshot_locked,
        sync_emergency_latch=breaker._sync_emergency_latch,
        read_unavailable_sentinel=read_sentinel,
    )
    breaker._operator_controls = OperatorControls(
        enabled=breaker.enabled,
        context=breaker._context,
        connect=connect,
        infrastructure_lock=breaker._infrastructure_lock,
        emergency_latch_path=breaker._emergency_latch_path,
        sync_emergency_latch=breaker._sync_emergency_latch,
        seed_today=breaker._seed_today,
        reconcile_quota_holds_locked=breaker._reconcile_quota_holds_locked,
        effective_state_locked=breaker._effective_state_locked,
        totals=breaker._totals,
        notify_if_needed=breaker._notify_if_needed,
        state_row=breaker._state_row,
        emergency_file_lock=breaker._emergency_file_lock,
        notify_auto_resets_if_needed=breaker._notify_auto_resets_if_needed,
        read_unavailable_sentinel=read_sentinel,
        write_unavailable_sentinel=write_sentinel,
        read_infrastructure_error=read_infra_error,
        write_infrastructure_error=write_infra_error,
    )
