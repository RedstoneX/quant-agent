"""Entry-order catch-alls are loud: traceback + counted row; clean pass has its own row."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from src import pipeline_entry_orders as eo
from src.sentinel import reconciliation


@pytest.fixture
def rows(monkeypatch):
    seen = []
    monkeypatch.setattr(reconciliation, "record_reconciliation", lambda **kw: seen.append((kw["kind"], kw["result"])))
    return seen


def _pipeline(db):
    return SimpleNamespace(db=db)


def test_swallowed_fault_logs_traceback_and_writes_disagreed_row(rows, caplog):
    class Db:
        def delete_pending_repeg(self, _id):
            raise TypeError("boom")

    with caplog.at_level(logging.ERROR):
        eo._delete_repeg_wal(_pipeline(Db()), 7)  # still swallowed, never raises
    assert rows and rows[0][0] == "guarded:entry_orders.repeg.wal_delete"
    assert rows[0][1][0]["error"] == "TypeError"
    assert any(r.exc_info and r.levelno == logging.ERROR for r in caplog.records)


def test_clean_pass_writes_its_own_agreed_row(rows):
    class Db:
        def resolve_pending_repeg(self, *_a):
            return None

        def repoint_trade_broker_order_id(self, *_a, **_k):
            return 1

    assert eo._repoint_trade(_pipeline(Db()), 1, "old", "new", "SYM") is not None
    assert rows == [("guarded:entry_orders.repeg.repoint", [])]


def test_site_never_reached_writes_no_row(rows):
    eo._delete_repeg_wal(_pipeline(object()), None)  # early return before the try
    assert rows == []
