"""The fake broker refuses what the real Alpaca paper broker refuses.

1. A sell/stop for more shares than are free (not held for other open
   orders) -> 403 insufficient qty available (held_for_orders).
2. An order while an opposite-side order is open on the same symbol -> 403
   potential wash trade detected (the block documented in
   src/execution/scale_in.py).
"""

from __future__ import annotations

import pytest

from tests.fakes.holding_broker import SYM, ApiErr, HoldingBroker


def test_stop_larger_than_position_refused():
    b = HoldingBroker(10)
    with pytest.raises(ApiErr) as e:
        b.submit_stop(SYM, 11, 95.0)
    assert e.value.status_code == 403 and "insufficient qty available" in str(e.value)


def test_second_stop_exceeding_free_shares_refused():
    b = HoldingBroker(10)
    b.rest(8)
    with pytest.raises(ApiErr) as e:
        b.submit_stop(SYM, 3, 95.0)
    assert e.value.status_code == 403 and "held_for_orders" in str(e.value)
    b.submit_stop(SYM, 2, 95.0)  # exactly the free shares is accepted


def test_sell_into_held_shares_refused():
    b = HoldingBroker(10)
    b.rest(10)
    with pytest.raises(ApiErr) as e:
        b.sell(SYM, 4)
    assert e.value.status_code == 403 and "insufficient qty available" in str(e.value)


def test_opposite_side_order_is_wash_trade_refused():
    b = HoldingBroker(10)
    b.rest(5)  # resting SELL stop
    with pytest.raises(ApiErr) as e:
        b.submit_stop(SYM, 1, 105.0, side="buy")
    assert e.value.status_code == 403 and "potential wash trade detected" in str(e.value)
    with pytest.raises(ApiErr) as e:
        b.sell(SYM, 1, side="buy")
    assert "potential wash trade detected" in str(e.value)


def test_wash_clears_once_opposite_order_cancelled():
    b = HoldingBroker(10)
    oid = b.rest(5)
    b.cancel_order_by_id(oid)  # confirmed cancel
    b.submit_stop(SYM, 1, 105.0, side="buy")
