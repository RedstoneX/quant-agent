"""The morning-research catch-alls are loud: fault, clean pass, unreached site."""

import logging
import sqlite3
from types import SimpleNamespace

from src.sentinel.morning_guarded import record_morning_fault
from src.storage.db import Database


def _owner(tmp_path):
    db = Database(str(tmp_path / "morning_guarded.db"))
    db.initialize()
    return SimpleNamespace(db=db), db.conn


def _rows(conn):
    try:
        return [tuple(r) for r in conn.execute("select * from reconciliation_runs").fetchall()]
    except sqlite3.Error:
        return []


def test_swallowed_fault_logs_traceback_and_counts(tmp_path, caplog):
    owner, conn = _owner(tmp_path)
    with caplog.at_level(logging.ERROR):
        try:
            raise TypeError("boom")
        except Exception as exc:  # noqa: BLE001
            record_morning_fault(owner, "unit", exc, symbol="X")
    assert any(r.exc_info for r in caplog.records)
    rows = _rows(conn)
    assert len(rows) == 1 and "morning.unit" in str(rows[0]) and "TypeError" in str(rows[0])


def test_clean_pass_writes_its_own_row(tmp_path):
    owner, conn = _owner(tmp_path)
    record_morning_fault(owner, "unit")
    rows = _rows(conn)
    assert len(rows) == 1 and "TypeError" not in str(rows[0])


def test_unreached_site_writes_nothing(tmp_path):
    _, conn = _owner(tmp_path)
    assert _rows(conn) == []


def test_no_handle_still_logs_and_never_raises(caplog):
    with caplog.at_level(logging.ERROR):
        record_morning_fault(SimpleNamespace(), "unit", ValueError("x"))
    assert caplog.records
