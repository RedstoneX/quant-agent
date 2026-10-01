"""Board item 224 — the realised `(sector, side)` weights of the orders the
constructor actually built, one durable row per run.

RECORDING ONLY. These tests assert the row's shape, unit and NULL
discipline. Nothing here derives, tunes or proposes a sector cap, and
nothing in the product reads these rows back into a decision.
"""
import ast
import json
import sqlite3
from pathlib import Path

import pytest

from src.models import TradeDecision
from src.storage.db import Database

SRC = Path(__file__).resolve().parents[1] / "src"


def _decision(action, symbol, pct):
    short = action == "SHORT"
    return TradeDecision(
        action=action, symbol=symbol, allocation_pct=pct,
        entry_price=100.0,
        stop_loss=105.0 if short else 95.0,
        take_profit=80.0 if short else 120.0,
        reasoning="test",
    )


@pytest.fixture()
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    return d


def _row(db):
    cur = db.conn.execute("SELECT * FROM realised_sector_weights")
    cols = [c[0] for c in cur.description]
    rows = cur.fetchall()
    return [dict(zip(cols, r)) for r in rows]


def test_weights_are_grouped_by_sector_and_side(db):
    ok = db.record_realised_sector_weights(
        decisions=[
            _decision("BUY", "AAA", 4.0),
            _decision("BUY", "BBB", 6.0),
            _decision("SHORT", "CCC", 3.0),
            _decision("SELL", "DDD", 50.0),
        ],
        sectors={"AAA": "Technology", "BBB": "Technology", "CCC": "Energy"},
        total_value=100_000.0,
        run_id="run-1",
    )
    assert ok
    rows = _row(db)
    assert len(rows) == 1
    weights = {(w["sector"], w["side"]): w for w in json.loads(rows[0]["weights_json"])}
    assert weights[("Technology", "long")]["weight_pct"] == pytest.approx(10.0)
    assert weights[("Technology", "long")]["orders"] == 2
    assert weights[("Energy", "short")]["weight_pct"] == pytest.approx(3.0)
    # A reduction is never added into a weight, but it is counted.
    assert rows[0]["entry_orders_built"] == 3
    assert rows[0]["reducing_orders_built"] == 1


def test_denominator_is_recorded_explicitly_and_matches_item_222_unit(db):
    db.record_realised_sector_weights(
        decisions=[_decision("BUY", "AAA", 4.0)],
        sectors={"AAA": "Technology"}, total_value=50_000.0, run_id="run-2",
    )
    row = _row(db)[0]
    assert row["denominator"] == Database.REALISED_SECTOR_WEIGHT_DENOMINATOR
    d = row["denominator"]
    assert "total account equity" in d
    assert "before the gross multiplier" in d
    assert row["total_value"] == pytest.approx(50_000.0)


def test_unknown_sector_is_null_never_other(db):
    db.record_realised_sector_weights(
        decisions=[_decision("BUY", "ZZZ", 2.5)],
        sectors={}, total_value=10_000.0, run_id="run-3",
    )
    row = _row(db)[0]
    entries = json.loads(row["weights_json"])
    assert entries[0]["sector"] is None
    assert row["unknown_sector_orders"] == 1
    assert "other" not in row["weights_json"].lower()


def test_a_run_that_built_nothing_records_that_fact_not_zero_concentration(db):
    db.record_realised_sector_weights(
        decisions=[], sectors={}, total_value=10_000.0, run_id="run-4",
    )
    row = _row(db)[0]
    assert row["weights_json"] is None
    assert row["entry_orders_built"] == 0


def test_one_row_per_run(db):
    for _ in range(3):
        db.record_realised_sector_weights(
            decisions=[_decision("BUY", "AAA", 1.0)],
            sectors={"AAA": "Energy"}, total_value=1.0, run_id="run-5",
        )
    assert len(_row(db)) == 1


def test_recording_is_reachable_from_executable_product_code():
    """The write must be reached from the product, not only from a test."""
    stages = (SRC / "pipeline_stages.py").read_text()
    tree = ast.parse(stages)
    helper = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and n.name == "_record_realised_sector_weights"
    )
    assert any(
        isinstance(n, ast.Attribute) and n.attr == "record_realised_sector_weights"
        for n in ast.walk(helper)
    )
    callers = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "_record_realised_sector_weights"
    ]
    assert len(callers) == 1, "exactly one product call site"


def test_nothing_reads_the_table_back_into_a_decision():
    """RECORDING ONLY — no product module may SELECT from this table."""
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        if "realised_sector_weights" in text and "SELECT" in text.upper():
            for line in text.splitlines():
                up = line.upper()
                if "REALISED_SECTOR_WEIGHTS" in up and "SELECT" in up:
                    offenders.append(f"{path}: {line.strip()}")
    assert not offenders, offenders


def test_the_recording_never_blocks_a_trade(db):
    class _Boom:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("disk gone")

    db.conn = _Boom()
    assert db.record_realised_sector_weights(
        decisions=[_decision("BUY", "AAA", 1.0)],
        sectors={"AAA": "Energy"}, total_value=1.0, run_id="run-6",
    ) is False
