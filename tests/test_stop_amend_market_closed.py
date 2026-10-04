"""An amend the shut market refuses must be OWED, not lost.

Measured 2026-10-02 against the paper broker: `replace_order_by_id` on a
resting stop answers HTTP 422 "cannot replace order in accepted status", and
after the 16:00 ET close every resting stop carries that status. The close
session's window is 15:30-16:00 ET and its deterministic trails run late, so
the amend is reached after the bell.

Owner ruling the same day: a shut tape cannot elect a stop, so the un-made
amend is harmless -- the failure is the desk not KNOWING, and an owed level
that never reaches the next open.
"""
from __future__ import annotations

import pytest

from src.execution.broker_parts.stop_amend import StopAmender
from src.execution.pending_stop_amends import intent_is_protective
from src.execution.pending_stop_drain import (
    drain_pending_stop_amends,
)


class _Clock:
    def __init__(self, is_open): self.is_open = is_open


class _Client:
    def __init__(self, is_open):
        self._clock = _Clock(is_open)
        self.replace_calls: list = []
        self.cancel_calls: list = []

    def get_clock(self): return self._clock

    def replace_order_by_id(self, order_id, req):
        self.replace_calls.append(order_id)
        raise AssertionError("the amend must not be attempted while shut")

    def cancel_order_by_id(self, order_id):
        self.cancel_calls.append(order_id)
        raise AssertionError("a protective stop must never be cancelled while shut")


def _amender(is_open):
    client = _Client(is_open)
    return client, StopAmender(
        client=client,
        list_open_stop_orders_by_side=lambda *a, **k: [],
        snapshot_stop_order=lambda *a, **k: None,
    )


class _Order:
    legs = None
    order_type = "stop"


def test_closed_market_defers_instead_of_amending_or_cancelling():
    client, amender = _amender(is_open=False)
    out = amender._amend_resting_stop_price(
        symbol="AAPL", live_orders=[_Order()],
        stop_specs=[{"id": "o1", "qty": 10, "stop_price": 90.0}],
        new_stop_price=95.0, position_qty=10.0,
    )
    assert out["amend_status"] == "market_closed"
    assert out["id"] is None                      # nothing may be written back
    assert out["intended_stop"] == 95.0
    assert client.replace_calls == []             # no doomed 422
    assert client.cancel_calls == []              # no unprotected moment


# --- the dangerous failure: an owed stop that never lands at the open ------

class _Db:
    def __init__(self, rows): self.rows = list(rows)
    def _trades(self): return self


class _FakeStore:
    @staticmethod
    def get_all(ledger): return list(ledger.rows)

    @staticmethod
    def delete(ledger, row_id):
        ledger.rows = [r for r in ledger.rows if r["id"] != row_id]
        return 1


@pytest.fixture(autouse=True)
def _fake_store(monkeypatch):
    from src.execution import pending_stop_drain
    monkeypatch.setattr(pending_stop_drain, "_store", _FakeStore)


class _Broker:
    def __init__(self, current): self.current = current; self.placed = []
    def get_current_stop_price(self, symbol): return self.current
    def replace_stop_loss(self, symbol, price, **kw):
        self.placed.append((symbol, price))
        self.current = price
        return {"id": "new1", "status": "new", "symbol": symbol}
    def get_positions(self): return []


def test_stop_intended_after_the_close_is_in_place_at_the_next_open(monkeypatch):
    db = _Db([{"id": 1, "symbol": "AAPL", "intended_stop": 95.0, "is_short": 0}])
    broker = _Broker(current=90.0)
    monkeypatch.setattr(
        "src.execution.stop_records.write_back_stop_loss",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "src.execution.broker_parts.stop_window.record_unprotected_windows",
        lambda *a, **k: None,
    )
    assert drain_pending_stop_amends(broker, db) == 1
    assert broker.placed == [("AAPL", 95.0)], "the owed stop never reached the open"
    assert broker.get_current_stop_price("AAPL") == 95.0
    assert db.rows == []                          # discharged exactly once


def test_an_owed_stop_that_cannot_be_applied_stays_owed(monkeypatch):
    db = _Db([{"id": 1, "symbol": "AAPL", "intended_stop": 95.0, "is_short": 0}])

    class _Refusing(_Broker):
        def replace_stop_loss(self, symbol, price, **kw):
            return {"id": None, "status": "refused", "symbol": symbol}

    monkeypatch.setattr(
        "src.execution.broker_parts.stop_window.record_unprotected_windows",
        lambda *a, **k: None,
    )
    assert drain_pending_stop_amends(_Refusing(current=90.0), db) == 0
    assert db.rows, "an owed stop must never be silently dropped"


@pytest.mark.parametrize("intended,current,is_short,ok", [
    (95.0, 90.0, False, True),    # long: tighter is protective
    (85.0, 90.0, False, False),   # long: looser increases what can be lost
    (105.0, 110.0, True, True),   # short: lower is protective
    (115.0, 110.0, True, False),  # short: higher increases what can be lost
    (95.0, None, False, True),    # nothing resting to loosen
])
def test_a_pending_level_never_loosens_protection(intended, current, is_short, ok):
    assert intent_is_protective(intended, current, is_short=is_short) is ok


def test_owed_level_round_trips_through_the_real_database(tmp_path):
    from src.storage.db import Database
    from src.storage.trades import pending_stop_amends_store as store
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    ledger = db._trades()
    store.record(ledger, "AAA", 90.0)
    store.record(ledger, "AAA", 95.0, is_short=True)  # a later intent replaces the earlier
    rows = store.get_all(ledger)
    assert [(r["symbol"], r["intended_stop"], r["is_short"]) for r in rows] == [("AAA", 95.0, 1)]
    assert store.delete(ledger, rows[0]["id"]) == 1
    assert store.get_all(ledger) == []
