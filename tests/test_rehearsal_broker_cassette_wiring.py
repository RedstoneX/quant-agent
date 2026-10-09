from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest

from ops.rehearsal.broker_cassette import (
    BrokerCassette,
    RecordingBrokerClient,
    install_rehearsal_broker_transport,
    install_replay_broker_cassette,
)
from src.execution.broker import AlpacaBroker
from src.sentinel.cancel_attempts import CancelRecordingClient
from src.sentinel.order_attempts import OrderAttemptLog
from src.storage.db import Database


ORDER_ID = UUID("00000000-0000-4000-8000-000000000002")


def test_installer_keeps_cancel_attempt_journal_outside_replay(tmp_path):
    cassette = BrokerCassette()
    recording = RecordingBrokerClient(
        SimpleNamespace(cancel_order_by_id=lambda _order_id: None),
        cassette,
        "trading",
    )
    recording.cancel_order_by_id("captured-order")

    database = Database(str(tmp_path / "journal.db"))
    database.initialize()
    wrapper = CancelRecordingClient(inner=object(), conn_getter=lambda: database.conn)
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._fill_stream_enabled = False
    broker.client = wrapper
    replay = install_replay_broker_cassette(broker, cassette.to_payload())

    assert broker.client is wrapper
    broker.client.cancel_order_by_id("replayed-order")
    replay.assert_consumed()
    row = OrderAttemptLog(conn=database.conn).recent(limit=1)[0]
    assert (row["outcome"], row["broker_order_id"]) == ("cancelled", "replayed-order")
    database.close()


def test_consumed_submit_order_populates_honest_cassette_report(tmp_path):
    request = LimitOrderRequest(
        symbol="SPY",
        qty=2,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=500,
        client_order_id="captured-client-id",
    )
    cassette = BrokerCassette()
    recording = RecordingBrokerClient(
        SimpleNamespace(
            submit_order=lambda order_data: SimpleNamespace(
                id=ORDER_ID,
                symbol="SPY",
                status="accepted",
            )
        ),
        cassette,
        "trading",
    )
    recording.submit_order(request)

    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._fill_stream_enabled = False
    broker.client = object()
    transport = install_rehearsal_broker_transport(
        broker,
        None,
        now=None,
        fill_model="immediate",
        payload=json.loads(json.dumps(cassette.to_payload())),
    )
    replay_request = LimitOrderRequest(
        symbol="SPY",
        qty=2,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=500,
        client_order_id="replayed-client-id",
    )
    replayed_order = broker.client.submit_order(replay_request)
    transport.assert_complete()

    database = Database(str(tmp_path / "report.db"))
    database.initialize()
    database.close()
    from ops.rehearsal.report import collect

    report = collect(
        session="morning",
        rehearsed_date="2026-10-03",
        run_id="replay-report",
        source_run_id=None,
        result={"status": "executed"},
        db_path=str(tmp_path / "report.db"),
        library=None,
        trading_stub=transport.trading_stub,
        isolation_checks=[],
        unavailable=[],
        network_attempts=[],
        notes=[],
        fill_model=transport.fill_model,
        duration_s=0.1,
    )

    assert report.orders_recorded == [
        {
            "id": str(replayed_order.id),
            "symbol": "SPY",
            "side": "buy",
            "qty": 2.0,
            "type": "limit",
            "limit_price": 500.0,
            "stop_price": None,
            "status": "accepted",
        }
    ]
    assert report.executed == 1
    rendered = report.render()
    assert "replay historical answers" in rendered
    assert "were never asked" not in rendered
