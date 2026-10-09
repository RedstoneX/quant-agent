"""Boundary witness: the lifted trade-calibration / reporting read cluster builds and runs with no Database or pipeline behind it.

Collaborators are the sqlite3 connection, the lock and the executed-trade SQL
predicate, all keyword-only (clause 5 of tests/boundary_harness.py). Also
proves the per-call shim on Database still reaches a collaborator swapped in
after construction.
"""

from __future__ import annotations

import inspect
import sqlite3
import threading
from unittest.mock import MagicMock

from src.storage.analytics.calibration import TradeAnalytics
from src.storage.db import Database
from src.storage.schema.manager import DatabaseSchema
from tests.boundary_harness import check_boundary


def test_constructor_takes_only_keyword_collaborators():
    params = inspect.signature(TradeAnalytics).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())
    TradeAnalytics(conn=MagicMock(name="conn"), lock=threading.Lock(), executed_trade_predicate=lambda: "1=1")


def test_module_passes_the_boundary_harness():
    # The harness resolves a module to one file, so it targets the package's
    # implementation module, as tests/test_storage_schema_boundary.py does.
    verdict = check_boundary("src.storage.analytics.calibration")
    assert not verdict.failures, verdict.failures


def test_calibration_runs_on_a_bare_connection_with_no_database_object():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    DatabaseSchema(conn=conn)._create_tables()
    analytics = TradeAnalytics(
        conn=conn, lock=threading.Lock(), executed_trade_predicate=Database._executed_trade_predicate
    )
    stats = analytics.compute_trade_calibration(lookback_days=45)
    assert isinstance(stats, dict)
    assert analytics.get_daily_pnl(limit=5) == []
    assert analytics.get_earliest_daily_pnl() is None


def test_database_shim_builds_the_analytics_object_per_call():
    db = Database(":memory:")
    db.conn = MagicMock(name="swapped_in_after_construction")
    db.conn.execute.return_value.fetchall.return_value = []
    db.get_daily_pnl(limit=1)
    assert db.conn.execute.called
