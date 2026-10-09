"""The cancel-then-resubmit fallback must never end with NO resting stop.

Two holes, both on the fallback that runs for order shapes the in-place amend
cannot cover:

  IN HOURS  — the cancel succeeds, the resubmit fails and the rollback fails
              too. The position is left with no protective order at all, and
              `replace_stop_loss` returned a bare `None`, which every caller
              reads as "nothing happened, protection is intact".

  OUT OF HOURS — the fallback cancels a live protective order to re-place it
              while the tape is shut. A shut tape cannot elect a stop, so the
              amend costs nothing; the cancel is a real unprotected moment
              traded for a harmless non-event (owner ruling 2026-10-02, the
              same ruling PR 1158 applied to the in-place amend path).

Offline: every broker call is a mock.
"""

from unittest.mock import MagicMock, patch

from src.execution.broker import AlpacaBroker


def _broker(mock_tc_cls, *, market_open=True):
    client = MagicMock()
    clock = MagicMock()
    clock.is_open = market_open
    client.get_clock.return_value = clock
    mock_tc_cls.return_value = client
    return AlpacaBroker(api_key="t", secret_key="t", paper=True), client


def _stop(oid, stop, qty=10, otype="stop", limit=None):
    o = MagicMock()
    o.id, o.order_type, o.order_class = oid, otype, "simple"
    o.legs, o.parent_id, o.side = None, None, "sell"
    o.stop_price, o.qty, o.limit_price = stop, qty, limit
    return o


def _unamendable(*args, **kwargs):
    """A bracket parent: the one shape item 201 still sends to cancel+resubmit
    (stop_invariant.py fits quantities in place on every amendable shape)."""
    o = _stop(*args, **kwargs)
    o.legs = [MagicMock()]
    return o


def _pos(qty):
    p = MagicMock()
    p.symbol, p.qty = "ZZZ", qty
    return p


def _fallback_broker(tc, *, market_open=True):
    """A broker whose shape (fractional coverage repair) forces the fallback."""
    b, client = _broker(tc, market_open=market_open)
    b._list_open_stop_orders_by_side = MagicMock(return_value=([_unamendable("s1", 100.0, qty=10)], []))
    b._list_open_protective_stop_orders = MagicMock(return_value=[])
    b.get_positions = MagicMock(return_value=[_pos(10.5)])
    return b, client


# ---------------------------------------------------------------- in hours


@patch("src.execution.broker.TradingClient")
def test_in_hours_submit_and_rollback_both_fail_so_the_desk_reprotects(tc):
    b, client = _fallback_broker(tc)
    # First submit (the new, tighter stop) fails; the rollback of the
    # cancelled original fails too; the immediate re-protect must land.
    b._submit_stop_legs = MagicMock(
        side_effect=[
            RuntimeError("broker 500 on the replacement stop"),
            [{"id": "reprotect1", "status": "accepted"}],
        ]
    )
    b._restore_stop_orders = MagicMock(return_value=(0, [{"id": "s1"}]))

    out = b.replace_stop_loss("ZZZ", 101.0)

    client.cancel_order_by_id.assert_called_once_with("s1")
    assert b._submit_stop_legs.call_count == 2, (
        "after a failed resubmit AND a failed rollback the desk must immediately re-protect the position, not return"
    )
    assert out is not None and out.get("id") == "reprotect1"


@patch("src.execution.broker.TradingClient")
def test_in_hours_a_position_left_with_no_stop_is_never_reported_as_nothing(tc):
    b, client = _fallback_broker(tc)
    b._submit_stop_legs = MagicMock(side_effect=RuntimeError("broker 500"))
    b._restore_stop_orders = MagicMock(return_value=(0, [{"id": "s1"}]))

    out = b.replace_stop_loss("ZZZ", 101.0)

    # Ground truth: the broker holds no protective order for ZZZ.
    client.cancel_order_by_id.assert_called_once_with("s1")
    assert b._list_open_protective_stop_orders.return_value == []

    # The desk must KNOW it. `None` is the same answer this method gives for
    # "the broker refused, the original stop is still resting" — the two
    # cannot be the same answer.
    assert out is not None, (
        "replace_stop_loss returned None after leaving ZZZ with NO resting "
        "protective stop — indistinguishable from a harmless refusal"
    )
    assert out.get("amend_status") == "naked"
    assert not out.get("id"), "an unprotected result must not read as accepted"


# ------------------------------------------------------------- out of hours


@patch("src.execution.broker.TradingClient")
def test_out_of_hours_the_fallback_cancels_nothing(tc):
    b, client = _fallback_broker(tc, market_open=False)
    b._submit_stop_legs = MagicMock(return_value=[{"id": "n1"}])

    out = b.replace_stop_loss("ZZZ", 101.0)

    client.cancel_order_by_id.assert_not_called()
    b._submit_stop_legs.assert_not_called()
    assert out is not None and out.get("amend_status") == "market_closed"
    assert out.get("intended_stop") == 101.0


@patch("src.execution.broker.TradingClient")
def test_out_of_hours_the_ex_dividend_shift_cancels_nothing(tc):
    b, client = _broker(tc, market_open=False)
    # A stop-limit leg with no readable limit price is not amendable in place,
    # so shift_stops_down drops to its own cancel+resubmit fallback.
    b._list_open_sell_stop_orders = MagicMock(return_value=[_stop("s1", 100.0, otype="stop_limit", limit=None)])
    b.cancel_snapshotted_stops = MagicMock(return_value=MagicMock(cleared=True))
    b._restore_stop_orders = MagicMock(return_value=(1, []))

    out = b.shift_stops_down("ZZZ", 1.0)

    b.cancel_snapshotted_stops.assert_not_called()
    client.cancel_order_by_id.assert_not_called()
    assert out is not None and out.get("status") == "market_closed"


@patch("src.execution.broker.TradingClient")
def test_an_unreadable_clock_never_blocks_an_in_hours_fallback(tc):
    b, client = _fallback_broker(tc)
    client.get_clock.side_effect = RuntimeError("clock unreachable")
    b._submit_stop_legs = MagicMock(return_value=[{"id": "n1", "status": "accepted"}])

    out = b.replace_stop_loss("ZZZ", 101.0)

    client.cancel_order_by_id.assert_called_once_with("s1")
    assert out["id"] == "n1"
