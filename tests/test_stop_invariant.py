"""Item 201: the resting-stop QUANTITY invariant against a broker that HOLDS shares.

The fake below behaves like Alpaca on the three facts the design rests on:
a resting closing stop holds its shares (a submit or amend that would push
the resting sum over what is held is refused with the 403 text), a fractional
order's quantity can never be amended (42210000), and a hold is released only
once a cancel is CONFIRMED cancelled. Cancels can be made to stay pending.

Offline: no network, no real broker.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.execution.broker import AlpacaBroker
from src.execution.broker_parts import stop_invariant as inv

SYM = "ZZZ"
OLD, NEW = 100.0, 105.0


class ApiErr(Exception):
    def __init__(self, msg, status_code):
        super().__init__(msg)
        self.status_code = status_code


class Order:
    def __init__(self, oid, qty, stop, side):
        self.id, self.qty, self.stop_price, self.limit_price, self.side = oid, qty, stop, None, side
        self.status, self.order_type, self.order_class = "new", "stop", "simple"
        self.legs = self.parent_id = None


class HoldingBroker:
    """Fake Alpaca: share holds, no fractional qty amend, cancels confirm or stay pending."""

    def __init__(self, held, *, pending_cancels=False):
        self.held, self.pending = float(held), pending_cancels
        self.orders: dict[str, Order] = {}
        self.calls: list = []
        self.n = 0
        self.refuse_submits = 0        # how many of the next submits to refuse
        self.refuse_qty_amend = False  # the broker says NO to a whole-share qty amend
        self.qty_amend_unknown = False  # no answer at all to a qty amend

    # -- the hold rule -------------------------------------------------
    def closing_side(self):
        return "buy" if self.held < 0 else "sell"

    def holding(self, excluding=None):
        return sum(o.qty for o in self.orders.values() if o.id != excluding
                   and o.side == self.closing_side()
                   and o.status in ("new", "accepted", "pending_cancel"))

    def hold_check(self, extra, excluding=None):
        if self.holding(excluding) + extra > abs(self.held) + 1e-9:
            raise ApiErr("insufficient qty available for order (held_for_orders)", 403)

    def rest(self, qty, stop=OLD):
        o = Order(self.new_id(), float(qty), stop, self.closing_side())
        self.hold_check(o.qty)
        self.orders[o.id] = o
        return o.id

    def new_id(self):
        self.n += 1
        return f"o{self.n}"

    def book(self):
        return sorted((o.qty, o.stop_price) for o in self.orders.values()
                      if o.status in ("new", "accepted"))

    # -- the client surface the desk calls --------------------------------
    def get_clock(self):
        return SimpleNamespace(is_open=True)

    def replace_order_by_id(self, oid, req):
        fields = {k: getattr(req, k) for k in ("qty", "stop_price") if getattr(req, k, None) is not None}
        self.calls.append(("replace", oid, fields))
        old = self.orders[oid]
        assert old.status in ("new", "accepted"), f"replace of a {old.status} order"
        if req.qty is not None:
            if old.qty != int(old.qty):
                raise ApiErr("42210000: cannot replace qty in fractional stop order", 422)
            if self.qty_amend_unknown:
                raise ApiErr("gateway timeout", 504)
            if self.refuse_qty_amend:
                raise ApiErr("422: replace refused", 422)
            if float(req.qty) > old.qty:  # a reduction frees shares; only growth needs room
                self.hold_check(float(req.qty), excluding=oid)
        new = Order(self.new_id(), float(req.qty) if req.qty is not None else old.qty,
                    float(req.stop_price or old.stop_price), old.side)
        old.status = "replaced"
        self.orders[new.id] = new
        return new

    def cancel_order_by_id(self, oid):
        self.calls.append(("cancel", oid))
        self.orders[oid].status = "pending_cancel" if self.pending else "canceled"

    def submit_stop(self, symbol, qty, stop_price, limit_price=None, *, side="sell"):
        self.calls.append(("submit", float(qty), side))
        if self.refuse_submits:
            self.refuse_submits -= 1
            raise ApiErr("422: submit refused", 422)
        self.hold_check(float(qty))
        o = Order(self.new_id(), float(qty), float(stop_price), side)
        self.orders[o.id] = o
        return {"id": o.id, "status": "accepted", "symbol": symbol, "qty": qty}

    def wait_terminal(self, oid):
        return self.orders[oid].status

    def by_side(self, symbol):
        live = [o for o in self.orders.values() if o.status in ("new", "accepted")]
        return [o for o in live if o.side == "sell"], [o for o in live if o.side == "buy"]

    def positions(self):
        return [SimpleNamespace(symbol=SYM, qty=self.held)]


def _desk(fake):
    with patch("src.execution.broker.TradingClient", return_value=MagicMock()):
        b = AlpacaBroker(api_key="t", secret_key="t", paper=True)
    b.client = fake
    b._list_open_stop_orders_by_side = fake.by_side
    b._list_open_protective_stop_orders = lambda symbol, side="sell": []
    b.get_positions = fake.positions
    b._submit_stop_limit_order = fake.submit_stop
    b.wait_for_order_terminal = fake.wait_terminal
    return b


@pytest.fixture
def alerts(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr("src.notifier.send_owner_alert", lambda text, **kw: sent.append(text))
    return sent


def _cancels(fake):
    return [c[1] for c in fake.calls if c[0] == "cancel"]


def _qty_amends(fake):
    return [(c[1], c[2]["qty"]) for c in fake.calls if c[0] == "replace" and "qty" in c[2]]


def _windows(b):
    return list(b.__dict__.get("_unprotected_windows", []))


# --------------------------------------------------------- the decisive case

def test_grow_from_1_37_to_2_cancels_the_sliver_before_the_gtc_grows(alerts):
    fake = HoldingBroker(2.0)
    gtc, day = fake.rest(1), fake.rest(0.37)
    b = _desk(fake)
    out = b.replace_stop_loss(SYM, NEW)
    assert out["amend_status"] == "accepted", out
    assert fake.book() == [(2.0, NEW)]
    assert _cancels(fake) == [f"o{int(day[1:]) + 2}"], fake.calls  # the sliver's post-price-amend id
    assert gtc not in _cancels(fake)
    # ORDER: price amends, then the sliver cancel, THEN the GTC quantity amend.
    kinds = [(c[0], c[2].get("qty") if c[0] == "replace" else None) for c in fake.calls]
    assert kinds.index(("cancel", None)) < kinds.index(("replace", 2))
    (row,) = _windows(b)
    assert row["outcome"] == "replaced" and row["leg_kind"] == "DAY"
    assert row["exposed_qty"] == 1.0, "a whole share (held - GTC) was exposed, not 0.37"
    assert alerts == []


def test_a_right_total_with_a_whole_share_on_day_legs_is_corrected(alerts):
    fake = HoldingBroker(2.0)
    fake.rest(1), fake.rest(0.37), fake.rest(0.63)
    b = _desk(fake)
    out = b.replace_stop_loss(SYM, NEW)
    assert out["amend_status"] == "accepted"
    assert fake.book() == [(2.0, NEW)]
    assert len(_cancels(fake)) == 2 and _windows(b)[0]["exposed_qty"] == 1.0


# ------------------------------------------------------------ no-cancel cases

def test_a_correct_book_only_moves_prices(alerts):
    fake = HoldingBroker(1.37)
    fake.rest(1), fake.rest(0.37)
    b = _desk(fake)
    out = b.replace_stop_loss(SYM, NEW)
    assert out["amend_status"] == "accepted"
    assert fake.book() == [(0.37, NEW), (1.0, NEW)]
    assert _cancels(fake) == [] and _qty_amends(fake) == [] and _windows(b) == []


def test_a_sliver_below_the_fraction_is_topped_up_without_a_cancel(alerts):
    fake = HoldingBroker(2.77)
    fake.rest(2), fake.rest(0.37)
    b = _desk(fake)
    out = b.replace_stop_loss(SYM, NEW)
    assert out["amend_status"] == "accepted"
    assert fake.book() == [(0.37, NEW), (0.4, NEW), (2.0, NEW)]
    assert _cancels(fake) == [] and _windows(b) == []
    assert [c for c in fake.calls if c[0] == "submit"] == [("submit", 0.4, "sell")]


def test_a_missing_sliver_is_placed_without_a_cancel(alerts):
    fake = HoldingBroker(2.5)
    fake.rest(2)
    b = _desk(fake)
    out = b.replace_stop_loss(SYM, NEW)
    assert out["amend_status"] == "accepted"
    assert fake.book() == [(0.5, NEW), (2.0, NEW)] and _cancels(fake) == []


def test_room_to_grow_amends_the_largest_lot_in_place(alerts):
    fake = HoldingBroker(9)
    fake.rest(3), fake.rest(4)
    b = _desk(fake)
    out = b.replace_stop_loss(SYM, NEW)
    assert out["amend_status"] == "accepted"
    assert fake.book() == [(3.0, NEW), (6.0, NEW)] and _cancels(fake) == []


def test_shrink_amends_the_largest_lot_down_in_place(alerts):
    fake = HoldingBroker(7)        # the lots were right at 7; two shares were sold
    fake.rest(3), fake.rest(4)
    fake.held = 5.0
    b = _desk(fake)
    out = b.replace_stop_loss(SYM, NEW)
    assert out["amend_status"] == "accepted"
    assert fake.book() == [(2.0, NEW), (3.0, NEW)] and _cancels(fake) == []


# ------------------------------------------------------------------ the short

def test_a_short_is_treated_exactly_like_a_long(alerts):
    fake = HoldingBroker(-3)       # was short 5, covered 2; the buy-stop still says 5
    fake.held = -5.0
    fake.rest(5)
    fake.held = -3.0
    b = _desk(fake)
    out = b.replace_stop_loss(SYM, OLD - 5)  # a short's stop ratchets DOWN
    assert out["amend_status"] == "accepted"
    assert fake.book() == [(3.0, OLD - 5)] and _cancels(fake) == []
    assert all(o.side == "buy" for o in fake.orders.values())


def test_a_short_with_no_leg_gets_one_gtc_leg_and_the_hold_refuses_a_second():
    fake = HoldingBroker(-5)
    b = _desk(fake)
    out = b.replace_stop_loss(SYM, OLD)
    assert out and fake.book() == [(5.0, OLD)]
    with pytest.raises(ApiErr, match="held_for_orders"):
        fake.submit_stop(SYM, 5, OLD, side="buy")


# --------------------------------------------------------------- failure paths

def test_a_pending_cancel_amends_nothing_and_resubmits_nothing(alerts):
    fake = HoldingBroker(2.0, pending_cancels=True)
    fake.rest(1), fake.rest(0.37)
    b = _desk(fake)
    out = b.replace_stop_loss(SYM, NEW)
    assert out["amend_status"] == inv.CANCEL_PENDING and out["id"] is None
    assert out["next_reread"] == inv.NEXT_REREAD
    assert _qty_amends(fake) == [] and not [c for c in fake.calls if c[0] == "submit"]
    assert len(_cancels(fake)) == 1
    assert _windows(b)[0]["outcome"] == inv.CANCEL_PENDING


def test_a_refused_gtc_amend_restores_the_sliver(alerts):
    fake = HoldingBroker(2.0)
    fake.rest(1), fake.rest(0.37)
    fake.refuse_qty_amend = True
    b = _desk(fake)
    out = b.replace_stop_loss(SYM, NEW)
    assert out["amend_status"] == "refused"
    assert fake.book() == [(0.37, NEW), (1.0, NEW)], "coverage must equal the pre-change book"
    assert _windows(b)[0]["outcome"] == "restored" and alerts == []


def test_a_refused_restore_is_retried_then_recorded_exposed_and_alerted(alerts):
    fake = HoldingBroker(2.0)
    fake.rest(1), fake.rest(0.37)
    fake.refuse_qty_amend, fake.refuse_submits = True, 2
    b = _desk(fake)
    out = b.replace_stop_loss(SYM, NEW)
    assert out["amend_status"] == inv.EXPOSED and out["id"] is None
    assert out["exposed_qty"] == 0.37
    assert fake.book() == [(1.0, NEW)]
    assert [c for c in fake.calls if c[0] == "submit"] == [("submit", 0.37, "sell")] * 2
    assert _windows(b)[0]["outcome"] == "restore_refused"
    assert len(alerts) == 1 and "WITHOUT a protective stop" in alerts[0]


def test_an_unknown_gtc_amend_restores_nothing_and_alerts(alerts):
    fake = HoldingBroker(2.0)
    fake.rest(1), fake.rest(0.37)
    fake.qty_amend_unknown = True
    b = _desk(fake)
    out = b.replace_stop_loss(SYM, NEW)
    assert out["amend_status"] == "unknown" and out["next_reread"] == inv.NEXT_REREAD
    assert fake.book() == [(1.0, NEW)]
    assert not [c for c in fake.calls if c[0] == "submit"]
    assert _windows(b)[0]["outcome"] == "unknown"
    assert len(alerts) == 1 and inv.NEXT_REREAD in alerts[0]


def test_no_fractional_quantity_amend_is_ever_sent(alerts):
    for held, legs in ((2.0, (1, 0.37)), (2.77, (2, 0.37)), (1.37, (1, 0.37)), (3.0, (1, 0.37, 0.63))):
        fake = HoldingBroker(held)
        for q in legs:
            fake.rest(q)
        b = _desk(fake)
        b.replace_stop_loss(SYM, NEW)
        for oid, _ in _qty_amends(fake):
            assert fake.orders[oid].qty == int(fake.orders[oid].qty), fake.calls


# ------------------------------------------------------------- pure helpers

def test_book_shape_classes_legs_by_quantity():
    shape = inv.book_shape([{"id": "a", "qty": 2}, {"id": "b", "qty": 0.37}, {"id": "c", "qty": 0.4}], 2.77)
    assert (shape["whole"], shape["frac"], shape["g"], shape["d"]) == (2.0, 0.77, 2.0, 0.77)
    assert [s["id"] for s in shape["day"]] == ["b", "c"]
