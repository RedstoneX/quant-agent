"""The handle-free counted recorder: a durable row, not a log line.

These sites sit in plain functions with no ledger handle in scope, so the
only thing that proves the record is durable is reading it back out of the
database file after the call returns and the connection is closed.
"""
from __future__ import annotations

import sqlite3

import pytest

from src.sentinel import counted
from src.storage.schema.sentinel_tables import ensure_sentinel_tables


@pytest.fixture()
def desk_db(tmp_path, monkeypatch):
    path = tmp_path / "quant_agent.db"
    conn = sqlite3.connect(path)
    ensure_sentinel_tables(conn=conn)
    conn.close()
    monkeypatch.setattr(counted, "db_path", lambda: path)
    return path


def rows(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT kind, agreed, detail FROM reconciliation_runs ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_swallowed_fault_lands_as_a_durable_disagreed_row(desk_db):
    counted.record_swallowed("macro._write_cache.serialise", ValueError("bad frame"),
                             series_id="DGS10")
    got = rows(desk_db)
    assert len(got) == 1
    kind, agreed, detail = got[0]
    assert kind == "guarded:macro._write_cache.serialise"
    assert agreed == 0
    assert "bad frame" in detail and "DGS10" in detail


def test_clean_pass_and_fault_stay_distinct_states(desk_db):
    counted.record_clean_pass("sector_reference._fetch", symbol="AAA")
    counted.record_swallowed("sector_reference._fetch", RuntimeError("boom"), symbol="BBB")
    got = rows(desk_db)
    assert [r[1] for r in got] == [1, 0]


def test_a_site_never_reached_writes_no_row(desk_db):
    assert rows(desk_db) == []


def test_record_swallowed_here_reads_the_live_exception(desk_db):
    try:
        raise KeyError("missing")
    except Exception:
        counted.record_swallowed_here("macro._as_date.date")
    got = rows(desk_db)
    assert len(got) == 1 and got[0][1] == 0
    assert "KeyError" in got[0][2]


def test_no_database_file_is_never_created_by_recording(tmp_path, monkeypatch, caplog):
    absent = tmp_path / "nothing" / "quant_agent.db"
    monkeypatch.setattr(counted, "db_path", lambda: absent)
    counted.record_swallowed("somewhere", ValueError("x"))
    assert not absent.exists()
    assert "UNCOUNTED" in caplog.text


def test_recorder_never_raises_into_the_money_path(tmp_path, monkeypatch):
    def explode():
        raise OSError("disk gone")

    monkeypatch.setattr(counted, "db_path", explode)
    counted.record_swallowed("somewhere", ValueError("x"))
