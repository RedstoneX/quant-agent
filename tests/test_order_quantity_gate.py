"""2026-10-01 audit — the quantity gate at the broker boundary.

`submit_order` refused a bad stop PRICE but accepted ANY quantity: −5, 0,
NaN and 1e9 all reached the client. These pin the pure gate
(`src/execution/order_gates.py`), its wiring at BOTH submission sites
(entry and protective stop), that a refusal is the recorded
`rejected_bad_qty` outcome — never a silent None — and that the broker
client is NOT called. The last test pins that a legitimate request is
built with exactly the same fields as before the gate existed.
"""

import math
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.execution.broker import AlpacaBroker
from src.execution.order_gates import (
    BadOrderQuantity,
    QTY_REJECTED,
    check_order_quantity,
)

NAN, INF = float("nan"), float("inf")


def _broker(*, positions=(), equity=10_000.0, max_position_pct=None, fractionable=True):
    client = MagicMock()
    client.submit_order.return_value = SimpleNamespace(
        id="o-1",
        status="accepted",
        symbol="NVDA",
    )
    client.get_all_positions.return_value = [SimpleNamespace(symbol=s, qty=str(q)) for s, q in positions]
    client.get_account.return_value = SimpleNamespace(
        portfolio_value=str(equity),
        last_equity=str(equity),
        cash="0",
        non_marginable_buying_power="0",
    )
    client.get_asset.return_value = SimpleNamespace(fractionable=fractionable)
    with patch("src.execution.broker.TradingClient", return_value=client):
        b = AlpacaBroker(api_key="test", secret_key="test", paper=True, max_position_pct=max_position_pct)
    return b, client


# ----------------------------------------------------------- pure gate
@pytest.mark.parametrize("qty", [-5, 0, 0.0, NAN, INF, -INF, "abc", None])
def test_gate_refuses_non_positive_and_non_finite(qty):
    assert check_order_quantity(qty, side="buy") is not None


def test_gate_refuses_more_than_nine_decimals():
    # 9 is the project's own ceiling (`pipeline_sizing` clamps
    # `fractional_share_decimals` to <= 9); counted as the SDK sends it.
    assert "decimal" in check_order_quantity(1.0000000025, side="buy")
    assert "decimal" in check_order_quantity(7.000000000000001, side="buy")
    assert check_order_quantity(0.123456789, side="buy") is None
    assert check_order_quantity(12.3456, side="buy") is None


def test_gate_refuses_fraction_on_whole_share_name():
    assert "whole shares" in check_order_quantity(2.5, side="buy", fractionable=False)
    assert check_order_quantity(2.5, side="buy", fractionable=True) is None
    assert check_order_quantity(3.0, side="buy", fractionable=False) is None


def test_gate_notional_ceiling_uses_the_configured_cap():
    # 65% of $10,000 = $6,500: 100 x $66 is over, 100 x $65 is not.
    assert "per-position cap" in check_order_quantity(100, side="buy", price=66.0, equity=10_000.0, max_position_pct=65)
    assert (
        check_order_quantity(
            100,
            side="buy",
            price=65.0,
            equity=10_000.0,
            max_position_pct=65,
        )
        is None
    )
    # A SELL reduces exposure: never capped.
    assert (
        check_order_quantity(
            1_000,
            side="sell",
            price=66.0,
            equity=10_000.0,
            max_position_pct=65,
            held_qty=1_000,
        )
        is None
    )
    # Cap configured but equity unreadable: an ENTRY fails closed.
    assert "equity" in check_order_quantity(1, side="buy", price=1.0, equity=None, max_position_pct=65)


def test_gate_refuses_selling_more_than_held():
    assert "only 10 are held" in check_order_quantity(11, side="sell", held_qty=10.0)
    assert check_order_quantity(10, side="sell", held_qty=10.0) is None
    assert "only 0 are held" in check_order_quantity(1, side="sell", held_qty=0.0)


# ----------------------------------------- entry site: submit_order
@pytest.mark.parametrize("qty", [-5, 0, NAN, INF, 1.0000000025])
def test_entry_refuses_bad_quantity_and_never_calls_the_client(qty):
    b, client = _broker()
    result = b.submit_order(symbol="NVDA", qty=qty, side="buy", limit_price=100.0, stop_loss_price=90.0)
    assert result["status"] == QTY_REJECTED
    assert result["id"] is None and result["detail"]
    client.submit_order.assert_not_called()


def test_entry_refuses_oversized_notional_at_the_boundary():
    # The 65% check upstream looks at the SUGGESTED allocation; this is the
    # FINAL quantity: 100 x $100 = $10,000 against $6,500 allowed.
    b, client = _broker(equity=10_000.0, max_position_pct=65)
    result = b.submit_order(symbol="NVDA", qty=100, side="buy", limit_price=100.0, stop_loss_price=90.0)
    assert result["status"] == QTY_REJECTED
    assert "per-position cap" in result["detail"]
    client.submit_order.assert_not_called()
    ok = b.submit_order(symbol="NVDA", qty=60, side="buy", limit_price=100.0, stop_loss_price=90.0)
    assert ok["status"] == "accepted"
    assert client.submit_order.call_count == 1


def test_entry_refuses_fraction_on_whole_share_name():
    b, client = _broker(fractionable=False)
    result = b.submit_order(symbol="NVDA", qty=2.5, side="buy", limit_price=100.0, stop_loss_price=90.0)
    assert result["status"] == QTY_REJECTED
    client.submit_order.assert_not_called()


def test_sell_refuses_more_than_the_position_held():
    b, client = _broker(positions=[("NVDA", 10)])
    result = b.submit_order(symbol="NVDA", qty=11, side="sell")
    assert result["status"] == QTY_REJECTED
    assert "only 10 are held" in result["detail"]
    client.submit_order.assert_not_called()
    assert b.submit_order(symbol="NVDA", qty=10, side="sell")["status"] == "accepted"


def test_sell_of_a_symbol_not_held_is_refused():
    b, client = _broker(positions=[("AAPL", 10)])
    assert b.submit_order(symbol="NVDA", qty=1, side="sell")["status"] == QTY_REJECTED
    client.submit_order.assert_not_called()


# ------------------------------------ protective-stop site
@pytest.mark.parametrize("qty", [-5, 0, NAN, INF, 1.0000000025])
def test_protective_stop_refuses_bad_quantity_without_a_client_call(qty):
    b, client = _broker()
    with pytest.raises(BadOrderQuantity) as info:
        b._submit_stop_limit_order("NVDA", qty, 90.0)
    assert info.value.status == QTY_REJECTED and info.value.detail
    client.submit_order.assert_not_called()


def test_protective_stop_legs_route_through_the_gate():
    # A zero/NaN qty splits to no legs and is passed through AS-IS, so
    # the gate inside `_submit_stop_limit_order` is what refuses it.
    b, client = _broker()
    with pytest.raises(BadOrderQuantity):
        b._submit_stop_legs(symbol="NVDA", qty=0, stop_price=90.0)
    client.submit_order.assert_not_called()


# ------------------------------------- legitimate orders unchanged
@patch("src.execution.broker._session_date_key", return_value="2026-10-01")
def test_legitimate_requests_carry_exactly_the_expected_fields(_d):
    b, client = _broker(positions=[("AAPL", 3)], equity=10_000.0, max_position_pct=65)
    b.submit_order(symbol="BRK-B", qty=2, side="buy", limit_price=500.0, stop_loss_price=450.0, reference_price=505.0)
    b.submit_order(symbol="AAPL", qty=3, side="sell")
    b._submit_stop_limit_order("AAPL", 3, 95.0)
    b._submit_stop_limit_order("MSFT", 2.5, 95.0, side="buy")
    reqs = [c.args[0] for c in client.submit_order.call_args_list]
    assert len(reqs) == 4
    fields = [{k: v for k, v in r.__dict__.items() if v is not None} for r in reqs]
    # Everything except the (deterministic, separately tested) key.
    for f in fields:
        f.pop("client_order_id")
    assert [f["qty"] for f in fields] == [2, 3, 3, 2.5]
    assert [str(f["symbol"]) for f in fields] == ["BRK.B", "AAPL", "AAPL", "MSFT"]
    assert [str(f["side"].value) for f in fields] == ["buy", "sell", "sell", "buy"]
    assert [str(f["time_in_force"].value) for f in fields] == ["day", "day", "gtc", "day"]
    assert fields[0]["limit_price"] == 500.0 and "limit_price" not in fields[1]
    assert fields[2]["stop_price"] == 95.0 and fields[3]["stop_price"] == 95.0
