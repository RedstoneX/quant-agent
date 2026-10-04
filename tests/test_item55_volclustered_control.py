"""Item 55 follow-up: the fair control must keep volatility clustering, and the
measurement must be able to return a different answer when levels are real."""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ops" / "research"))

from item55_level_sweep import shuffle_series  # noqa: E402
from item55_volclustered_control import (  # noqa: E402
    realised_vol_autocorr,
    run,
    signflip_series,
)


def _clustered_bars(n: int = 900, seed: int = 11) -> list[dict]:
    """A GARCH-ish path: volatility itself persists, so |return| autocorrelates."""
    rng = random.Random(seed)
    bars, price, vol = [], 100.0, 0.01
    for _ in range(n):
        # Long-lived volatility regimes: calm and violent days arrive in runs.
        vol = 0.98 * vol + 0.02 * abs(rng.gauss(0, 0.05))
        r = rng.gauss(0, vol)
        price *= math.exp(r)
        bars.append({"close": price, "high": price * 1.004, "low": price * 0.996})
    return bars


def test_signflip_keeps_clustering_that_the_plain_shuffle_destroys():
    """Measured on the committed real panel, not on a synthetic stand-in."""
    from item55_volclustered_control import load_panel
    panel = load_panel()
    bars = max(
        (b for b in panel.values() if len(b) > 800),
        key=lambda b: realised_vol_autocorr(b),
    )
    real = realised_vol_autocorr(bars)
    fair = realised_vol_autocorr(signflip_series(bars, seed=3))
    plain = realised_vol_autocorr(shuffle_series(bars, seed=3))
    assert real > 0.15, real  # genuine clustering present in the real bars
    # The fair control reproduces the real clustering essentially exactly.
    assert abs(fair - real) < 0.02, (fair, real)
    # The control it replaces wipes it out.
    assert abs(plain) < real / 2, (plain, real)


def test_signflip_destroys_price_structure_but_keeps_drift_and_shape():
    bars = _clustered_bars()
    ctrl = signflip_series(bars, seed=5)
    assert len(ctrl) == len(bars)
    real_drift = math.log(bars[-1]["close"] / bars[0]["close"]) / (len(bars) - 1)
    # Sign flipping preserves the drift term in expectation, not per draw, so
    # the check is over many draws rather than one.
    drifts = [
        math.log(c[-1]["close"] / c[0]["close"]) / (len(c) - 1)
        for c in (signflip_series(bars, seed=s) for s in range(40))
    ]
    assert abs(sum(drifts) / len(drifts) - real_drift) < abs(real_drift) + 1e-4
    assert ctrl[1:] != bars[1:]
    for b in ctrl[1:]:
        assert b["high"] >= b["close"] >= b["low"] > 0


def _planted_panel(symbols: int = 12, n: int = 900) -> dict[str, list[dict]]:
    """Price really does turn at two fixed prices: a world where levels work."""
    panel = {}
    for s in range(symbols):
        rng = random.Random(900 + s)
        lo_b, hi_b = 90.0, 110.0
        price, bars = 100.0, []
        for _ in range(n):
            price *= math.exp(rng.gauss(0, 0.012))
            if price < lo_b:
                price = lo_b + (lo_b - price)
            elif price > hi_b:
                price = hi_b - (price - hi_b)
            bars.append({"close": price, "high": price * 1.003, "low": price * 0.997})
        panel[f"SYN{s}"] = bars
    return panel


def test_a_different_answer_is_possible_when_levels_are_real(capsys):
    rows = run(replications=3, panel=_planted_panel())
    capsys.readouterr()
    best = max(rows, key=lambda r: r["edge"])
    # On a panel where turns really happen at fixed prices, the same pipeline
    # and the same fair control report a large, significant positive edge.
    assert best["edge"] > 0.05, best
    assert best["edge"] - best["err"] > 0, best
