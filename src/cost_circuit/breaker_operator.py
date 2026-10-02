"""src.cost_circuit.breaker_operator -- thin shims; bodies moved verbatim to src/cost_circuit/parts/operator_controls.py."""
from __future__ import annotations
from src.cost_circuit.parts.operator_controls import OperatorControls


class _BreakerOperatorMixin:
    def _operator_controls(self) -> OperatorControls:
        """Thin shim: builds the standalone object from this breaker's collaborators
        (bodies moved to src/cost_circuit/parts/operator_controls.py). Built per call so a collaborator swapped after
        construction is what the body sees."""
        return OperatorControls(
            enabled=self.enabled,
            context=self._context,
            connect=self._connect,
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
            read_unavailable_sentinel=lambda: self._unavailable_sentinel,  # read live, not snapshotted
            write_unavailable_sentinel=lambda value: setattr(self, "_unavailable_sentinel", value),
            read_infrastructure_error=lambda: self._infrastructure_error,  # read live, not snapshotted
            write_infrastructure_error=lambda value: setattr(self, "_infrastructure_error", value),
        )

    def status(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/operator_controls.py."""
        return self._operator_controls().status(*args, **kwargs)

    def reset(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/operator_controls.py."""
        return self._operator_controls().reset(*args, **kwargs)
