"""src.cost_circuit.breaker_notify -- thin shims; bodies moved verbatim to src/cost_circuit/parts/owner_notify.py."""
from __future__ import annotations
from src.cost_circuit.parts.owner_notify import OwnerNotify
from src.cost_circuit.parts.shim_guard import _is_class_shim


class _BreakerNotifyMixin:
    def _owner_notify(self) -> OwnerNotify:
        """Thin shim: builds the standalone object from this breaker's collaborators
        (bodies moved to src/cost_circuit/parts/owner_notify.py). Built per call so a collaborator swapped after
        construction is what the body sees."""
        return OwnerNotify(
            enabled=self.enabled,
            infrastructure_lock=self._infrastructure_lock,
            unavailable_sentinel=self._unavailable_sentinel,
            connect=self._connect,
            refresh_latched_snapshot_locked=self._refresh_latched_snapshot_locked,
            state_row=self._state_row,
            notifier=self.notifier,
            episode_already_paged_locked=self._episode_already_paged_locked,
            suspension_still_inside_self_clear_window_locked=self._suspension_still_inside_self_clear_window_locked,
            record_suspension_deferral_locked=self._record_suspension_deferral_locked,
            episode_facts_locked=self._episode_facts_locked,
            format_alert=self.format_alert,
            format_quota_alert=self.format_quota_alert,
            format_recovery_alert=self.format_recovery_alert,
            format_auto_reset_alert=self.format_auto_reset_alert,
            # These three are moved bodies too -- same recursion guard as the broker's
            # `_stop_placer`: pass one ONLY when it is NOT this mixin's own shim.
            **{
                kw: getattr(self, attr)
                for kw, attr in (
                    ("notify_quota_holds_if_needed", "_notify_quota_holds_if_needed"),
                    ("notify_quota_recoveries_if_needed", "_notify_quota_recoveries_if_needed"),
                    ("notify_auto_resets_if_needed", "_notify_auto_resets_if_needed"),
                )
                if not _is_class_shim(getattr(self, attr, None), attr, _BreakerNotifyMixin)
            },
        )

    def _notify_if_needed(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/owner_notify.py."""
        return self._owner_notify()._notify_if_needed(*args, **kwargs)

    def _notify_quota_holds_if_needed(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/owner_notify.py."""
        return self._owner_notify()._notify_quota_holds_if_needed(*args, **kwargs)

    def _notify_quota_recoveries_if_needed(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/owner_notify.py."""
        return self._owner_notify()._notify_quota_recoveries_if_needed(*args, **kwargs)

    def _notify_auto_resets_if_needed(self, *args, **kwargs):
        """Thin shim: body moved to src/cost_circuit/parts/owner_notify.py."""
        return self._owner_notify()._notify_auto_resets_if_needed(*args, **kwargs)
