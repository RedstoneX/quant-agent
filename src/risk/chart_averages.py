"""The averages the alignment exit reads off the chart, and the finite guard
they share — lifted VERBATIM from `src.risk.alignment_exit` so that module
stays under its size ratchet. A leaf: imports nothing from execution.
"""

from __future__ import annotations

import math

__all__ = [
    "exponential_moving_average",
    "moving_average",
    "simple_moving_average",
]


def _finite(x: float | None) -> float | None:
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def simple_moving_average(closes: list[float], period: int) -> float | None:
    """Arithmetic mean of the last `period` closes, or None when short."""
    if period <= 0:
        return None
    vals = [v for v in (_finite(c) for c in closes) if v is not None]
    if len(vals) < period:
        return None
    return sum(vals[-period:]) / float(period)


def exponential_moving_average(closes: list[float], period: int) -> float | None:
    """Standard EMA of the last closes: alpha = 2/(period+1), seeded with the
    simple mean of the first `period` values.

    Exists because the thesis regex accepts "EMA50" and an EMA is NOT an
    SMA. An earlier draft judged a thesis that named an EMA against an SMA
    of the same period — a price the thesis never named, and in a trend a
    materially different one. The desk either computes what the thesis
    said or admits it cannot; it does not substitute.
    """
    if period <= 0:
        return None
    vals = [v for v in (_finite(c) for c in closes) if v is not None]
    if len(vals) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    ema = sum(vals[:period]) / float(period)
    for v in vals[period:]:
        ema = alpha * v + (1.0 - alpha) * ema
    return ema


def moving_average(closes: list[float], period: int, kind: str) -> float | None:
    """The average of the KIND the caller names: "EMA" or "SMA"."""
    if (kind or "").upper() == "EMA":
        return exponential_moving_average(closes, period)
    return simple_moving_average(closes, period)
