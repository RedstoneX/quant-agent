"""Does the noise band's sqrt(sessions_held) widening match the tape?

`src/risk/exit_guard.py::noise_band_atr` widens the adverse-move noise band as
`ATR * sqrt(sessions_held)`, with no cap: a position held 60 sessions must move
~7.8 ATR against entry before a discretionary sale is allowed. The sqrt shape
was adopted by analogy with random-walk dispersion, never measured, and the cap
was never derived because picking one is barred.

This measures the claim the band actually makes: that an adverse excursion from
entry, in ATRs, marks a finished trend -- and that the excursion size which
marks it GROWS with how long the position has been held.

Method (no fitting to the desk's record; only its recorded price inputs):
  * panel: the committed public daily-bar fixture (101 symbols, ~5y).
  * for each symbol, holding length h and entry bar e: adverse = (close[e] -
    close[e+h]) / ATR14[e], kept only when positive (a losing long).
  * outcome: forward return over the next F sessions from close[e+h], MINUS
    that symbol's own in-sample mean F-session return, so the equity drift
    common to every bucket cannot decide the answer.
  * discovery on the first 60% of each series: split adverse into deciles and
    take the threshold x*(h) to be the left edge of the LOWEST decile whose
    mean excess forward return is negative -- i.e. the smallest adverse
    excursion that
    actually marks a finished trend. Nothing is picked: the edge is read off
    the data's own distribution.
  * judgement on the held-out 40%: does x*(h) still separate forward returns?
  * control: the same pipeline on a returns-shuffled series per symbol, which
    keeps the return distribution and destroys the structure.

Had the sqrt widening been right, x*(h)/x*(1) would rise with h and track
sqrt(h) (1.0, 1.41, 2.24, 3.16, 4.47, 7.75 at h = 1, 2, 5, 10, 20, 60), and the
out-of-sample separation would hold at every h. A flat x*(h) says the widening
is unsupported; a rising-then-flat x*(h) would locate an honest cap.

No network, no broker, no desk record. Run:
    .venv/bin/python ops/research/noise_band_holding_scaling.py
"""
from __future__ import annotations

import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from item55_level_sweep import load_panel, shuffle_series  # noqa: E402

SPLIT = 0.60
ATR_WINDOW = 14          # the desk's own ATR convention, not swept here
HOLDS = (1, 2, 5, 10, 20, 40, 60)
FORWARDS = (10, 20, 60)
DECILES = 10


def atr_series(bars: list[dict]) -> list[float]:
    trs: list[float] = []
    for i, b in enumerate(bars):
        prev = bars[i - 1]["close"] if i else b["close"]
        trs.append(max(b["high"] - b["low"], abs(b["high"] - prev), abs(b["low"] - prev)))
    out = [0.0] * len(bars)
    for i in range(len(bars)):
        if i + 1 >= ATR_WINDOW:
            out[i] = sum(trs[i + 1 - ATR_WINDOW:i + 1]) / ATR_WINDOW
    return out


def baseline(bars, fwd: int, split: int) -> float:
    """The symbol's own unconditional in-sample F-session return."""
    vals = [(bars[i + fwd]["close"] - bars[i]["close"]) / bars[i]["close"]
            for i in range(0, split - fwd) if bars[i]["close"] > 0]
    return statistics.fmean(vals) if vals else 0.0


def observations(panel, hold: int, fwd: int, shuffled: bool):
    """(adverse_in_atr, excess_forward_return, in_sample) over every symbol."""
    rows = []
    for si, (sym, bars) in enumerate(sorted(panel.items())):
        if shuffled:
            bars = shuffle_series(bars, seed=si)
        n = len(bars)
        if n < 200:
            continue
        atr = atr_series(bars)
        split = int(n * SPLIT)
        base = baseline(bars, fwd, split)
        for e in range(ATR_WINDOW, n - hold - fwd):
            a = atr[e]
            if a <= 0:
                continue
            exitp = bars[e + hold]["close"]
            adverse = (bars[e]["close"] - exitp) / a
            if adverse <= 0 or exitp <= 0:
                continue
            fr = (bars[e + hold + fwd]["close"] - exitp) / exitp - base
            rows.append((adverse, fr, e + hold + fwd < split))
    return rows


def threshold(rows) -> tuple[float | None, int]:
    ins = sorted((r for r in rows if r[2]), key=lambda r: r[0])
    if len(ins) < DECILES * 30:
        return None, len(ins)
    step = len(ins) // DECILES
    for d in range(DECILES):
        chunk = ins[d * step:(d + 1) * step if d < DECILES - 1 else len(ins)]
        if statistics.fmean(x[1] for x in chunk) < 0:
            return chunk[0][0], len(ins)
    return None, len(ins)


def decile_table(rows):
    ins = sorted((r for r in rows if r[2]), key=lambda r: r[0])
    step = len(ins) // DECILES
    out = []
    for d in range(DECILES):
        c = ins[d * step:(d + 1) * step if d < DECILES - 1 else len(ins)]
        out.append((c[0][0], statistics.fmean(x[1] for x in c)))
    return out


def judge(rows, thr: float):
    above = [r[1] for r in rows if not r[2] and r[0] >= thr]
    below = [r[1] for r in rows if not r[2] and r[0] < thr]
    if len(above) < 30 or len(below) < 30:
        return None
    return statistics.fmean(above), len(above), statistics.fmean(below), len(below)


def main() -> None:
    panel = load_panel()
    print(f"panel: {len(panel)} symbols\n")
    print("decile table, forward 20 sessions (left edge in ATR / mean excess fwd return)")
    for shuffled in (False, True):
        print("REAL" if not shuffled else "CONTROL(shuffled)")
        for h in HOLDS:
            t = decile_table(observations(panel, h, 20, shuffled))
            print(f"  h={h:<3} " + "  ".join(f"{e:.2f}:{m*100:+.2f}%" for e, m in t))
    print()
    for fwd in FORWARDS:
        for shuffled in (False, True):
            tag = "CONTROL(shuffled)" if shuffled else "REAL"
            print(f"=== forward {fwd} sessions, {tag} ===")
            print(f"{'hold':>5} {'x*(h) ATR':>10} {'x*/x*(1)':>9} {'sqrt(h)':>8} "
                  f"{'n_in':>8} {'oos>=x*':>9} {'oos<x*':>9} {'n_oos>=':>8}")
            base = None
            for h in HOLDS:
                rows = observations(panel, h, fwd, shuffled)
                thr, nin = threshold(rows)
                if thr is None:
                    print(f"{h:>5} {'none':>10} {'-':>9} {math.sqrt(h):>8.2f} {nin:>8} "
                          f"{'-':>9} {'-':>9} {'-':>8}")
                    continue
                if base is None:
                    base = thr
                j = judge(rows, thr)
                ratio = thr / base if base else float("nan")
                if j is None:
                    print(f"{h:>5} {thr:>10.2f} {ratio:>9.2f} {math.sqrt(h):>8.2f} {nin:>8} "
                          f"{'thin':>9} {'thin':>9} {'-':>8}")
                else:
                    print(f"{h:>5} {thr:>10.2f} {ratio:>9.2f} {math.sqrt(h):>8.2f} {nin:>8} "
                          f"{j[0]:>9.4f} {j[2]:>9.4f} {j[1]:>8}")
            print()


if __name__ == "__main__":
    main()
