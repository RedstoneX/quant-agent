"""Read the book after an in-place amend's replacement came back dead (item 201).

Moved verbatim out of `StopAmender` so that class stays under the file-size
ratchet; `self` is the amender (it supplies `client`,
`_list_open_stop_orders_by_side` and `_snapshot_stop_order`).
"""

from __future__ import annotations

from src.sentinel.guarded import NO_LEDGER, record_guarded_pass
import logging

logger = logging.getLogger("src.execution.broker")


def classify_after_dead_replacement(
    self,
    *,
    symbol: str,
    spec: dict,
    new_price: float,
    leg: dict,
) -> str:
    """Read the book after a replacement came back dead. Never guess.

    ASSUMED, NOT VERIFIED: that a replace moves the original order to
    REPLACED before the replacement is accepted. If that is how it works,
    a rejected or cancelled REPLACEMENT can mean the symbol has NO
    protective stop at all — so "the original is still resting" must be
    read off the broker, not inferred. What would settle the assumption:
    a rehearsal that forces a replacement to be rejected and then lists
    the symbol's open orders.

    Returns "refused" (the original is confirmed still resting),
    "amended" (something IS resting at the new level), "naked" (the book
    shows no protective stop for this symbol) or "unknown" (the book
    could not be read, or shows a level that is neither).
    """
    errors: list = []
    try:
        sells, buys = self._list_open_stop_orders_by_side(symbol, errors=errors)
        live_orders = list(sells or []) + list(buys or [])
        record_guarded_pass(self, "stop_dead_replacement.reread", context={"symbol": symbol})
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(self, "stop_dead_replacement.reread", exc, context={"symbol": symbol})
        leg["detail"] += f"; the book could not be re-read ({exc})"
        return "unknown"
    if errors:
        leg["detail"] += "; the book could not be re-read"
        return "unknown"
    live = [spec_ for spec_ in (self._snapshot_stop_order(o) for o in live_orders) if spec_ is not None]
    if not live:
        # A replacement is also rejected when the ORIGINAL already
        # triggered: the book is then empty because the position is gone,
        # and calling that UNPROTECTED would alert about a flat symbol.
        try:
            held = [
                abs(float(getattr(pos, "qty", 0) or 0))
                for pos in (self.client.get_all_positions() or [])
                if getattr(pos, "symbol", None) == symbol
            ]
            record_guarded_pass(self, "stop_dead_replacement.positions", context={"symbol": symbol})
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(self, "stop_dead_replacement.positions", exc, context={"symbol": symbol})
            leg["detail"] += (
                f"; the book is empty and the position could not be re-read ({exc}) — treating it as UNPROTECTED"
            )
            return "naked"
        if not held or sum(held) <= 0:
            leg["detail"] += (
                "; the book is empty because the position is FLAT — the "
                "original stop most likely filled, so there is nothing "
                "left to protect"
            )
            return "flat"
        # HONEST LIMIT, no settle window: a replace that is still pending
        # at the broker can also present as an empty book for a moment.
        # The two are indistinguishable from one read, so this reports the
        # LOUD direction — a false UNPROTECTED alert costs attention, a
        # missed one costs the position. What would settle it: a rehearsal
        # that lists open orders during a pending replace.
        leg["detail"] += (
            "; the broker shows NO resting protective stop while the "
            "position is still open — UNPROTECTED (a replace still "
            "pending at the broker can look the same from one read)"
        )
        return "naked"
    if any(str(s["id"]) == str(spec.get("id")) for s in live):
        leg["detail"] += "; the ORIGINAL order was read back, still resting"
        return "refused"
    at_new = [s for s in live if abs(s["stop_price"] - new_price) <= 1e-9]
    if at_new:
        leg["new_id"] = at_new[0]["id"]
        leg["detail"] += "; a stop was read back at the NEW level despite the dead replacement"
        return "amended"
    leg["detail"] += "; a stop is resting but at neither the old nor the new level"
    return "unknown"
