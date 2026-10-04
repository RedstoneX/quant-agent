"""Fill-reconciler catch-alls are loud: traceback + counted row; clean pass has its own row; unreached writes none."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from src.protection.fill_reconciler import FillReconciler
from src.sentinel import reconciliation


@pytest.fixture
def rows(monkeypatch):
    seen = []
    monkeypatch.setattr(reconciliation, "record_reconciliation",
                        lambda **kw: seen.append((kw["kind"], kw["result"])))
    return seen


def _rec(db, broker=None):
    return FillReconciler(broker=broker, db=db, config=SimpleNamespace(), flag_stop_out_anomaly=None,
                          format_qty=str, parse_broker_fill_timestamp=lambda x: x)


def test_swallowed_fault_logs_traceback_and_writes_disagreed_row(rows, caplog):
    class Db:
        def get_orphaned_pending_submits(self):
            raise TypeError("boom")

    with caplog.at_level(logging.ERROR):
        assert _rec(Db())._reconcile_orphan_pending_submits() == 0  # still swallowed
    assert [r[0] for r in rows] == ["guarded:fill_reconciler.orphan.db_read"]
    assert rows[0][1][0]["error"] == "TypeError"
    assert any(r.exc_info and r.levelno == logging.ERROR for r in caplog.records)


def test_clean_pass_writes_its_own_agreed_row(rows):
    class Db:
        def get_orphaned_pending_submits(self):
            return []

    assert _rec(Db())._reconcile_orphan_pending_submits() == 0
    assert rows == [("guarded:fill_reconciler.orphan.db_read", [])]


def test_unreached_site_writes_no_row(rows):
    class Db:
        def get_orphaned_pending_submits(self):
            return []

    _rec(Db())._reconcile_orphan_pending_submits()
    assert not any("stop_out" in r[0] or "surface" in r[0] for r in rows)


def test_observer_fault_never_breaks_reconciliation(monkeypatch):
    def bad(**_kw):
        raise RuntimeError("ledger locked")
    monkeypatch.setattr(reconciliation, "record_reconciliation", bad)

    class Db:
        def get_orphaned_pending_submits(self):
            raise ValueError("x")

    assert _rec(Db())._reconcile_orphan_pending_submits() == 0
