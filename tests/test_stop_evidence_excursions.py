"""Stop-floor evidence: a position that closes carries its excursions.

RECORDING ONLY (docs/WORK.md items 90 and 199). These tests prove the
evidence a future pass needs is durably on the row — entry ATR, entry price,
the stop placed at entry, whether that stop sat on a COMPUTED structural
level or on the ATR band, the worst and best excursion while open, the
realised outcome and whether the stop was hit. They assert nothing about
what the floor SHOULD be and nothing here may be optimised against.
"""
from types import SimpleNamespace

import pytest

from src.portfolio_constructor import (
    LEVEL_BACKED_STOP_RULES, STOP_RULE_ATR_BAND, STOP_RULE_LEVEL_HONOURED,
)
from src.storage.db import Database, _categorize_exit_reason


def _snapshot(symbol, qty, avg_entry, current_price):
    return SimpleNamespace(
        symbol=symbol, qty=qty, avg_entry=avg_entry,
        current_price=current_price, market_value=qty * current_price,
        unrealized_pnl=(current_price - avg_entry) * qty, sector="Tech",
    )


@pytest.fixture()
def db(tmp_path):
    database = Database(str(tmp_path / "evidence.db"))
    database.initialize()
    yield database
    database.conn.close()


def _row(db, trade_id):
    return dict(
        db.conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    )


def test_closed_long_keeps_its_adverse_excursion(db):
    """The load-bearing one: a position that OPENS, runs against the desk
    and then CLOSES still has the worst excursion it reached stored."""
    trade_id = db.insert_trade(
        symbol="AAA", action="BUY", qty=10, price=100.0, reasoning="entry",
        run_id="r1", stop_loss=94.0, entry_atr=2.0,
        stop_basis=STOP_RULE_ATR_BAND,
    )
    # Two sessions open: 96 then a recovery to 99. Monotonic — the recovery
    # must not erase the excursion that preceded it.
    db.sync_positions([_snapshot("AAA", 10, 100.0, 96.0)])
    db.sync_positions([_snapshot("AAA", 10, 100.0, 99.0)])
    # The position closes: the broker snapshot no longer carries it.
    db.sync_positions([])

    row = _row(db, trade_id)
    assert row["max_adverse_excursion"] == pytest.approx(4.0)
    assert row["entry_atr"] == pytest.approx(2.0)
    assert row["price"] == pytest.approx(100.0)
    assert row["initial_stop_loss"] == pytest.approx(94.0)
    assert row["stop_basis"] == STOP_RULE_ATR_BAND
    assert row["stop_basis"] not in LEVEL_BACKED_STOP_RULES
    # Stop distance in ATR multiples is RECOMPUTED from the stored facts,
    # never stored a second time.
    atr_multiple = (row["price"] - row["initial_stop_loss"]) / row["entry_atr"]
    assert atr_multiple == pytest.approx(3.0)


def test_favourable_excursion_is_recorded_alongside_the_adverse_one(db):
    trade_id = db.insert_trade(
        symbol="BBB", action="BUY", qty=5, price=50.0, reasoning="entry",
        run_id="r1", stop_loss=47.0, entry_atr=1.5,
        stop_basis=STOP_RULE_LEVEL_HONOURED,
    )
    db.sync_positions([_snapshot("BBB", 5, 50.0, 58.0)])   # +8 in favour
    db.sync_positions([_snapshot("BBB", 5, 50.0, 48.0)])   # -2 against
    db.sync_positions([_snapshot("BBB", 5, 50.0, 55.0)])   # back up, no widen

    row = _row(db, trade_id)
    assert row["max_favourable_excursion"] == pytest.approx(8.0)
    assert row["max_adverse_excursion"] == pytest.approx(2.0)
    assert row["stop_basis"] in LEVEL_BACKED_STOP_RULES


def test_short_excursions_use_the_other_side(db):
    trade_id = db.insert_trade(
        symbol="CCC", action="SHORT", qty=-4, price=20.0, reasoning="entry",
        run_id="r1", stop_loss=23.0, entry_atr=1.0,
        stop_basis=STOP_RULE_ATR_BAND,
    )
    db.sync_positions([_snapshot("CCC", -4, 20.0, 22.5)])  # against a short
    db.sync_positions([_snapshot("CCC", -4, 20.0, 17.0)])  # in its favour

    row = _row(db, trade_id)
    assert row["max_adverse_excursion"] == pytest.approx(2.5)
    assert row["max_favourable_excursion"] == pytest.approx(3.0)


def test_stop_hit_is_readable_from_the_exit_row(db):
    """Whether the stop was HIT is not a new field: the exit row's
    deterministic `exit_reason_category` already says so, and the realised
    outcome sits on the same row."""
    db.insert_trade(
        symbol="DDD", action="BUY", qty=3, price=10.0, reasoning="entry",
        run_id="r1", stop_loss=8.0, entry_atr=0.8,
        stop_basis=STOP_RULE_ATR_BAND,
    )
    exit_id = db.insert_trade(
        symbol="DDD", action="STOP_OUT", qty=3, price=8.0,
        reasoning="broker stop filled", run_id="r2", fill_status="filled",
    )
    # A confirmed broker stop fill categorises as a stop, and nothing else
    # does — this is the fact a later pass joins to the entry evidence.
    assert _categorize_exit_reason("STOP_OUT", None, "filled", 3) == "broker_stop_fill"
    assert _categorize_exit_reason("SELL", "trend over", "filled", 3) != "broker_stop_fill"
    assert "exit_reason_category" in _row(db, exit_id)
    with db._lock:
        db.conn.execute(
            "UPDATE trades SET realized_pnl = ? WHERE id = ?", (-6.0, exit_id),
        )
        db.conn.commit()
    assert _row(db, exit_id)["realized_pnl"] == pytest.approx(-6.0)


def test_missing_values_are_stored_null_never_guessed(db):
    """An entry whose ATR was unavailable stores NULL, not a substitute."""
    trade_id = db.insert_trade(
        symbol="EEE", action="BUY", qty=1, price=5.0, reasoning="entry",
        run_id="r1", stop_loss=4.0, entry_atr=None, stop_basis=None,
    )
    row = _row(db, trade_id)
    assert row["entry_atr"] is None
    assert row["stop_basis"] is None
    # No entry ATR means no evidence row to widen — excursions stay NULL
    # rather than being accumulated against an unusable baseline.
    db.sync_positions([_snapshot("EEE", 1, 5.0, 4.5)])
    assert _row(db, trade_id)["max_adverse_excursion"] is None
