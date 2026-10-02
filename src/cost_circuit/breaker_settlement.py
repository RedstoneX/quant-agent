"""src.cost_circuit.breaker_settlement -- thin shims; bodies moved verbatim to src/cost_circuit/parts/settlement.py."""
from __future__ import annotations
from src.cost_circuit.parts.settlement import Settlement


class _BreakerSettlementMixin:
    def _settlement(self) -> Settlement:
        """Thin shim: builds the standalone object from this breaker's collaborators
        (bodies moved to src/cost_circuit/parts/settlement.py). Built per call so a collaborator swapped after
        construction is what the body sees."""
        return Settlement(
            config=self.config,
            enabled=self.enabled,
            connect=self._connect,
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
            read_unavailable_sentinel=lambda: self._unavailable_sentinel,  # read live, not snapshotted
        )

    def before_provider_attempt(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/settlement.py."""
        return self._settlement().before_provider_attempt(*args, **kwargs)

    def complete_call(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/settlement.py."""
        return self._settlement().complete_call(*args, **kwargs)

    def fail_call(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/settlement.py."""
        return self._settlement().fail_call(*args, **kwargs)
