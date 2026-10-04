"""Plain records the backtest engine carries: an open position and a closed trade, plus the slippage fill rule.

Moved out of ``engine`` unchanged so that module can stop growing; ``engine``
re-exports the names.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass
class _OpenPosition:
    symbol: str
    direction: str  # "long" | "short"
    signal_date: date
    entry_date: date
    entry_index: int
    entry_price: float  # fill price (slippage-adjusted)
    stop_initial: float
    stop: float  # current (possibly trailed) stop
    target: float | None
    setup_type: str
    shares: float
    risk_pct: float


@dataclass(frozen=True)
class Trade:
    """One closed round-trip. Every price here is a FILL price (slippage
    applied) except `stop_price` / `target_price`, which are the structural
    price LEVELS the trade was managed against."""

    symbol: str
    direction: str  # "long" | "short"
    signal_date: date
    entry_date: date
    entry_price: float
    stop_price: float
    target_price: float | None
    exit_date: date
    exit_price: float
    exit_reason: str  # "stop" | "target" | "horizon" | "end_of_data"
    shares: float
    risk_pct: float
    setup_type: str
    hold_days: int
    pnl: float
    r_multiple: float


def _fill_price(raw_price: float, direction: str, side: str, slippage_bps: float) -> float:
    """Apply a flat slippage assumption in the ADVERSE direction only.

    `side` is "open" (establishing the position) or "close" (exiting it).
    Buying always costs slippage; selling always gives it up. A long open
    and a short close are both buys; a long close and a short open are both
    sells.
    """
    frac = slippage_bps / 10_000.0
    buying = (direction == "long" and side == "open") or (direction == "short" and side == "close")
    return raw_price * (1 + frac) if buying else raw_price * (1 - frac)
