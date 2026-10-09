"""Trade-stream catch-alls: a swallowed fault is loud, a clean pass has its own row."""

import asyncio
import logging
from types import SimpleNamespace

from src.execution.broker import AlpacaBroker
from src.execution.broker_parts.trade_stream import _TradeUpdatesHub
from src.sentinel.guarded import attach_reconciliation_db
from src.sentinel.reconciliation import AGREED, DISAGREED, NOT_RUN, ReconciliationLog
from src.storage.db import Database

WHERE = "trade_stream.hub_handler"


def _hub(tmp_path):
    broker = AlpacaBroker.__new__(AlpacaBroker)
    db = Database(str(tmp_path / "ts_guards.db"))
    db.initialize()
    attach_reconciliation_db(broker, lambda: db.conn)
    broker.api_key, broker.secret_key, broker._paper = "k", "s", True
    hub = _TradeUpdatesHub(broker)
    captured = {}
    hub._stream = None

    class _Stream:
        def subscribe_trade_updates(self, handler):
            captured["handler"] = handler

        def run(self):
            return None

    hub._stream = _Stream()
    return hub, db, captured


def _install_handler(hub, captured):
    import src.execution.broker_parts.trade_stream as ts

    orig = ts.TradingStream
    ts.TradingStream = lambda *a, **k: hub._stream
    try:
        hub.start()
    except Exception:
        pass
    finally:
        ts.TradingStream = orig
    return captured.get("handler")


def _status(db):
    return ReconciliationLog(conn=db.conn).status(kind=f"guarded:execution.broker_parts.{WHERE}")


def test_hub_handler_clean_and_swallowed_are_distinct_and_loud(tmp_path, caplog):
    hub, db, captured = _hub(tmp_path)
    handler = _install_handler(hub, captured)
    assert handler is not None
    assert _status(db) == NOT_RUN
    clean = SimpleNamespace(order=SimpleNamespace(id="o1", status="new"))
    asyncio.run(handler(clean))
    assert _status(db) == AGREED
    hub._kick = lambda: (_ for _ in ()).throw(TypeError("duplicate argument"))
    with caplog.at_level(logging.ERROR):
        asyncio.run(handler(clean))
    assert _status(db) == DISAGREED
    assert any(r.exc_info and "duplicate argument" in str(r.exc_info[1]) for r in caplog.records)
