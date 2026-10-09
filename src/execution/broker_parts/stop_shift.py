"""Thin shim for the ex-dividend stop shift: the body lives in
`stop_shifter.py` as a separately-constructible part (`StopShifter`).

`StopPlacer` binds `shift_stops_down` below as its method, so its public
surface is unchanged. The part is built PER CALL from the placer's own
collaborators, so a client or cluster method swapped on the placer after
construction is what the body sees -- collaborators are read live, never
snapshotted here. The mixin this file used to hold is gone: a mixin is not a
boundary, because it cannot be built or exercised without its host.
"""

from __future__ import annotations

from src.execution.broker_parts.stop_shifter import StopShifter


def build_stop_shifter(placer) -> StopShifter:
    """Build the part from the placer's live collaborators (one getattr per
    collaborator, per call)."""
    return StopShifter(
        client=placer.client,
        list_open_sell_stop_orders=placer._list_open_sell_stop_orders,
        snapshot_stop_order=placer._snapshot_stop_order,
        stop_order_amendable_in_place=placer._stop_order_amendable_in_place,
        amend_one_stop_price=placer._amend_one_stop_price,
        cancel_snapshotted_stops=placer.cancel_snapshotted_stops,
        restore_stop_orders=placer._restore_stop_orders,
        window_log=placer._window_log,
    )


def shift_stops_down(placer, symbol: str, amount: float) -> dict | None:
    """Thin shim: body moved to src/execution/broker_parts/stop_shifter.py."""
    return build_stop_shifter(placer).shift_stops_down(symbol, amount)


# --- lazy re-export mirror (the ONE allowed block) ---------------------------
# Names that lived in this module before the move; callers that imported them
# from here keep resolving. Resolved on access so the mirror holds no copies.
def __getattr__(name: str):
    if name in {"_quantize_price", "defer_shift_if_closed", "logger"}:
        import src.execution.broker_parts.stop_shifter as _part

        return getattr(_part, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# --- end mirror --------------------------------------------------------------
