"""Owner FREEZE at the broker door (owner ruling 2026-10-09 ~23:20 UTC).

Frozen: the desk keeps protecting and exiting what it holds but opens nothing
new. Each case is judged against the broker's own position read.
"""

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import owner_flags, owner_intents as oi
from src.execution import owner_flags_gate as gate
from src.storage.db import Database


@pytest.fixture
def frozen_db(tmp_path):
    p = str(tmp_path / "desk.db")
    db = Database(p)
    db.initialize()
    db.conn.close()
    gate.configure(p)
    c = sqlite3.connect(p)
    try:
        oi.raise_intent(c, oi.FREEZE)
    finally:
        c.close()
    oi.intake(p)
    assert owner_flags.read_flags(p).frozen
    yield p
    gate.release()


def _door(positions):
    """A stand-in broker holding `positions` ({symbol: signed qty})."""
    calls = []
    ns = {n: (lambda n: lambda self, *a, **k: calls.append(n) or "THROUGH")(n) for n in gate._REFUSALS}
    ns["get_positions"] = lambda self: [SimpleNamespace(symbol=s, qty=q) for s, q in positions.items()]
    cls = type("Door", (), ns)
    gate.install(cls)
    return cls(), calls


def _halted(result):
    return isinstance(result, dict) and result.get("status") == "owner_flag_halted"


def test_buy_to_cover_a_short_is_allowed(frozen_db):
    d, calls = _door({"XYZ": -10})
    assert d.submit_order(symbol="XYZ", qty=10, side="buy") == "THROUGH"
    assert calls == ["submit_order"]


def test_sell_short_is_blocked(frozen_db):
    d, calls = _door({})
    assert _halted(d.submit_order(symbol="XYZ", qty=5, side="sell_short"))
    d, calls = _door({"XYZ": -5})  # adding to a short is an entry too
    assert _halted(d.submit_order("XYZ", 5, "sell_short"))
    assert _halted(d.submit_order("XYZ", 5, "sell"))  # a plain sell adds to a short as well
    assert d.submit_order("XYZ", 5, "buy") == "THROUGH"  # ...while its cover goes through
    assert calls == ["submit_order"]


def test_long_add_is_blocked(frozen_db):
    d, calls = _door({"XYZ": 10})
    assert _halted(d.submit_order(symbol="XYZ", qty=3, side="buy"))
    assert d.submit_order(symbol="XYZ", qty=3, side="sell") == "THROUGH"  # a partial exit is not
    d, calls2 = _door({})  # a fresh open / re-entry
    assert _halted(d.submit_order("XYZ", 3, "buy"))
    assert calls == ["submit_order"] and not calls2


def test_flip_is_blocked(frozen_db):
    d, calls = _door({"XYZ": 10})
    assert _halted(d.submit_order(symbol="XYZ", qty=15, side="sell"))  # long -> short
    d, calls = _door({"XYZ": -10})
    assert _halted(d.submit_order(symbol="XYZ", qty=15, side="buy"))  # short -> long
    assert d.submit_order(symbol="XYZ", qty=10, side="buy") == "THROUGH"  # exact cover: not a flip
    assert calls == ["submit_order"]


def test_entry_limit_repeg_is_blocked(frozen_db):
    d, calls = _door({})
    assert _halted(d.replace_entry_limit("order-1", 10.0, qty=5))
    assert d.cancel_entry_order("order-1") == "THROUGH"  # withdrawing the entry is not
    assert d.close_position("XYZ") == "THROUGH"
    assert calls == ["cancel_entry_order", "close_position"]


def test_stop_placement_and_upkeep_are_allowed(frozen_db):
    d, calls = _door({"XYZ": 10, "NKD": -4})
    assert d.place_entry_protection("XYZ", "order-1", 9.0) == "THROUGH"
    assert d.replace_stop_loss("XYZ", 9.5) == "THROUGH"
    # A naked short's stop repair is an immediate market cover through submit_order.
    assert d.submit_order(symbol="NKD", qty=4, side="buy") == "THROUGH"
    assert calls == ["place_entry_protection", "replace_stop_loss", "submit_order"]


def test_full_sell_of_a_long_is_allowed(frozen_db):
    d, calls = _door({"XYZ": 10})
    assert d.submit_order(symbol="XYZ", qty=10, side="sell") == "THROUGH"
    assert d.close_position("XYZ") == "THROUGH"
    assert calls == ["submit_order", "close_position"]


def test_unreadable_flag_blocks_entry_but_allows_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(owner_flags.time, "sleep", lambda s: None)
    seen = []
    monkeypatch.setattr(gate, "unknown_state_recorder", seen.append)
    p = tmp_path / "desk.db"
    p.write_bytes(b"not a database")
    owner_flags._CACHE.clear()
    gate.configure(str(p))
    try:
        d, calls = _door({"XYZ": 10})
        assert _halted(d.submit_order(symbol="XYZ", qty=1, side="buy"))
        assert d.submit_order(symbol="XYZ", qty=10, side="sell") == "THROUGH"
        assert calls == ["submit_order"]
        assert len(seen) == 1 and "UNREADABLE" in seen[0]
    finally:
        gate.release()
        Path(f"{p}.owner_flags.json").unlink(missing_ok=True)


def test_unreadable_position_is_treated_as_an_entry(frozen_db):
    d, calls = _door({})
    type(d).get_positions = lambda self: (_ for _ in ()).throw(RuntimeError("broker down"))
    assert _halted(d.submit_order(symbol="XYZ", qty=1, side="sell"))
    assert not calls


def test_not_frozen_lets_entries_through(frozen_db):
    c = sqlite3.connect(frozen_db)
    try:
        oi.raise_intent(c, oi.UNFREEZE)
    finally:
        c.close()
    oi.intake(frozen_db)
    d, calls = _door({})
    assert d.submit_order(symbol="XYZ", qty=1, side="sell_short") == "THROUGH"
