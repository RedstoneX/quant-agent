"""src.cost_circuit.breaker_holds -- thin shims; bodies moved verbatim to src/cost_circuit/parts/quota_holds.py."""
from __future__ import annotations
from src.cost_circuit.parts.quota_holds import QuotaHolds
from src.cost_circuit.parts.shim_guard import _is_class_shim


class _BreakerHoldsMixin:
    def _quota_holds(self) -> QuotaHolds:
        """Thin shim: builds the standalone object from this breaker's collaborators
        (bodies moved to src/cost_circuit/parts/quota_holds.py). Built per call so a collaborator swapped after
        construction is what the body sees."""
        return QuotaHolds(
            config=self.config,
            auto_clear_transient_latch_locked=self._auto_clear_transient_latch_locked,
            scope_key=self._scope_key,
            state_row=self._state_row,
            # Moved bodies too -- same recursion guard as the broker's `_stop_placer`:
            # pass one ONLY when it is NOT this mixin's own shim.
            **{
                kw: getattr(self, attr)
                for kw, attr in (
                    ("hold_quota_locked", "_hold_quota_locked"),
                    ("trip_locked", "_trip_locked"),
                )
                if not _is_class_shim(getattr(self, attr, None), attr, _BreakerHoldsMixin)
            },
        )

    def _reconcile_quota_holds_locked(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/quota_holds.py."""
        return self._quota_holds()._reconcile_quota_holds_locked(*args, **kwargs)

    def _hold_quota_locked(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/quota_holds.py."""
        return self._quota_holds()._hold_quota_locked(*args, **kwargs)

    def _refresh_latched_snapshot_locked(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/quota_holds.py."""
        return self._quota_holds()._refresh_latched_snapshot_locked(*args, **kwargs)

    def _trip_locked(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/quota_holds.py."""
        return self._quota_holds()._trip_locked(*args, **kwargs)
