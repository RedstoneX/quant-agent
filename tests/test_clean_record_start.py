"""Clean record start (owner ruling 2026-10-09): only positions whose opening
entry is on/after 2026-10-12 13:30 UTC (Monday market open, New York) count."""

from src.storage.db import Database


def _round_trip(db, sym, opened_at):
    for action, price in (("BUY", 100.0), ("SELL", 110.0)):
        tid = db.insert_trade(
            symbol=sym,
            action=action,
            qty=10,
            price=price,
            reasoning="t",
            run_id="r1",
            conviction="high",
            fill_status="filled",
            stop_loss=90.0 if action == "BUY" else None,
        )
        ts = opened_at if action == "BUY" else opened_at.replace(":00:00", ":05:00")
        db.conn.execute("UPDATE trades SET timestamp = ? WHERE id = ?", (ts, tid))
        if action == "SELL":
            db.conn.execute("UPDATE trades SET realized_pnl = 100 WHERE id = ?", (tid,))
    db.conn.commit()


def test_pre_start_position_excluded_and_counted_post_start_included(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _round_trip(db, "OLD", "2026-10-09 14:00:00")
    for i in range(3):  # compute_trade_calibration needs >= 3 closed trades to render
        _round_trip(db, f"NEW{i}", "2026-10-12 14:00:00")
    stats = db.compute_trade_calibration(lookback_days=100_000)
    assert stats["by_conviction"]["high"]["closed"] == 3
    assert stats["excluded_before_clean_record_n"] == 1
    n = db.conn.execute("SELECT COUNT(*) FROM trades WHERE symbol = 'OLD'").fetchone()[0]
    assert n == 2  # old rows stay in the database
    db.close()


def test_position_opened_before_the_bell_is_excluded(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    for i in range(3):
        _round_trip(db, f"NEW{i}", "2026-10-12 14:00:00")
    _round_trip(db, "EARLY", "2026-10-12 13:00:00")
    stats = db.compute_trade_calibration(lookback_days=100_000)
    assert stats["by_conviction"]["high"]["closed"] == 3
    assert stats["excluded_before_clean_record_n"] == 1
    db.close()


def _mixed_db(tmp_path):
    """3 clean long trades + 1 pre-start SHORT round trip, all with risk set."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    for i in range(3):
        _round_trip(db, f"NEW{i}", "2026-10-12 14:00:00")
    tid = db.insert_trade(
        symbol="OLDS",
        action="SHORT",
        qty=1000,
        price=100.0,
        reasoning="t",
        run_id="r1",
        conviction="low",
        allocated_risk_pct=0.5,
        fill_status="filled",
        stop_loss=110.0,
    )
    cid = db.insert_trade(
        symbol="OLDS", action="COVER", qty=1000, price=90.0, reasoning="t", run_id="r1", fill_status="filled"
    )
    db.conn.execute("UPDATE trades SET timestamp = '2026-10-09 14:00:00' WHERE id = ?", (tid,))
    db.conn.execute("UPDATE trades SET timestamp = '2026-10-09 15:00:00', realized_pnl = 1 WHERE id = ?", (cid,))
    db.conn.execute("UPDATE trades SET allocated_risk_pct = 0.5 WHERE id = ?", (tid,))
    db.conn.commit()
    return db


def test_overall_excludes_pre_start_trade(tmp_path):
    db = _mixed_db(tmp_path)
    assert db.compute_trade_calibration(lookback_days=100_000)["n"] == 3
    db.close()


def test_by_size_excludes_pre_start_trade(tmp_path):
    db = _mixed_db(tmp_path)
    sizes = db.compute_trade_calibration(lookback_days=100_000)["by_size"]
    assert sum(b["n"] for b in sizes.values()) == 3  # the $100k old short would be "large"
    assert sizes["large (≥$10k)"]["n"] == 0
    db.close()


def test_by_side_excludes_pre_start_trade(tmp_path):
    db = _mixed_db(tmp_path)
    assert db.compute_trade_calibration(lookback_days=100_000)["by_side"]["short"]["n"] == 0
    db.close()


def test_by_allocated_risk_excludes_pre_start_trade(tmp_path):
    db = _mixed_db(tmp_path)
    stats = db.compute_trade_calibration(lookback_days=100_000)
    assert stats["by_allocated_risk"]["low (<1%)"].get("n", 0) == 0
    assert stats["allocated_risk_unknown_n"] == 3
    db.close()
