"""Factories that build the lifted protection objects from a host pipeline's
collaborators, moved out of `pipeline_protection.py`.

Each takes any `host` carrying the named attributes, so a SimpleNamespace is
enough to construct and exercise them without the pipeline. The bodies are
unchanged; the local imports of the built classes predate this move.
"""

from __future__ import annotations


def _collab_of(obj, name: str):
    """A collaborator for a lifted protection object: `obj._collab(name)` when the host's
    CLASS defines the deferred-error stand-in (TradingPipeline does), else the plain
    attribute -- so a MagicMock host hands over its own `db`/`broker`, not a child mock."""
    collab = getattr(type(obj), "_collab", None)
    if collab is not None:
        return collab(obj, name)
    return getattr(obj, name)


def _build_owner_alerts(host):
    """Builds the standalone OwnerAlerts from the host pipeline's collaborators (bodies moved to src/protection/owner_alerts.py)."""
    from src.protection.owner_alerts import OwnerAlerts

    return OwnerAlerts(
        broker=_collab_of(host, "broker"),
        still_uncovered=_collab_of(host, "_still_uncovered"),
        alert_owner_no_stop=_collab_of(host, "_alert_owner_no_stop"),
        format_qty=_collab_of(host, "_format_qty"),
    )


def _build_sell_finalization(host):
    """Builds the standalone SellFinalization from the host pipeline's collaborators (bodies moved to src/protection/sell_finalization.py)."""
    from src.protection.sell_finalization import SellFinalization

    return SellFinalization(
        broker=_collab_of(host, "broker"),
        db=_collab_of(host, "db"),
        terminal_order_statuses=host._TERMINAL_ORDER_STATUSES,
        finalize_protection_after_sell=_collab_of(host, "_finalize_protection_after_sell"),
        register_exit_settlement=_collab_of(host, "_register_exit_settlement"),
        finalize_protection_after_sell_core=_collab_of(host, "_finalize_protection_after_sell_core"),
        cancel_stray_stops_on_flat=_collab_of(host, "_cancel_stray_stops_on_flat"),
        current_position_qty_for_finalize=_collab_of(host, "_current_position_qty_for_finalize"),
        persist_orphaned_protection_restore=_collab_of(host, "_persist_orphaned_protection_restore"),
        reprotect_residual_after_partial_sell=_collab_of(host, "_reprotect_residual_after_partial_sell"),
        derive_close_side_for_drain=_collab_of(host, "_derive_close_side_for_drain"),
    )


def _build_fill_reconciler(host):
    """Builds the standalone FillReconciler from the host pipeline's collaborators (bodies moved to src/protection/fill_reconciler.py)."""
    from src.protection.fill_reconciler import FillReconciler

    return FillReconciler(
        broker=_collab_of(host, "broker"),
        db=_collab_of(host, "db"),
        config=getattr(host, "config", None),
        flag_stop_out_anomaly=_collab_of(host, "_flag_stop_out_anomaly"),
        format_qty=_collab_of(host, "_format_qty"),
        parse_broker_fill_timestamp=_collab_of(host, "_parse_broker_fill_timestamp"),
    )


def _build_repeg_drain(host):
    """Builds the standalone RepegDrain from the host pipeline's collaborators (bodies moved to src/protection/repeg_drain.py)."""
    from src.protection.repeg_drain import RepegDrain

    return RepegDrain(
        broker=_collab_of(host, "broker"),
        db=_collab_of(host, "db"),
        delete_repeg_row=_collab_of(host, "_delete_repeg_row"),
    )


def _build_coverage_election(host):
    """Builds the standalone CoverageElection from the host pipeline's collaborators (bodies moved to src/protection/coverage_election.py)."""
    from src.protection.coverage_election import CoverageElection

    return CoverageElection()
