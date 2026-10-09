"""Sentinel seams (src/sentinel/): rows are REALLY written and read back, and the pieces build without a pipeline.

The repo has shipped "recordings that record nothing" before, so every test
here drives a real sqlite connection through the real migration ladder and
reads the row back; nothing is asserted on a mock.
"""

from __future__ import annotations

import inspect
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.sentinel.order_attempts import OrderAttemptLog, record_order_attempt_from_event
from src.sentinel.reconciliation import AGREED, DISAGREED, NOT_RUN, ReconciliationLog, record_reconciliation
from src.storage.schema.manager import DatabaseSchema
from src.storage.schema.sentinel_tables import ensure_sentinel_tables
from tests.boundary_harness import check_boundary


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    schema = DatabaseSchema(conn=c)
    schema._create_tables()
    schema._migrate()  # the appended step creates both tables
    yield c
    c.close()


# --- boundary ---------------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    [
        "src.sentinel.order_attempts",
        "src.sentinel.reconciliation",
        "src.storage.schema.sentinel_tables",
    ],
)
def test_sentinel_modules_pass_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


@pytest.mark.parametrize("cls", [OrderAttemptLog, ReconciliationLog])
def test_sentinel_classes_build_from_stubs_with_keyword_only_args(cls):
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())
    cls(**{name: MagicMock(name=name) for name in params})


# --- migration --------------------------------------------------------------


def test_migration_creates_both_tables_and_is_idempotent(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"order_attempts", "reconciliation_runs"} <= names
    ensure_sentinel_tables(conn=conn)  # second application is a no-op
    DatabaseSchema(conn=conn)._migrate()


# --- order attempts ---------------------------------------------------------


def test_order_attempt_row_is_written_and_read_back(conn):
    log = OrderAttemptLog(conn=conn)
    rowid = log.record(
        symbol="ZZZT",
        side="buy",
        qty=3.0,
        outcome="submitted",
        client_order_id="ENT-ZZZT-2026-01-02-x",
        broker_order_id="b1",
        run_id="r1",
        reason="broker_accepted",
        limit_price=1.5,
    )
    assert rowid == 1
    rows = log.recent(limit=5)
    assert len(rows) == 1
    row = rows[0]
    assert (row["symbol"], row["side"], row["qty"], row["outcome"]) == ("ZZZT", "buy", 3.0, "submitted")
    assert row["client_order_id"] == "ENT-ZZZT-2026-01-02-x"
    assert row["broker_order_id"] == "b1" and row["run_id"] == "r1"
    assert row["recorded_at"]
    assert log.count_since(since_utc="2000-01-01 00:00:00") == 1
    assert log.count_since(since_utc="2999-01-01 00:00:00") == 0


def test_rejected_attempt_without_quantity_or_id_is_still_a_row(conn):
    log = OrderAttemptLog(conn=conn)
    log.record(symbol="ZZZT", side=None, qty=None, outcome="rejected", reason="broker_rejected")
    row = log.recent(limit=1)[0]
    assert row["outcome"] == "rejected" and row["client_order_id"] is None and row["qty"] is None


def test_event_funnel_writes_an_attempt_row_for_order_events(conn):
    """`_record_pipeline_event(... 'order' ...)` -- the execution stage's existing
    call -- now lands one row; a non-order event lands none."""
    from src import pipeline_stages

    pipeline = SimpleNamespace(db=SimpleNamespace(conn=conn))
    ctx = SimpleNamespace(run_id="run-9", decision_id="d1")
    with patch.object(pipeline_stages, "_persist_evidence"):
        pipeline_stages._record_pipeline_event(
            pipeline,
            ctx,
            "ZZZT",
            "order",
            "submitted",
            "broker_accepted",
            broker_order_id="b7",
            qty=2,
            limit_price=1.25,
            side="sell",
        )
        pipeline_stages._record_pipeline_event(pipeline, ctx, "ZZZT", "sizing", "accepted")
    rows = OrderAttemptLog(conn=conn).recent(limit=10)
    assert len(rows) == 1
    assert rows[0]["run_id"] == "run-9" and rows[0]["side"] == "sell" and rows[0]["qty"] == 2.0
    assert rows[0]["broker_order_id"] == "b7" and rows[0]["client_order_id"] is None


def test_event_hook_skips_a_db_without_a_sqlite_connection():
    record_order_attempt_from_event(
        db=MagicMock(spec=[]), symbol="ZZZT", outcome="submitted", reason="", run_id=None, details={}
    )


# --- reconciliation ---------------------------------------------------------


def test_not_run_is_distinct_from_agreed_and_disagreed(conn):
    log = ReconciliationLog(conn=conn)
    assert log.status(kind="stop_coverage") == NOT_RUN
    assert log.latest(kind="stop_coverage") is None
    log.record(kind="stop_coverage", agreed=True, run_id="r1")
    assert log.status(kind="stop_coverage") == AGREED
    log.record(kind="stop_coverage", agreed=False, detail="[{'symbol': 'ZZZT'}]", run_id="r2")
    assert log.status(kind="stop_coverage") == DISAGREED
    latest = log.latest(kind="stop_coverage")
    assert latest["run_id"] == "r2" and latest["agreed"] is False and latest["ran_at"]
    # another kind that never ran stays 'not_run' even though the table has rows
    assert log.status(kind="orphan_submits") == NOT_RUN
    assert len({NOT_RUN, AGREED, DISAGREED}) == 3


def test_record_reconciliation_hook_writes_a_row_and_returns_the_result_unchanged(conn):
    db = SimpleNamespace(conn=conn)
    gaps = [{"symbol": "ZZZT", "gap": "no stop"}]
    assert record_reconciliation(db=db, kind="stop_coverage", result=gaps, run_id="r3") is gaps
    assert record_reconciliation(db=db, kind="orphan_submits", result=0) == 0
    rl = ReconciliationLog(conn=conn)
    assert rl.status(kind="stop_coverage") == DISAGREED
    assert "ZZZT" in rl.latest(kind="stop_coverage")["detail"]
    assert rl.status(kind="orphan_submits") == AGREED
    assert rl.status(kind="stop_out_fills") == NOT_RUN


def test_record_reconciliation_hook_tolerates_a_db_without_a_connection():
    assert record_reconciliation(db=MagicMock(spec=[]), kind="stop_coverage", result=[1]) == [1]


def test_reconciler_hooks_are_wired_at_the_four_return_sites():
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    prot = (root / "src/pipeline_protection.py").read_text()
    fr = (root / "src/protection/fill_reconciler.py").read_text()
    kinds = re.findall(r'record_reconciliation\(db=self\.db, kind="(\w+)"', prot + fr)
    assert sorted(kinds) == ["orphan_submits", "recorded_stop_levels", "stop_coverage", "stop_out_fills"]
