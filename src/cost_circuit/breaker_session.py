"""src.cost_circuit.breaker_session -- thin shims; bodies moved verbatim to src/cost_circuit/parts/session_lifecycle.py."""
from __future__ import annotations
from src.cost_circuit.parts.session_lifecycle import SessionLifecycle
from src.cost_circuit.parts.shim_guard import _is_class_shim


class _BreakerSessionMixin:
    def _session_lifecycle(self) -> SessionLifecycle:
        """Thin shim: builds the standalone object from this breaker's collaborators
        (bodies moved to src/cost_circuit/parts/session_lifecycle.py). Built per call so a collaborator swapped after
        construction is what the body sees."""
        return SessionLifecycle(
            enabled=self.enabled,
            connect=self._connect,
            session_context=self._session_context,
            infrastructure_lock=self._infrastructure_lock,
            sync_emergency_latch=self._sync_emergency_latch,
            reconcile_quota_holds_locked=self._reconcile_quota_holds_locked,
            run_with_infra_retry=self._run_with_infra_retry,
            notify_if_needed=self._notify_if_needed,
            enforce_current_limits=self.enforce_current_limits,
            status=self.status,
            read_unavailable_sentinel=lambda: self._unavailable_sentinel,  # read live, not snapshotted
            # Moved bodies too -- same recursion guard as the broker's `_stop_placer`:
            # pass one ONLY when it is NOT this mixin's own shim.
            **{
                kw: getattr(self, attr)
                for kw, attr in (
                    ("seed_today", "_seed_today"),
                    ("validate_accounting_invariants", "_validate_accounting_invariants"),
                )
                if not _is_class_shim(getattr(self, attr, None), attr, _BreakerSessionMixin)
            },
        )

    def _initialize(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/session_lifecycle.py."""
        return self._session_lifecycle()._initialize(*args, **kwargs)

    def _validate_accounting_invariants(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/session_lifecycle.py."""
        return self._session_lifecycle()._validate_accounting_invariants(*args, **kwargs)

    def _seed_today(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/session_lifecycle.py."""
        return self._session_lifecycle()._seed_today(*args, **kwargs)

    def activate_session(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/session_lifecycle.py."""
        return self._session_lifecycle().activate_session(*args, **kwargs)

    def set_session_context(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/session_lifecycle.py."""
        return self._session_lifecycle().set_session_context(*args, **kwargs)

    def _context(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/session_lifecycle.py."""
        return self._session_lifecycle()._context(*args, **kwargs)
