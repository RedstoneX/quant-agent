"""Sweep the protective-stop ATR multiple against committed daily price history.

WHY: `src.config.RiskConfig.min_stop_atr_multiple` (2.5) governs every stop the
desk places with no structural anchor. It is an unsourced number. This script
measures, over committed bars only (no network), how often a stop at distance D
is hit and then the price recovers (the stop cost money for nothing) versus hit
and the price keeps falling (the stop did its job).

CONTROL FLOW REPRODUCED (read from
`src.portfolio_constructor.entry_stop.resolver.EntryStopResolver`, 2026-10-04):

  * Both call sites that consume the multiple reach the SAME placement
    expression, `fallback_edge`:
      - site A: the PM/analyst typed no stop at all (`stop_loss is None`);
      - site B: a stop was typed, it sits INSIDE the band, and
        `_level_backing_stop` found no computed structural level behind it.
    `fallback_edge` = the WIDER of the ATR band (`entry - multiple * ATR` long)
    and the signal bar's far edge (the entry bar's low, long). So the two sites
    differ only in WHICH trades reach them, never in the price placed. Which
    trades reach which site depends on text the PM typed and is NOT derivable
    from price history: see the report note.
  * A stop that IS backed by a computed level is exempt from this multiple and
    is floored at `absolute_min_stop_atr_multiple` (1.0) instead. Those stops
    are out of scope here and are not measured.
  * `stop_atr_multiple` multiplies the base by a setup scaler and a regime
    scaler. With no setup and no regime it returns the base unchanged, so the
    sweep is over the EFFECTIVE multiple reaching `band_edge`, which is what the
    scalers also produce.
  * ATR is Wilder 14 (`src.data.technical.atr_series`, ATR_PERIOD), imported
    here rather than reimplemented so the measurement cannot drift from the
    desk's own reading.

SWEEP BOUNDS, each justified, none invented:
  * low end 0.5 = half of `absolute_min_stop_atr_multiple` (1.0), the tightest
    stop any code path in this repo will place.
  * high end 5.0 = above the top (3.5) of the published band quoted in
    `src.risk.alignment_exit` (Wilder 1978; Le Beau). 1.5 (the earlier MAE
    measurement) and 2.5 (the live value) both sit inside.
  * step 0.25: resolution, not a threshold.

HORIZONS: the repo pins NO default holding horizon --
`expected_horizon_sessions` is required per trade and has no fallback. So the
sweep is reported at several horizons rather than at one picked number.

Hermetic: reads only committed .json.gz fixtures. No network, no broker, no
writes outside stdout.
"""
from __future__ import annotations

import gzip
import json
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from src.data.technical import ATR_PERIOD, atr_series  # noqa: E402
from src.models.analysis import OHLCV  # noqa: E402

LONG_FIXTURE = REPO / "ops/model_policy/fixtures/yf_daily_bars_2026-08-28.json.gz"
REHEARSAL_FIXTURE = REPO / "ops/rehearsal/recordings/market_bars.json.gz"

MULTIPLES = [round(0.5 + 0.25 * i, 2) for i in range(19)]  # 0.50 .. 5.00
HORIZONS = (5, 10, 20, 40, 60)
CONTROL_SEED = 20261004


def _load() -> dict[str, list[dict]]:
    """Committed bars, long fixture preferred where both carry a symbol."""
    out: dict[str, list[dict]] = {}
    with gzip.open(REHEARSAL_FIXTURE) as fh:
        for sym, bars in json.load(fh)["bars"].items():
            out[sym] = bars
    with gzip.open(LONG_FIXTURE) as fh:
        for sym, bars in json.load(fh).items():
            out[sym] = bars  # longer history wins
    return out


def _atrs(bars: list[dict]) -> list[float | None]:
    rows = [
        OHLCV(
            date=b["date"], open=b["open"], high=b["high"],
            low=b["low"], close=b["close"], volume=int(b["volume"]),
        )
        for b in bars
    ]
    series = atr_series(rows, ATR_PERIOD)
    pad = len(bars) - len(series)
    return [None] * pad + [float(v) for v in series]


def _outcomes(bars, atrs, mult, horizon, bar_floor, rng=None):
    """Returns (entries, hits, regret, justified) for one multiple/horizon.

    regret   -- stop hit, and a later close inside the same horizon is back
                at or above the entry: the stop cost money for nothing.
    justified-- stop hit and price never regained entry inside the horizon.
    """
    entries = hits = regret = 0
    n = len(bars)
    for i in range(n - horizon):
        atr = atrs[i] if rng is None else rng.choice(atrs[ATR_PERIOD:])
        if not atr or atr <= 0:
            continue
        entry = bars[i]["close"]
        if entry <= 0:
            continue
        stop = entry - mult * atr
        if bar_floor:
            stop = min(stop, bars[i]["low"])  # signal-bar edge wins when lower
        if stop <= 0:
            continue
        entries += 1
        hit_at = None
        for t in range(i + 1, i + 1 + horizon):
            if bars[t]["low"] <= stop:
                hit_at = t
                break
        if hit_at is None:
            continue
        hits += 1
        if any(bars[t]["close"] >= entry for t in range(hit_at + 1, i + 1 + horizon)):
            regret += 1
    return entries, hits, regret, hits - regret


def main() -> None:
    data = _load()
    syms = sorted(data)
    first = min(b[0]["date"] for b in data.values())
    last = max(b[-1]["date"] for b in data.values())
    total_bars = sum(len(b) for b in data.values())
    print(f"sample: {len(syms)} symbols, {total_bars} daily bars, {first} .. {last}")
    print(f"symbols: {' '.join(syms)}")
    print("ATR = Wilder 14 from src.data.technical.atr_series (the desk's own)")
    print()
    atrs = {s: _atrs(b) for s, b in data.items()}

    for bar_floor in (False, True):
        label = ("band only (no signal-bar edge recorded)" if not bar_floor
                 else "band OR entry-bar low, whichever is wider (item 54)")
        print(f"=== placement: {label} ===")
        for horizon in HORIZONS:
            print(f"-- horizon {horizon} sessions --")
            print(f"{'D(ATR)':>7} {'entries':>8} {'hit%':>7} {'regret%ofhits':>13}"
                  f" {'regret/100':>10} {'saved/100':>9} {'ctrl regret%':>12}")
            for mult in MULTIPLES:
                e = h = r = j = 0
                ce = ch = cr = 0
                rng = random.Random(CONTROL_SEED + int(mult * 100) + horizon)
                for s in syms:
                    a, b_, c, d = _outcomes(data[s], atrs[s], mult, horizon, bar_floor)
                    e += a
                    h += b_
                    r += c
                    j += d
                    a2, b2, c2, _ = _outcomes(
                        data[s], atrs[s], mult, horizon, bar_floor, rng=rng,
                    )
                    ce += a2
                    ch += b2
                    cr += c2
                if not e:
                    continue
                print(f"{mult:7.2f} {e:8d} {100*h/e:7.2f} "
                      f"{(100*r/h if h else 0):13.2f} {100*r/e:10.2f} "
                      f"{100*j/e:9.2f} {(100*cr/ch if ch else 0):12.2f}")
            print()


if __name__ == "__main__":
    main()
