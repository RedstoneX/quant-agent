from __future__ import annotations

import json
from datetime import datetime

from alpaca.trading.requests import GetCalendarRequest

import pytest

from ops.rehearsal.broker_cassette import (
    BrokerCassette,
    BrokerReplayViolation,
    RecordingBrokerClient,
)
from ops.rehearsal.isolation import Sandbox
from ops.rehearsal.runner import run_rehearsal
from src.storage.db import Database
from src.trading_calendar import ET


class _ClosedCalendarClient:
    def get_calendar(self, _request):
        return []


def _closed_day_cassette(on_date):
    cassette = BrokerCassette()
    client = RecordingBrokerClient(
        _ClosedCalendarClient(), cassette, "trading"
    )
    client.get_calendar(GetCalendarRequest(start=on_date, end=on_date))
    # Force the same JSON boundary a checked-in public bundle uses.
    return json.loads(json.dumps(cassette.to_payload()))


def _sandbox(tmp_path, name):
    source_db = tmp_path / f"{name}-source.db"
    database = Database(str(source_db))
    database.initialize()
    database.close()
    return Sandbox.prepare(source_db, tmp_path / name)


def test_runner_drives_real_morning_session_through_strict_broker_replay(tmp_path):
    sandbox = _sandbox(tmp_path, "successful")
    now = datetime(2026, 10, 3, 9, 35, tzinfo=ET)

    report = run_rehearsal(
        sandbox,
        session="morning",
        now_et=now,
        config_overrides={"execution.fill_stream_enabled": False},
        broker_cassette=_closed_day_cassette(now.date()),
    )

    # This is the real TradingPipeline.run_morning holiday path.  The fixture
    # proves transport wiring and strict consumption only; it is synthetic and
    # deliberately does not count as item 233's bounded live capture.
    assert report.status == "market_holiday"
    assert report.fill_model == "cassette"
    assert report.network_attempts == []
    assert any(
        "every recorded broker call was consumed exactly once" in check
        for check in report.isolation_checks
    )
    assert any(
        "synthetic cassette proves this wiring only" in note
        for note in report.notes
    )


def test_runner_voids_a_broker_gap_at_the_real_calendar_path(tmp_path):
    sandbox = _sandbox(tmp_path, "missing-call")
    now = datetime(2026, 10, 3, 9, 35, tzinfo=ET)

    with pytest.raises(BrokerReplayViolation) as caught:
        run_rehearsal(
            sandbox,
            session="morning",
            now_et=now,
            config_overrides={"execution.fill_stream_enabled": False},
            broker_cassette=BrokerCassette().to_payload(),
        )

    assert caught.value.report.status == "did_not_finish"
    assert "MissingRecordedBrokerCall" in caught.value.report.error
    assert caught.value.report.network_attempts == []
    assert "unrecorded broker call" in str(caught.value)


def test_runner_installs_exact_session_provider_ledger(tmp_path):
    sandbox = _sandbox(tmp_path, "captured-inputs")
    now = datetime(2026, 10, 3, 9, 35, tzinfo=ET)

    report = run_rehearsal(
        sandbox,
        session="morning",
        now_et=now,
        config_overrides={"execution.fill_stream_enabled": False},
        broker_cassette=_closed_day_cassette(now.date()),
        session_input_payload={"schema": 1, "entries": []},
    )

    assert report.status == "market_holiday"
    assert report.network_attempts == []
    assert "all captured provider calls were consumed exactly" in report.isolation_checks
    assert "session providers replay captured call outcomes only" in report.isolation_checks


def test_exact_session_provider_ledger_rejects_unused_calls(tmp_path):
    from ops.rehearsal.session_inputs import SessionInputError

    sandbox = _sandbox(tmp_path, "unused-input")
    now = datetime(2026, 10, 3, 9, 35, tzinfo=ET)

    with pytest.raises(SessionInputError, match="not consumed") as caught:
        run_rehearsal(
            sandbox,
            session="morning",
            now_et=now,
            config_overrides={"execution.fill_stream_enabled": False},
            broker_cassette=_closed_day_cassette(now.date()),
            session_input_payload={"schema": 1, "entries": [
                {"kind": "yfinance.download", "key": "unused", "value": None},
            ]},
        )

    assert caught.value.report.network_attempts == []
