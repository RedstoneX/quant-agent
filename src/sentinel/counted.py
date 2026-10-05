"""A counted record for a money-path catch-all in a function holding NO db handle.

Why this module exists
----------------------
``src.sentinel.guarded`` (the broker's) and ``src.sentinel.entry_guard`` (the
entry desk's) both make a swallowed fault loud and counted, and both need the
caller to already hold a ledger handle -- ``self.db`` on the broker, the
pipeline's ``db`` on the entry path. Measured 2026-10-05 over the guard's own
derived money scope (223 modules, ``scripts/money_modules.py``): of the 98
silently-swallowed money-path handlers still open, only 21 sit in a function
that can reach a handle. The other 77 are plain module-level functions -- the
FRED and yfinance fetchers, the sector reference, the notifier alerts, the
decision checkpoint, the evidence gate -- and they had nowhere to write, so
the campaign stalled there rather than count a log line as a record.

Threading a handle into eighty call sites was the obvious answer and is not
the one this desk already uses. ``src.data_paths`` is the single resolved
location of the desk's on-disk data: it derives the database path from this
file's own position, COMPUTES IT ON EVERY CALL, caches nothing and stores no
absolute path. So a handle was never actually required to reach the ledger --
only a handle was required to reach it CHEAPLY. On an error path, which is
what every one of these sites is, the cost of opening the desk's sqlite file
for one insert is not a reason to leave the failure uncounted.

What it writes
--------------
Nothing new. The row goes on the EXISTING reconciliation channel via
``record_guarded_outcome``, so these sites are indistinguishable downstream
from the 21 already cleared, and the three states stay distinct:

  * ``not_run``   -- the site was never reached (no row at all)
  * ``agreed``    -- it ran and swallowed nothing (``record_clean_pass``)
  * ``disagreed`` -- it ran and swallowed a fault (``record_swallowed``)

This module stores no bookkeeping of its own: no list of sites, no buffer, no
flush interval, no retry count. Every call opens, writes, commits and closes.
Handlers keep their existing behaviour exactly; these calls only add what they
RECORD, never re-raise, and never return anything a caller branches on.

Never pass a credential, token or account id in ``context``: the row is
durable and the repository is public.
"""
from __future__ import annotations

import logging
import sqlite3
import sys

from src.data_paths import db_path
from src.sentinel.reconciliation import record_guarded_outcome
from src.storage.schema.sentinel_tables import ensure_sentinel_tables

logger = logging.getLogger(__name__)


class _Handle:
    """The minimal shape ``record_reconciliation`` reads: an object with ``.conn``."""

    __slots__ = ("conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn


def _open() -> sqlite3.Connection | None:
    """The desk's ledger, opened fresh, or None when this checkout has no database.

    The path is NOT created. ``sqlite3.connect`` would happily make an empty
    file wherever this runs -- in a test, in a throwaway worktree, in an ops
    one-shot -- and a brand new database is not the desk's audit trail. When
    the file is absent there is no durable store to write to, and that is
    reported loudly by the caller rather than silently satisfied.
    """
    path = db_path()
    if not path.exists():
        return None
    conn = sqlite3.connect(path)
    ensure_sentinel_tables(conn=conn)
    return conn


def _record(where: str, exc: BaseException | None, log, run_id: str | None,
            context: dict) -> None:
    conn = None
    emitter = log or logger
    try:
        conn = _open()
        if conn is None:
            emitter.error(
                "no desk database at %s: the fault at %s is LOUD but UNCOUNTED",
                db_path(), where, exc_info=exc,
            )
        else:
            record_guarded_outcome(db=_Handle(conn), where=where, exc=exc,
                                   run_id=run_id, log=log, context=context)
    except Exception:  # noqa: BLE001 - an observer must never break the money path
        emitter.error("could not count the swallowed fault at %s", where, exc_info=True)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                emitter.error("could not close the ledger after counting %s", where,
                              exc_info=True)


def record_swallowed(where: str, exc: BaseException, *, log=None,
                     run_id: str | None = None, **context) -> None:
    """One ``disagreed`` row for a handler that caught ``exc`` and carried on."""
    _record(where, exc, log, run_id, context)


def record_swallowed_here(where: str, *, log=None, run_id: str | None = None,
                          **context) -> None:
    """For a handler that binds no name: reads the exception currently being handled."""
    exc = sys.exc_info()[1]
    if exc is not None:
        _record(where, exc, log, run_id, context)


def record_clean_pass(where: str, *, log=None, run_id: str | None = None,
                      **context) -> None:
    """One ``agreed`` row: this site ran and swallowed nothing."""
    _record(where, None, log, run_id, context)
