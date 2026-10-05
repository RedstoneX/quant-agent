"""The knobs one backtest run takes, and the per-run read meter they carry.

Split out of `src/backtest/engine.py` so the engine file does not grow."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from src.backtest.swept_values import SweepMeter

#: Trading days of history a symbol needs before this engine will evaluate it
#: for a signal. 210 = 200 (MA200) + 10 (the slope lookback `compute_market_context`
#: needs to say whether that average is rising or falling) — below this, both
#: `find_structural_levels` and `compute_market_context` are working with a
#: materially incomplete picture, and the live system would be too.
MIN_BARS_FOR_SIGNAL = 210

DEFAULT_MAX_HOLD_DAYS = 20
DEFAULT_INITIAL_EQUITY = 100_000.0
DEFAULT_SLIPPAGE_BPS = 5.0


@dataclass(frozen=True)
class BacktestParams:
    """Engine-only knobs. None of these has a live-system counterpart to
    reuse: the live horizon comes from the Tech Analyst's own
    `expected_horizon_sessions` estimate (an LLM output this engine cannot
    reproduce), and there is no dedicated backtest slippage field in
    `Settings` — see `scripts/backtest.py` for how the default is chosen."""

    start: date
    end: date
    max_hold_days: int = DEFAULT_MAX_HOLD_DAYS
    initial_equity: float = DEFAULT_INITIAL_EQUITY
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS
    min_bars_for_signal: int = MIN_BARS_FOR_SIGNAL
    #: This run's swept-value overrides and read counts. Built with the
    #: params, so it is born and discarded with the run it describes.
    meter: SweepMeter = field(default_factory=SweepMeter, compare=False, repr=False)
