"""Position rules the backtest engine applies to an open position.

Moved out of ``engine`` unchanged so that module can stop growing: how much
risk budget an open position still consumes, and whether today's bar closes
it. Both read only the position's attributes; ``engine`` re-exports them.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from src.models import OHLCV

if TYPE_CHECKING:
    from src.backtest.engine import _OpenPosition


def _existing_risk_pct(pos: _OpenPosition, equity: float) -> float:
    """Risk % of equity an open position is currently consuming, based on
    its LIVE (possibly trailed) stop. Once a stop trails to or past entry,
    the position stops consuming budget — same behaviour `trailing.py`
    documents ("this position stops consuming risk budget")."""
    if pos.direction == "long":
        risk_per_share = max(0.0, pos.entry_price - pos.stop)
    else:
        risk_per_share = max(0.0, pos.stop - pos.entry_price)
    if equity <= 0:
        return 0.0
    return pos.shares * risk_per_share / equity * 100.0


def _check_exit(
    pos: _OpenPosition, bar: OHLCV, idx: int, max_hold_days: int,
) -> tuple[str | None, float | None]:
    """Whether today's bar closes `pos`, and at what raw (pre-slippage)
    price. Returns `(None, None)` when the position stays open.

    STOP-FIRST ORDERING: when a day's range could have touched both the
    stop and the target, the stop is assumed to have been hit first — see
    the module docstring ("EXIT ORDERING"). Factored out from the main loop
    so it is directly unit-testable for both directions without needing the
    signal-generation machinery around it.
    """
    if pos.direction == "long":
        if bar.low <= pos.stop:
            return "stop", pos.stop
        if pos.target is not None and bar.high >= pos.target:
            return "target", pos.target
    else:
        if bar.high >= pos.stop:
            return "stop", pos.stop
        if pos.target is not None and bar.low <= pos.target:
            return "target", pos.target

    hold_days = idx - pos.entry_index
    if hold_days >= max_hold_days:
        return "horizon", bar.close
    return None, None
