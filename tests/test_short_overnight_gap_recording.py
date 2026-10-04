"""Short-side gap evidence: a short that closes carries its worst gap.

RECORDING ONLY (docs/WORK.md item 186). The short-side sizing haircut
(`RiskConfig.short_gap_risk_multiple`, 1.5) was DELETED 2026-10-04; two attempts
to read it off the instrument were withdrawn — see docs/BOARD_NOTES.md item
186 for both failure reasons. Both failed because the desk has never kept a
record of what a short actually suffers overnight. These tests prove that
record is now durably on the trade row, beside the volatility read and the
stop distance already pinned at entry.

They assert nothing about what the haircut SHOULD be, and nothing here may
be optimised against: fitting a multiple to this desk's own history is
barred by doctrine whatever the sample size.
"""
from types import SimpleNamespace

import pytest

from src.portfolio_constructor import STOP_RULE_ATR_BAND
from src.storage.db import Database


def _snapshot(symbol, qty, avg_entry, current_price):
    return SimpleNamespace(
        symbol=symbol, qty=qty, avg_entry=avg_entry,
        current_price=current_price, market_value=qty * current_price,
        unrealized_pnl=(avg_entry - current_price) * abs(qty), sector="Tech",
    )


@pytest.fixture()
def db(tmp_path):
    database = Database(str(tmp_path / "gaps.db"))
    database.initialize()
    yield database
    database.conn.close()


def _row(db, trade_id):
    return dict(
        db.conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    )


def _open_short(db, symbol="SSS"):
    return db.insert_trade(
        symbol=symbol, action="SHORT", qty=-10, price=100.0,
        reasoning="entry", run_id="r1", stop_loss=105.0, entry_atr=2.0,
        stop_basis=STOP_RULE_ATR_BAND,
    )


def test_closed_short_keeps_its_worst_adverse_overnight_gap(db):
    """The load-bearing one. A short opens, gaps against the desk across
    several sessions, gaps back in its favour, then CLOSES — and the worst
    adverse gap it suffered is still on the row, next to the entry
    volatility read and the stop distance."""
    trade_id = _open_short(db)

    # Three sessions held. The 3.50 gap must survive both the smaller gap
    # before it and the favourable gap after it.
    db.record_overnight_gap("SSS", prev_close=100.0, open_price=101.2,
                            session_date="2026-09-28")
    db.record_overnight_gap("SSS", prev_close=101.2, open_price=104.7,
                            session_date="2026-09-29")
    db.record_overnight_gap("SSS", prev_close=104.7, open_price=103.0,
                            session_date="2026-09-30")
    # The position closes: the broker snapshot no longer carries it.
    db.sync_positions([])

    row = _row(db, trade_id)
    assert row["max_adverse_overnight_gap"] == pytest.approx(3.5)
    assert row["overnight_gap_sessions"] == 3
    # Recorded BESIDE the stop distance and the volatility read at entry —
    # the whole point, since the gap alone says nothing.
    assert row["entry_atr"] == pytest.approx(2.0)
    assert row["price"] == pytest.approx(100.0)
    assert row["initial_stop_loss"] == pytest.approx(105.0)
    # Both comparisons a later reader wants are RECOMPUTED from stored
    # facts, never stored a second time.
    stop_distance = row["initial_stop_loss"] - row["price"]
    assert stop_distance == pytest.approx(5.0)
    assert row["max_adverse_overnight_gap"] / stop_distance == pytest.approx(0.7)
    assert row["max_adverse_overnight_gap"] / row["entry_atr"] == pytest.approx(1.75)


def test_an_all_favourable_short_records_a_negative_worst_not_a_blank(db):
    """Absence of evidence stays distinguishable from evidence of absence:
    a short that never gapped against the desk records a NEGATIVE worst and
    a session count, not a NULL."""
    trade_id = _open_short(db, "FAV")
    db.record_overnight_gap("FAV", prev_close=100.0, open_price=99.0,
                            session_date="2026-09-29")
    db.record_overnight_gap("FAV", prev_close=99.0, open_price=97.5,
                            session_date="2026-09-30")

    row = _row(db, trade_id)
    assert row["max_adverse_overnight_gap"] == pytest.approx(-1.0)
    assert row["overnight_gap_sessions"] == 2


def test_a_never_observed_short_is_null_and_uncounted(db):
    trade_id = _open_short(db, "NIL")
    row = _row(db, trade_id)
    assert row["max_adverse_overnight_gap"] is None
    assert row["overnight_gap_sessions"] is None


def test_the_same_session_cannot_be_counted_twice(db):
    """A second position sync in one session must not double-count."""
    trade_id = _open_short(db, "DUP")
    assert db.record_overnight_gap("DUP", 100.0, 102.0, "2026-09-30") is True
    assert db.record_overnight_gap("DUP", 100.0, 102.0, "2026-09-30") is False

    row = _row(db, trade_id)
    assert row["overnight_gap_sessions"] == 1
    assert row["max_adverse_overnight_gap"] == pytest.approx(2.0)


def test_recording_is_scoped_to_shorts_and_to_sane_prices(db):
    """A long is not touched (only the short-side multiple is in question),
    and a zero or missing price is skipped rather than guessed."""
    long_id = db.insert_trade(
        symbol="LNG", action="BUY", qty=10, price=100.0, reasoning="entry",
        run_id="r1", stop_loss=94.0, entry_atr=2.0,
        stop_basis=STOP_RULE_ATR_BAND,
    )
    assert db.record_overnight_gap("LNG", 100.0, 103.0, "2026-09-30") is False
    assert _row(db, long_id)["max_adverse_overnight_gap"] is None

    short_id = _open_short(db, "BAD")
    assert db.record_overnight_gap("BAD", 0.0, 102.0, "2026-09-30") is False
    assert db.record_overnight_gap("BAD", 100.0, None, "2026-09-30") is False
    assert db.record_overnight_gap("BAD", 100.0, 102.0, "") is False
    assert _row(db, short_id)["max_adverse_overnight_gap"] is None
