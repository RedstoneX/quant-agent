"""Board item 127(b) — a stray protective stop on a now-FLAT position is
auto-cleaned after a full exit.

Owner ruling 2026-09-25: a forced/emergency sale fires IMMEDIATELY and never
waits on stop-work (the heavy lock-wait of #650 was rejected). The tolerated
cost is that a concurrent stop-repair can re-add a protective stop inside the
cancel-then-sell window; once the exit takes the position flat, that stop is a
stray. It is not in the sell's ``cancelled_specs`` (placed AFTER the pre-sell
snapshot), the reprotect path skips it on a full exit, and
``_reconcile_stop_coverage`` never inspects a flat symbol — so nothing else
would ever clear it, and a stop resting on zero shares can later elect into an
unintended short.

This file proves the cheap replacement cleanup:
  1. The finalize core, on a full exit, asks the broker to cancel any stray
     protective stop for the symbol (side-correct for a long and a short).
  2. The broker method actually cancels each resting stop by id, without a
     rollback, best-effort, and never raises.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.execution.broker import AlpacaBroker
from src.pipeline import TradingPipeline


def _mk_pipeline() -> TradingPipeline:
    p = TradingPipeline.__new__(TradingPipeline)
    p.db = MagicMock()
    p.broker = MagicMock()
    p._format_qty = lambda q: str(q)
    return p


# ==========================================================================
# 1. Finalize wiring: a full exit triggers stray-stop cleanup.
# ==========================================================================

def test_full_exit_cancels_stray_stop_long():
    """A SELL that takes a long fully flat must ask the broker to clear any
    stray protective SELL-stop left resting on the now-flat symbol."""
    p = _mk_pipeline()
    p.broker.get_order_fill_info.return_value = {
        "status": "filled", "filled_qty": "10", "filled_avg_price": "100",
    }
    # Cached residual would be 0 (10 - 10); the broker confirms position=0.
    p._current_position_qty_for_finalize = MagicMock(return_value=0.0)

    cancelled = [{"id": "stop-old", "qty": 10, "stop_price": 95.0}]
    ok, retry = p._finalize_protection_after_sell_core(
        "sell-order-1", "NVDA", 10.0, cancelled,
    )

    assert ok is True and retry == []
    # No residual → no reprotect, but the stray-stop sweep DID run, side-correct.
    p.broker.cancel_stray_protective_stops.assert_called_once_with("NVDA", side="sell")


def test_full_exit_cancels_stray_stop_short_uses_buy_side():
    """Covering a short fully flat must clear a stray BUY-stop, not a SELL-stop."""
    p = _mk_pipeline()
    p.broker.get_order_fill_info.return_value = {
        "status": "filled", "filled_qty": "0", "filled_avg_price": None,
    }
    # No fill on THIS cover, but a concurrent path already took it flat.
    p._current_position_qty_for_finalize = MagicMock(return_value=0.0)

    cancelled = [{"id": "buy-stop-old", "qty": 40, "stop_price": 262.5}]
    ok, retry = p._finalize_protection_after_sell_core(
        "cover-order-1", "TSLA", 40.0, cancelled, side="buy",
    )

    assert ok is True and retry == []
    p.broker.cancel_stray_protective_stops.assert_called_once_with("TSLA", side="buy")


def test_partial_exit_does_not_sweep_stray_stops():
    """A partial SELL leaves a live residual — the resting stop is legitimate
    coverage, not a stray. The sweep must NOT run."""
    p = _mk_pipeline()
    p.broker.get_order_fill_info.return_value = {
        "status": "filled", "filled_qty": "4", "filled_avg_price": "100",
    }
    p._current_position_qty_for_finalize = MagicMock(return_value=6.0)
    p._reprotect_residual_after_partial_sell = MagicMock(return_value=True)

    cancelled = [{"id": "stop-old", "qty": 10, "stop_price": 95.0}]
    ok, retry = p._finalize_protection_after_sell_core(
        "sell-order-2", "NVDA", 10.0, cancelled,
    )

    assert ok is True and retry == []
    p.broker.cancel_stray_protective_stops.assert_not_called()


def test_stray_cleanup_failure_is_not_fatal_to_the_exit():
    """The exit already succeeded — a housekeeping error clearing the stray
    stop must be swallowed, not raised."""
    p = _mk_pipeline()
    p.broker.get_order_fill_info.return_value = {
        "status": "filled", "filled_qty": "10", "filled_avg_price": "100",
    }
    p._current_position_qty_for_finalize = MagicMock(return_value=0.0)
    p.broker.cancel_stray_protective_stops.side_effect = RuntimeError("broker down")

    cancelled = [{"id": "stop-old", "qty": 10, "stop_price": 95.0}]
    ok, retry = p._finalize_protection_after_sell_core(
        "sell-order-3", "NVDA", 10.0, cancelled,
    )

    assert ok is True and retry == []


# ==========================================================================
# 2. Broker method: cancels each stray stop by id, no rollback.
# ==========================================================================

@patch("src.execution.broker.TradingClient")
def test_cancel_stray_protective_stops_cancels_by_id(mock_tc_cls):
    mock_client = MagicMock()
    mock_client.get_orders.return_value = [
        SimpleNamespace(
            id="stray-1", order_type="stop_limit", side="sell", symbol="NVDA",
            qty="10", stop_price="95", limit_price="94.5",
        ),
    ]
    mock_tc_cls.return_value = mock_client
    broker = AlpacaBroker(api_key="k", secret_key="s", paper=True)

    n = broker.cancel_stray_protective_stops("NVDA", side="sell")

    assert n == 1
    mock_client.cancel_order_by_id.assert_called_once_with("stray-1")


@patch("src.execution.broker.TradingClient")
def test_cancel_stray_protective_stops_no_stops_is_noop(mock_tc_cls):
    mock_client = MagicMock()
    mock_client.get_orders.return_value = []
    mock_tc_cls.return_value = mock_client
    broker = AlpacaBroker(api_key="k", secret_key="s", paper=True)

    n = broker.cancel_stray_protective_stops("NVDA", side="sell")

    assert n == 0
    mock_client.cancel_order_by_id.assert_not_called()


@patch("src.execution.broker.TradingClient")
def test_cancel_stray_protective_stops_never_restores(mock_tc_cls):
    """Unlike cancel_snapshotted_stops there is NO rollback: the position is
    flat, so a restore would only re-place the stray order we are removing."""
    mock_client = MagicMock()
    mock_client.get_orders.return_value = [
        SimpleNamespace(
            id="stray-1", order_type="stop", side="sell", symbol="NVDA",
            qty="10", stop_price="95", limit_price=None,
        ),
    ]
    mock_tc_cls.return_value = mock_client
    broker = AlpacaBroker(api_key="k", secret_key="s", paper=True)
    broker._restore_stop_orders = MagicMock()

    broker.cancel_stray_protective_stops("NVDA", side="sell")

    broker._restore_stop_orders.assert_not_called()
