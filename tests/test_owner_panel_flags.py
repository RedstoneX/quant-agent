"""Owner command panel, instalment 1: the intent record and the three flags."""
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import owner_flags, owner_intents as oi
from src.execution import owner_flags_gate as gate
from src.execution.broker import AlpacaBroker
from src.storage.db import Database

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "desk.db")
    db = Database(p)
    db.initialize()
    db.conn.close()
    gate.configure(p)
    yield p
    gate.configure(None)


def _raise(db_path, *a, **k):
    c = sqlite3.connect(db_path)
    try:
        return oi.raise_intent(c, *a, **k)
    finally:
        c.close()


def _rows(db_path):
    c = sqlite3.connect(db_path)
    try:
        return c.execute("SELECT id, action, symbol, state, outcome FROM owner_intents ORDER BY id").fetchall()
    finally:
        c.close()


def test_intent_survives_restart_and_is_acted_on(db_path):
    _raise(db_path, oi.PAUSE, reason="owner asked while desk down")
    assert owner_flags.read_flags(db_path).paused is False  # raised, not yet acted
    oi.intake(db_path)  # the desk starting
    assert _rows(db_path)[0][3] == "acted"
    assert owner_flags.read_flags(db_path).paused is True


def test_unknown_intent_is_refused_with_its_reason(db_path):
    c = sqlite3.connect(db_path)
    c.execute("INSERT INTO owner_intents (action, raised_at) VALUES ('HANDS_OFF', '2026-01-01T00:00:00+00:00')")
    c.commit()
    c.close()
    oi.intake(db_path)
    (_, _, _, state, outcome), = _rows(db_path)
    assert state == "refused" and "unknown action" in outcome
    assert not hasattr(owner_flags.Flags(), "hands_off")  # no per-position exemption exists


def test_owner_expiry_is_honoured(db_path):
    _raise(db_path, oi.PAUSE, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    oi.intake(db_path)
    assert _rows(db_path)[0][3] == "expired"
    assert owner_flags.read_flags(db_path).paused is False


def test_flags_replay_in_order(db_path):
    for a in [oi.PAUSE, oi.RESUME, oi.PAUSE]:
        _raise(db_path, a)
    oi.intake(db_path)
    assert owner_flags.read_flags(db_path).paused is True
    assert not hasattr(owner_flags.Flags(), "never_touch")  # no per-symbol exclusion exists


class _Door:
    """Stands in for the broker: records which verbs got through."""
    calls: list
    client = None


def _recording_class():
    calls = []
    ns = {n: (lambda n: lambda self, *a, **k: calls.append(n) or "THROUGH")(n) for n in gate._REFUSALS}
    ns["get_positions"] = lambda self: "READ"
    cls = type("Door", (), ns)
    gate.install(cls)
    return cls, calls


def test_paused_desk_raises_no_orders_and_resume_restores(db_path):
    cls, calls = _recording_class()
    d = cls()
    _raise(db_path, oi.PAUSE)
    oi.intake(db_path)
    for n in gate.PAUSE_BLOCKS:
        assert getattr(d, n)("ABC") != "THROUGH"
    assert not calls
    d.replace_stop_loss("ABC", 1.0)  # protection upkeep continues under pause
    assert calls == ["replace_stop_loss"]
    _raise(db_path, oi.RESUME)
    oi.intake(db_path)
    assert d.submit_order("ABC") == "THROUGH"


def test_every_broker_write_verb_is_gated_and_reads_are_not():
    names = gate.write_methods(AlpacaBroker)
    assert set(names) == set(gate._REFUSALS), "rule table and broker verbs diverged"
    for n in names:
        assert getattr(getattr(AlpacaBroker, n), "_owner_flag_gated", False), n
    assert not getattr(AlpacaBroker.get_positions, "_owner_flag_gated", False)


def test_every_sdk_order_write_lives_behind_the_gated_broker():
    sdk = re.compile(r"\.(submit_order|cancel_order_by_id|cancel_orders|replace_order_by_id|"
                     r"close_position|close_all_positions)\(")
    # order_idempotency.py is the broker parts' own submit helper. No other exception.
    allowed = ("src/execution/broker.py", "src/execution/broker_parts/",
               "src/execution/order_idempotency.py")
    stray = []
    for p in (ROOT / "src").rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        for line in p.read_text().splitlines():
            m = sdk.search(line)
            if m and re.search(r"(client|_client)\.\w+\($", line[:m.end()]) and not rel.startswith(allowed):
                stray.append((rel, line.strip()))
    assert not stray, stray


def test_wholesale_cancels_are_not_symbol_filtered(db_path):
    cls, calls = _recording_class()
    d = cls()
    assert d.cancel_open_orders() == "THROUGH" and d.cancel_open_entry_orders() == "THROUGH"
    _raise(db_path, oi.PAUSE)
    oi.intake(db_path)
    assert d.cancel_open_orders() == "THROUGH"  # a pause never blocks a cancel


def test_unreadable_flag_uses_last_known_then_refuses_new_exposure_only(db_path, monkeypatch):
    monkeypatch.setattr(owner_flags.time, "sleep", lambda s: None)
    _raise(db_path, oi.PAUSE)
    oi.intake(db_path)
    assert owner_flags.read_flags(db_path).paused  # primes the durable copy
    Path(db_path).write_bytes(b"not a database")
    cls, calls = _recording_class()
    d = cls()
    owner_flags._CACHE.clear()  # a restarted desk: only the file copy survives
    f = owner_flags.read_flags(db_path)
    assert f.stale and f.paused and not f.unknown
    assert d.submit_order("ZZZ")["status"] == "owner_flag_halted"  # the copy says paused
    Path(owner_flags._cache_file(db_path)).unlink()
    owner_flags._CACHE.clear()
    seen = []
    monkeypatch.setattr(gate, "unknown_state_recorder", seen.append)
    assert owner_flags.read_flags(db_path).unknown
    assert d.submit_order("ZZZ")["status"] == "owner_flag_halted"  # new exposure refused
    assert d.replace_entry_limit("o", 1.0)["status"] == "owner_flag_halted"
    assert d.replace_stop_loss("ZZZ", 1.0) == "THROUGH"            # protection continues
    assert d.cancel_protective_stops("ZZZ") == "THROUGH"
    assert d.close_position("ZZZ") == "THROUGH"                    # reduces exposure
    assert seen and "unreadable" in seen[0]
