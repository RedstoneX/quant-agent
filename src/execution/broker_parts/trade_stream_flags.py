"""Loud set/clear of the reconnect guard's auth and connected events, split out of trade_stream.py.

Each call that fails logs a full traceback at ERROR. No ledger handle is in
reach inside the reconnect guard (it is built from the bare stream object), so
`record_guarded_pass` is lent no owner and writes no counted row.
"""

from __future__ import annotations

import logging

from src.sentinel.guarded import record_guarded_pass

logger = logging.getLogger(__name__)


def _signal_event(stream: object, attr: str, method: str, owner=None) -> None:
    """Call `stream.<attr>.<method>()` when the event exists; never raises."""
    event = getattr(stream, attr, None)
    if event is None:
        return
    where = f"trade_stream.reconnect.{attr.removeprefix('_qamc_')}_{method}"
    try:
        getattr(event, method)()
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(owner, where, exc, log=logger, context={"effect": "auth/connected flag left stale"})
    else:
        record_guarded_pass(owner, where, context={})
