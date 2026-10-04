"""Tripwire: the no-ATR stop branch is only reachable on under 15 bars.

`ConstructorConfig.structural_stop_buffer_pct` is consumed only by the branch
entered when the analysis carries no ATR(14). Over every 40-bar window of the
committed daily-bar fixture the live indicator code yields an ATR, so that
branch is NEVER EXERCISED there. If this fails, the branch became reachable
and the ledger note for the buffer must be revisited.
"""
import gzip
import json
from pathlib import Path

from src.data.technical import compute_indicators
from src.models.analysis import OHLCV

_FIX = Path(__file__).resolve().parents[1] / (
    "ops/model_policy/fixtures/yf_daily_bars_2026-08-28.json.gz")


def _bars():
    with gzip.open(_FIX, "rt") as fh:
        raw = json.load(fh)
    return {s: [OHLCV(**r) for r in rows] for s, rows in raw.items()}


def test_atr_present_on_every_real_window():
    windows = missing = 0
    for sym, bars in _bars().items():
        for end in range(40, len(bars), 7):
            windows += 1
            if compute_indicators(sym, bars[end - 40:end]).atr_14 is None:
                missing += 1
    assert windows > 1000
    assert missing == 0


def test_atr_absent_only_below_fifteen_bars():
    sym, bars = next(iter(_bars().items()))
    assert compute_indicators(sym, bars[:14]).atr_14 is None
    assert compute_indicators(sym, bars[:40]).atr_14 is not None
