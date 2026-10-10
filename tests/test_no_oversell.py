"""No exit may sell (or cover) more than the broker holds.

Every full or partial exit is sized from a position read taken BEFORE the
protective stops are cleared. If a resting stop fills in between, the old size
would open a short (or, for a cover, a long). The protected sell re-reads the
position once no stop can fill any more and sizes the order to what is held;
the stop-cancel rollback re-reads it before putting any stop back.

Hermetic: MagicMock brokers that report what they hold (tests/fakes/held_book.py).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.execution.broker import AlpacaBroker
from tests.fakes.held_book import hold
from tests.pipeline_factory import build_pipeline

SPECS = [{"id": "s1", "qty": 73.0, "stop_price": 90.0}]


def _pipe(held: float):
    pipe = build_pipeline(
        broker=MagicMock(),
        db=MagicMock(),
        _cancel_stops_with_write_ahead=MagicMock(return_value=(True, list(SPECS), 99)),
        _order_accepted=MagicMock(return_value=True),
    )
    pipe.broker.submit_order.return_value = {"id": "ord-1", "status": "accepted", "symbol": "NVDA"}
    hold(pipe.broker, {"NVDA": held})
    return pipe


def _exit(pipe, qty: float, side: str = "sell"):
    return pipe._submit_protected_sell(
        symbol="NVDA",
        qty=qty,
        limit_price=None,
        reference_price=100.0,
        position_qty_before_sell=qty,
        label="SELL" if side == "sell" else "COVER",
        side=side,
    )


def test_stop_filled_before_the_exit_sends_no_order_and_opens_no_short():
    pipe = _pipe(held=0.0)  # the resting stop sold all 73 between the read and the cancel
    assert _exit(pipe, 73.0) is None
    pipe.broker.submit_order.assert_not_called()
    assert pipe._last_stop_clear_refusal == "stop_fired"
    pipe.db.delete_pending_protection_restore.assert_called_once_with(99)  # nothing to restore onto
    pipe.broker._restore_stop_orders.assert_not_called()


def test_the_reread_happens_after_the_stop_cancel_not_before():
    """The race itself: 73 held when the exit is sized, the stop fills during the cancel."""
    pipe = _pipe(held=73.0)
    book = {"NVDA": 73.0}
    pipe.broker.get_positions.side_effect = lambda: [SimpleNamespace(symbol=s, qty=q) for s, q in book.items()]

    def _cancel_while_the_stop_fills(*a, **k):
        book["NVDA"] = 0.0
        return True, list(SPECS), 99

    pipe._cancel_stops_with_write_ahead = MagicMock(side_effect=_cancel_while_the_stop_fills)
    assert pipe.broker.get_positions()[0].qty == 73.0  # what the caller sized the exit from
    assert _exit(pipe, 73.0) is None
    pipe.broker.submit_order.assert_not_called()


def test_unreadable_positions_alert_once_per_outage_not_per_symbol():
    pipe = _pipe(held=73.0)
    pipe._alert_owner_exit_declined = MagicMock()
    pipe.broker.get_positions = MagicMock(side_effect=RuntimeError("broker down"))
    for _ in range(3):
        assert _exit(pipe, 73.0) is None
    assert pipe._alert_owner_exit_declined.call_count == 1
    hold(pipe.broker, {"NVDA": 73.0})
    _exit(pipe, 73.0)  # a good read ends the outage
    pipe.broker.get_positions = MagicMock(side_effect=RuntimeError("broker down"))
    _exit(pipe, 73.0)
    assert pipe._alert_owner_exit_declined.call_count == 2
    pipe.broker.submit_order.assert_called_once()


def test_partial_stop_fill_sizes_the_sell_to_the_remainder():
    pipe = _pipe(held=40.0)  # the stop sold 33 of 73
    order, prot = _exit(pipe, 73.0)
    assert pipe.broker.submit_order.call_args.kwargs["qty"] == 40.0
    assert prot["submitted_qty"] == 40.0 and prot["position_qty_before_sell"] == 40.0


def test_cover_is_sized_to_the_short_held():
    pipe = _pipe(held=-20.0)  # a BUY stop covered 30 of a 50-share short
    _exit(pipe, 50.0, side="buy")
    kwargs = pipe.broker.submit_order.call_args.kwargs
    assert kwargs["side"] == "buy" and kwargs["qty"] == 20.0


def test_a_legitimate_full_close_is_sent_unchanged():
    pipe = _pipe(held=73.0)
    order, prot = _exit(pipe, 73.0)
    assert pipe.broker.submit_order.call_args.kwargs["qty"] == 73.0
    assert prot["position_qty_before_sell"] == 73.0


SPEC_A = {"id": "stop-a", "qty": 51.0, "stop_price": 248.5}
SPEC_B = {"id": "stop-b", "qty": 20.0, "stop_price": 246.0}


def _cancel_broker(mock_tc_cls, held: float):
    client = MagicMock()
    client.cancel_order_by_id.side_effect = lambda oid: None if oid == "stop-a" else _raise()
    mock_tc_cls.return_value = client
    broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)
    broker._restore_stop_orders = MagicMock(return_value=(1, []))
    return hold(broker, {"AMZN": held})


def _raise():
    raise RuntimeError("422: order is already filled")


@patch("src.execution.broker.TradingClient")
def test_failed_cancel_on_a_flat_position_restores_nothing(mock_tc_cls):
    broker = _cancel_broker(mock_tc_cls, held=0.0)  # stop-b's cancel failed because it FILLED
    outcome = broker.cancel_snapshotted_stops("AMZN", [SPEC_A, SPEC_B])
    broker._restore_stop_orders.assert_not_called()
    assert outcome.cleared is False and outcome.coverage_shrank is False
    assert outcome.detail.startswith("position_gone")


@patch("src.execution.broker.TradingClient")
def test_failed_cancel_restores_stops_at_the_held_size_not_the_old_size(mock_tc_cls):
    broker = _cancel_broker(mock_tc_cls, held=30.0)  # 71 covered at snapshot; fills left 30 held
    outcome = broker.cancel_snapshotted_stops("AMZN", [SPEC_A, SPEC_B])
    restored = broker._restore_stop_orders.call_args[0][1]
    assert [(s["id"], s["qty"]) for s in restored] == [("stop-a", 30.0)]
    assert outcome.coverage_shrank is False


@patch("src.execution.broker.TradingClient")
def test_failed_cancel_never_restores_sell_stops_onto_a_flipped_short(mock_tc_cls):
    broker = _cancel_broker(mock_tc_cls, held=-10.0)  # the book is SHORT now: a SELL stop would add to it
    outcome = broker.cancel_snapshotted_stops("AMZN", [SPEC_A, SPEC_B])
    broker._restore_stop_orders.assert_not_called()
    assert outcome.coverage_shrank is False


@patch("src.execution.broker.TradingClient")
def test_failed_cancel_restores_buy_stops_onto_a_short_at_the_held_size(mock_tc_cls):
    broker = _cancel_broker(mock_tc_cls, held=-30.0)
    buy_a, buy_b = {**SPEC_A, "side": "buy"}, {**SPEC_B, "side": "buy"}
    broker.cancel_snapshotted_stops("AMZN", [buy_a, buy_b])
    restored, kwargs = broker._restore_stop_orders.call_args[0][1], broker._restore_stop_orders.call_args.kwargs
    assert [(x["id"], x["qty"]) for x in restored] == [("stop-a", 30.0)] and kwargs == {"side": "buy"}
