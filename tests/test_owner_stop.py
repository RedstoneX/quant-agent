"""Owner Stop (ruling 2026-10-09): the desk is OFF. Resting entries cancelled, stops kept, nothing else runs."""

import sqlite3
import sys
from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest

from src.execution.broker import AlpacaBroker
from src.owner_flags import PAUSE, RESUME, STOP, current_flags
from src.scheduler import TradingScheduler
from src.storage.schema.owner_intent_tables import apply
from tests.pipeline_factory import build_pipeline
from tests.test_freeze_cancel import FakeBroker, _order, _pos

READS_ALLOWED = {"get_positions", "list_open_orders_checked", "cancel_entry_order", "sweep_frozen_resting_orders"}


class CountingBroker(FakeBroker):
    """The freeze-cancel fake, recording every other broker call a job makes."""

    def __init__(self):
        super().__init__(
            [_pos("MSFT", "5")], [_order("entry", "AAPL", "buy", "10"), _order("stop", "MSFT", "sell", "5", "stop")]
        )
        self.other_calls = []

    def is_trading_day(self):
        self.other_calls.append("is_trading_day")
        return True

    def get_bars(self, *a, **k):
        self.other_calls.append("get_bars")

    def sweep_frozen_resting_orders(self, db_path):
        return AlpacaBroker.sweep_frozen_resting_orders(self, db_path)


def _db(tmp_path, *actions):
    db = str(tmp_path / "desk.db")
    conn = sqlite3.connect(db)
    apply(conn)
    for i, action in enumerate(actions):  # raised by the owner, not yet picked up by the desk
        conn.execute("INSERT INTO owner_intents (action, raised_at) VALUES (?, ?)", (action, f"2026-10-10T14:0{i}:00Z"))
    conn.commit()
    conn.close()
    return db


def _scheduler(tmp_path, monkeypatch, *actions):
    db = _db(tmp_path, *actions)
    broker = CountingBroker()
    pipe = build_pipeline(broker=broker)
    monkeypatch.setattr(pipe.config.storage, "db_path", db)
    with patch("src.scheduler.TradingPipeline", return_value=pipe):
        scheduler = TradingScheduler(pipe.config)
    return scheduler, broker, db


def _job(ran):
    def job():
        ran.append(True)
        return {"status": "executed"}

    return job


def test_stopped_scheduled_job_cancels_entries_keeps_stops_and_does_nothing_else(tmp_path, monkeypatch):
    scheduler, broker, _ = _scheduler(tmp_path, monkeypatch, STOP)
    ran = []

    scheduler._run_safe(_job(ran), "intra_check")

    assert broker.cancelled == ["entry"]  # the protective stop is kept, positions untouched
    assert broker.replaced == []
    assert broker.other_calls == []  # no broker read beyond the cancel sweep
    assert ran == []  # the job itself never ran: no reads, no AI


def test_start_clears_stop_and_the_job_runs_again(tmp_path, monkeypatch):
    scheduler, broker, db = _scheduler(tmp_path, monkeypatch, STOP)
    ran = []
    scheduler._run_safe(_job(ran), "intra_check")
    assert ran == []

    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO owner_intents (action, raised_at) VALUES (?, '2026-10-10T15:00:00Z')", (RESUME,))
    conn.commit()
    conn.close()
    scheduler._run_safe(_job(ran), "intra_check")

    assert ran == [True]


@pytest.mark.parametrize("order", [(PAUSE, STOP), (STOP, PAUSE)])
def test_stop_takes_precedence_over_freeze(tmp_path, monkeypatch, order):
    scheduler, broker, db = _scheduler(tmp_path, monkeypatch, *order)
    ran = []

    scheduler._run_safe(_job(ran), "morning")

    assert ran == [] and broker.cancelled == ["entry"]
    conn = sqlite3.connect(db)
    flags = current_flags(conn)
    conn.close()
    assert flags.stopped and flags.frozen  # the broker door refuses entries too


def _run_main(tmp_path, monkeypatch, *actions):
    import main

    db = _db(tmp_path, *actions)
    cfg = tmp_path / "settings.yaml"
    cfg.write_text("# loaded through the patched load_config\n")
    broker = CountingBroker()
    config = NS(storage=NS(db_path=db), api_keys=NS(alpaca_key="k", alpaca_secret="s"), alpaca=NS(paper=True))
    monkeypatch.setattr(main, "load_config", lambda path: config)
    monkeypatch.setattr("src.execution.broker.AlpacaBroker", lambda **kw: broker)
    built = []
    monkeypatch.setattr(main, "TradingPipeline", lambda *a, **k: built.append("pipeline") or pytest.fail("pipeline"))
    monkeypatch.setattr(main, "TelegramNotifier", lambda *a, **k: built.append("notifier") or pytest.fail("notifier"))
    monkeypatch.setattr(sys, "argv", ["main.py", "--mode", "morning", "--config", str(cfg)])
    with pytest.raises(BaseException) as exc:
        main.main()
    return exc.value, broker, built


def test_one_shot_entry_exits_early_while_stopped(tmp_path, monkeypatch):
    import main

    exited, broker, built = _run_main(tmp_path, monkeypatch, STOP)

    assert isinstance(exited, SystemExit) and exited.code == main.OWNER_STOP_EXIT
    assert built == []  # no notifier, no pipeline, so no AI client constructed
    assert broker.cancelled == ["entry"] and broker.other_calls == []


def test_one_shot_entry_runs_once_start_clears_stop(tmp_path, monkeypatch):
    exited, broker, built = _run_main(tmp_path, monkeypatch, STOP, RESUME)

    assert built == ["notifier"]  # proceeded into the normal session path
    assert broker.cancelled == []
