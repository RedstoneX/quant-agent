"""Thin delegating mixin: keeps every `TradingPipeline._<name>` protection entry
point alive while the logic lives in the standalone `ProtectionService`
(`src/pipeline_protection.py`, conversion step 12).

Each call builds a `ProtectionService` from the host's CURRENT collaborators
(tests build the pipeline via `__new__` and assign `broker`, `db`, `market`,
`config` afterwards, so they are read per call, never snapshotted) and forwards
the call unchanged. Dispatch is through the module-level `_call`, never through
`self.<helper>`: tests bind these methods onto a `MagicMock` host, where any
`self.<helper>` would resolve to a mock child instead of running. The two pieces
of lazily-created state the bodies keep (`_unsettled_exit_orders`, the register
of exit orders still working at the broker, and `_last_stop_clear_refusal`, why
the last stop clear was declined) live on the HOST: the service is handed
`state=self`, so reads and writes go to the pipeline attribute itself, live,
exactly as before the step. A method patched or reassigned on the host (a Mock,
a stub) is installed on the service for the call, so the moved bodies calling
each other still reach the patched one. The nine `@staticmethod`s are the same
function objects, aliased.

External callers that still reach protection through the pipeline (remove an
entry here when its caller takes a `ProtectionService` instead):
`src/pipeline_exits.py`, `src/pipeline_delever.py`, `src/pipeline_intraday.py`,
`src/pipeline_stages.py`, `src/stage_execution.py` and the API/ops scripts.

This module may not import `src.pipeline`: it is one of its bases.
"""

from src.pipeline_protection import ProtectionService
from src.storage.event_journal import DatabaseEventJournal


def _call(owner, name, args, kwargs):
    svc = ProtectionMixin._build(owner)
    # A method patched or reassigned on the host must still be the one the
    # moved bodies reach when they call each other.
    for other, own in vars(ProtectionMixin).items():
        if not callable(own) or other == name or other == "_build":
            continue
        bound = getattr(owner, other, None)
        if bound is not None and getattr(bound, "__func__", None) is not own:
            setattr(svc, other, bound)
    return getattr(svc, name)(*args, **kwargs)


class ProtectionMixin:
    # Tests introspect the exact terminal set through the pipeline class.
    _TERMINAL_ORDER_STATUSES = ProtectionService._TERMINAL_ORDER_STATUSES

    def _build(self) -> ProtectionService:
        """From the host's CURRENT attributes. The four pipeline-defined helpers
        are read plainly (every host defines them); the collaborators tests
        assign after `__new__` are read with a None default, as the bodies did."""
        g = lambda name: getattr(self, name, None)  # noqa: E731
        return ProtectionService(
            broker=g("broker"), db=g("db"), journal=DatabaseEventJournal(g("db")),
            market=g("market"), config=g("config"),
            format_qty=self._format_qty,
            record_exit_refusal=self._record_exit_refusal,
            sweeper=self._sweeper,
            retired_cash_park_symbol=self._retired_cash_park_symbol,
            state=self,
        )

    _alert_owner_elected_unfilled = staticmethod(ProtectionService._alert_owner_elected_unfilled)
    _alert_owner_repair_resolved = staticmethod(ProtectionService._alert_owner_repair_resolved)
    _alert_owner_no_stop = staticmethod(ProtectionService._alert_owner_no_stop)
    _alert_owner_stop_pending_acceptance = staticmethod(ProtectionService._alert_owner_stop_pending_acceptance)
    _alert_owner_unreadable_stop = staticmethod(ProtectionService._alert_owner_unreadable_stop)
    _alert_owner_exit_declined = staticmethod(ProtectionService._alert_owner_exit_declined)
    _alert_owner_kill_switch_blocked = staticmethod(ProtectionService._alert_owner_kill_switch_blocked)
    _order_accepted = staticmethod(ProtectionService._order_accepted)
    _parse_broker_fill_timestamp = staticmethod(ProtectionService._parse_broker_fill_timestamp)

    def _current_position_qty_for_finalize(self, *args, **kwargs):
        return _call(self, '_current_position_qty_for_finalize', args, kwargs)

    def _reconcile_stop_coverage(self, *args, **kwargs):
        return _call(self, '_reconcile_stop_coverage', args, kwargs)

    def _elected_unfilled_stop_row(self, *args, **kwargs):
        return _call(self, '_elected_unfilled_stop_row', args, kwargs)

    def _still_uncovered(self, *args, **kwargs):
        return _call(self, '_still_uncovered', args, kwargs)

    def _alert_owner_session_repair_failed(self, *args, **kwargs):
        return _call(self, '_alert_owner_session_repair_failed', args, kwargs)

    def _wire_protective_stop_block_recorder(self, *args, **kwargs):
        return _call(self, '_wire_protective_stop_block_recorder', args, kwargs)

    def _repair_stop_coverage(self, *args, **kwargs):
        return _call(self, '_repair_stop_coverage', args, kwargs)

    def _submit_protected_sell(self, *args, **kwargs):
        return _call(self, '_submit_protected_sell', args, kwargs)

    def _register_exit_settlement(self, *args, **kwargs):
        return _call(self, '_register_exit_settlement', args, kwargs)

    def _open_exit_relief(self, *args, **kwargs):
        return _call(self, '_open_exit_relief', args, kwargs)

    def _finalize_pending_protections(self, *args, **kwargs):
        return _call(self, '_finalize_pending_protections', args, kwargs)

    def _finalize_protection_after_sell(self, *args, **kwargs):
        return _call(self, '_finalize_protection_after_sell', args, kwargs)

    def _finalize_protection_after_sell_core(self, *args, **kwargs):
        return _call(self, '_finalize_protection_after_sell_core', args, kwargs)

    def _cancel_stray_stops_on_flat(self, *args, **kwargs):
        return _call(self, '_cancel_stray_stops_on_flat', args, kwargs)

    def _write_ahead_protection_restore(self, *args, **kwargs):
        return _call(self, '_write_ahead_protection_restore', args, kwargs)

    def _cancel_stops_with_write_ahead(self, *args, **kwargs):
        return _call(self, '_cancel_stops_with_write_ahead', args, kwargs)

    def _restore_after_unconfirmed_sell(self, *args, **kwargs):
        return _call(self, '_restore_after_unconfirmed_sell', args, kwargs)

    def _persist_orphaned_protection_restore(self, *args, **kwargs):
        return _call(self, '_persist_orphaned_protection_restore', args, kwargs)

    def _derive_close_side_for_drain(self, *args, **kwargs):
        return _call(self, '_derive_close_side_for_drain', args, kwargs)

    def _resolve_wal_row_side(self, *args, **kwargs):
        return _call(self, '_resolve_wal_row_side', args, kwargs)

    def _drain_pending_repegs(self, *args, **kwargs):
        return _call(self, '_drain_pending_repegs', args, kwargs)

    def _delete_repeg_row(self, *args, **kwargs):
        return _call(self, '_delete_repeg_row', args, kwargs)

    def _drain_pending_protection_restores(self, *args, **kwargs):
        return _call(self, '_drain_pending_protection_restores', args, kwargs)

    def _reprotect_residual_after_partial_sell(self, *args, **kwargs):
        return _call(self, '_reprotect_residual_after_partial_sell', args, kwargs)

    def _record_reprotect_identity_gap(self, *args, **kwargs):
        return _call(self, '_record_reprotect_identity_gap', args, kwargs)

    def _alert_owner_reprotect_left_naked(self, *args, **kwargs):
        return _call(self, '_alert_owner_reprotect_left_naked', args, kwargs)

    def _reconcile_fills(self, *args, **kwargs):
        return _call(self, '_reconcile_fills', args, kwargs)

    def _reconcile_orphan_pending_submits(self, *args, **kwargs):
        return _call(self, '_reconcile_orphan_pending_submits', args, kwargs)

    def _flag_stop_out_anomaly(self, *args, **kwargs):
        return _call(self, '_flag_stop_out_anomaly', args, kwargs)

    def _reconcile_stop_out_fills(self, *args, **kwargs):
        return _call(self, '_reconcile_stop_out_fills', args, kwargs)

    def _surface_reconcile_outcomes(self, *args, **kwargs):
        return _call(self, '_surface_reconcile_outcomes', args, kwargs)

    def _handle_ex_dividends(self, *args, **kwargs):
        return _call(self, '_handle_ex_dividends', args, kwargs)


# `inspect.getsource`/`inspect.signature` follow `__wrapped__`, so a test that
# reads the source of a pipeline entry point still reads the moved body.
for _name, _fn in vars(ProtectionMixin).items():
    if callable(_fn) and _name != "_build" and not isinstance(_fn, staticmethod):
        _fn.__wrapped__ = getattr(ProtectionService, _name)
del _name, _fn
