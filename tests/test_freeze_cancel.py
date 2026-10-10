"""FREEZE step 2: resting exposure-adding orders are cancelled; exits and stops never are."""

from types import SimpleNamespace as NS

import pytest

from src.execution import freeze_cancel as fc


class FakeBroker:
    def __init__(self, positions, orders, positions_error=None):
        self._positions = positions
        self._positions_error = positions_error
        self.cancelled = []
        self.replaced = []
        self._orders = orders

    def list_open_orders_checked(self):
        return True, list(self._orders)

    def replace_order_qty(self, order_id, qty):
        self.replaced.append((order_id, qty))
        return True, ""

    def get_positions(self):
        if self._positions_error:
            raise self._positions_error
        return self._positions

    def cancel_entry_order(self, order_id):
        self.cancelled.append(order_id)
        return True


def _pos(symbol, qty):
    return NS(symbol=symbol, qty=qty)


def _order(oid, symbol, side, qty, order_type="limit", filled="0"):
    return NS(id=oid, symbol=symbol, side=NS(value=side), qty=qty, filled_qty=filled, order_type=NS(value=order_type))


def test_resting_long_entry_cancelled():
    broker = FakeBroker([], [_order("e1", "AAPL", "buy", "10"), _order("e2", "MSFT", "buy", "5", "stop")])
    res = fc.cancel_resting_entries(broker)
    assert broker.cancelled == ["e1", "e2"]
    assert res.cancelled == ["e1", "e2"] and res.ok


def test_add_to_long_cancelled_but_protective_stop_kept():
    broker = FakeBroker(
        [_pos("AAPL", "10")],
        [_order("stop", "AAPL", "sell", "10", "stop"), _order("add", "AAPL", "buy", "3")],
    )
    res = fc.cancel_resting_entries(broker)
    assert broker.cancelled == ["add"]
    assert res.kept == ["stop"]


def test_partial_close_sell_kept():
    broker = FakeBroker([_pos("AAPL", "10")], [_order("trim", "AAPL", "sell", "4")])
    res = fc.cancel_resting_entries(broker)
    assert broker.cancelled == [] and res.kept == ["trim"] and res.ok


def test_short_cover_buy_kept_and_short_entry_cancelled():
    broker = FakeBroker(
        [_pos("TSLA", "-8")],
        [
            _order("cover", "TSLA", "buy", "8", "stop"),
            _order("more_short", "TSLA", "sell", "2"),
            _order("new_short", "NVDA", "sell_short", "1"),
        ],
    )
    res = fc.cancel_resting_entries(broker)
    assert broker.cancelled == ["more_short", "new_short"]
    assert res.kept == ["cover"]


def test_positions_unreadable_cancels_nothing_and_names_fault():
    broker = FakeBroker(None, [_order("e1", "AAPL", "buy", "10")], positions_error=ConnectionError("down"))
    res = fc.cancel_resting_entries(broker)
    assert broker.cancelled == []
    assert [f[0] for f in res.faults] == [fc.POSITIONS_UNREADABLE]
    assert not res.ok


def test_oversized_stop_shrunk_to_held_never_cancelled():
    # Stop left at 10 after a partial close to 6: the door calls 10 a flip, so it is
    # shrunk to 6 in place -- not left oversized, not cancelled.
    broker = FakeBroker([_pos("AAPL", "6")], [_order("stop", "AAPL", "sell", "10", "stop")])
    res = fc.cancel_resting_entries(broker)
    assert broker.cancelled == []
    assert broker.replaced == [("stop", 6)]
    assert res.shrunk == [("stop", 6)] and res.ok


def test_orders_unreadable_touches_nothing_and_names_fault():
    broker = FakeBroker([], [_order("e1", "AAPL", "buy", "1")])
    broker.list_open_orders_checked = lambda: (False, [])
    res = fc.cancel_resting_entries(broker)
    assert broker.cancelled == [] and [f[0] for f in res.faults] == [fc.ORDERS_UNREADABLE]


def test_oversized_fractional_stop_kept_with_named_fault():
    broker = FakeBroker([_pos("AAPL", "6.5")], [_order("stop", "AAPL", "sell", "10", "stop")])
    res = fc.cancel_resting_entries(broker)
    assert broker.cancelled == [] and broker.replaced == []
    assert res.faults == [(fc.OVERSIZED_EXIT_UNAMENDED, "stop")]


def test_unreadable_entry_cancelled_like_the_door():
    # A notional order has no qty: the door would refuse it as an entry, so the sweep cancels it.
    broker = FakeBroker([], [_order("notional", "AAPL", "buy", None)])
    res = fc.cancel_resting_entries(broker)
    assert broker.cancelled == ["notional"] and res.ok


def test_unreadable_stop_kept_with_named_fault():
    broker = FakeBroker([_pos("AAPL", "5")], [_order("stop", "AAPL", "weird", "5", "stop")])
    res = fc.cancel_resting_entries(broker)
    assert broker.cancelled == []
    assert res.faults == [(fc.ORDER_UNREADABLE, "stop")]


def test_every_action_recorded_durably_per_symbol(monkeypatch):
    rows = []
    monkeypatch.setattr(
        fc,
        "record_guarded_pass",
        lambda owner, where, exc=None, context=None: rows.append((where, exc is not None, context)),
    )
    broker = FakeBroker(
        [_pos("AAPL", "6")],
        [_order("e1", "MSFT", "buy", "1"), _order("stop", "AAPL", "sell", "10", "stop")],
    )
    fc.cancel_resting_entries(broker)
    assert rows == [
        ("freeze_cancel.cancelled", False, {"symbol": "MSFT", "order": "e1"}),
        ("freeze_cancel.shrunk", False, {"symbol": "AAPL", "order": "stop"}),
    ]
    rows.clear()
    fc.cancel_resting_entries(FakeBroker(None, [], positions_error=OSError("x")))
    assert [(w, f) for w, f, _ in rows] == [("freeze_cancel.positions_unreadable", True)]


def test_exact_decimal_partial_fill_uses_remaining_qty():
    # 0.3 ordered, 0.1 filled: 0.2 remaining <= 0.2 held -> exit, kept (float would misjudge).
    broker = FakeBroker([_pos("BTC/USD", "0.2")], [_order("x", "BTCUSD", "sell", "0.3", filled="0.1")])
    res = fc.cancel_resting_entries(broker)
    assert broker.cancelled == [] and res.ok


def test_sweep_runs_only_when_frozen_or_unknown(monkeypatch):
    broker = FakeBroker([], [_order("e1", "AAPL", "buy", "1")])
    monkeypatch.setattr(fc.owner_flags, "read_flags", lambda db: fc.owner_flags.Flags(paused=False))
    assert fc.sweep_if_frozen(broker, "db") is None and broker.cancelled == []
    monkeypatch.setattr(fc.owner_flags, "read_flags", lambda db: fc.owner_flags.Flags(unknown=True))
    assert fc.sweep_if_frozen(broker, "db").cancelled == ["e1"]


def _frozen_pipeline(tmp_path, monkeypatch):
    import sqlite3
    from src.execution.broker import AlpacaBroker
    from src.owner_flags import PAUSE
    from src.storage.schema.owner_intent_tables import apply
    from tests.pipeline_factory import build_pipeline

    db = str(tmp_path / "desk.db")
    conn = sqlite3.connect(db)
    apply(conn)
    conn.execute(
        "INSERT INTO owner_intents (action, raised_at, state) VALUES (?, '2026-10-09T23:30:00Z', 'acted')", (PAUSE,)
    )
    conn.commit()
    conn.close()

    broker = FakeBroker(
        [_pos("MSFT", "5")], [_order("entry", "AAPL", "buy", "10"), _order("stop", "MSFT", "sell", "5", "stop")]
    )
    broker.is_trading_day = lambda: True
    broker.get_bars = lambda *a, **k: None  # wired at construction, never called here
    broker.sweep_frozen_resting_orders = lambda db_path: AlpacaBroker.sweep_frozen_resting_orders(broker, db_path)
    pipe = build_pipeline(broker=broker)
    monkeypatch.setattr(pipe.config.storage, "db_path", db)
    return pipe, broker


def test_pipeline_pickup_cancels_resting_entry_when_frozen(tmp_path, monkeypatch):
    """The seam moved: the owner-intent pickup (not a session body) sweeps with Freeze on."""
    pipe, broker = _frozen_pipeline(tmp_path, monkeypatch)

    pipe.pickup_owner_intents()

    assert broker.cancelled == ["entry"]  # the protective stop is kept


@pytest.mark.parametrize("session", ["run_morning", "run_position_review", "run_evening", "run_earnings_preprocess"])
def test_session_bodies_no_longer_sweep(tmp_path, monkeypatch, session):
    """One sweep site: a session body run directly does not sweep (its launcher's pickup does)."""
    from src import pipeline as pl

    pipe, broker = _frozen_pipeline(tmp_path, monkeypatch)
    for helper, fn in (
        (pl._morning_helpers, "run_morning"),
        (pl._review, "run_position_review"),
        (pl._review, "run_earnings_preprocess"),
        (pl._evening, "run_evening"),
    ):
        monkeypatch.setattr(helper, fn, lambda *a, **k: {"status": "executed"})

    getattr(pipe, session)()

    assert broker.cancelled == []
