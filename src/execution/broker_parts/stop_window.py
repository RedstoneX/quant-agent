"""Item 201: the brief window in which a cancel+resubmit leaves a position with
NO protective stop, made visible. Only the genuinely un-amendable paths still
open one; each is timed, given a reason, and written as a durable row.
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("src.execution.broker")

#: One row per cancel+resubmit that left a live position without a stop.
STOP_UNPROTECTED_WINDOW_KIND = "stop_unprotected_window"


def fallback_reason(stop_specs: list[dict], position_qty: float | None) -> str:
    """WHY a replace had to cancel and resubmit, as one stable word."""
    try:
        qtys = [abs(float(s["qty"])) for s in stop_specs]
    except (TypeError, ValueError, KeyError):
        return "stop_unreadable"
    if position_qty is None:
        return "position_unread"
    if abs(sum(qtys) - position_qty) <= 1e-9:
        return "unamendable_shape"
    if any(q != int(q) for q in qtys) or position_qty != int(position_qty):
        return "fractional_quantity_change"  # broker refuses it (2026-09-30)
    if len(qtys) > 1:
        return "lot_consolidation"  # a design choice, not a broker limit
    return "quantity_change_not_amendable"


class UnprotectedWindow:
    """Times one cancel+resubmit and appends its facts to `sink` on close."""

    def __init__(self, symbol: str, reason: str, sink: list,
                 path: str = "replace_stop_loss", **facts):
        self.symbol, self.reason, self.sink = symbol, reason, sink
        self.path, self.facts = path, facts  # e.g. exposed_qty, leg_kind
        self.cancelled_ids: list[str] = []
        self._start = time.monotonic()

    def cancelled(self, order_id: Any) -> None:
        self.cancelled_ids.append(str(order_id))

    def close(self, outcome: str) -> None:
        if not self.cancelled_ids:
            return
        seconds = round(time.monotonic() - self._start, 3)
        self.sink.append({"symbol": self.symbol, "reason": self.reason,
                          "cancelled_ids": list(self.cancelled_ids), "outcome": outcome,
                          "window_seconds": seconds, "path": self.path, **self.facts})
        logger.warning(
            "%s: %s was WITHOUT a protective stop for %.3fs "
            "(reason=%s, outcome=%s, cancelled=%s)",
            self.path, self.symbol, seconds, self.reason, outcome,
            self.cancelled_ids)


#: Payload field saying WHERE the row's run id came from. A window recorded
#: without a session id used to be indistinguishable from one recorded with
#: it (`_insert` substitutes `unattributed` for a null), so no window could be
#: joined to the session that produced it. This field makes the difference
#: readable instead of silent.
RUN_ID_SOURCE_FIELD = "run_id_source"
#: `run_id_source` when the calling session supplied its own id — the row is
#: joinable to that session.
RUN_ID_FROM_SESSION = "session"
#: `run_id_source` when the call site genuinely carries no session id. The
#: `caller` field in the same payload says which site.
RUN_ID_UNATTRIBUTED = "unattributed"


def record_unprotected_windows(broker: Any, db: Any, symbol: str, *,
                               run_id: str | None = None,
                               caller: str = "unknown") -> None:
    """Persist every window the broker reports, whatever the replace's outcome.

    A failed replace is exactly where the window mattered most. A record is
    never trading authority, so nothing here can raise.

    `run_id` is the calling session's id, so the window can be joined to the
    session that produced it. A site that has none is recorded honestly: the
    payload says `run_id_source=unattributed` and names the `caller`.
    """
    log = getattr(broker, "_unprotected_windows", None)
    if not isinstance(log, list) or not log:
        return
    windows, log[:] = list(log), []
    try:
        from src.execution.exit_path_records import _insert
        session = str(run_id or "").strip()
        source = RUN_ID_FROM_SESSION if session else RUN_ID_UNATTRIBUTED
        for w in windows:
            _insert(db, run_id=session or None, kind=STOP_UNPROTECTED_WINDOW_KIND,
                    symbol=symbol,
                    payload={"code": f"stop_window_{w['outcome']}", **w,
                             RUN_ID_SOURCE_FIELD: source, "caller": str(caller)})
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not record unprotected stop window for %s: %s", symbol, exc)
