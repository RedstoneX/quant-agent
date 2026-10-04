"""The broker's in-place amend endpoint, for the rehearsal stand-in.

`RehearsalTradingClient` (ops/rehearsal/broker.py) has `submit_order` and
`cancel_order_by_id` but no `replace_order_by_id`, so the production
trailing path's PREFERRED route — amend the resting stop's price in place
(`AlpacaBroker.replace_stop_loss`, measured against the broker on the
rehearsal account on 2026-09-30) — ends every rehearsal with "no broker
answer" and leaves the old stop where it was. This gives the stand-in that
endpoint, modelled on what the measurement recorded: the old order goes to
`replaced`, a new id is issued at the new price with the same side, qty and
time-in-force, and exactly one open stop covers the symbol at every
instant. An order that is not resting is refused the way the broker
refuses it (an error, nothing changed).

Attached from the outside (`give_amend_endpoint`) rather than written into
the client, because that file is over the size floor the ratchet enforces.
"""
from __future__ import annotations

import types
import uuid
from dataclasses import replace

RESTING = ("new", "accepted", "pre_existing")


class AmendRefused(RuntimeError):
    """The broker answered: that order cannot be replaced."""


def _replace_order_by_id(self, order_id, request):
    old = self._orders.get(str(order_id))
    if old is None or old.status not in RESTING:
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
    self.amended.append((old.order_id, new.order_id, fields))
    return types.SimpleNamespace(
        id=new.order_id, symbol=new.symbol, status=new.status, qty=new.qty,
        stop_price=new.stop_price, limit_price=new.limit_price,
    )


def give_amend_endpoint(trading):
    """Give one `RehearsalTradingClient` the amend endpoint; returns it."""
    trading.amended = []
    trading.replace_order_by_id = types.MethodType(_replace_order_by_id, trading)
    return trading
