"""Guard 1 (2026-09-02 operational safety guard) — the kill switch.

A file whose mere EXISTENCE halts every order this desk would place, entries
and exits alike. Checked with `path.exists()` and nothing else in
`src/execution/broker.py` (the deterministic execution layer), never by an
agent or a prompt.

These tests assert three things the guard promises:
  1. It is `path.exists()` and nothing more — a zero-byte or garbage file
     halts identically to a well-formed one, because content is never read.
  2. It is unconditional — UNLIKE every other hard block / circuit breaker
     in this codebase, it also refuses a SELL/COVER/protective-stop order.
  3. Ops can flip it with `touch`/`rm` alone: no path is disabled when the
     file is absent, and every guarded method still works normally then.
"""
from unittest.mock import MagicMock, patch

import pytest

from src.execution.broker import AlpacaBroker


def _broker(tmp_path, *, halted: bool, garbage: bool = False) -> AlpacaBroker:
    flag = tmp_path / "KILL_SWITCH"
    if halted:
        if garbage:
            flag.write_bytes(b"\x00\x01not json at all {{{")
        else:
            flag.touch()
    broker = AlpacaBroker(
        api_key="test", secret_key="test", paper=True,
        kill_switch_path=str(flag),
    )
    return broker


# --- _kill_switch_active(): path.exists() and nothing more -----------------

@patch("src.execution.broker.TradingClient")
def test_kill_switch_inactive_when_file_absent(mock_tc_cls, tmp_path):
    mock_tc_cls.return_value = MagicMock()
    broker = _broker(tmp_path, halted=False)
    assert broker._kill_switch_active() is False


@patch("src.execution.broker.TradingClient")
def test_kill_switch_active_on_empty_file(mock_tc_cls, tmp_path):
    """Zero-byte file — the check must not try to parse it."""
    mock_tc_cls.return_value = MagicMock()
    broker = _broker(tmp_path, halted=True)
    assert broker._kill_switch_active() is True


@patch("src.execution.broker.TradingClient")
def test_kill_switch_active_on_garbage_content(mock_tc_cls, tmp_path):
    """A malformed/garbage file halts identically — content is never read,
    so it cannot fail open on bad content."""
    mock_tc_cls.return_value = MagicMock()
    broker = _broker(tmp_path, halted=True, garbage=True)
    assert broker._kill_switch_active() is True


@patch("src.execution.broker.TradingClient")
def test_kill_switch_inactive_when_no_path_configured(mock_tc_cls):
    """kill_switch_path=None (a construction site that predates the guard,
    e.g. an isolated unit test) must not raise or accidentally halt."""
    mock_tc_cls.return_value = MagicMock()
    broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)
    assert broker._kill_switch_active() is False


@patch("src.execution.broker.TradingClient")
def test_kill_switch_takes_effect_between_calls(mock_tc_cls, tmp_path):
    """No caching — a `touch` mid-session must be seen on the very next
    check, which is what 'works even when the process is wedged
    mid-session' requires."""
    mock_tc_cls.return_value = MagicMock()
    broker = _broker(tmp_path, halted=False)
    assert broker._kill_switch_active() is False
    (tmp_path / "KILL_SWITCH").touch()
    assert broker._kill_switch_active() is True
    (tmp_path / "KILL_SWITCH").unlink()
    assert broker._kill_switch_active() is False


# --- submit_order(): halts BUY (entry) and SELL (exit) alike ---------------

@patch("src.execution.broker.TradingClient")
def test_submit_order_buy_blocked_when_halted(mock_tc_cls, tmp_path):
    mock_client = MagicMock()
    mock_tc_cls.return_value = mock_client
    broker = _broker(tmp_path, halted=True)

    result = broker.submit_order(symbol="NVDA", qty=10, side="buy", limit_price=100.0)

    assert result["id"] is None
    assert result["status"] == "kill_switch_halted"
    mock_client.submit_order.assert_not_called()


@patch("src.execution.broker.TradingClient")
def test_submit_order_sell_ALSO_blocked_when_halted(mock_tc_cls, tmp_path):
    """The one deliberate exception in the codebase: unlike
    RiskRuleEngine.check and apply_gross_ceiling (which exempt SELL/COVER),
    the kill switch blocks an exit too."""
    mock_client = MagicMock()
    mock_tc_cls.return_value = mock_client
    broker = _broker(tmp_path, halted=True)

    result = broker.submit_order(symbol="NVDA", qty=10, side="sell")

    assert result["id"] is None
    assert result["status"] == "kill_switch_halted"
    mock_client.submit_order.assert_not_called()


@patch("src.execution.broker.TradingClient")
def test_submit_order_cover_ALSO_blocked_when_halted(mock_tc_cls, tmp_path):
    """A COVER (buy-to-close a short) is risk-reducing and still halted."""
    mock_client = MagicMock()
    mock_tc_cls.return_value = mock_client
    broker = _broker(tmp_path, halted=True)

    result = broker.submit_order(symbol="GME", qty=5, side="buy")

    assert result["id"] is None
    assert result["status"] == "kill_switch_halted"
    mock_client.submit_order.assert_not_called()


@patch("src.execution.broker.TradingClient")
def test_submit_order_proceeds_normally_when_not_halted(mock_tc_cls, tmp_path):
    """Regression guard: the guard must not block ordinary operation."""
    mock_client = MagicMock()
    mock_client.submit_order.return_value = MagicMock(
        id="abc123", status="accepted",
    )
    mock_tc_cls.return_value = mock_client
    broker = _broker(tmp_path, halted=False)

    result = broker.submit_order(symbol="NVDA", qty=10, side="buy", limit_price=100.0)

    assert result["id"] == "abc123"
    assert result["status"] != "kill_switch_halted"
    mock_client.submit_order.assert_called_once()


# --- _submit_stop_limit_order(): a protective stop is risk-reducing, and --
# --- the kill switch is the one guard that blocks it anyway --------------

@patch("src.execution.broker.TradingClient")
def test_protective_stop_blocked_when_halted(mock_tc_cls, tmp_path):
    mock_client = MagicMock()
    mock_tc_cls.return_value = mock_client
    broker = _broker(tmp_path, halted=True)

    result = broker._submit_stop_limit_order(
        symbol="NVDA", qty=10, stop_price=95.0,
    )

    assert result["id"] is None
    assert result["status"] == "kill_switch_halted"
    mock_client.submit_order.assert_not_called()


@patch("src.execution.broker.TradingClient")
def test_protective_stop_proceeds_normally_when_not_halted(mock_tc_cls, tmp_path):
    mock_client = MagicMock()
    mock_client.submit_order.return_value = MagicMock(id="stop1", status="accepted")
    mock_tc_cls.return_value = mock_client
    broker = _broker(tmp_path, halted=False)

    result = broker._submit_stop_limit_order(
        symbol="NVDA", qty=10, stop_price=95.0,
    )

    assert result["id"] == "stop1"
    mock_client.submit_order.assert_called_once()


# --- replace_entry_limit(): re-pegging is still order flow -----------------

@patch("src.execution.broker.TradingClient")
def test_replace_entry_limit_blocked_when_halted(mock_tc_cls, tmp_path):
    mock_client = MagicMock()
    mock_tc_cls.return_value = mock_client
    broker = _broker(tmp_path, halted=True)

    result = broker.replace_entry_limit("order-1", 101.0)

    assert result["id"] is None
    assert result["status"] == "kill_switch_halted"
    mock_client.replace_order_by_id.assert_not_called()


@patch("src.execution.broker.TradingClient")
def test_replace_entry_limit_proceeds_normally_when_not_halted(mock_tc_cls, tmp_path):
    mock_client = MagicMock()
    mock_client.replace_order_by_id.return_value = MagicMock(id="order-2", status="new")
    mock_tc_cls.return_value = mock_client
    broker = _broker(tmp_path, halted=False)

    result = broker.replace_entry_limit("order-1", 101.0)

    assert result["id"] == "order-2"
    mock_client.replace_order_by_id.assert_called_once()


# --- Pipeline-level early check: visible, single alert, saves LLM spend ---

def _pipeline_with_kill_switch(tmp_path, *, halted: bool):
    from src.pipeline import TradingPipeline

    flag = tmp_path / "KILL_SWITCH"
    if halted:
        flag.touch()
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline._kill_switch_path = flag
    return pipeline


def test_kill_switch_halt_result_none_when_absent(tmp_path):
    pipeline = _pipeline_with_kill_switch(tmp_path, halted=False)
    assert pipeline._kill_switch_halt_result("run-1") is None


def test_kill_switch_halt_result_present_when_active(tmp_path):
    pipeline = _pipeline_with_kill_switch(tmp_path, halted=True)
    result = pipeline._kill_switch_halt_result("run-1")
    assert result is not None
    assert result["status"] == "kill_switch_halted"
    assert result["run_id"] == "run-1"
    assert result["orders"] == []


def test_kill_switch_halt_result_merges_extra_fields(tmp_path):
    """run_position_review passes positions=0 to match its own
    market_holiday early-return shape."""
    pipeline = _pipeline_with_kill_switch(tmp_path, halted=True)
    result = pipeline._kill_switch_halt_result("run-1", positions=0)
    assert result["positions"] == 0


@patch("src.execution.broker.TradingClient")
def test_run_morning_short_circuits_when_halted(mock_tc_cls, tmp_path, monkeypatch):
    """run_morning must not reach any broker call once halted — it should
    return the kill_switch_halted payload before _reconcile_stop_coverage
    or any other broker-touching preamble step."""
    from src.pipeline import TradingPipeline
    from src.pipeline_context import RunContext

    mock_tc_cls.return_value = MagicMock()
    flag = tmp_path / "KILL_SWITCH"
    flag.touch()

    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline._kill_switch_path = flag
    pipeline._is_trading_day = lambda: True
    pipeline._reconcile_stop_coverage = MagicMock(
        side_effect=AssertionError("must not run past the kill switch"),
    )

    result = pipeline.run_morning()

    assert result["status"] == "kill_switch_halted"
    pipeline._reconcile_stop_coverage.assert_not_called()


@patch("src.execution.broker.TradingClient")
def test_run_intra_check_short_circuits_when_halted(mock_tc_cls, tmp_path):
    from src.pipeline import TradingPipeline

    mock_tc_cls.return_value = MagicMock()
    flag = tmp_path / "KILL_SWITCH"
    flag.touch()

    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline._kill_switch_path = flag
    pipeline._is_trading_day = lambda: True
    pipeline._drain_pending_protection_restores = MagicMock(
        side_effect=AssertionError("must not run past the kill switch"),
    )

    result = pipeline.run_intra_check()

    assert result["status"] == "kill_switch_halted"
    pipeline._drain_pending_protection_restores.assert_not_called()


@patch("src.execution.broker.TradingClient")
def test_run_position_review_short_circuits_when_halted(mock_tc_cls, tmp_path):
    from src.pipeline import TradingPipeline

    mock_tc_cls.return_value = MagicMock()
    flag = tmp_path / "KILL_SWITCH"
    flag.touch()

    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline._kill_switch_path = flag
    pipeline._is_trading_day = lambda: True

    result = pipeline.run_position_review(session_type="midday")

    assert result["status"] == "kill_switch_halted"
    assert result["positions"] == 0
