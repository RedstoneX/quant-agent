"""Board item 127 — two desk processes writing to the broker at once.

MEASURED (2026-09-16 13:30:42 UTC, production journal + app log + broker
order history, read-only): the standalone coverage sweep placed BRK-B's
0.4393-share DAY stop (broker order created 13:30:42.121); ~675 ms later an
`intra_check` and a `morning` run, started 5 ms apart, each read the same
stale gap and each tried to place the same stop. Both were refused
(`held_for_orders`), both exhausted their retries, and both declared a
failure on a stop that was in fact resting at the broker.

What was unguarded on main, and what these tests pin:

  * `intra_check`'s preamble (drains, coverage repair, cash-park release,
    orphan and fill reconciles) wrote to the broker before any lock. The
    worst pairing is not two repairs racing: it is the drain ADDING stops
    back while a live session has deliberately cancelled them to sell.
  * The standalone sweep's repair pass deferred to a session's lock but
    could not see `intra_check` at all.

Both now take the same advisory flock the paid scan already held
(`.intraday_scan.lock` beside the database), and `intra_check` also defers
to a live morning/midday/close owner, the check its paid scan already uses.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import alert_watchdog, coverage_watchdog
from src.pipeline import TradingPipeline
from src.trading_calendar import ET

_FRI_1005 = datetime(2026, 9, 11, 10, 5, tzinfo=ET).astimezone(timezone.utc)


def _intra_pipeline_with_a_cancelled_stop(tmp_path):
    """The session's cancel-stops-then-sell window, as the desk records it:
    NVDA's protective stop was cancelled so a SELL could go out, and the
    write-ahead row that says "put this stop back" is pending. This is the
    exact row `intra_check`'s drain replays."""
    from src.models import Position
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    cancelled = [{"id": "stop-old", "qty": 100, "stop_price": 95.0}]
    db.insert_pending_protection_restore(
        symbol="NVDA", sell_order_id="alpaca-sell-in-flight",
        position_qty_before_sell=100.0, specs_json=json.dumps(cancelled),
    )
    p = TradingPipeline.__new__(TradingPipeline)
    p.db = db
    p.config = SimpleNamespace(storage=SimpleNamespace(db_path=db.db_path))
    p.broker = MagicMock()
    p.broker.is_trading_day.return_value = True
    p.broker.get_account.return_value = {
        "portfolio_value": 100_500.0, "last_equity": 100_000.0, "cash": 5000.0,
    }
    p.broker.get_positions.return_value = [
        Position(
            symbol="NVDA", qty=100.0, avg_entry=100.0, current_price=100.0,
            market_value=10000.0, unrealized_pnl=0.0,
            unrealized_intraday_pnl=0.0, sector="Tech",
        ),
    ]
    p.broker.get_order_fill_info.return_value = {
        "status": "canceled", "filled_qty": "0", "filled_avg_price": None,
    }
    p.broker._restore_stop_orders.return_value = (1, [])
    p.risk_engine = MagicMock()
    return p, db, cancelled


def _live_owner(monkeypatch, tmp_path, mode="morning"):
    """`run_if_et_window.sh`'s owner file for a live session: mode, date,
    start time, pid. The pid is this test process so the liveness probe
    (`os.kill(pid, 0)`) answers "alive" exactly as it would for a real run."""
    home = tmp_path / "home"
    lock = home / ".cache" / "quant-agent" / "active-session.lock"
    lock.mkdir(parents=True)
    (lock / "owner").write_text(
        f"{mode} 2026-09-16 {int(time.time())} {os.getpid()}"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


# ---------------------------------------------------------------------------
# intra_check's preamble vs a live session cancelling stops to sell
# ---------------------------------------------------------------------------

def test_intra_check_does_not_put_back_a_stop_a_live_session_cancelled_to_sell(
    tmp_path, monkeypatch,
):
    p, db, _cancelled = _intra_pipeline_with_a_cancelled_stop(tmp_path)
    _live_owner(monkeypatch, tmp_path, mode="morning")

    result = p.run_intra_check()

    assert not p.broker._restore_stop_orders.called, (
        "intra_check re-placed a stop inside a live session's "
        "cancel-stops-then-sell window"
    )
    assert not p.broker._submit_protective_stop_retrying.called
    # The write-ahead row is the session's to finish, untouched.
    assert [r["symbol"] for r in db.get_pending_protection_restores()] == ["NVDA"]
    assert "morning" in result.get("preamble_deferred", "")
    # The loss check is never starved: it still read the account this tick.
    assert p.broker.get_account.called
    assert result["status"] == "ok"
    db.close()


def test_intra_check_preamble_runs_when_no_session_owns_the_desk(tmp_path, monkeypatch):
    """Positive control: same book, no live owner, lock free — the drain
    puts the stop back exactly as before."""
    p, db, cancelled = _intra_pipeline_with_a_cancelled_stop(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "empty-home"))

    result = p.run_intra_check()

    p.broker._restore_stop_orders.assert_called_once_with(
        "NVDA", cancelled, check_idempotency=True,
    )
    assert "preamble_deferred" not in result
    db.close()


def test_intra_check_preamble_waits_out_a_coverage_sweep_holding_the_lock(
    tmp_path, monkeypatch,
):
    """The standalone sweep's repair pass holds the shared flock: this tick's
    preamble must not write to the broker alongside it."""
    p, db, _cancelled = _intra_pipeline_with_a_cancelled_stop(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "empty-home"))

    import fcntl

    lock_file = Path(db.db_path).parent / ".intraday_scan.lock"
    with open(lock_file, "w") as sweep_holder:
        fcntl.flock(sweep_holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = p.run_intra_check()

    assert not p.broker._restore_stop_orders.called
    assert "lock" in result.get("preamble_deferred", "")
    assert p.broker.get_account.called
    db.close()


# ---------------------------------------------------------------------------
# the standalone sweep vs intra_check
# ---------------------------------------------------------------------------

def _gap_broker():
    """ORCL 5.3089 held, 5.0 covered, market open Friday 10:05 ET."""
    broker = MagicMock()
    broker.get_positions.return_value = [
        SimpleNamespace(symbol="ORCL", qty=5.3089, current_price=150.28),
    ]
    broker.snapshot_protective_stops.return_value = (
        True, [{"id": "gtc", "qty": 5.0, "stop_price": 137.53}],
    )
    broker.is_trading_day.return_value = True

    def _edge(hour, minute):
        return lambda on_date=None: datetime(
            on_date.year, on_date.month, on_date.day, hour, minute, tzinfo=ET,
        )

    broker.get_session_open.side_effect = _edge(9, 30)
    broker.get_session_close.side_effect = _edge(16, 0)
    broker.get_latest_price.return_value = 150.28
    broker.STOP_LIMIT_BUFFER_PCT = 0.01
    broker._submit_protective_stop_retrying.return_value = {
        "id": "new-day-stop", "uncovered_qty": 0.0,
    }
    return broker


def test_sweep_does_not_repair_while_intra_check_holds_the_desk_lock(tmp_path, monkeypatch):
    """`intra_check` holds its lock (its preamble or its paid scan); the
    sweep reads the gap, reports it, and places nothing."""
    from src.execution import scale_in

    db_path = tmp_path / "quant_agent.db"
    monkeypatch.setattr(alert_watchdog, "DB_PATH", db_path)
    monkeypatch.setattr(coverage_watchdog, "DB_PATH", db_path)
    monkeypatch.setattr(scale_in, "_SESSION_LOCK_DIR", tmp_path / "no-session")
    intra = TradingPipeline.__new__(TradingPipeline)
    intra.config = SimpleNamespace(storage=SimpleNamespace(db_path=str(db_path)))
    broker = _gap_broker()

    with intra._intraday_scan_process_lock() as acquired:
        assert acquired
        status = coverage_watchdog.check_coverage(
            broker, now=_FRI_1005, db_path=db_path,
            state_path=tmp_path / "state.json",
            last_buy=lambda _s, action="BUY": {"stop_loss": 137.53},
        )

    assert not broker._submit_protective_stop_retrying.called
    assert status.repairs == []
    assert "lock" in status.repair_deferred
    assert [g.symbol for g in status.gaps] == ["ORCL"]


def test_sweep_repairs_once_the_desk_lock_is_free(tmp_path, monkeypatch):
    from src.execution import scale_in

    db_path = tmp_path / "quant_agent.db"
    monkeypatch.setattr(alert_watchdog, "DB_PATH", db_path)
    monkeypatch.setattr(coverage_watchdog, "DB_PATH", db_path)
    monkeypatch.setattr(scale_in, "_SESSION_LOCK_DIR", tmp_path / "no-session")
    broker = _gap_broker()

    status = coverage_watchdog.check_coverage(
        broker, now=_FRI_1005, db_path=db_path,
        state_path=tmp_path / "state.json",
        last_buy=lambda _s, action="BUY": {"stop_loss": 137.53},
    )

    assert broker._submit_protective_stop_retrying.called
    assert getattr(status, "repair_deferred", "") == ""


def test_the_sweep_and_intra_check_share_one_lock_file(tmp_path):
    """The two processes exclude each other only if they name the same
    file. Hold it through the sweep's helper; the pipeline's must refuse."""
    db_path = tmp_path / "quant_agent.db"
    intra = TradingPipeline.__new__(TradingPipeline)
    intra.config = SimpleNamespace(storage=SimpleNamespace(db_path=str(db_path)))
    with coverage_watchdog.repair_lock(db_path) as held:
        assert held
        with intra._intraday_scan_process_lock() as acquired:
            assert acquired is False


# ---------------------------------------------------------------------------
# criterion (b): the seller's cancel-stops-then-sell window vs a repair pass
# ---------------------------------------------------------------------------

def test_repair_cannot_add_a_stop_during_the_cancel_then_sell_window(
    tmp_path, monkeypatch,
):
    """Item 127 (b): a session has cancelled NVDA's protective stop and is
    submitting the SELL; in that naked window a repair pass reading a coverage
    gap must place NOTHING. `_submit_protected_sell` now holds the shared flock
    across cancel -> submit, so the repair's own non-blocking acquire is
    refused — the worst pairing (a stop reappearing on shares un-protected in
    order to sell) can no longer happen."""
    from src.execution import scale_in
    from src.storage.db import Database

    db = Database(str(tmp_path / "quant_agent.db"))
    db.initialize()
    db_path = Path(db.db_path)
    monkeypatch.setattr(coverage_watchdog, "DB_PATH", db_path)
    monkeypatch.setattr(alert_watchdog, "DB_PATH", db_path)
    monkeypatch.setattr(scale_in, "_SESSION_LOCK_DIR", tmp_path / "no-session")

    p = TradingPipeline.__new__(TradingPipeline)
    p.db = db
    p.config = SimpleNamespace(storage=SimpleNamespace(db_path=db.db_path))
    p.broker = MagicMock()
    p.broker.snapshot_protective_stops.return_value = (
        True, [{"id": "stop-nvda", "qty": 100, "stop_price": 95.0}],
    )
    p.broker.cancel_snapshotted_stops.return_value = True

    gap_broker = _gap_broker()
    probe: dict = {}

    def _submit_probing_a_repair(**_kwargs):
        # We are in the naked window: NVDA's stop is cancelled and the SELL is
        # not yet resting. A repair pass firing right now must defer.
        probe["status"] = coverage_watchdog.check_coverage(
            gap_broker, now=_FRI_1005, db_path=db_path,
            state_path=tmp_path / "state.json",
            last_buy=lambda _s, action="BUY": {"stop_loss": 137.53},
        )
        return {"id": "sell-nvda", "status": "accepted"}

    p.broker.submit_order.side_effect = _submit_probing_a_repair

    result = p._submit_protected_sell(
        symbol="NVDA", qty=100.0, limit_price=None, reference_price=100.0,
        position_qty_before_sell=100.0, label="REDUCE",
    )

    assert result is not None, "the deliberate exit itself must still go through"
    status = probe["status"]
    assert status.repairs == [], "a repair placed a stop during the sell window"
    assert "lock" in status.repair_deferred
    assert not gap_broker._submit_protective_stop_retrying.called
    # The gap is still SEEN and reported — deferral is not blindness.
    assert [g.symbol for g in status.gaps] == ["ORCL"]
    db.close()


def test_sell_inside_a_held_scan_lock_does_not_deadlock(tmp_path):
    """A sell reached from inside a path that ALREADY holds the scan lock
    (e.g. a de-lever inside `intra_check`) must not block on itself. The
    blocking acquire in `_submit_protected_sell` would deadlock against our own
    outer flock fd (man flock: independent fds) without the reentrancy guard;
    with it, the nested acquire is a no-op and the sell proceeds promptly."""
    import threading

    from src.storage.db import Database

    db = Database(str(tmp_path / "quant_agent.db"))
    db.initialize()
    p = TradingPipeline.__new__(TradingPipeline)
    p.db = db
    p.config = SimpleNamespace(storage=SimpleNamespace(db_path=db.db_path))
    p.broker = MagicMock()
    p.broker.snapshot_protective_stops.return_value = (True, [])
    p.broker.submit_order.return_value = {"id": "sell-1", "status": "accepted"}

    box: dict = {}
    done = threading.Event()

    def _run():
        with p._intraday_scan_process_lock() as outer:
            box["outer"] = outer
            box["result"] = p._submit_protected_sell(
                symbol="NVDA", qty=10.0, limit_price=None, reference_price=100.0,
                position_qty_before_sell=10.0, label="REDUCE",
            )
        done.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    assert done.wait(timeout=10), (
        "a sell inside a held scan lock deadlocked on the flock"
    )
    assert box["outer"] is True
    assert box["result"] is not None
    db.close()


# ---------------------------------------------------------------------------
# criterion (b), adversary fixes: the wait is BOUNDED, and proceeding
# unlocked leaves a durable record AND pages the owner
# ---------------------------------------------------------------------------

def test_sell_gives_up_a_stuck_lock_and_records_and_alerts(tmp_path, monkeypatch):
    """FIX 1 + FIX 2: a must-fill exit must not stall forever on a stuck lock
    holder. With the shared flock held by someone else, `_submit_protected_sell`
    waits only the bounded engineering timeout, then PROCEEDS unlocked — and
    because the item-127 naked-window serialization was dropped for this
    symbol, it leaves a DURABLE record and pages the owner."""
    import fcntl

    from src import notifier
    from src.storage.db import Database

    db = Database(str(tmp_path / "quant_agent.db"))
    db.initialize()

    # Someone else (a stuck repair pass) holds the shared flock and never lets
    # go for the duration of this test — a separate fd, so it genuinely blocks
    # this process's non-blocking acquire.
    lock_path = Path(db.db_path).parent / ".intraday_scan.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "w")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)

    # A tiny bounded wait so the test is fast; the real value is 120s.
    monkeypatch.setattr("src.pipeline._DESK_LOCK_BLOCKING_TIMEOUT_S", 0.4)
    monkeypatch.setattr("src.pipeline._DESK_LOCK_POLL_INTERVAL_S", 0.05)

    alerts: list = []
    monkeypatch.setattr(
        notifier, "send_owner_alert",
        lambda text, symbols=None: alerts.append((text, symbols)) or True,
    )

    p = TradingPipeline.__new__(TradingPipeline)
    p.db = db
    p.config = SimpleNamespace(storage=SimpleNamespace(db_path=db.db_path))
    p.broker = MagicMock()
    p.broker.snapshot_protective_stops.return_value = (True, [])
    p.broker.submit_order.return_value = {"id": "sell-1", "status": "accepted"}

    started = time.monotonic()
    result = p._submit_protected_sell(
        symbol="NVDA", qty=10.0, limit_price=None, reference_price=100.0,
        position_qty_before_sell=10.0, label="FORCE_DELEVER",
    )
    waited = time.monotonic() - started

    # The exit itself went through, and it did so promptly — bounded, not stuck.
    assert result is not None, "the must-fill exit must proceed unlocked"
    assert waited < 5.0, "the wait was not bounded by the engineering timeout"

    # Durable record: one stop_serialization_dropped row for NVDA.
    rows = db.get_latest_symbol_evidence("stop_serialization_dropped", ["NVDA"])
    assert "NVDA" in rows, "no durable serialization-dropped record was written"
    payload = json.loads(rows["NVDA"]["evidence_json"])
    assert payload["label"] == "FORCE_DELEVER"

    # Owner alert: paged, and scoped to the symbol.
    assert len(alerts) == 1, "the owner was not paged that serialization dropped"
    text, symbols = alerts[0]
    assert "SERIALIZATION DROPPED" in text
    assert symbols == ["NVDA"]

    fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    holder.close()
    db.close()
