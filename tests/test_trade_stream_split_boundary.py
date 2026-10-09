"""Boundary witness for the modules lifted out of trade_stream.py (second-round split)."""

from __future__ import annotations

from tests.boundary_harness import check_boundary

_MODULES = (
    "src.execution.broker_parts.trade_stream_bounds",
    "src.execution.broker_parts.trade_stream_auth",
    "src.execution.broker_parts.trade_stream_reconnect",
)


def test_split_modules_pass_the_boundary_check():
    for module in _MODULES:
        verdict = check_boundary(module)
        # Clause 1 can only name Exception subclasses (inherited __init__) and
        # the budget-less value classes; those moved verbatim.
        assert set(verdict.failures) <= {1}, (module, verdict.failures)
        if verdict.failures:
            assert verdict.failures[1] == ["TradeStreamGaveUp: no __init__"], verdict.failures


def test_moved_names_are_the_same_objects_on_trade_stream():
    from src.execution.broker_parts import (
        trade_stream,
        trade_stream_auth,
        trade_stream_reconnect,
    )
    from src.execution.broker_parts import trade_stream_bounds

    assert trade_stream.TradeStreamAuthRejected is trade_stream_auth.TradeStreamAuthRejected
    assert trade_stream.TradeStreamGaveUp is trade_stream_reconnect.TradeStreamGaveUp
    assert trade_stream._STREAM_ATTEMPT_BUDGET is trade_stream_reconnect._STREAM_ATTEMPT_BUDGET
    assert trade_stream._ALPACA_STREAM_AUTH_DEADLINE_S == trade_stream_bounds._ALPACA_STREAM_AUTH_DEADLINE_S
