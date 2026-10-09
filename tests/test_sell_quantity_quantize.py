"""2026-10-09 09:33 ET live defect: partial sells of ETN 2.6703218688 and
META 0.9719239536 were refused by the desk's own quantity gate (more than 9
decimal places). Pins the ONE rounding point every SELL passes through
(`src/execution/sell_quantity.py`, called from `OrderDesk.submit_order`):
floor onto the gate's grid, never up, refuse loudly at zero — and pins that
the gate itself was NOT loosened.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.execution.broker import AlpacaBroker
from src.execution.order_gates import ORDER_QTY_DECIMALS, QTY_REJECTED, check_order_quantity
from src.execution.sell_quantity import floor_sell_qty


def _broker(positions):
    client = MagicMock()
    client.submit_order.return_value = SimpleNamespace(id="o-1", status="accepted", symbol="ETN")
    client.get_all_positions.return_value = [SimpleNamespace(symbol=s, qty=str(q)) for s, q in positions]
    with patch("src.execution.broker.TradingClient", return_value=client):
        b = AlpacaBroker(api_key="test", secret_key="test", paper=True)
    return b, client


def _sent_qty(client):
    return client.submit_order.call_args.args[0].qty


# ----------------------------------------------------------- pure helper
def test_the_live_etn_quantity_floors_to_nine_decimals():
    assert floor_sell_qty(2.6703218688) == (2.670321868, None)
    assert floor_sell_qty(0.9719239536) == (0.971923953, None)


def test_float_noise_from_residual_subtraction_floors_never_rounds_up():
    qty, refusal = floor_sell_qty(2.6703437375999997)
    assert refusal is None and qty == 2.670343737
    assert qty <= 2.6703437375999997


def test_a_quantity_already_on_the_grid_is_returned_untouched():
    assert floor_sell_qty(2.670321868) == (2.670321868, None)
    qty, _ = floor_sell_qty(3)
    assert qty == 3 and isinstance(qty, int)


def test_a_quantity_that_floors_to_zero_is_refused_with_a_reason():
    qty, refusal = floor_sell_qty(4e-10)
    assert qty == 0.0
    assert refusal is not None and f"{ORDER_QTY_DECIMALS} decimal places" in refusal


def test_garbage_is_left_for_the_gate_to_refuse():
    for bad in (-1, 0, float("nan"), "abc", None):
        qty, refusal = floor_sell_qty(bad)
        assert qty is bad and refusal is None


# ----------------------------------------------------------- wired at the desk
def test_desk_submits_the_floored_partial_sell():
    b, client = _broker([("ETN", 5.340643738)])
    result = b.submit_order(symbol="ETN", qty=2.6703218688, side="sell")
    assert result["status"] == "accepted"
    assert _sent_qty(client) == 2.670321868


def test_desk_sells_the_exact_held_quantity_unchanged():
    b, client = _broker([("META", 0.971923953)])
    assert b.submit_order(symbol="META", qty=0.971923953, side="sell")["status"] == "accepted"
    assert _sent_qty(client) == 0.971923953


def test_desk_refuses_loudly_when_the_sell_floors_to_zero():
    b, client = _broker([("ETN", 1.0)])
    result = b.submit_order(symbol="ETN", qty=4e-10, side="sell")
    assert result["status"] == QTY_REJECTED and result["detail"]
    client.submit_order.assert_not_called()


def test_gate_still_rejects_a_raw_over_precision_quantity_sent_around_the_desk():
    assert "decimal" in check_order_quantity(2.6703218688, side="sell")
    assert "decimal" in check_order_quantity(2.6703437375999997, side="sell", held_qty=3.0)
    # The protective-stop path calls the gate directly and is not floored.
    b, client = _broker([("ETN", 3)])
    try:
        b._submit_stop_limit_order("ETN", 2.6703218688, 95.0)
    except ValueError as exc:
        assert QTY_REJECTED in str(exc)
    else:
        raise AssertionError("an over-precision stop quantity reached the broker")
    client.submit_order.assert_not_called()
