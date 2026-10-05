"""The retrying SQLite write lock, as a plain function.

Lifted out of `src.storage.db` so a store can serialise its writes without
owning, importing or reaching back into the `Database` object. Takes the
process lock BY VALUE; it holds no state of its own.
"""

import logging
import sqlite3
import threading
import time as _time

logger = logging.getLogger(__name__)


def locked_write(lock: threading.Lock, do, *, label: str = "write"):
    """Run a write closure under `lock` with bounded retry on cross-process
    SQLite lock contention.

    busy_timeout (5s) only covers the lock-WAIT; a WAL checkpoint stall
    longer than that surfaces as `OperationalError: database is locked`
    AFTER the wait expires. The bare execute path would then either raise
    (trade / recovery-queue inserts) or silently lose the row
    (agent_logs). intra_check is explicitly exempt from the cross-mode
    session lock (CLAUDE.md), so it WILL write concurrently with a long
    morning — the in-process threading.Lock serializes only within THIS
    process; this retry is what protects the write across processes.

    ~1.55s of extra backoff on top of the 5s busy_timeout; if still
    locked after that, re-raise (a stuck DB is a real problem worth
    surfacing, not silently dropping).
    """
    last_exc: sqlite3.OperationalError | None = None
    for attempt in range(5):
        try:
            with lock:
                return do()
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last_exc = exc
            logger.warning(
                "DB %s contended (attempt %d/5): %s — retrying",
                label, attempt + 1, exc,
            )
            _time.sleep(0.05 * (2 ** attempt))  # 0.05,0.1,0.2,0.4,0.8s
    logger.error("DB %s still locked after retries — giving up: %s", label, last_exc)
    raise last_exc
