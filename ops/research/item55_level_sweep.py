"""Item 55: measure what defines a structural level, on real bars.

Reads the committed five-year public daily-bar panel (101 symbols, 1236 bars,
`ops/model_policy/fixtures/yf_daily_bars_pm_public_day_2026-09-14.json.gz`) and
asks the Tsinaslanidis 4.5 question directly: when price enters a candidate
level's zone, how often does it leave the way it came? Sweeps the two
underived numbers -- the pivot window and the cluster tolerance -- plus the
threshold-free bar-overlap variant, each against a returns-shuffled control
built from the same symbol. Levels are discovered on the first 60% of each
series and every event is counted out of sample, so nothing is fitted.

No network, no broker, no desk record. Run:
    .venv/bin/python ops/research/item55_level_sweep.py
"""
from __future__ import annotations

import gzip
import json
import math
import random
from pathlib import Path

PANEL = Path(__file__).resolve().parents[2] / (
    "ops/model_policy/fixtures/yf_daily_bars_pm_public_day_2026-09-14.json.gz"
)
MIN_TOUCHES = 2  # settled and sourced (Tsinaslanidis 2012); not swept here.
SPLIT = 0.60     # discovery / out-of-sample boundary
WINDOWS = (3, 5, 10, 25)
TOLERANCES = (0.5, 1.0, 2.0, 3.0, 5.0)


def load_panel() -> dict[str, list[dict]]:
    return json.loads(gzip.open(PANEL).read())


def shuffle_series(bars: list[dict], seed: int) -> list[dict]:
    """Same return distribution, no structure: the control."""
    rng = random.Random(seed)
    rets, shape = [], []
    for i in range(1, len(bars)):
        p, c = bars[i - 1]["close"], bars[i]["close"]
        if p <= 0 or c <= 0:
            return []
        rets.append(math.log(c / p))
        shape.append((bars[i]["high"] / c, bars[i]["low"] / c))
    order = list(range(len(rets)))
    rng.shuffle(order)
    out = [dict(bars[0])]
    price = bars[0]["close"]
    for j in order:
        price *= math.exp(rets[j])
        hr, lr = shape[j]
        out.append({"close": price, "high": price * hr, "low": price * lr})
    return out


def pivots(bars: list[dict], window: int) -> list[tuple[int, float, float, float]]:
    """(index, pivot price, bar low, bar high) for confirmed swing highs/lows."""
    found = []
    for i in range(window, len(bars) - window):
        lo, hi = i - window, i + window + 1
        h = [b["high"] for b in bars[lo:hi]]
        l = [b["low"] for b in bars[lo:hi]]
        if bars[i]["high"] >= max(h):
            found.append((i, bars[i]["high"], bars[i]["low"], bars[i]["high"]))
        elif bars[i]["low"] <= min(l):
            found.append((i, bars[i]["low"], bars[i]["low"], bars[i]["high"]))
    return found


def cluster_pct(pvs, tol_pct: float):
    """Greedy first-fit over price-sorted pivots -- the desk's own partition."""
    out = []
    for p in sorted(pvs, key=lambda x: x[1]):
        if out and abs(p[1] - out[-1][0][1]) <= abs(out[-1][0][1]) * tol_pct / 100.0:
            out[-1].append(p)
        else:
            out.append([p])
    levels = []
    for grp in out:
        if len(grp) < MIN_TOUCHES:
            continue
        price = sum(p[1] for p in grp) / len(grp)
        half = abs(price) * tol_pct / 100.0
        levels.append((price - half, price + half, len(grp)))
    return levels


def cluster_overlap(pvs):
    """Threshold-free: pivots join when their bars' ranges overlap every
    member's; the zone is the union of those bar ranges. Invents nothing."""
    out = []
    for p in sorted(pvs, key=lambda x: x[1]):
        placed = False
        for grp in out:
            if all(p[2] <= m[3] and m[2] <= p[3] for m in grp):
                grp.append(p)
                placed = True
                break
        if not placed:
            out.append([p])
    levels = []
    for grp in out:
        if len(grp) < MIN_TOUCHES:
            continue
        levels.append((min(p[2] for p in grp), max(p[3] for p in grp), len(grp)))
    return levels


def bounce_events(test: list[dict], levels) -> tuple[int, int]:
    """Price enters the zone; does the first close back outside it land on the
    side it came from (hold) or through (break)? No cutoff is chosen."""
    holds = breaks = 0
    for lo, hi, _touches in levels:
        side = None
        prev = None
        for b in test:
            inside = b["low"] <= hi and lo <= b["high"]
            if side is None:
                # An entry only counts when price came from clearly outside,
                # so the side it must return to is never ambiguous.
                if inside and prev is not None:
                    side = "above" if prev > hi else ("below" if prev < lo else None)
                prev = b["close"]
                continue
            prev = b["close"]
            if b["close"] > hi:
                exited = "above"
            elif b["close"] < lo:
                exited = "below"
            else:
                continue
            if exited == side:
                holds += 1
            else:
                breaks += 1
            side = None
    return holds, breaks


def rate_ci(h: int, b: int) -> tuple[float, float]:
    n = h + b
    if n == 0:
        return float("nan"), float("nan")
    p = h / n
    return p, 1.96 * math.sqrt(max(p * (1 - p), 1e-12) / n)


def run() -> None:
    panel = load_panel()
    series = {}
    for sym, bars in panel.items():
        bars = [b for b in bars if b.get("close") and b.get("high") and b.get("low")]
        if len(bars) < 200:
            continue
        series[sym] = (bars, shuffle_series(bars, seed=hash(sym) & 0xFFFF))
    print(f"symbols={len(series)} bars/symbol={len(next(iter(series.values()))[0])} "
          f"split={SPLIT} min_touches={MIN_TOUCHES}")
    print(f"{'window':>6} {'zone':>9} {'real':>7} {'ctrl':>7} {'edge':>8} "
          f"{'+/-95%':>7} {'n_real':>7} {'levels':>7}")
    rows = []
    for w in WINDOWS:
        piv = {s: (pivots(r, w), pivots(c, w)) for s, (r, c) in series.items()}
        for tol in list(TOLERANCES) + ["overlap"]:
            agg = [0, 0, 0, 0, 0]
            for sym, (real, ctrl) in series.items():
                cut = int(len(real) * SPLIT)
                for which, bars in ((0, real), (1, ctrl)):
                    pvs = [p for p in piv[sym][which] if p[0] < cut]
                    lv = cluster_overlap(pvs) if tol == "overlap" else cluster_pct(pvs, tol)
                    h, b = bounce_events(bars[cut:], lv)
                    agg[which * 2] += h
                    agg[which * 2 + 1] += b
                    if which == 0:
                        agg[4] += len(lv)
            pr, pe = rate_ci(agg[0], agg[1])
            cr, ce = rate_ci(agg[2], agg[3])
            edge = pr - cr
            err = math.sqrt(pe ** 2 + ce ** 2)
            label = "overlap" if tol == "overlap" else f"{tol:.1f}%"
            print(f"{w:>6} {label:>9} {pr:7.4f} {cr:7.4f} {edge:+8.4f} "
                  f"{err:7.4f} {agg[0]+agg[1]:>7} {agg[4]:>7}")
            rows.append((w, label, pr, cr, edge, err, agg[0] + agg[1]))
    sig = [r for r in rows if r[4] - r[5] > 0]
    print()
    if not sig:
        print("NO setting beats its shuffled control at 95%. Neither the pivot "
              "window nor the zone width is derivable from this panel.")
    else:
        best = max(sig, key=lambda r: r[4])
        print(f"settings clearing control at 95%: {len(sig)}/{len(rows)}; "
              f"largest edge window={best[0]} zone={best[1]} edge={best[4]:+.4f}")


if __name__ == "__main__":
    run()
