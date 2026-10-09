"""Target scorecard: record only; clean-record start applies."""

import pytest

from src.storage.analytics import target_scorecard as ts
from src.storage.db import Database

NEW = "2026-10-12 14:00:00"
OLD = "2026-10-09 14:00:00"


def _trade(db, sym, action, price, when, **kw):
    tid = db.insert_trade(
        symbol=sym,
        action=action,
        qty=10,
        price=price,
        reasoning="t",
        run_id="r1",
        fill_status="filled",
        **kw,
    )
    db.conn.execute("UPDATE trades SET timestamp = ? WHERE id = ?", (when, tid))
    return tid


def _position(db, sym, side, entry, target, mfe, when, close=True):
    opener, closer = ("BUY", "SELL") if side == "long" else ("SHORT", "COVER")
    tid = _trade(
        db,
        sym,
        opener,
        entry,
        when,
        take_profit=target,
        entry_atr=1.0,
        stop_loss=entry * (0.9 if side == "long" else 1.1),
    )
    if mfe is not None:
        db.conn.execute("UPDATE trades SET max_favourable_excursion = ? WHERE id = ?", (mfe, tid))
    if close:
        _trade(db, sym, closer, entry, when.replace(":00:00", ":05:00"))
    db.conn.commit()


@pytest.fixture()
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    yield d
    d.close()


def test_scorecard_counts(db):
    _position(db, "OLD", "long", 100, 110, 20, OLD)
    _position(db, "LREACH", "long", 100, 110, 10, NEW)
    _position(db, "SREACH", "short", 100, 90, 12, NEW)
    _position(db, "MISS", "long", 100, 110, 4, NEW)
    _position(db, "OPEN", "long", 100, 110, 3, NEW, close=False)
    _position(db, "NOTGT", "long", 100, 0, 3, NEW)
    _position(db, "NOMFE", "long", 100, 110, None, NEW)
    s = ts.target_scorecard(db.conn)
    assert (s["closed_reached"], s["closed_not_reached"], s["still_open"]) == (2, 1, 1)
    assert s["excluded"] == {
        "opened_before_clean_record": 1,
        "no_entry_target": 1,
        "no_best_move_figure": 1,
        "trade_rows_without_position": 0,
    }
    frac = {p["symbol"]: p["fraction_of_target_reached_lower_bound"] for p in s["positions"]}
    assert frac["MISS"] == pytest.approx(0.4)
    assert frac["SREACH"] == pytest.approx(1.2)
    assert s["best_move_is_lower_bound"] is True


def test_pre_clean_record_excluded_and_would_count_without_exclusion(db, monkeypatch):
    _position(db, "OLD", "long", 100, 110, 20, OLD)
    s = ts.target_scorecard(db.conn)
    assert s["excluded"]["opened_before_clean_record"] == 1
    assert s["closed_reached"] == 0
    monkeypatch.setattr(ts, "_opened_before_clean_record", lambda _t: False)
    assert ts.target_scorecard(db.conn)["closed_reached"] == 1  # the exclusion is what keeps it out


def test_trade_rows_without_a_position_are_counted_not_dropped(db):
    _position(db, "ORPHAN", "long", 100, 110, 10, NEW)
    db.conn.execute("UPDATE trades SET position_id = NULL WHERE symbol = 'ORPHAN'")
    db.conn.commit()
    s = ts.target_scorecard(db.conn)
    assert s["excluded"]["trade_rows_without_position"] == 2
    assert (s["closed_reached"], s["closed_not_reached"], s["still_open"]) == (0, 0, 0)
