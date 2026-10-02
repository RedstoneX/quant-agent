"""src.cost_circuit.breaker_latch -- thin shims; bodies moved verbatim to src/cost_circuit/parts/emergency_latch.py."""
from __future__ import annotations
from src.cost_circuit.parts.emergency_latch import EmergencyLatch
from src.cost_circuit.parts.shim_guard import _is_class_shim


class _BreakerLatchMixin:
    def _emergency_latch(self) -> EmergencyLatch:
        """Thin shim: builds the standalone object from this breaker's collaborators
        (bodies moved to src/cost_circuit/parts/emergency_latch.py). Built per call so a collaborator swapped after
        construction is what the body sees."""
        return EmergencyLatch(
            connect=self._connect,
            notifier=self.notifier,
            infrastructure_lock=self._infrastructure_lock,
            emergency_latch_path=self._emergency_latch_path,
            emergency_lock_path=self._emergency_lock_path,
            read_unavailable_sentinel=lambda: self._unavailable_sentinel,  # read live, not snapshotted
            write_unavailable_sentinel=lambda value: setattr(self, "_unavailable_sentinel", value),
            read_infrastructure_error=lambda: self._infrastructure_error,  # read live, not snapshotted
            write_infrastructure_error=lambda value: setattr(self, "_infrastructure_error", value),
            # Moved bodies too -- same recursion guard as the broker's `_stop_placer`:
            # pass one ONLY when it is NOT this mixin's own shim.
            **{
                kw: getattr(self, attr)
                for kw, attr in (
                    ("read_emergency_latch", "_read_emergency_latch"),
                    ("emergency_file_lock", "_emergency_file_lock"),
                )
                if not _is_class_shim(getattr(self, attr, None), attr, _BreakerLatchMixin)
            },
        )

    def _best_effort_emergency_snapshot(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/emergency_latch.py."""
        return self._emergency_latch()._best_effort_emergency_snapshot(*args, **kwargs)

    def _read_emergency_latch(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/emergency_latch.py."""
        return self._emergency_latch()._read_emergency_latch(*args, **kwargs)

    def _sync_emergency_latch(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/emergency_latch.py."""
        return self._emergency_latch()._sync_emergency_latch(*args, **kwargs)

    @staticmethod
    def _safe_optional_int(*args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/emergency_latch.py."""
        return EmergencyLatch._safe_optional_int(*args, **kwargs)

    @staticmethod
    def _safe_optional_float(*args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/emergency_latch.py."""
        return EmergencyLatch._safe_optional_float(*args, **kwargs)

    def _emergency_file_lock(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/emergency_latch.py."""
        return self._emergency_latch()._emergency_file_lock(*args, **kwargs)

    def _write_emergency_latch(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/emergency_latch.py."""
        return self._emergency_latch()._write_emergency_latch(*args, **kwargs)
