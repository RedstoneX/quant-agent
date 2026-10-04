"""Item 55 follow-up: re-run the level sweep against a FAIR control.

The first sweep (`item55_level_sweep.py`) compared every level definition to a
plain returns-shuffle. A plain shuffle destroys volatility clustering -- the
real tendency of calm and violent days to arrive in runs -- so the control
series has unrealistically even volatility, which changes how often price
revisits any zone at all. Revisit frequency is exactly what the test measures,
so that control flatters itself and the finding could only be stated as "no
measurable effect".

This module changes ONLY the control. Everything else -- the panel, the pivot
definition, the clustering, the 60/40 out-of-sample split, the bounce counter,
the confidence interval -- is imported unchanged from the original sweep.

The fair control is a sign-randomised surrogate: write each log return as
drift + deviation, keep the deviation's MAGNITUDE at its own index, and flip
only its sign. The series |d_1|, |d_2|, ... is therefore identical bar for bar
to the real one, so volatility clustering is preserved exactly, as is the
drift and the intrabar high/low shape. Only the sign sequence -- the thing
that builds a price path and puts structure at particular prices -- is
destroyed. Nothing is tuned and no block length, window or cut-off is picked.

Many replications are run because one draw of a random control is not a
control; the spread across draws is reported alongside the mean.

No network, no broker, no desk record. Run:
    .venv/bin/python ops/research/item55_volclustered_control.py
"""
from __future__ import annotations

import math
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from item55_level_sweep import (  # noqa: E402  (path shim above)
    MIN_TOUCHES,
    SPLIT,
    TOLERANCES,
    WINDOWS,
    bounce_events,
    cluster_overlap,
    cluster_pct,
    load_panel,
    pivots,
    rate_ci,
)

REPLICATIONS = 20  # independent control draws; a sample size, not a threshold.


def signflip_series(bars: list[dict], seed: int) -> list[dict]:
    """Volatility clustering preserved exactly; price structure destroyed.

    r_t = mu + d_t  ->  r'_t = mu + s_t * d_t  with s_t in {-1, +1}.
    |d_t| keeps its own index, so the realised-volatility path, the drift and
    the intrabar range shape all survive untouched.
    """
    rng = random.Random(seed)
    rets: list[float] = []
    shape: list[tuple[float, float]] = []
    for i in range(1, len(bars)):
        p, c = bars[i - 1]["close"], bars[i]["close"]
        if p <= 0 or c <= 0:
            return []
        rets.append(math.log(c / p))
        shape.append((bars[i]["high"] / c, bars[i]["low"] / c))
    if not rets:
        return []
    mu = sum(rets) / len(rets)
    out = [dict(bars[0])]
    price = bars[0]["close"]
    for i, r in enumerate(rets):
        dev = r - mu
        if rng.random() < 0.5:
            dev = -dev
        price *= math.exp(mu + dev)
        hr, lr = shape[i]
        out.append({"close": price, "high": price * hr, "low": price * lr})
    return out


def realised_vol_autocorr(bars: list[dict]) -> float:
    """Lag-1 autocorrelation of |log return| -- the clustering the control must keep."""
    a = []
    for i in range(1, len(bars)):
        p, c = bars[i - 1]["close"], bars[i]["close"]
        if p <= 0 or c <= 0:
            return float("nan")
        a.append(abs(math.log(c / p)))
    if len(a) < 3:
        return float("nan")
    m = sum(a) / len(a)
    num = sum((a[i] - m) * (a[i - 1] - m) for i in range(1, len(a)))
    den = sum((x - m) ** 2 for x in a)
    return num / den if den else float("nan")


def levels_for(pvs, tol, cut):
    pvs = [p for p in pvs if p[0] < cut]
    return cluster_overlap(pvs) if tol == "overlap" else cluster_pct(pvs, tol)


def hold_rate(series_bars, window, tol, piv_cache=None):
    """Aggregate hold rate across the panel for one (window, zone) setting."""
    h_tot = b_tot = n_levels = 0
    for sym, bars in series_bars.items():
        cut = int(len(bars) * SPLIT)
        pvs = piv_cache[sym] if piv_cache is not None else pivots(bars, window)
        lv = levels_for(pvs, tol, cut)
        h, b = bounce_events(bars[cut:], lv)
        h_tot += h
        b_tot += b
        n_levels += len(lv)
    return h_tot, b_tot, n_levels


def run(replications: int = REPLICATIONS, panel=None) -> list[dict]:
    panel = panel if panel is not None else load_panel()
    real: dict[str, list[dict]] = {}
    for sym, bars in panel.items():
        bars = [b for b in bars if b.get("close") and b.get("high") and b.get("low")]
        if len(bars) < 200:
            continue
        real[sym] = bars

    # Evidence the control is fair on the quantity the old one broke.
    ra = statistics.median(
        v for v in (realised_vol_autocorr(b) for b in real.values()) if v == v
    )
    one = {s: signflip_series(b, seed=1000 + i) for i, (s, b) in enumerate(real.items())}
    ca = statistics.median(
        v for v in (realised_vol_autocorr(b) for b in one.values()) if v == v
    )
    print(f"symbols={len(real)} split={SPLIT} min_touches={MIN_TOUCHES} "
          f"replications={replications}")
    print(f"median lag-1 autocorr of |return|: real={ra:+.4f} sign-flip control={ca:+.4f}")
    print(f"{'window':>6} {'zone':>9} {'real':>7} {'ctrl_mu':>8} {'ctrl_sd':>8} "
          f"{'ctrl_min':>8} {'ctrl_max':>8} {'edge':>8} {'+/-95%':>7} {'n_real':>8}")

    rows: list[dict] = []
    for w in WINDOWS:
        real_piv = {s: pivots(b, w) for s, b in real.items()}
        ctrl_sets = []
        for rep in range(replications):
            cs = {s: signflip_series(b, seed=(rep + 1) * 7919 + i)
                  for i, (s, b) in enumerate(real.items())}
            ctrl_sets.append((cs, {s: pivots(b, w) for s, b in cs.items()}))
        for tol in list(TOLERANCES) + ["overlap"]:
            rh, rb, nlv = hold_rate(real, w, tol, real_piv)
            pr, pe = rate_ci(rh, rb)
            reps = []
            ch_tot = cb_tot = 0
            for cs, cp in ctrl_sets:
                h, b, _ = hold_rate(cs, w, tol, cp)
                ch_tot += h
                cb_tot += b
                if h + b:
                    reps.append(h / (h + b))
            cmu = statistics.mean(reps) if reps else float("nan")
            csd = statistics.pstdev(reps) if len(reps) > 1 else 0.0
            _, ce = rate_ci(ch_tot, cb_tot)
            edge = pr - cmu
            err = math.sqrt(pe ** 2 + ce ** 2 + csd ** 2)
            label = "overlap" if tol == "overlap" else f"{tol:.1f}%"
            print(f"{w:>6} {label:>9} {pr:7.4f} {cmu:8.4f} {csd:8.4f} "
                  f"{min(reps):8.4f} {max(reps):8.4f} {edge:+8.4f} {err:7.4f} "
                  f"{rh + rb:>8}")
            rows.append({"window": w, "zone": label, "real": pr, "ctrl_mu": cmu,
                         "ctrl_sd": csd, "edge": edge, "err": err,
                         "n_real": rh + rb, "levels": nlv})
    sig = [r for r in rows if r["edge"] - r["err"] > 0]
    print()
    if not sig:
        print("NO setting beats the volatility-clustered control at 95%. The "
              "original finding SURVIVES a fair control: no measurable edge.")
    else:
        best = max(sig, key=lambda r: r["edge"])
        print(f"settings clearing the fair control at 95%: {len(sig)}/{len(rows)}; "
              f"largest edge window={best['window']} zone={best['zone']} "
              f"edge={best['edge']:+.4f}")
    return rows


if __name__ == "__main__":
    run()
