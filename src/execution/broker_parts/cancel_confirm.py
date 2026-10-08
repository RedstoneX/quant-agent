"""Read whether cancelled protective stops really reached a terminal state.

Lifted verbatim from src/execution/scale_in.py so the stop invariant
(src/execution/broker_parts/stop_invariant.py) can confirm its cancels
without importing scale_in, whose import chain reaches broker.py and would
close an import cycle through stop_place -> stop_invariant. scale_in
re-exports every name here, so existing callers and patch targets hold.
Leaf module: imports nothing from the broker.
"""

from __future__ import annotations

from typing import Any

from src.execution.scale_in_loud import record_scale_in

#: Terminal statuses that mean the protective sell is gone and did not
#: sell shares. `filled` is deliberately NOT here: a stop that fires
#: during cancel is a real exit and the BUY add must not proceed.
_CANCEL_CONFIRMED = frozenset({
    "canceled", "cancelled", "expired", "rejected", "replaced",
})
_STOP_FILLED = frozenset({"filled"})


def cancelled_stop_specs(specs: list[dict] | None) -> list[dict]:
    """Specs that were actually at the broker (have an id)."""
    out: list[dict] = []
    for spec in specs or []:
        if spec.get("id"):
            out.append(spec)
    return out


def _confirm_cancels_status(broker: Any, specs: list[dict]) -> tuple[str, str]:
    """Terminal state of the cancelled protective stops, as a status token.

    Returns ``(status, detail)`` where status is one of:

      * ``"confirmed"`` — every spec with an id reached a cancelled-like
        terminal state; the add may proceed.
      * ``"filled"``    — a protective stop FIRED during the cancel. The
        position was EXITED. A long's sell-stop firing sold the long; a
        short's buy-stop firing COVERED the short. Either way the add must
        abort, and a short must NOT restore a stop onto a now-flat name.
      * ``"unconfirmed"`` — no id to confirm, the wait raised, or the broker
        did not report a cancelled-like terminal state in the window.

    Wait is `wait_for_order_terminal` (websocket-first with the fill stream
    on since 2026-09-18, bounded REST otherwise) — unchanged either way.
    """
    for spec in cancelled_stop_specs(specs):
        order_id = str(spec.get("id") or "")
        if not order_id:
            return "unconfirmed", "a cancelled stop had no id to confirm"
        try:
            status = broker.wait_for_order_terminal(order_id)
        except Exception as exc:  # noqa: BLE001
            record_scale_in(broker, "confirm_cancels", exc, order=order_id)
            return "unconfirmed", f"cancel confirm raised for {order_id}: {exc}"
        else:
            record_scale_in(broker, "confirm_cancels")
        status = str(status or "").lower()
        if status in _STOP_FILLED:
            return "filled", (
                f"protective stop {order_id} FILLED during cancel — "
                "the add is aborted rather than adding into an exit"
            )
        if status not in _CANCEL_CONFIRMED:
            return "unconfirmed", (
                f"protective stop {order_id} not confirmed cancelled "
                f"(status={status or 'unknown'})"
            )
    return "confirmed", ""
