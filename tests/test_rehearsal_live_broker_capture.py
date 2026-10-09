"""The live cassette attaches before broker calls without losing cancel logging."""

import sqlite3
from types import SimpleNamespace

import pytest

from ops.rehearsal.broker_cassette import (
    BrokerCassetteError,
    RecordingBrokerClient,
    install_recording_broker_cassette,
)
from src.sentinel.cancel_attempts import CancelRecordingClient
from src.sentinel.order_attempts import OrderAttemptLog
from src.storage.schema.manager import DatabaseSchema


class Trading:
    def get_account(self):
        return {"status": "ACTIVE"}

    def cancel_order_by_id(self, order_id):
        return None


class Historical:
    def get_stock_bars(self, request):
        return {"AAPL": [1, 2]}


def _broker(*, stream=False, used=False):
    broker = SimpleNamespace(
        client=CancelRecordingClient(inner=Trading(), conn_getter=lambda: None),
        _data_client=Historical() if used else None,
        _trading_day_cache={},
        _session_open_cache={},
        api_key="test-key",
        secret_key="test-secret",
        fill_stream_enabled=lambda: stream,
    )
    return broker


def test_capture_wraps_both_sdk_clients_beneath_cancel_journal(monkeypatch):
    from alpaca.data.historical import stock

    monkeypatch.setattr(stock, "StockHistoricalDataClient", lambda *_: Historical())
    broker = _broker()
    original_cancel_journal = broker.client
    cassette = install_recording_broker_cassette(broker)

    assert broker.client is original_cancel_journal
    assert isinstance(broker.client._inner, RecordingBrokerClient)
    assert isinstance(broker._data_client, RecordingBrokerClient)
    assert broker.client.get_account() == {"status": "ACTIVE"}
    assert broker._data_client.get_stock_bars("AAPL") == {"AAPL": [1, 2]}
    assert [entry["client"] for entry in cassette.to_payload()["entries"]] == [
        "trading",
        "stock_historical_data",
    ]


def test_captured_cancel_still_writes_one_durable_attempt(monkeypatch):
    from alpaca.data.historical import stock

    monkeypatch.setattr(stock, "StockHistoricalDataClient", lambda *_: Historical())
    connection = sqlite3.connect(":memory:")
    try:
        schema = DatabaseSchema(conn=connection)
        schema._create_tables()
        schema._migrate()
        broker = _broker()
        broker.client._conn_getter = lambda: connection
        cassette = install_recording_broker_cassette(broker)
        broker.client.cancel_order_by_id("captured-order")
        rows = OrderAttemptLog(conn=connection).recent(limit=5)
        assert [(row["outcome"], row["broker_order_id"]) for row in rows] == [("cancelled", "captured-order")]
        assert [entry["method"] for entry in cassette.to_payload()["entries"]] == ["cancel_order_by_id"]
    finally:
        connection.close()


@pytest.mark.parametrize("stream,used", [(True, False), (False, True)])
def test_capture_refuses_unrecorded_stream_or_prior_broker_use(stream, used):
    broker = _broker(stream=stream, used=used)
    original = broker.client._inner
    with pytest.raises(BrokerCassetteError):
        install_recording_broker_cassette(broker)
    assert broker.client._inner is original
