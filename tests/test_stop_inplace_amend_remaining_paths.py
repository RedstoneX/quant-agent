"""Item 201, remaining cancel+resubmit paths: convert what CAN amend, and make
the window visible for what cannot. Offline: every broker call is a mock."""
import json
from unittest.mock import MagicMock, patch

from src.execution.broker import AlpacaBroker
from src.execution.stop_records import replace_stop_and_record


def _broker(mock_tc_cls):
    client = MagicMock()
    mock_tc_cls.return_value = client
    return AlpacaBroker(api_key="t", secret_key="t", paper=True), client


def _stop(oid, stop, qty=10, otype="stop", limit=None, klass="simple",
          parent=None, legs=None):
    o = MagicMock()
    o.id, o.order_type, o.order_class = oid, otype, klass
    o.legs, o.parent_id, o.side = legs, parent, "sell"
    o.stop_price, o.qty, o.limit_price = stop, qty, limit
    return o


def _pos(qty):
    p = MagicMock()
    p.symbol, p.qty = "ZZZ", qty
    return p


@patch("src.execution.broker.TradingClient")
def test_stop_limit_leg_is_amended_in_place_keeping_its_limit_buffer(tc):
    b, client = _broker(tc)
    b._list_open_sell_stop_orders = MagicMock(return_value=[
        _stop("s1", 100.0, otype="stop_limit", limit=97.0)])
    b.cancel_snapshotted_stops = MagicMock(return_value=MagicMock(cleared=True))
    out = b.shift_stops_down("ZZZ", 1.0)
    assert out["mode"] == "amend" and out["shifted"] == 1
    client.cancel_order_by_id.assert_not_called()
    b.cancel_snapshotted_stops.assert_not_called()
    req = client.replace_order_by_id.call_args[0][1]
    assert (req.stop_price, req.limit_price) == (99.0, 96.0)


@patch("src.execution.broker.TradingClient")
def test_bracket_child_stop_is_amended_in_place(tc):
    b, client = _broker(tc)
    b._list_open_sell_stop_orders = MagicMock(return_value=[
        _stop("c1", 100.0, klass="bracket", parent="p1")])
    b.cancel_snapshotted_stops = MagicMock(return_value=MagicMock(cleared=True))
    out = b.shift_stops_down("ZZZ", 1.0)
    assert out["mode"] == "amend" and out["shifted"] == 1
    client.cancel_order_by_id.assert_not_called()
    b.cancel_snapshotted_stops.assert_not_called()


@patch("src.execution.broker.TradingClient")
def test_a_bracket_parent_with_legs_is_still_not_amended(tc):
    b, _ = _broker(tc)
    assert not b._stop_order_amendable_in_place(
        _stop("p1", 100.0, klass="bracket", legs=[object()]))


@patch("src.execution.broker.TradingClient")
def test_whole_share_coverage_repair_amends_qty_and_price_in_place(tc):
    b, client = _broker(tc)
    b._list_open_stop_orders_by_side = MagicMock(return_value=([_stop("s1", 100.0, qty=10)], []))
    b.get_positions = MagicMock(return_value=[_pos(7)])
    out = b.replace_stop_loss("ZZZ", 101.0)
    client.cancel_order_by_id.assert_not_called()
    client.submit_order.assert_not_called()
    req = client.replace_order_by_id.call_args[0][1]
    assert (req.qty, req.stop_price) == (7, 101.0)
    assert out["amend_status"] == "accepted"


@patch("src.execution.broker.TradingClient")
def test_fractional_coverage_change_still_cancels_but_the_window_is_recorded(tc):
    b, client = _broker(tc)
    b._list_open_stop_orders_by_side = MagicMock(return_value=([_stop("s1", 100.0, qty=10)], []))
    b._list_open_protective_stop_orders = MagicMock(return_value=[])
    b.get_positions = MagicMock(return_value=[_pos(10.5)])
    b._submit_stop_legs = MagicMock(return_value=[{"id": "n1", "status": "accepted"}])
    db = MagicMock()
    out = replace_stop_and_record(b, db, "ZZZ", 101.0)
    client.cancel_order_by_id.assert_called_once()
    assert out["id"] == "n1"
    kw = db.insert_specialist_evidence.call_args.kwargs
    assert kw["kind"] == "stop_unprotected_window" and kw["symbol"] == "ZZZ"
    pay = json.loads(kw["evidence_json"])
    assert pay["reason"] == "fractional_quantity_change"
    assert pay["cancelled_ids"] == ["s1"] and pay["outcome"] == "replaced"
    assert pay["window_seconds"] >= 0


@patch("src.execution.broker_parts.stop_shift.defer_shift_if_closed",
       return_value=None)
@patch("src.execution.broker.TradingClient")
def test_shift_fallback_window_is_recorded_not_silent(tc, _closed):
    """The ex-dividend shift's un-amendable fallback still cancels; its naked
    window must become a durable row, the same as replace_stop_loss's."""
    from src.stop_cancel_outcome import StopCancelOutcome
    from src.execution.broker_parts.stop_window import record_unprotected_windows
    b, client = _broker(tc)
    specs = [{"id": "s1", "qty": 10, "stop_price": 100.0}]
    # A bracket PARENT carrying legs is the shape that genuinely cannot amend.
    b._list_open_sell_stop_orders = MagicMock(return_value=[
        _stop("s1", 100.0, klass="bracket", legs=[object()])])
    b._snapshot_stop_order = MagicMock(side_effect=lambda o: dict(specs[0]))
    b.cancel_snapshotted_stops = MagicMock(
        return_value=StopCancelOutcome.all_cleared("ZZZ", tuple(specs)))
    b._restore_stop_orders = MagicMock(return_value=(1, []))
    out = b.shift_stops_down("ZZZ", 1.0)
    assert out["mode"] == "cancel_resubmit"
    db = MagicMock()
    record_unprotected_windows(b, db, "ZZZ")
    kw = db.insert_specialist_evidence.call_args.kwargs
    assert kw["kind"] == "stop_unprotected_window"
    pay = json.loads(kw["evidence_json"])
    assert pay["path"] == "shift_stops_down"
    assert pay["cancelled_ids"] == ["s1"] and pay["outcome"] == "restored"
    assert pay["window_seconds"] >= 0


@patch("src.execution.broker.TradingClient")
def test_an_amended_shift_opens_no_window_at_all(tc):
    """Recording must not invent a window on the path that cancels nothing."""
    b, _client = _broker(tc)
    b._list_open_sell_stop_orders = MagicMock(return_value=[_stop("s1", 100.0)])
    out = b.shift_stops_down("ZZZ", 1.0)
    assert out["mode"] == "amend"
    db = MagicMock()
    from src.execution.broker_parts.stop_window import record_unprotected_windows
    record_unprotected_windows(b, db, "ZZZ")
    db.insert_specialist_evidence.assert_not_called()
