"""One conversion proven end to end against a REAL ledger, nothing mocked at the record path."""
import logging

from src.execution import exit_path_records as epr
from src.storage.db import Database

KIND = "guarded:execution.exit_path_records.last_trail_states"


def _db(tmp_path):
    db = Database(str(tmp_path / "b6y2.db"))
    db.initialize()
    return db


def _rows(db):
    return db.conn.execute(
        "SELECT agreed, detail FROM reconciliation_runs WHERE kind = ?", (KIND,)
    ).fetchall()


def test_never_reached_writes_no_row(tmp_path):
    assert _rows(_db(tmp_path)) == []


def test_failure_writes_exactly_one_disagreed_row_and_a_traceback(tmp_path, caplog):
    db = _db(tmp_path)
    db.get_latest_symbol_evidence = lambda *a, **k: (_ for _ in ()).throw(TypeError("boom"))
    with caplog.at_level(logging.ERROR):
        assert epr.last_trail_states(db, ["ZZZ"]) == {}
    rows = _rows(db)
    assert len(rows) == 1 and rows[0][0] == 0
    assert "TypeError" in rows[0][1] and "boom" in rows[0][1]
    assert any(r.exc_info and r.levelno == logging.ERROR for r in caplog.records)


def test_clean_pass_writes_exactly_one_agreed_row(tmp_path):
    db = _db(tmp_path)
    assert epr.last_trail_states(db, ["ZZZ"]) == {}
    rows = _rows(db)
    assert len(rows) == 1 and rows[0][0] == 1
