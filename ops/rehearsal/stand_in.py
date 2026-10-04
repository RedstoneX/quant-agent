"""What the rehearsal's broker stand-ins owe the desk, and the amend endpoint.

Two things live here, both born of one incident. The stand-in trading client
(ops/rehearsal/broker.py) had `submit_order` and `cancel_order_by_id` but no
`replace_order_by_id`. The desk's PREFERRED way to move a protective stop is
an in-place amend through exactly that call (`AlpacaBroker.replace_stop_loss`,
measured against the broker on the rehearsal account on 2026-09-30), and the
desk — correctly — treats any exception from an amend as "no broker answer":
nothing cancelled, nothing moved. So every rehearsal ever run reported
success while the stop ratchet, the mechanism that locks in a gain, had never
once executed. The attribute error was swallowed three layers up and the
report said PASS.

1. `LoudStandIn` — the contract every stand-in now honours. Any attribute the
   desk asks for that the stand-in does not implement is JOURNALLED and raised
   as `StandInGap`. The raise alone is not enough (the desk catches broad
   exceptions on money paths by design), so `assert_stand_in_answered` reads
   the journal after the session and VOIDS the run — exit 2, "the rig could
   not judge it" — the same way a network breach does. There is no allow
   list: a call the stand-in cannot answer is a rig defect, never a soft
   result, and the message names the call so the stand-in can be extended.

2. `AmendEndpoint` — `replace_order_by_id`, modelled on what the 2026-09-30
   measurement recorded: the old order goes to `replaced`, a new id is issued
   at the new price with the same side, qty and time-in-force, and exactly
   one open stop covers the symbol at every instant. An order that is not
   resting is refused the way the broker refuses it (422; nothing changed).

Both are mixins rather than edits to broker.py because that file sits at the
size ratchet's floor and may only shrink.
"""
from __future__ import annotations

import uuid
from dataclasses import replace
from types import SimpleNamespace

# Alpaca's "open" order statuses, plus the rehearsal's own seed marker.
OPEN_STATUSES = frozenset({
    "new", "accepted", "pre_existing", "partially_filled", "pending_new",
    "accepted_for_bidding", "held",
})


class StandInGap(RuntimeError):
    """The desk asked a stand-in for something it does not implement.

    The run is void: whatever the desk did after this point, it did against
    an answer the real broker would not have given.
    """


class LoudStandIn:
    """Mixin: an unimplemented broker call is journalled and raised, never
    silently absent. Dunder and private lookups keep Python's normal
    `AttributeError` so copying, pickling and introspection still work."""

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        journal = self.__dict__.setdefault("unsupported_calls", [])
        journal.append(name)
        raise StandInGap(
            f"rehearsal stand-in {type(self).__name__} does not implement "
            f"`{name}`, which the desk just asked the broker for; the real "
            f"broker would have answered, so this run proves nothing about "
            f"that path — extend ops/rehearsal/stand_in.py or broker.py"
        )


def assert_stand_in_answered(*stand_ins) -> str:
    """Post-session check: every broker call the desk made was answered.

    Returns the isolation-check line on success; raises `StandInGap` naming
    every unanswered call otherwise. Read AFTER the session, because the
    desk swallows the in-flight raise on its money paths by design.
    """
    gaps: list[str] = []
    for stand_in in stand_ins:
        gaps.extend(
            f"{type(stand_in).__name__}.{call}"
            for call in getattr(stand_in, "unsupported_calls", [])
        )
    if gaps:
        raise StandInGap(
            f"{len(gaps)} broker call(s) the stand-in could not answer: "
            + ", ".join(dict.fromkeys(gaps))
            + ". The desk carried on against a non-answer, so no verdict may "
            "be read off this run."
        )
    return "every broker call the desk made was answered by the stand-in"


def status_matches(wanted, actual: str) -> bool:
    """Honour alpaca-py's `GetOrdersRequest(status=...)` the way the broker
    does: OPEN lists resting orders only, CLOSED the rest, ALL/None both."""
    wanted = str(getattr(wanted, "value", wanted) or "all").lower()
    if wanted == "open":
        return actual in OPEN_STATUSES
    if wanted == "closed":
        return actual not in OPEN_STATUSES
    return True


class AmendRefused(RuntimeError):
    """The broker answered: that order cannot be replaced (422)."""

    status_code = 422


class AmendEndpoint:
    """Mixin giving `RehearsalTradingClient` the broker's in-place amend."""

    def get_clock(self):
        """The broker clock at the rehearsal's own `now`: open 09:30-16:00 ET on a weekday."""
        t = self._now
        is_open = t.weekday() < 5 and (9, 30) <= (t.hour, t.minute) < (16, 0)
        return SimpleNamespace(is_open=is_open, timestamp=t)

    def replace_order_by_id(self, order_id, request):
        old = self._orders.get(str(order_id))
        if old is None or old.status not in OPEN_STATUSES:
            raise AmendRefused(
                f"order {order_id} is not resting "
                f"({'unknown' if old is None else old.status}); 422 unprocessable"
            )
        fields = {}
        for name in ("stop_price", "limit_price"):
            value = getattr(request, name, None)
            if value is not None:
                fields[name] = float(value)
        qty = getattr(request, "qty", None)
        if qty is not None:
            fields["qty"] = float(qty)
        new = replace(
            old, order_id=f"rehearsal-{uuid.uuid4().hex[:12]}", status="new",
            submitted_at=self._now, **fields,
        )
        old.status = "replaced"
        self._orders[new.order_id] = new
        self.__dict__.setdefault("amended", []).append(
            (old.order_id, new.order_id, fields)
        )
        return SimpleNamespace(
            id=new.order_id, symbol=new.symbol, status=new.status, qty=new.qty,
            stop_price=new.stop_price, limit_price=new.limit_price,
            replaces=old.order_id,
        )
