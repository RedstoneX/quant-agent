"""Install strict SDK-call recording on an existing Alpaca broker."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ops.rehearsal.broker_cassette import BrokerCassette


def install_recording_broker_cassette(broker) -> BrokerCassette:
    """Record both real Alpaca SDK clients before any broker read or write.

    Capture cannot observe the trade-updates websocket, so it must use the
    existing bounded REST fill path. A broker that has already read data is
    refused: a cassette missing those earlier calls cannot replay the run.
    """
    from ops.rehearsal.broker_cassette import (
        BrokerCassette,
        BrokerCassetteError,
        RecordingBrokerClient,
    )

    if broker.fill_stream_enabled():
        raise BrokerCassetteError(
            "broker cassette capture requires execution.fill_stream_enabled=false"
        )
    if getattr(broker, "_data_client", None) is not None or \
            getattr(broker, "_trading_day_cache", None) or \
            getattr(broker, "_session_open_cache", None):
        raise BrokerCassetteError("broker was used before cassette capture began")

    from alpaca.data.historical.stock import StockHistoricalDataClient
    from src.execution.broker_parts.market_data import _install_http_timeout
    from src.sentinel.cancel_attempts import CancelRecordingClient

    current = getattr(broker, "client", None)
    if current is None:
        raise BrokerCassetteError("broker has no trading client to record")
    cassette = BrokerCassette()
    if isinstance(current, CancelRecordingClient):
        current._inner = RecordingBrokerClient(current._inner, cassette, "trading")
    else:
        broker.client = RecordingBrokerClient(current, cassette, "trading")
    data_client = StockHistoricalDataClient(broker.api_key, broker.secret_key)
    _install_http_timeout(data_client)
    broker._data_client = RecordingBrokerClient(
        data_client, cassette, "stock_historical_data"
    )
    return cassette
