"""Direct witnesses: the lifted entry-protection and stop-cancel functions run on a fake broker.

Each module is imported on its own (never through the orchestrator) and a
real function is exercised with a stub broker.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.execution.broker_parts import entry_protection, stop_cancel


def test_cancel_protective_stops_with_no_open_stops_is_a_clean_no_op():
    broker = MagicMock()
    broker.snapshot_protective_stops.return_value = (True, [])
    assert stop_cancel.cancel_protective_stops(broker, "AAPL") == (True, [])
    broker.snapshot_protective_stops.assert_called_once_with("AAPL")
    broker.cancel_snapshotted_stops.assert_not_called()


def test_cancel_protective_stops_reports_unknown_when_the_listing_failed():
    broker = MagicMock()
    broker.snapshot_protective_stops.return_value = (False, [])
    assert stop_cancel.cancel_protective_stops(broker, "AAPL") == (False, [])
    broker.cancel_snapshotted_stops.assert_not_called()


def test_snapshot_protective_stops_with_an_empty_listing_reports_no_stop():
    broker = MagicMock()
    broker._list_open_protective_stop_orders.return_value = []
    assert stop_cancel.snapshot_protective_stops(broker, "AAPL") == (True, [])
    broker._list_open_protective_stop_orders.assert_called_once()


def test_place_entry_protection_refuses_an_unknown_side_before_touching_the_broker():
    broker = MagicMock()
    result = entry_protection.place_entry_protection(
        broker, "AAPL", "order-1", 100.0, side="sideways", _ENTRY_FILL_TIMEOUT_S=0.0,
    )
    assert result is None
    assert broker.mock_calls == []


def test_broker_shims_delegate_to_the_lifted_functions():
    import src.execution.broker as broker_module
    cls = broker_module.AlpacaBroker
    assert broker_module._ENTRY_SIDES is entry_protection._ENTRY_SIDES
    fake = MagicMock(spec=cls)
    fake.snapshot_protective_stops.return_value = (True, [])
    assert cls.cancel_protective_stops(fake, "MSFT") == (True, [])
    assert cls.place_entry_protection(fake, "MSFT", "o", 10.0, side=None) is None
