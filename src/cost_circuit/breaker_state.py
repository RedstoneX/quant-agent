"""src.cost_circuit.breaker_state -- thin shims; bodies moved verbatim to src/cost_circuit/parts/circuit_state.py."""
from __future__ import annotations
from src.cost_circuit.parts.circuit_state import CircuitState
from src.cost_circuit.parts.shim_guard import _is_class_shim


class _BreakerStateMixin:
    def _circuit_state(self) -> CircuitState:
        """Thin shim: builds the standalone object from this breaker's collaborators
        (bodies moved to src/cost_circuit/parts/circuit_state.py). Built per call so a collaborator swapped after
        construction is what the body sees."""
        return CircuitState(
            config=self.config,
            context=self._context,
            emergency_latch_path=self._emergency_latch_path,
            # Moved bodies too -- same recursion guard as the broker's `_stop_placer`:
            # pass one ONLY when it is NOT this mixin's own shim.
            **{
                kw: getattr(self, attr)
                for kw, attr in (
                    ("totals", "_totals"),
                    ("state_row", "_state_row"),
                    ("active_quota_hold_locked", "_active_quota_hold_locked"),
                )
                if not _is_class_shim(getattr(self, attr, None), attr, _BreakerStateMixin)
            },
        )

    @staticmethod
    def _totals(*args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/circuit_state.py."""
        return CircuitState._totals(*args, **kwargs)

    def _state_row(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/circuit_state.py."""
        return self._circuit_state()._state_row(*args, **kwargs)

    @staticmethod
    def _scope_key(*args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/circuit_state.py."""
        return CircuitState._scope_key(*args, **kwargs)

    def _active_quota_hold_locked(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/circuit_state.py."""
        return self._circuit_state()._active_quota_hold_locked(*args, **kwargs)

    def _effective_state_locked(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/circuit_state.py."""
        return self._circuit_state()._effective_state_locked(*args, **kwargs)

    def _auto_clear_transient_latch_locked(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/circuit_state.py."""
        return self._circuit_state()._auto_clear_transient_latch_locked(*args, **kwargs)
