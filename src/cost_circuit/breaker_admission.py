"""src.cost_circuit.breaker_admission -- thin shims; bodies moved verbatim to src/cost_circuit/parts/admission.py."""
from __future__ import annotations
from src.cost_circuit.parts.admission import Admission
from src.cost_circuit.parts.shim_guard import _is_class_shim


class _BreakerAdmissionMixin:
    def _admission(self) -> Admission:
        """Thin shim: builds the standalone object from this breaker's collaborators
        (bodies moved to src/cost_circuit/parts/admission.py). Built per call so a collaborator swapped after
        construction is what the body sees."""
        return Admission(
            config=self.config,
            enabled=self.enabled,
            connect=self._connect,
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
            read_unavailable_sentinel=lambda: self._unavailable_sentinel,  # read live, not snapshotted
            # Moved bodies too -- same recursion guard as the broker's `_stop_placer`:
            # pass one ONLY when it is NOT this mixin's own shim.
            **{
                kw: getattr(self, attr)
                for kw, attr in (
                    ("enforce_settled_limits_locked", "_enforce_settled_limits_locked"),
                    ("enforce_current_limits", "enforce_current_limits"),
                )
                if not _is_class_shim(getattr(self, attr, None), attr, _BreakerAdmissionMixin)
            },
        )

    def _enforce_settled_limits_locked(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/admission.py."""
        return self._admission()._enforce_settled_limits_locked(*args, **kwargs)

    def enforce_current_limits(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/admission.py."""
        return self._admission().enforce_current_limits(*args, **kwargs)

    def require_paid_analysis(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/admission.py."""
        return self._admission().require_paid_analysis(*args, **kwargs)

    def begin_call(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/admission.py."""
        return self._admission().begin_call(*args, **kwargs)
