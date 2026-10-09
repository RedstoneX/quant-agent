"""Board item 193 — the protection-restore WAL id ceiling is attributable.

`pending_protection_restores.id` is a SHARED autoincrement. The scale-in
cancel path (`scale_in.WAL_SCALE_IN_SENTINEL`) and the protected-sell exit
path (`pipeline._WAL_SELL_SENTINEL`) both draw ids from it, and every row is
DELETED once discharged. That is why the production id ceiling stood above
the count of filed `scale_in|protective_sell_cancelled` events, and why the
cancel count could only be called a floor: an id spent by the other writer,
or by a preparation that rolled its cancel back, left nothing behind.

This file proves the attribution now survives the row:
  1. every insert writes exactly one never-deleted audit row carrying the
     SAME id the caller was handed, with the writer's own sentinel;
  2. deleting (discharging) the WAL row leaves the audit row standing, so a
     discharged id is still attributable;
  3. the two writers are told apart by sentinel, which is what turns an
     id-ceiling gap into an answer rather than an argument;
  4. an audit-table failure does NOT stop the protective WAL row being
     persisted — bookkeeping never blocks protection.
"""

import json

from src.execution.scale_in import WAL_SCALE_IN_SENTINEL
from src.pipeline_protection import _WAL_SELL_SENTINEL
from src.storage.db import Database


def _mk_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    return db


def _insert(db, symbol, sentinel, qty, side):
    return db.insert_pending_protection_restore(
        symbol=symbol,
        sell_order_id=sentinel,
        position_qty_before_sell=qty,
        specs_json=json.dumps([{"stop_price": 1.0}]),
        side=side,
    )


def test_every_id_handed_out_is_recorded_with_its_writer(tmp_path):
    db = _mk_db(tmp_path)
    scale_id = _insert(db, "AAA", WAL_SCALE_IN_SENTINEL, 10.0, "sell")
    sell_id = _insert(db, "BBB", _WAL_SELL_SENTINEL, 5.0, "sell")
    short_id = _insert(db, "CCC", WAL_SCALE_IN_SENTINEL, -7.0, "buy")

    audit = db.get_protection_restore_wal_audit()
    assert [r["row_id"] for r in audit] == [scale_id, sell_id, short_id]
    by_id = {r["row_id"]: r for r in audit}
    assert by_id[scale_id]["sell_order_id"] == WAL_SCALE_IN_SENTINEL
    assert by_id[sell_id]["sell_order_id"] == _WAL_SELL_SENTINEL
    assert by_id[short_id]["position_qty_before_sell"] == -7.0
    assert by_id[short_id]["side"] == "buy"
    assert by_id[scale_id]["created_at"]


def test_discharged_row_stays_attributable(tmp_path):
    db = _mk_db(tmp_path)
    scale_id = _insert(db, "AAA", WAL_SCALE_IN_SENTINEL, 10.0, "sell")
    sell_id = _insert(db, "BBB", _WAL_SELL_SENTINEL, 5.0, "sell")
    db.delete_pending_protection_restore(scale_id)
    db.delete_pending_protection_restore(sell_id)

    assert db.get_pending_protection_restores() == []
    audit = db.get_protection_restore_wal_audit()
    assert [r["row_id"] for r in audit] == [scale_id, sell_id]


def test_gap_between_ceiling_and_scale_in_cancels_is_explained(tmp_path):
    """The item's own question, asked of the audit table instead of guessed."""
    db = _mk_db(tmp_path)
    ids = [
        _insert(db, "AAA", WAL_SCALE_IN_SENTINEL, 10.0, "sell"),
        _insert(db, "BBB", _WAL_SELL_SENTINEL, 5.0, "sell"),
        _insert(db, "CCC", _WAL_SELL_SENTINEL, 2.0, "sell"),
        _insert(db, "DDD", WAL_SCALE_IN_SENTINEL, 3.0, "sell"),
    ]
    for row_id in ids:
        db.delete_pending_protection_restore(row_id)

    audit = db.get_protection_restore_wal_audit()
    ceiling = max(r["row_id"] for r in audit)
    scale_ids = [r["row_id"] for r in audit if r["sell_order_id"] == WAL_SCALE_IN_SENTINEL]
    other_ids = [r["row_id"] for r in audit if r["sell_order_id"] != WAL_SCALE_IN_SENTINEL]
    # Ceiling exceeds the scale-in count, and every missing id is named.
    assert ceiling > len(scale_ids)
    assert sorted(scale_ids + other_ids) == sorted(r["row_id"] for r in audit)
    assert other_ids == [ids[1], ids[2]]


def test_audit_failure_does_not_block_the_protective_row(tmp_path):
    """Bookkeeping never blocks protection: drop the audit table and the
    protective-restore intent is still persisted and still returns its id."""
    db = _mk_db(tmp_path)
    db.conn.execute("DROP TABLE protection_restore_wal_audit")
    db.conn.commit()

    row_id = _insert(db, "AAA", WAL_SCALE_IN_SENTINEL, 10.0, "sell")

    assert row_id > 0
    pending = db.get_pending_protection_restores()
    assert [r["id"] for r in pending] == [row_id]
