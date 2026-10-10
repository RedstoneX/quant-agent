"""FREEZE step 2 runs at EVERY owner-intent pickup, so a Freeze picked up before a 30-minute check sweeps then."""

import sqlite3
from unittest.mock import patch

from src.execution.broker import AlpacaBroker
from src.owner_flags import PAUSE
from src.scheduler import TradingScheduler
from src.storage.schema.owner_intent_tables import apply
from tests.pipeline_factory import build_pipeline
from tests.test_freeze_cancel import FakeBroker, _order, _pos


def _desk(tmp_path, monkeypatch, *, freeze_raised: bool):
    db = str(tmp_path / "desk.db")
    conn = sqlite3.connect(db)
    apply(conn)
    if freeze_raised:  # raised by the owner, not yet picked up by the desk
        conn.execute("INSERT INTO owner_intents (action, raised_at) VALUES (?, '2026-10-10T14:05:00Z')", (PAUSE,))
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
    with patch("src.scheduler.TradingPipeline", return_value=pipe):
        scheduler = TradingScheduler(pipe.config)
    return scheduler, broker


def _intra_check(broker, seen):
    def job():
        seen.append(list(broker.cancelled))
        return {"status": "executed"}

    return job


def test_freeze_picked_up_before_30_minute_check_cancels_entry_keeps_stop(tmp_path, monkeypatch):
    scheduler, broker = _desk(tmp_path, monkeypatch, freeze_raised=True)
    seen = []

    scheduler._run_safe(_intra_check(broker, seen), "intra_check")

    assert broker.cancelled == ["entry"]  # the protective stop is kept
    assert seen == [["entry"]]  # swept BEFORE the 30-minute check ran


def test_failed_sweep_is_recorded_and_retried_at_next_pickup(tmp_path, monkeypatch):
    scheduler, broker = _desk(tmp_path, monkeypatch, freeze_raised=True)
    real = broker.sweep_frozen_resting_orders
    calls = []

    def flaky(db_path):
        calls.append(db_path)
        if len(calls) == 1:
            raise ConnectionError("broker unreachable")
        return real(db_path)

    broker.sweep_frozen_resting_orders = flaky
    recorded = []
    monkeypatch.setattr(
        "src.sentinel.guarded.record_guarded_pass", lambda owner, where, exc=None, **kw: recorded.append((where, exc))
    )
    seen = []

    scheduler._run_safe(_intra_check(broker, seen), "intra_check")
    assert broker.cancelled == [] and seen == [[]]  # the job still ran
    assert [(w, type(e)) for w, e in recorded] == [("pipeline.freeze_sweep", ConnectionError)]

    scheduler._run_safe(_intra_check(broker, seen), "intra_check")  # flag unchanged: still swept
    assert broker.cancelled == ["entry"] and seen[-1] == ["entry"]


def test_not_frozen_no_sweep(tmp_path, monkeypatch):
    scheduler, broker = _desk(tmp_path, monkeypatch, freeze_raised=False)
    read = []
    broker.list_open_orders_checked = lambda: read.append(1) or (True, [])

    scheduler._run_safe(_intra_check(broker, []), "intra_check")

    assert read == [] and broker.cancelled == []


def test_failed_pickup_still_sweeps_a_freeze_already_in_force(tmp_path, monkeypatch):
    scheduler, broker = _desk(tmp_path, monkeypatch, freeze_raised=True)
    scheduler.pipeline.pickup_owner_intents()  # Freeze now acted on
    broker.cancelled.clear()
    broker._orders.append(_order("entry2", "NVDA", "buy", "3"))
    recorded = []
    monkeypatch.setattr(
        "src.sentinel.guarded.record_guarded_pass", lambda owner, where, exc=None, **kw: recorded.append(where)
    )

    def broken_intake(db_path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("src.owner_intents.intake", broken_intake)
    scheduler._run_safe(_intra_check(broker, []), "intra_check")

    assert recorded == ["pipeline.owner_intent_pickup"]
    assert "entry2" in broker.cancelled
