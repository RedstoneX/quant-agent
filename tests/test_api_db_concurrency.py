"""Stage 2 Checkpoint C — concurrent read-only API access never blocks or
corrupts the trading process's SQLite writer.

`src/storage/db.py` already runs in WAL mode specifically so a second,
independent connection can read while the trading process writes (see its
`initialize()` docstring). `src/api/db_reads.py` opens its own `mode=ro`
connection rather than sharing the trading `Database` instance's connection
or lock. This test proves the combination actually holds under concurrent
load: a background thread hammers real trading writes (`insert_trade`) while
the main thread hammers `db_reads.get_trades()` reads against the same file,
and afterward both (a) the writer saw zero `OperationalError: database is
locked` and (b) the row count matches what was written and (c) SQLite's own
integrity check passes.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

import src.api.db_reads as db_reads
from src.storage.db import Database

N_WRITES = 150


def test_concurrent_api_reads_do_not_block_or_corrupt_trading_writes(tmp_path, monkeypatch):
    db_path = tmp_path / "concurrency_test.db"
    writer = Database(str(db_path))
    writer.initialize()

    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(db_path))

    write_errors: list[Exception] = []
    read_errors: list[Exception] = []
    stop_reading = threading.Event()

    def _write_loop():
        for i in range(N_WRITES):
            try:
                writer.insert_trade(
                    symbol="AAPL", action="BUY", qty=1, price=100.0 + i,
                    reasoning="concurrency test", run_id=f"run-{i:06d}",
                )
            except sqlite3.OperationalError as exc:
                write_errors.append(exc)

    def _read_loop():
        while not stop_reading.is_set():
            try:
                db_reads.get_trades(limit=1000)
            except Exception as exc:  # pragma: no cover - defensive, should never fire
                read_errors.append(exc)

    reader_thread = threading.Thread(target=_read_loop, daemon=True)
    writer_thread = threading.Thread(target=_write_loop)

    reader_thread.start()
    writer_thread.start()
    writer_thread.join(timeout=30)
    stop_reading.set()
    reader_thread.join(timeout=5)

    assert not write_errors, f"writer saw {len(write_errors)} OperationalError(s) from a concurrent reader"
    assert not read_errors, f"db_reads saw {len(read_errors)} unexpected error(s): {read_errors}"

    rows = writer.get_trades(limit=N_WRITES + 10)
    assert len(rows) == N_WRITES

    integrity = writer.conn.execute("PRAGMA integrity_check").fetchone()[0]
    assert integrity == "ok"

    writer.close()


def test_api_read_connection_is_physically_unable_to_write(tmp_path, monkeypatch):
    """Defense-in-depth check: the mode=ro connection db_reads.py opens
    rejects a write attempt at the SQLite layer itself, not just by
    convention. (This directly exercises `_connect()`, not just the public
    read functions, since none of the public functions ever attempt a
    write — that's the point.)"""
    db_path = tmp_path / "ro_test.db"
    writer = Database(str(db_path))
    writer.initialize()
    writer.close()

    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(db_path))
    conn = db_reads._connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO trades (symbol, action, qty, price, reasoning, run_id) "
                         "VALUES ('X', 'BUY', 1, 1.0, 'x', 'run-x')")
    finally:
        conn.close()
