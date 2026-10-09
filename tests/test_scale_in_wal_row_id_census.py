"""The protection write-ahead row id is NOT a scale-in census.

Item 193's last open DONE WHEN was the gap between `pending_protection_restores`
row ids and filed `scale_in|protective_sell_cancelled` events: if ids ran ahead
of events, the measured pair count would be a floor and some cancel could have
gone unrecorded, which on a live book means a position that went naked without
leaving a trace.

Measured on the production database (`/home/qamc/quant-agent/data/quant_agent.db`,
read-only snapshot, 2026-10-01): 15 cancel events exist and every one carries its
own `wal_row_id` — 5, 6, 7, 9, 10, 11, 12, 13, 14, 15, 16, 18, 19, 20, 23 — while
the table's AUTOINCREMENT sequence stands at 23. The eight ids the scale-in path
does not hold (1, 2, 3, 4, 8, 17, 21, 22) were consumed by the OTHER writer of the
same table: the ordinary protective-sell restore path in `src/pipeline.py`. The
sequence is shared, so the maximum id was never a count of scale-ins, and the
sparse set the events name is the complete census.

This test pins the two properties that make that argument mechanical rather than
historical: the two writers draw from one sequence, and a scale-in row is
identifiable by its sentinel `sell_order_id` rather than by its id.
"""

import sqlite3

from src.execution.scale_in import WAL_SCALE_IN_SENTINEL


def _ids(db_path):
    con = sqlite3.connect(str(db_path))
    try:
        return list(con.execute("SELECT id, sell_order_id FROM pending_protection_restores ORDER BY id"))
    finally:
        con.close()


def test_wal_row_ids_are_shared_so_max_id_is_not_a_scale_in_count(tmp_path):
    from src.storage.db import Database

    path = tmp_path / "wal_census.db"
    db = Database(str(path))
    db.initialize()

    # The ordinary protective-sell restore path writes the same table.
    first = db.insert_pending_protection_restore(
        symbol="AAA",
        sell_order_id="sell-order-1",
        position_qty_before_sell=10.0,
        specs_json="[]",
        side="sell",
    )
    # A scale-in allocation: identifiable by the sentinel, not by its id.
    scale_in_one = db.insert_pending_protection_restore(
        symbol="AAA",
        sell_order_id=WAL_SCALE_IN_SENTINEL,
        position_qty_before_sell=10.0,
        specs_json="[]",
        side="sell",
    )
    assert scale_in_one > first, "both writers draw from one id sequence"

    # Discharging a row does not return its id to the sequence, so a later
    # scale-in row is non-contiguous with the earlier one. This is exactly the
    # production shape: a sparse set of scale-in ids inside a denser sequence.
    db.delete_pending_protection_restore(first)
    db.delete_pending_protection_restore(scale_in_one)
    non_scale_in = db.insert_pending_protection_restore(
        symbol="BBB",
        sell_order_id="sell-order-2",
        position_qty_before_sell=5.0,
        specs_json="[]",
        side="sell",
    )
    scale_in_two = db.insert_pending_protection_restore(
        symbol="BBB",
        sell_order_id=WAL_SCALE_IN_SENTINEL,
        position_qty_before_sell=5.0,
        specs_json="[]",
        side="sell",
    )
    assert non_scale_in > scale_in_one
    assert scale_in_two > non_scale_in
    assert scale_in_two - scale_in_one > 1, (
        "the highest row id counts every writer of the table, so it may never be read as the number of scale-ins"
    )

    # The census that IS correct: filter on the sentinel.
    rows = _ids(path)
    scale_in_rows = [i for i, s in rows if s == WAL_SCALE_IN_SENTINEL]
    assert scale_in_rows == [scale_in_two]
