"""End to end, nothing mocked at the record path: the quantity gate's positions read.

A real ledger is lent through the client already in scope. Never reached writes
no row; a swallowed fault writes exactly one ``disagreed`` row carrying the error
type and message; a clean pass writes exactly one ``agreed`` row.
"""
import json
from types import SimpleNamespace

import pytest

from src.execution.order_gates import quantity_refusal_live
from src.storage.db import Database

KIND = "guarded:broker.order_gates.positions_read"


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "gate.db"))
    d.initialize()
    return d


def _rows(db):
    return db.conn.execute("SELECT * FROM reconciliation_runs WHERE kind = ?", (KIND,)).fetchall()


def _gate(client, side):
    return quantity_refusal_live("ZZZZ", "ZZZZ", 1, side, price=10.0, client=client,
                                 get_fractionability=None, get_account=None, max_position_pct=None)


def test_never_reached_writes_nothing(db):
    _gate(SimpleNamespace(db=db, get_all_positions=lambda: []), "buy")
    assert _rows(db) == []


def test_failure_writes_one_disagreed_row(db):
    def boom():
        raise ValueError("positions down")
    _gate(SimpleNamespace(db=db, get_all_positions=boom), "sell")
    rows = _rows(db)
    assert len(rows) == 1
    assert not rows[0]["agreed"]
    detail = json.loads(rows[0]["detail"])
    assert detail[0]["error"] == "ValueError" and detail[0]["message"] == "positions down"


def test_clean_pass_writes_one_agreed_row(db):
    _gate(SimpleNamespace(db=db, get_all_positions=lambda: []), "sell")
    rows = _rows(db)
    assert len(rows) == 1 and rows[0]["agreed"]
