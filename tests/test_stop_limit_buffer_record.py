"""The stop-LIMIT buffer's counter: one durable row per protective placement.

The ledger row for ``STOP_LIMIT_BUFFER_PCT`` cannot close either way until
something records how often the stop-limit FALLBACK leg is taken -- its own
DELETE outcome depends on that count. These pin the four facts that matter:
the fallback leg writes its row, the primary (stop-MARKET) leg writes a
DIFFERENT one, a site never reached writes NOTHING (never-exercised is not
zero), and a recording failure cannot take the placement down.
"""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

from alpaca.trading.requests import StopLimitOrderRequest, StopOrderRequest

from src.execution.broker import AlpacaBroker
from src.execution.stop_limit_buffer_records import (
    KIND_PREFIX, LEG_FALLBACK, LEG_PRIMARY, LIMIT_FROM_BUFFER,
    LIMIT_FROM_CALLER, record_stop_leg, stop_leg_measurement,
)
from src.sentinel.reconciliation import ReconciliationLog


class _FakeComboReject(Exception):
    """The broker's unsupported order-type/tif refusal of a stop-MARKET."""

    def __init__(self, message="stop market orders are not supported", status_code=422):
        super().__init__(message)
        self.status_code = status_code


def _broker(mock_tc_cls):
    mock_client = MagicMock()
    mock_tc_cls.return_value = mock_client
    return AlpacaBroker(api_key="t", secret_key="t", paper=True), mock_client


def _ledger_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE reconciliation_runs (id INTEGER PRIMARY KEY,"
        " ran_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, kind TEXT, agreed INTEGER,"
        " detail TEXT, run_id TEXT)"
    )
    return conn


class _Owner:
    """What a call site already has in scope: an object carrying a ledger."""

    def __init__(self, conn):
        self.conn = conn


def _rows(conn):
    cur = conn.execute("SELECT kind, detail FROM reconciliation_runs ORDER BY id")
    return [(k, json.loads(d)) for k, d in cur.fetchall()]


def test_fallback_leg_writes_a_row_carrying_the_buffer_measurement():
    conn = _ledger_conn()
    record_stop_leg(_Owner(conn), leg=LEG_FALLBACK, symbol="AAA", qty=10,
                    side="sell", stop_price=200.0, limit_price=194.0,
                    buffer_pct=0.03, limit_source=LIMIT_FROM_BUFFER)
    rows = _rows(conn)
    assert len(rows) == 1
    kind, detail = rows[0]
    assert kind == f"{KIND_PREFIX}:{LEG_FALLBACK}"
    assert detail["fallback_taken"] is True
    # Not a bare "it fired": the distance and the buffer in force are what
    # let a later reader judge whether 3% was the right distance.
    assert detail["limit_distance"] == 6.0
    assert detail["buffer_pct"] == 0.03
    assert detail["limit_source"] == LIMIT_FROM_BUFFER


def test_primary_leg_row_is_a_distinct_kind_from_the_fallback():
    conn = _ledger_conn()
    record_stop_leg(_Owner(conn), leg=LEG_PRIMARY, symbol="AAA", qty=10,
                    side="sell", stop_price=200.0, limit_price=194.0,
                    buffer_pct=0.03, limit_source=LIMIT_FROM_BUFFER)
    kind, detail = _rows(conn)[0]
    assert kind == f"{KIND_PREFIX}:{LEG_PRIMARY}"
    assert detail["fallback_taken"] is False


def test_a_site_never_reached_leaves_no_row_at_all():
    """Never-exercised is NOT zero: with nothing recorded the table is empty,
    and ``ReconciliationLog.status`` answers 'not_run' for both legs."""
    conn = _ledger_conn()
    log = ReconciliationLog(conn=conn)
    assert log.status(kind=f"{KIND_PREFIX}:{LEG_FALLBACK}") == "not_run"
    assert log.status(kind=f"{KIND_PREFIX}:{LEG_PRIMARY}") == "not_run"
    assert _rows(conn) == []


def test_a_recording_failure_logs_a_traceback_and_never_raises(caplog):
    # A real ledger handle whose table does not exist: the write raises
    # exactly as a locked or migrated-away database would.
    broken = _Owner(sqlite3.connect(":memory:"))
    record_stop_leg(broken, leg=LEG_FALLBACK, symbol="AAA", qty=10, side="sell",
                    stop_price=200.0, limit_price=194.0, buffer_pct=0.03,
                    limit_source=LIMIT_FROM_BUFFER)
    assert any(r.levelname == "ERROR" and r.exc_info for r in caplog.records)


def test_no_ledger_in_reach_is_a_skip_not_a_failure():
    record_stop_leg(object(), leg=LEG_PRIMARY, symbol="AAA", qty=10, side="sell",
                    stop_price=200.0, limit_price=194.0, buffer_pct=0.03,
                    limit_source=LIMIT_FROM_BUFFER)


def test_measurement_records_a_caller_supplied_limit_as_not_governed_by_the_buffer():
    out = stop_leg_measurement(
        leg=LEG_FALLBACK, symbol="AAA", qty=1, side="sell", stop_price=100.0,
        limit_price=99.0, buffer_pct=0.03, limit_source=LIMIT_FROM_CALLER)
    assert out["limit_source"] == LIMIT_FROM_CALLER
    assert out["limit_distance"] == 1.0


@patch("src.execution.broker_parts.stop_place._record_leg")
@patch("src.execution.broker.TradingClient")
def test_the_fallback_placement_records_the_fallback_leg(mock_tc_cls, rec):
    broker, client = _broker(mock_tc_cls)
    client.submit_order.side_effect = [
        _FakeComboReject(), MagicMock(id="o1", status="new"),
    ]
    broker._submit_stop_limit_order(symbol="AAA", qty=10, stop_price=200.0)
    reqs = [c.args[0] for c in client.submit_order.call_args_list]
    assert isinstance(reqs[0], StopOrderRequest)
    assert isinstance(reqs[1], StopLimitOrderRequest)
    # (placer, leg, symbol, qty, side, stop_price_q, limit_price_q, limit_source)
    assert [c.args[1] for c in rec.call_args_list] == [LEG_FALLBACK]
    assert rec.call_args.args[7] == LIMIT_FROM_BUFFER
    assert float(rec.call_args.args[6]) == 194.0


@patch("src.execution.broker_parts.stop_place._record_leg")
@patch("src.execution.broker.TradingClient")
def test_the_main_leg_placement_records_the_primary_leg(mock_tc_cls, rec):
    broker, client = _broker(mock_tc_cls)
    client.submit_order.side_effect = [MagicMock(id="o1", status="new")]
    broker._submit_stop_limit_order(symbol="AAA", qty=10, stop_price=200.0)
    assert [c.args[1] for c in rec.call_args_list] == [LEG_PRIMARY]


@patch("src.execution.broker.TradingClient")
def test_a_recorder_that_explodes_does_not_break_the_placement(mock_tc_cls):
    """The stop must still be placed and returned unchanged."""
    broker, client = _broker(mock_tc_cls)
    client.submit_order.side_effect = [
        _FakeComboReject(), MagicMock(id="o1", status="new"),
    ]
    with patch("src.execution.stop_limit_buffer_records.ledger_in_reach",
               side_effect=RuntimeError("boom")):
        out = broker._submit_stop_limit_order(symbol="AAA", qty=10, stop_price=200.0)
    assert out["id"] == "o1"
    assert out["status"] == "new"
