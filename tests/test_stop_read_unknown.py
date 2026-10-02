"""An unreadable protective stop is not "no stop": it is recorded and alerted."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.pipeline_protection import ProtectionMixin

ALERT = "src.notifier.owner_alert.send_owner_alert"


@pytest.fixture(autouse=True)
def _fresh_dedupe():
    from src.execution import stop_read
    stop_read._alerted.clear()
    yield
    stop_read._alerted.clear()


def _pipeline(broker_stop):
    import src.pipeline_protection as pp
    p = ProtectionMixin.__new__(ProtectionMixin)
    p.broker = MagicMock()
    p.broker.is_trading_day.return_value = True
    if isinstance(broker_stop, Exception):
        p.broker.get_current_stop_price.side_effect = broker_stop
    else:
        p.broker.get_current_stop_price.return_value = broker_stop
    p.db = MagicMock()
    p.db.get_trades.return_value = []
    p.market = MagicMock()
    p.market.get_upcoming_ex_dividend.return_value = {
        "date": pp.et_today() + timedelta(days=1), "amount": 0.5}
    return p


def _pos():
    return SimpleNamespace(symbol="ZZZT", current_price=50.0, qty=10)


def test_unreadable_stop_is_recorded_and_alerted_not_silently_skipped():
    p = _pipeline(RuntimeError("broker timeout"))
    with patch(ALERT, return_value=True) as alert:
        out = p._handle_ex_dividends([_pos()], run_id="r1")
    assert out == []
    p.broker.shift_stops_down.assert_not_called()
    kinds = [c.kwargs["kind"] for c in p.db.insert_specialist_evidence.call_args_list]
    assert "stop_read_unreadable" in kinds
    assert alert.call_count == 1
    text = alert.call_args.args[0]
    assert "could not read" in text and "does not know whether" in text
    assert "no stop" not in text.lower().replace("no stop adjustment", "")


def test_genuine_no_stop_still_skips_quietly():
    p = _pipeline(None)
    with patch(ALERT, return_value=True) as alert:
        out = p._handle_ex_dividends([_pos()], run_id="r1")
    assert out == []
    p.broker.shift_stops_down.assert_not_called()
    alert.assert_not_called()
    p.db.insert_specialist_evidence.assert_not_called()


def test_a_found_stop_still_shifts():
    p = _pipeline(48.0)
    p.broker.shift_stops_down.return_value = None
    with patch(ALERT, return_value=True):
        p._handle_ex_dividends([_pos()], run_id="r1")
    p.broker.shift_stops_down.assert_called_once_with("ZZZT", 0.5)


@patch("src.execution.broker.TradingClient")
def test_broker_read_failure_raises_instead_of_returning_none(mock_tc_cls):
    from src.execution.broker import AlpacaBroker
    from src.execution.stop_read import StopReadUnavailable
    b = AlpacaBroker(api_key="t", secret_key="t", paper=True)
    b.client = MagicMock()
    b.client.get_orders.side_effect = RuntimeError("503")
    with pytest.raises(StopReadUnavailable):
        b.get_current_stop_price("ZZZT")
    b.client.get_orders.side_effect = None
    b.client.get_orders.return_value = []
    assert b.get_current_stop_price("ZZZT") is None


def test_read_stop_three_answers_cannot_be_confused():
    from src.execution.stop_read import read_stop
    broker = MagicMock()
    with patch(ALERT, return_value=True):
        broker.get_current_stop_price.return_value = None
        assert read_stop(broker, "ZZZT").absent
        broker.get_current_stop_price.return_value = 12.5
        r = read_stop(broker, "ZZZT")
        assert r.found and r.price == 12.5
        broker.get_current_stop_price.side_effect = RuntimeError("x")
        r = read_stop(broker, "ZZZT")
        assert r.unreadable and not r.absent
        with pytest.raises(LookupError):
            r.price
