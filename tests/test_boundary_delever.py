"""Clause-5 witness for src.pipeline_delever (conversion step 11, MONEY):
DeleverService is built from explicit stand-ins and exercised with no trading
pipeline anywhere in this file. One forced sell, two refusals, one durable
record, one alert on a sell the protection layer declined."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
from boundary_harness import check_boundary  # noqa: E402

from src.pipeline_context import RunContext
from src.pipeline_delever import DeleverService


class _Broker:
    """Stand-in for the four broker calls the forced de-lever makes."""
    def __init__(self, cash_after):
        self.cash_after = cash_after
        self.cancelled = 0

    def get_latest_quote(self, symbol):
        return {}                      # no live quote -> market order

    def cancel_open_entry_orders(self):
        self.cancelled += 1

    def get_account(self):
        return {"cash": self.cash_after, "portfolio_value": 10_000.0}

    def get_positions(self):
        return []


class _TradeSink:
    """Stand-in for the one db method the forced de-lever calls."""
    def __init__(self):
        self.rows = []

    def insert_trade(self, **row):
        self.rows.append(row)


class _Protection:
    """Stand-in for the three order-shaped calls; `accept=False` declines."""
    def __init__(self, accept=True):
        self.accept = accept
        self.sells = []
        self.finalized = []

    def _submit_protected_sell(self, **kw):
        self.sells.append(kw)
        if not self.accept:
            return None
        return {"id": "order-1", "symbol": kw["symbol"]}, {"symbol": kw["symbol"]}

    def _open_exit_relief(self, positions):
        return [], False

    def _finalize_pending_protections(self, pending, *, context, wait=True):
        self.finalized.append((pending, context))


def _position(symbol="AAA", qty=100.0, price=100.0, pnl=-50.0):
    return SimpleNamespace(
        symbol=symbol, qty=qty, current_price=price, market_value=qty * price,
        unrealized_pnl=pnl, side="long",
    )


def _service(*, allow_margin=False, protection=None, broker=None, db=None):
    config = SimpleNamespace(risk=SimpleNamespace(allow_margin=allow_margin))
    return DeleverService(
        config=config, broker=broker or _Broker(cash_after=5_000.0),
        db=db or _TradeSink(), protection=protection or _Protection(),
        sweeper=lambda: None, sweep_symbol=lambda: None,
        full_sell_qty=lambda q: float(q), format_qty=lambda q: f"{q:g}",
        compute_deployable_cash=lambda cash, positions: cash,
    )


def _ctx(cash, positions):
    return RunContext(run_id="run-boundary", session="morning", cash=cash,
                      positions=positions, total_value=10_000.0)


def test_module_passes_the_boundary_harness():
    v = check_boundary("src.pipeline_delever")
    assert v.passed, v.failures


def test_margin_deficit_forces_one_whole_position_sell_without_a_pipeline():
    protection, db, broker = _Protection(), _TradeSink(), _Broker(cash_after=5_000.0)
    svc = _service(protection=protection, db=db, broker=broker)
    ctx = _ctx(-5_000.0, [_position()])
    orders = svc._force_delever(ctx)
    assert [o["id"] for o in orders] == ["order-1"]
    assert len(protection.sells) == 1
    sell = protection.sells[0]
    assert (sell["symbol"], sell["qty"], sell["limit_price"], sell["label"]) == ("AAA", 100.0, None, "FORCE_DELEVER")
    assert sell["escalate_to_market_on_reject"] is True
    assert db.rows[0]["action"] == "FORCE_DELEVER" and db.rows[0]["qty"] == 100.0
    assert protection.finalized[0][1] == "FORCE DE-LEVER"
    assert broker.cancelled == 1
    assert ctx.cash == 5_000.0 and ctx.positions == []   # refreshed from the broker


def test_refuses_when_margin_is_allowed_or_cash_is_not_in_deficit():
    protection = _Protection()
    assert _service(allow_margin=True, protection=protection)._force_delever(_ctx(-5_000.0, [_position()])) == []
    assert _service(protection=protection)._force_delever(_ctx(250.0, [_position()])) == []
    assert protection.sells == []


def test_declined_sell_is_not_recorded_as_a_trade_and_alerts_once():
    protection, db = _Protection(accept=False), _TradeSink()
    svc = _service(protection=protection, db=db)
    alerts = []
    svc._alert_owner_force_delever_incomplete = lambda **kw: alerts.append(kw)
    assert svc._force_delever(_ctx(-5_000.0, [_position()])) == []
    assert len(protection.sells) == 1 and db.rows == []
    assert alerts and alerts[0]["failed_symbols"] == ["AAA"]
