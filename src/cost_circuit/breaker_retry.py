"""src.cost_circuit.breaker_retry -- thin shims; bodies moved verbatim to src/cost_circuit/parts/infra_retry.py."""
from __future__ import annotations
from src.cost_circuit.parts.infra_retry import InfraRetry
from src.cost_circuit.parts.shim_guard import _is_class_shim


class _BreakerRetryMixin:
    def _infra_retry(self) -> InfraRetry:
        """Thin shim: builds the standalone object from this breaker's collaborators
        (bodies moved to src/cost_circuit/parts/infra_retry.py). Built per call so a collaborator swapped after
        construction is what the body sees."""
        return InfraRetry(
            config=self.config,
            context=self._context,
            notifier=self.notifier,
            infrastructure_lock=self._infrastructure_lock,
            emergency_latch_path=self._emergency_latch_path,
            emergency_lock_path=self._emergency_lock_path,
            best_effort_emergency_snapshot=self._best_effort_emergency_snapshot,
            write_emergency_latch=self._write_emergency_latch,
            sync_emergency_latch=self._sync_emergency_latch,
            read_unavailable_sentinel=lambda: self._unavailable_sentinel,  # read live, not snapshotted
            write_unavailable_sentinel=lambda value: setattr(self, "_unavailable_sentinel", value),
            read_infrastructure_error=lambda: self._infrastructure_error,  # read live, not snapshotted
            write_infrastructure_error=lambda value: setattr(self, "_infrastructure_error", value),
            # Moved bodies too -- same recursion guard as the broker's `_stop_placer`:
            # pass one ONLY when it is NOT this mixin's own shim.
            **{
                kw: getattr(self, attr)
                for kw, attr in (
                    ("infra_retry_backoff_s", "_infra_retry_backoff_s"),
                    ("mark_unavailable", "mark_unavailable"),
                )
                if not _is_class_shim(getattr(self, attr, None), attr, _BreakerRetryMixin)
            },
        )

    def _infra_retry_backoff_s(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/infra_retry.py."""
        return self._infra_retry()._infra_retry_backoff_s(*args, **kwargs)

    def _run_with_infra_retry(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/infra_retry.py."""
        return self._infra_retry()._run_with_infra_retry(*args, **kwargs)

    def mark_unavailable(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/infra_retry.py."""
        return self._infra_retry().mark_unavailable(*args, **kwargs)

    def _raise_if_unavailable(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/infra_retry.py."""
        return self._infra_retry()._raise_if_unavailable(*args, **kwargs)
