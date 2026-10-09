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
    with patch.object(stop_read, "_sleep"):
        yield
    stop_read._alerted.clear()


def _pipeline(broker_stop, _s=None):
    import src.pipeline_protection as pp

    p = ProtectionMixin.__new__(ProtectionMixin)
    p.broker = MagicMock()
    p.broker.is_trading_day.return_value = True
    p.broker.client.get_orders.side_effect = RuntimeError("bulk down too")
    if isinstance(broker_stop, Exception):
        p.broker.get_current_stop_price.side_effect = broker_stop
    else:
        p.broker.get_current_stop_price.return_value = broker_stop
    p.db = MagicMock()
    p.db.get_trades.return_value = []
    p.market = MagicMock()
    p.market.get_upcoming_ex_dividend.return_value = {"date": pp.et_today() + timedelta(days=1), "amount": 0.5}
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
    assert "no stop" not in text.lower()


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
    broker.client.get_orders.side_effect = RuntimeError("bulk down too")
    with patch(ALERT, return_value=True):
        broker.get_current_stop_price.return_value = None
        assert read_stop(broker, "ZZZT", db=None).absent
        broker.get_current_stop_price.return_value = 12.5
        r = read_stop(broker, "ZZZT", db=None)
        assert r.found and r.price == 12.5
        broker.get_current_stop_price.side_effect = RuntimeError("x")
        r = read_stop(broker, "ZZZT", db=None)
        assert r.unreadable and not r.absent
        with pytest.raises(LookupError):
            r.price


def _heat(unreadable):
    from src.risk.heat_unreadable import portfolio_heat_with_unreadable as portfolio_heat
    from src.risk.metrics import format_heat_block

    pos = [SimpleNamespace(symbol="ZZZT", qty=10, avg_entry=50.0, current_price=50.0)]
    heat = portfolio_heat(pos, 10_000.0, stops={}, unreadable_stops=unreadable)
    return format_heat_block(heat, 5.0)


def test_prompt_says_could_not_read_not_unprotected_when_read_failed():
    text = _heat({"ZZZT"})
    assert "COULD NOT BE READ" in text and "do NOT treat as unprotected" in text
    assert "UNPROTECTED (no stop found" not in text


def test_prompt_still_says_unprotected_for_a_genuine_no_stop():
    text = _heat(set())
    assert "UNPROTECTED (no stop found" in text
    assert "COULD NOT BE READ" not in text


def test_stop_map_separates_unreadable_from_absent():
    from src.pipeline_prompt_facts import PromptFactsMixin as M

    m = M.__new__(M)
    m.db = MagicMock()
    m.db.get_symbol_last_buy.return_value = None
    m.broker = MagicMock()

    def _answer(sym):
        if sym == "AAA":
            raise RuntimeError("x")
        return {"BBB": None, "CCC": 9.0}[sym]

    m.broker.get_current_stop_price.side_effect = _answer
    m.broker.client.get_orders.side_effect = RuntimeError("bulk down too")
    pos = [SimpleNamespace(symbol=s, qty=1) for s in ("AAA", "BBB", "CCC")]
    with patch(ALERT, return_value=True):
        live, _init, unreadable = m._build_stop_map(pos)
    assert unreadable == {"AAA"} and live == {"CCC": 9.0}


# ---- escalation: retry, ask a different way, act (owner ruling 2026-10-02) ----


@pytest.fixture
def _no_sleep():
    from src.execution import stop_read

    with patch.object(stop_read, "_sleep") as s:
        yield s


def _order(price, side="sell", sym="ZZZT"):
    return SimpleNamespace(symbol=sym, order_type="stop", side=side, stop_price=price)


def test_read_failing_twice_then_succeeding_places_no_duplicate(_no_sleep):
    from src.execution import stop_read

    broker = MagicMock()
    broker.get_current_stop_price.side_effect = [RuntimeError("a"), RuntimeError("b"), 9.5]
    establish = MagicMock()
    with patch(ALERT, return_value=True) as alert:
        r = stop_read.read_stop(broker, "ZZZT", db=MagicMock(), establish=establish)
    assert r.found and r.price == 9.5
    establish.assert_not_called()
    alert.assert_not_called()
    assert _no_sleep.call_count == 2


def test_per_symbol_failure_is_answered_by_the_bulk_read(_no_sleep):
    from src.execution.stop_read import read_stop

    broker = MagicMock()
    broker.get_current_stop_price.side_effect = RuntimeError("down")
    broker.client.get_orders.return_value = [_order(7.0), _order(99.0, sym="OTHR")]
    db = MagicMock()
    with patch(ALERT, return_value=True) as alert:
        r = read_stop(broker, "ZZZT", db=db)
    assert r.found and r.price == 7.0
    assert broker.get_current_stop_price.call_count == 3
    alert.assert_not_called()
    db.insert_specialist_evidence.assert_not_called()


def test_bulk_read_showing_no_stop_is_absent_not_unreadable(_no_sleep):
    from src.execution.stop_read import read_stop

    broker = MagicMock()
    broker.get_current_stop_price.side_effect = RuntimeError("down")
    broker.client.get_orders.return_value = []
    assert read_stop(broker, "ZZZT", db=MagicMock()).absent


def test_total_outage_records_alerts_and_states_what_it_did(_no_sleep):
    from src.execution.stop_read import read_stop

    broker = MagicMock()
    broker.get_current_stop_price.side_effect = RuntimeError("down")
    broker.client.get_orders.side_effect = RuntimeError("down too")
    db = MagicMock()
    with patch(ALERT, return_value=True) as alert:
        r = read_stop(broker, "ZZZT", db=db)
    assert r.unreadable and r.action.startswith("not_acted")
    assert "reporting read" in alert.call_args.args[0]
    assert "not_acted" in db.insert_specialist_evidence.call_args.kwargs["evidence_json"]


def test_every_read_failing_establishes_protection_through_the_exdiv_caller():
    p = _pipeline(RuntimeError("down"))
    p._repair_stop_coverage = MagicMock(return_value=True)
    with patch(ALERT, return_value=True) as alert:
        out = p._handle_ex_dividends([_pos()], run_id="r1")
    assert out == []
    p._repair_stop_coverage.assert_called_once_with("ZZZT", 10.0, is_short=False)
    p.broker.shift_stops_down.assert_not_called()
    text = alert.call_args.args[0]
    assert "established protection" in text and "did NOT" not in text
    assert "established protection" in p.db.insert_specialist_evidence.call_args.kwargs["evidence_json"]


def test_a_failed_placement_is_reported_as_not_placed():
    p = _pipeline(RuntimeError("down"))
    p._repair_stop_coverage = MagicMock(return_value=False)
    with patch(ALERT, return_value=True) as alert:
        p._handle_ex_dividends([_pos()], run_id="r1")
    assert "not_acted" in alert.call_args.args[0]


def test_read_failing_twice_then_succeeding_places_nothing_through_the_caller():
    p = _pipeline(None)
    p.broker.get_current_stop_price.side_effect = [RuntimeError("a"), RuntimeError("b"), 48.0]
    p._repair_stop_coverage = MagicMock(return_value=True)
    p.broker.shift_stops_down.return_value = None
    with patch(ALERT, return_value=True) as alert:
        p._handle_ex_dividends([_pos()], run_id="r1")
    p._repair_stop_coverage.assert_not_called()
    alert.assert_not_called()


def test_non_numeric_answer_is_unreadable_not_no_stop():
    from src.execution.stop_read import read_stop

    broker = MagicMock()
    broker.get_current_stop_price.return_value = "garbage"
    broker.client.get_orders.side_effect = RuntimeError("down")
    with patch(ALERT, return_value=True):
        assert read_stop(broker, "ZZZT", db=None).unreadable
