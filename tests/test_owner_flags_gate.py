"""The broker door refuses a plain SELL bigger than the long held -- frozen or not.

A short is only ever opened with `sell_short`, so a plain `sell` that exceeds
the freshly read long is an exit sized from a stale read and would open a
short. The door refuses it by name, per symbol, compared exactly as Decimal.
"""

from types import SimpleNamespace

import pytest

from src import owner_flags
from src.execution import owner_flags_gate as gate
from src.storage.db import Database


@pytest.fixture
def open_door(tmp_path):
    """An aimed door on a desk that is NOT frozen."""
    p = str(tmp_path / "desk.db")
    db = Database(p)
    db.initialize()
    db.conn.close()
    gate.configure(p)
    assert not owner_flags.read_flags(p).frozen and not owner_flags.read_flags(p).unknown
    yield p
    gate.release()


def _door(positions):
    calls = []
    ns = {n: (lambda n: lambda self, *a, **k: calls.append(n) or "THROUGH")(n) for n in gate._REFUSALS}
    ns["get_positions"] = lambda self: [SimpleNamespace(symbol=s, qty=q) for s, q in positions.items()]
    cls = type("Door", (), ns)
    gate.install(cls)
    return cls(), calls


def _refused(result, reason):
    return (
        isinstance(result, dict) and result.get("status") == "owner_flag_halted" and result["reason"].startswith(reason)
    )


def test_sell_bigger_than_the_long_is_refused_when_not_frozen(open_door):
    d, calls = _door({"XYZ": 10})
    out = d.submit_order(symbol="XYZ", qty=15, side="sell")
    assert _refused(out, "oversell_refused") and "XYZ" in out["reason"]
    assert calls == []


def test_sell_with_nothing_held_is_refused_when_not_frozen(open_door):
    d, calls = _door({})
    assert _refused(d.submit_order("XYZ", 5, "sell"), "oversell_refused")
    assert calls == []


def test_legitimate_full_and_partial_closes_pass(open_door):
    d, calls = _door({"XYZ": 10})
    assert d.submit_order(symbol="XYZ", qty=10, side="sell") == "THROUGH"
    assert d.submit_order(symbol="XYZ", qty=4, side="sell") == "THROUGH"
    assert d.submit_order(symbol="XYZ", qty=5, side="sell_short") == "THROUGH"  # shorts are opened by name
    assert calls == ["submit_order"] * 3


def test_unreadable_positions_refuse_a_plain_sell_when_not_frozen(open_door):
    d, calls = _door({"XYZ": 10})
    type(d).get_positions = lambda self: (_ for _ in ()).throw(RuntimeError("broker down"))
    assert _refused(d.submit_order(symbol="XYZ", qty=1, side="sell"), "positions_unreadable")
    assert calls == []


def test_float_noise_is_not_an_oversell_but_a_real_excess_is(open_door):
    d, calls = _door({"XYZ": 0.3})
    assert d.submit_order(symbol="XYZ", qty=0.1 + 0.2, side="sell") == "THROUGH"  # 0.30000000000000004
    assert _refused(d.submit_order(symbol="XYZ", qty=0.300000002, side="sell"), "oversell_refused")
    assert calls == ["submit_order"]


def test_door_refusals_are_recorded_per_symbol(open_door, monkeypatch):
    seen = []
    monkeypatch.setattr(gate, "record_guarded_pass", lambda owner, where, exc=None, **kw: seen.append((where, kw)))
    d, _ = _door({"XYZ": 10})
    d.submit_order(symbol="XYZ", qty=15, side="sell")
    type(d).get_positions = lambda self: (_ for _ in ()).throw(RuntimeError("broker down"))
    d.submit_order(symbol="ABC", qty=1, side="sell")
    assert [(w, kw["context"]["symbol"]) for w, kw in seen] == [
        ("owner_flags_gate.oversell_refused", "XYZ"),
        ("owner_flags_gate.positions_unreadable", "ABC"),
    ]
