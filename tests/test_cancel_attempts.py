"""Cancels are counted: every broker cancel writes one order_attempts row, failures included (real sqlite)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.sentinel.cancel_attempts import install_cancel_recording
from src.sentinel.order_attempts import OrderAttemptLog
from src.storage.schema.manager import DatabaseSchema
from tests.boundary_harness import check_boundary


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    s = DatabaseSchema(conn=c)
    s._create_tables()
    s._migrate()
    yield c
    c.close()


def _broker(conn, inner):
    broker = SimpleNamespace(client=inner)
    install_cancel_recording(broker=broker, conn_getter=lambda: conn)
    return broker


def test_module_passes_boundary_check():
    assert check_boundary("src.sentinel.cancel_attempts").passed


def test_successful_cancel_is_recorded(conn):
    inner = MagicMock()
    _broker(conn, inner).client.cancel_order_by_id("oid-1")
    row = OrderAttemptLog(conn=conn).recent(limit=5)[0]
    assert (row["outcome"], row["broker_order_id"]) == ("cancelled", "oid-1")
    assert row["recorded_at"]


def test_failed_cancel_is_recorded_with_error_and_still_raises(conn):
    inner = MagicMock()
    inner.cancel_order_by_id.side_effect = RuntimeError("order is not cancelable")
    with pytest.raises(RuntimeError):
        _broker(conn, inner).client.cancel_order_by_id("oid-2")
    row = OrderAttemptLog(conn=conn).recent(limit=5)[0]
    assert row["outcome"] == "cancel_failed" and "not cancelable" in row["reason"]


def test_cancel_all_records_one_row_per_order_including_refusals(conn):
    inner = MagicMock()
    inner.cancel_orders.return_value = [SimpleNamespace(id="a", status=200), SimpleNamespace(id="b", status=422)]
    _broker(conn, inner).client.cancel_orders()
    rows = {r["broker_order_id"]: r["outcome"] for r in OrderAttemptLog(conn=conn).recent(limit=5)}
    assert rows == {"a": "cancelled", "b": "cancel_failed"}


def test_recording_failure_never_breaks_the_cancel():
    inner = MagicMock()
    inner.cancel_order_by_id.return_value = "ok"
    broker = SimpleNamespace(client=inner)
    install_cancel_recording(broker=broker, conn_getter=lambda: None)
    assert broker.client.cancel_order_by_id("x") == "ok"


def test_install_is_idempotent_and_delegates_other_attributes(conn):
    inner = MagicMock()
    inner.get_account.return_value = "acct"
    b = _broker(conn, inner)
    first = b.client
    install_cancel_recording(broker=b, conn_getter=lambda: conn)
    assert b.client is first and b.client.get_account() == "acct"


def test_pipeline_installs_the_wrapper():
    assert "install_cancel_recording" in (Path(__file__).parent.parent / "src" / "pipeline.py").read_text()


def test_adapter_does_not_surface_the_client_order_id():
    """Why the column is NULL: the desk now sends a derived key on submits (src/execution/order_idempotency.py)
    but no adapter result dict carries it back, so the funnel has nothing to record. If one ever does, the
    column fills in and this test must be updated with that change."""
    src = "".join(
        p.read_text()
        for p in (Path(__file__).parent.parent / "src").rglob("*.py")
        if "sentinel" not in p.parts and "storage" not in p.parts
    )
    assert '"client_order_id"' not in src and "'client_order_id'" not in src


def test_install_skips_a_broker_with_no_trading_client(caplog):
    broker = SimpleNamespace()
    with caplog.at_level("WARNING"):
        install_cancel_recording(broker=broker, conn_getter=lambda: None)
    assert not hasattr(broker, "client")
    assert "SimpleNamespace" in caplog.text and "will not be counted" in caplog.text
