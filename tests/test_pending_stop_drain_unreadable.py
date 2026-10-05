"""An unreadable resting stop is not "no stop": the drain must not apply over it.

The drain re-tests each owed level against what rests on the broker. Reading a
failed broker answer as "nothing resting" applied the owed level even when a
tighter stop was live, moving a protective stop in the direction that loses more.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.execution import pending_stop_drain as drain
from src.execution import stop_read


class _Broker:
    def __init__(self, answer):
        self.answer = answer
        self.client = SimpleNamespace(get_orders=self._boom)

    def _boom(self, *a, **k):
        raise RuntimeError("bulk read down")

    def get_current_stop_price(self, symbol):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def world(monkeypatch):
    rows = [{"id": 1, "symbol": "AAA", "intended_stop": 95.0, "is_short": False}]
    deleted, applied = [], []
    store = SimpleNamespace(get_all=lambda _t: list(rows), delete=lambda _t, i: deleted.append(i))
    monkeypatch.setattr(drain, "_store", store)
    monkeypatch.setattr(drain, "replace_stop_and_record",
                        lambda b, d, s, lvl: applied.append(lvl) or {"id": "x"})
    monkeypatch.setattr(drain, "accepted_stop_order", lambda o: True)
    monkeypatch.setattr(drain, "record_guarded_pass", lambda *a, **k: None)
    monkeypatch.setattr(stop_read, "_sleep", lambda s: None)
    monkeypatch.setattr(stop_read, "_report_unreadable", lambda *a, **k: None)
    db = SimpleNamespace(_trades=lambda: object())
    return db, deleted, applied


def test_unreadable_resting_stop_keeps_the_row_and_applies_nothing(world):
    db, deleted, applied = world
    assert drain.drain_pending_stop_amends(_Broker(RuntimeError("down")), db) == 0
    assert applied == [] and deleted == []


def test_a_readable_tighter_stop_still_voids_the_owed_level(world):
    db, deleted, applied = world
    assert drain.drain_pending_stop_amends(_Broker(97.0), db) == 1
    assert applied == [] and deleted == [1]


def test_a_readable_absent_stop_still_applies_the_owed_level(world):
    db, deleted, applied = world
    assert drain.drain_pending_stop_amends(_Broker(None), db) == 1
    assert applied == [95.0] and deleted == [1]
