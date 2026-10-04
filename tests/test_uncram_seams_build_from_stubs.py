"""The four modules split out of guarded files build and run from stubs alone.

Each piece used to live inside a file too large to hold in one head. The
boundary test for the split is that none needs the object that owned it: a
bare sqlite connection, a SimpleNamespace host, a float, a hand-built record.
"""
from __future__ import annotations

import logging
import sqlite3
from types import SimpleNamespace

from src.execution.broker_parts.stop_amend_pure import (
    _is_terminal_broker_rejection, _quantize_price,
)
from src.execution.stop_level_report import StopLevelMismatch, report_stop_level_mismatches
from src.protection.collaborator_builders import _build_repeg_drain, _collab_of
from src.storage.schema.prune_indexes import ensure_prune_indexes


def test_prune_indexes_build_on_a_bare_connection_and_skip_missing_tables(caplog):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE trades (timestamp TEXT, position_id TEXT)")
    with caplog.at_level(logging.WARNING):
        ensure_prune_indexes(conn)
        ensure_prune_indexes(conn)  # idempotent
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_trades_timestamp", "idx_trades_position_id"} <= names
    assert "Index creation failed for agent_logs.timestamp" in caplog.text


def test_collaborator_builder_reads_a_plain_host():
    host = SimpleNamespace(broker="B", db="D", _delete_repeg_row="R")
    assert _collab_of(host, "broker") == "B"
    assert _build_repeg_drain(host) is not None


def test_pure_amend_helpers_need_no_broker():
    assert _quantize_price(106.515) == 106.52
    assert _quantize_price(float("nan")) is None
    assert _is_terminal_broker_rejection(SimpleNamespace(status_code=422)) is True
    assert _is_terminal_broker_rejection(SimpleNamespace(status_code=500)) is False


def test_mismatch_report_pages_from_hand_built_records(monkeypatch, caplog):
    sent = []
    from src import notifier
    monkeypatch.setattr(notifier, "send_owner_alert", lambda body, symbols: sent.append(symbols))
    with caplog.at_level(logging.ERROR):
        report_stop_level_mismatches([StopLevelMismatch("AAA", 1.0, 2.0, False, "differs")])
    assert sent == [["AAA"]]
    assert "STOP RECORD MISMATCH: AAA" in caplog.text
