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


def _hold(db_path, sym):
    c = sqlite3.connect(db_path)
    c.execute("INSERT INTO positions (symbol, qty, avg_entry, current_price, market_value, unrealized_pnl)"
              " VALUES (?,1,1,1,1,0)", (sym,))
    c.commit(); c.close()


def test_intent_survives_restart_and_is_acted_on(db_path):
    _raise(db_path, oi.PAUSE, reason="owner asked while desk down")
    assert owner_flags.read_flags(db_path).paused is False  # raised, not yet acted
    oi.intake(db_path)  # the desk starting
    assert _rows(db_path)[0][3] == "acted"
    assert owner_flags.read_flags(db_path).paused is True


def test_stale_intent_is_refused_with_its_reason(db_path):
    _raise(db_path, oi.HANDS_OFF, symbol="ZZZT")  # never held by the time the desk starts
    oi.intake(db_path)
    (_, _, _, state, outcome), = _rows(db_path)
    assert state == "refused" and "no longer a held position" in outcome
    assert owner_flags.read_flags(db_path).hands_off == frozenset()


def test_owner_expiry_is_honoured(db_path):
    _raise(db_path, oi.PAUSE, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    oi.intake(db_path)
    assert _rows(db_path)[0][3] == "expired"
    assert owner_flags.read_flags(db_path).paused is False


def test_flags_replay_in_order(db_path):
    _hold(db_path, "AAA")
    for a, s in [(oi.PAUSE, None), (oi.RESUME, None), (oi.NEVER_TOUCH_ADD, "bbb"),
                 (oi.NEVER_TOUCH_ADD, "ccc"), (oi.NEVER_TOUCH_REMOVE, "ccc"),
                 (oi.HANDS_OFF, "AAA"), (oi.HANDS_ON, "AAA"), (oi.HANDS_OFF, "AAA")]:
        _raise(db_path, a, symbol=s)
    oi.intake(db_path)
    f = owner_flags.read_flags(db_path)
    assert (f.paused, f.never_touch, f.hands_off) == (False, {"BBB"}, {"AAA"})


class _Door:
    """Stands in for the broker: records which verbs got through."""
    calls: list
    client = None


def _recording_class():
    calls = []
    ns = {n: (lambda n: lambda self, *a, **k: calls.append(n) or "THROUGH")(n) for n in gate._RULES}
    ns["get_positions"] = lambda self: "READ"
    cls = type("Door", (), ns)
    gate.install(cls)
    return cls, calls


def test_paused_desk_raises_no_orders_and_resume_restores(db_path):
    cls, calls = _recording_class()
    d = cls()
    _raise(db_path, oi.PAUSE); oi.intake(db_path)
    for n in gate.PAUSE_BLOCKS:
        assert getattr(d, n)("ABC") != "THROUGH"
    assert not calls
    d.replace_stop_loss("ABC", 1.0)  # protection upkeep continues under pause
    assert calls == ["replace_stop_loss"]
    _raise(db_path, oi.RESUME); oi.intake(db_path)
    assert d.submit_order("ABC") == "THROUGH"


@pytest.mark.parametrize("flag", [oi.HANDS_OFF, oi.NEVER_TOUCH_ADD])
def test_flagged_symbol_is_skipped_by_every_write_verb_but_still_reported(db_path, flag):
    _hold(db_path, "ABC")
    cls, calls = _recording_class()
    d = cls()
    _raise(db_path, flag, symbol="ABC"); oi.intake(db_path)
    for n, (kind, _) in gate._RULES.items():
        if kind in ("arg", "optarg"):
            getattr(d, n)("ABC")
    assert calls == []  # every symbol-bound verb refused
    d.client = type("C", (), {"get_order_by_id": lambda s, oid: type("O", (), {"symbol": "ABC"})()})()
    assert d.cancel_entry_order("oid") is False and d.replace_entry_limit("oid", 1.0)["status"] == "owner_flag_halted"
    assert d.cancel_open_orders() == 0 and calls == []
    assert d.submit_order("OTHER") == "THROUGH"  # other names unaffected
    assert d.get_positions() == "READ"  # reporting is not gated


def test_every_broker_write_verb_is_gated_and_reads_are_not():
    names = gate.write_methods(AlpacaBroker)
    assert set(names) == set(gate._RULES), "rule table and broker verbs diverged"
    for n in names:
        assert getattr(getattr(AlpacaBroker, n), "_owner_flag_gated", False), n
    assert not getattr(AlpacaBroker.get_positions, "_owner_flag_gated", False)


def test_every_sdk_order_write_lives_behind_the_gated_broker():
    sdk = re.compile(r"\.(submit_order|cancel_order_by_id|cancel_orders|replace_order_by_id|"
                     r"close_position|close_all_positions)\(")
    # order_idempotency.py is the broker parts' own submit helper. sell_finalization
    # cancels a lingering SELL the desk itself placed, so protection state can settle:
    # cancel-only and risk-reducing, deliberately left outside the flags (reported).
    allowed = ("src/execution/broker.py", "src/execution/broker_parts/",
               "src/execution/order_idempotency.py", "src/protection/sell_finalization.py")
    stray = []
    for p in (ROOT / "src").rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        for line in p.read_text().splitlines():
            m = sdk.search(line)
            if m and re.search(r"(client|_client)\.\w+\($", line[:m.end()]) and not rel.startswith(allowed):
                stray.append((rel, line.strip()))
    assert not stray, stray
