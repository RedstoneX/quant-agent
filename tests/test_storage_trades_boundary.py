"""Boundary witness: the lifted trade-ledger write cluster builds and runs with no Database or pipeline behind it.

Collaborators are the sqlite3 connection, the lock, the locked-write runner
and the three small SQL/time helpers, all keyword-only (clause 5 of
tests/boundary_harness.py). None of them is itself a lifted method, so the
per-call shim on Database can never hand the new object a function that
calls back into it. Also proves the shim still reaches a collaborator
swapped in after construction.
"""

from __future__ import annotations

import inspect
import sqlite3
import threading
from unittest.mock import MagicMock

from src.storage.db import Database
from src.storage.schema.manager import DatabaseSchema
from src.storage.trades.ledger import TradeLedger
from tests.boundary_harness import check_boundary


def _bare_ledger(conn: sqlite3.Connection) -> TradeLedger:
    def locked_write(do, *, label="write"):
        return do()

    return TradeLedger(
        conn=conn,
        lock=threading.Lock(),
        locked_write=locked_write,
        executed_trade_predicate=Database._executed_trade_predicate,
        sqlite_utc_timestamp=Database._sqlite_utc_timestamp,
        et_day_utc_bounds=Database._et_day_utc_bounds,
    )


def test_constructor_takes_only_keyword_collaborators():
    params = inspect.signature(TradeLedger).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())
    _bare_ledger(MagicMock(name="conn"))


def test_no_collaborator_is_a_lifted_method():
    lifted = {n for n, _ in inspect.getmembers(TradeLedger, inspect.isfunction) if n != "__init__"}
    assert not lifted & set(inspect.signature(TradeLedger).parameters), (
        "a collaborator that is also a lifted method makes the shim loop forever"
    )


def test_module_passes_the_boundary_harness():
    verdict = check_boundary("src.storage.trades.ledger")
    assert not verdict.failures, verdict.failures


def test_ledger_runs_on_a_bare_connection_with_no_database_object():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    DatabaseSchema(conn=conn)._create_tables()
    ledger = _bare_ledger(conn)
    assert ledger.get_trades(limit=5) == []
    assert ledger.get_pending_repegs() == []
    assert ledger.get_known_broker_order_ids("ZZZZ") == set()


def test_database_shim_builds_the_ledger_object_per_call():
    db = Database(":memory:")
    db.conn = MagicMock(name="swapped_in_after_construction")
    db.conn.execute.return_value.fetchall.return_value = []
    db.get_trades(limit=1)
    assert db.conn.execute.called
