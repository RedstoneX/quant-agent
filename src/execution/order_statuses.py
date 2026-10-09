"""The protective-order status vocabulary: three questions, three sets.

A leaf — imports nothing from this package — so the stop placer, the
idempotent submit path and the broker facade can all read ONE definition
(`src.execution.broker` and `broker_parts.stop_place` re-export every name).
"""

from __future__ import annotations

#: Broker order states in which a resting order is REAL protection.
#:
#: One definition, two readers. `AlpacaBroker`'s scale-in reprotect used a
#: local literal set; `TradingPipeline._finalize_protection_restore` needed
#: exactly the same judgement and, on 2026-09-30, was given a second copy of
#: the same four strings. Two copies of one rule is how this desk ended up
#: with four different spellings of "a trigger was named", so they are one
#: name now. Anything outside this set -- notably `pending_cancel`, which is
#: what a just-cancelled stop still reports for a moment -- is a dying order
#: and must never be counted as a stop that protects the position.
PROTECTIVE_ORDER_ACTIVE_STATUSES = frozenset({"new", "accepted", "held", "partially_filled"})

#: Broker order states in which an order has been ACCEPTED BY US to the
#: broker but is not yet working on the book. Alpaca's own enum names them:
#: `pending_new` (received, not yet routed) and `accepted_for_bidding`.
#:
#: These are deliberately NOT in the set above. That set answers "is this
#: resting order real protection right now?" and its reader
#: (`replace_stop_loss`'s failure path) is looking at the AGED order book —
#: an order that has been sitting there and is still `pending_new` is a
#: stuck order, not coverage.
PROTECTIVE_ORDER_PLACEMENT_PENDING_STATUSES = frozenset({"pending_new", "accepted_for_bidding"})

#: The THIRD question — "does this order still hold the shares?" — asked only
#: by the idempotent stop-submit path; rationale for each extra member in
#: src/execution/order_idempotency.py (`_submit_stop_request_idempotent`).
PROTECTIVE_ORDER_HOLDS_SHARES_STATUSES = (
    PROTECTIVE_ORDER_ACTIVE_STATUSES
    | PROTECTIVE_ORDER_PLACEMENT_PENDING_STATUSES
    | frozenset({"pending_replace", "stopped", "calculated", "filled"})
)
