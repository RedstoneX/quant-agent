"""`Database.get_position_open_timestamp` — the trail's bar window starts at
the POSITION OPEN, not at the most recent add.

Measured 2026-09-30 against the live production DB: the deterministic trail
sliced its bars from `get_symbol_last_buy`, i.e. the LATEST opening row, while
taking the entry PRICE from `position.avg_entry`, which is blended across every
add. MRVL's position `pos-08b43df53103` opened 2026-09-17 and took a third add
on 2026-09-23 at 17:19; the 19:31 trail evaluation that day therefore saw ZERO
bars "since entry" for a position four sessions old, and
`src/risk/trailing.py::_swing_lows` — which needs `2 * PIVOT_WINDOW + 1` = 7
bars before it can confirm one pivot — had nothing to read.

These tests pin the lookup itself and the one property that makes the change
safe to ship: it only ever LENGTHENS the window, so it cannot remove protection.
"""
import pytest

from src.storage.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "posopen.db"))
    database.initialize()
    yield database
    database.close()


def _buy(db, symbol, ts, qty=1.0, price=100.0, **kw):
    db.insert_trade(
        symbol=symbol, action="BUY", qty=qty, price=price,
        reasoning="entry", run_id="r", stop_loss=price * 0.9,
        fill_status="filled", **kw,
    )
    with db._lock:
        db.conn.execute(
            "UPDATE trades SET timestamp = ? WHERE id = "
            "(SELECT MAX(id) FROM trades WHERE symbol = ?)",
            (ts, symbol),
        )
        db.conn.commit()


def test_scale_in_does_not_move_the_position_open(db):
    """Three adds on one position: the open is the FIRST, not the last."""
    _buy(db, "MRVL", "2026-09-17 14:26:00", price=242.07)
    _buy(db, "MRVL", "2026-09-21 16:19:00", price=255.64)
    _buy(db, "MRVL", "2026-09-23 17:19:00", price=258.60)

    last = db.get_symbol_last_buy("MRVL")
    assert last["timestamp"].startswith("2026-09-23"), "fixture sanity"

    opened = db.get_position_open_timestamp(last)
    assert opened is not None
    assert opened.startswith("2026-09-17"), (
        "the position opened on the 17th; the add on the 23rd mints no new "
        "position and must not restart the trail's bar window"
    )


def test_window_only_ever_lengthens(db):
    """The property that makes this safe: the open is never AFTER the last buy.

    A longer bar window can hand structure bars it previously lacked and can
    only raise the chandelier's high-water anchor (a TIGHTER stop). It can
    never shorten the window, so it can never withdraw protection.
    """
    for ts in ("2026-09-17 14:26:00", "2026-09-21 16:19:00",
               "2026-09-23 17:19:00"):
        _buy(db, "ETN", ts)
    last = db.get_symbol_last_buy("ETN")
    assert db.get_position_open_timestamp(last) <= last["timestamp"]


def test_single_buy_position_is_its_own_open(db):
    """No scale-in: open and last buy are the same row, so nothing changes."""
    _buy(db, "NET", "2026-09-17 19:04:00", price=335.93)
    last = db.get_symbol_last_buy("NET")
    assert db.get_position_open_timestamp(last) == last["timestamp"]


def test_a_new_position_after_flat_does_not_reach_back(db):
    """A fully-closed-then-reopened name must NOT inherit the old chain's date.

    `_assign_position_ids` closes a chain when net qty returns to ~0 and mints
    a fresh id for the next entry. The lookup is scoped by `position_id`, so
    the second position's window starts at the second position's open — bars
    from a trade that was already exited are not levels this trade defended.
    """
    _buy(db, "RKLB", "2026-08-01 14:00:00", qty=5.0, price=50.0)
    db.insert_trade(
        symbol="RKLB", action="SELL", qty=5.0, price=55.0,
        reasoning="exit", run_id="r", fill_status="filled",
    )
    _buy(db, "RKLB", "2026-09-21 15:00:00", qty=3.0, price=69.0)

    last = db.get_symbol_last_buy("RKLB")
    opened = db.get_position_open_timestamp(last)
    assert opened is not None
    assert opened.startswith("2026-09-21"), (
        "the August position was closed; its bars belong to a different trade"
    )


def test_missing_or_unidentified_row_returns_none_not_a_guess(db):
    """None means 'unknown'. The caller keeps its own fallback; nothing here
    invents a date."""
    assert db.get_position_open_timestamp(None) is None
    assert db.get_position_open_timestamp({}) is None
    assert db.get_position_open_timestamp(
        {"symbol": "AAPL", "action": "BUY", "position_id": None},
    ) is None
    # A non-opening action is a caller bug, not a position.
    assert db.get_position_open_timestamp(
        {"symbol": "AAPL", "action": "SELL", "position_id": "pos-1"},
    ) is None
