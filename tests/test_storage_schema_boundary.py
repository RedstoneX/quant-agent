"""Boundary witness: the lifted schema cluster builds and runs with no Database or pipeline behind it.

The only collaborator is the sqlite3 connection, passed keyword-only
(clause 5 of tests/boundary_harness.py). Also proves the per-call shim on
Database still reaches a connection swapped in after construction.
"""

from __future__ import annotations

import inspect
import sqlite3
from unittest.mock import MagicMock

from src.storage.db import Database
from src.storage.schema.manager import DatabaseSchema
from tests.boundary_harness import check_boundary


def test_constructor_takes_only_keyword_collaborators():
    params = inspect.signature(DatabaseSchema).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())
    DatabaseSchema(conn=MagicMock(name="conn"))


def test_module_passes_the_boundary_harness():
    # The harness resolves a module to one file, so it is applied to the
    # package's implementation module (as tests/test_protection_boundary.py does).
    verdict = check_boundary("src.storage.schema.manager")
    assert not verdict.failures, verdict.failures


def test_schema_builds_and_migrates_on_a_bare_connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    DatabaseSchema(conn=conn)._create_tables()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "trades" in tables
    DatabaseSchema(conn=conn)._migrate()  # idempotent second pass


def test_database_shim_builds_the_schema_object_per_call():
    db = Database(":memory:")
    db.conn = MagicMock(name="swapped_in_after_construction")
    db._migrate()
    assert db.conn.commit.called
