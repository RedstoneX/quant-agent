"""The readings, candidate shapes and two-year walk for the trend-alignment measurement."""
from __future__ import annotations

import statistics

import trend_alignment  # noqa: F401  (puts the repo root on sys.path)
from src.data.levels import MAX_HORIZON_SESSIONS  # noqa: E402
from src.data.technical import ATR_PERIOD, compute_indicators  # noqa: E402
from src.risk.exit_guard import BREAK_CONFIRMATION_ATR_MULTIPLE  # noqa: E402
from src.risk.trailing import (  # noqa: E402
    CHANDELIER_ATR_MULTIPLE,
    _structural_pivot,
    _swing_highs,
    _swing_lows,
)
from trend_alignment.data import MeasurementDataError  # noqa: E402,F401

#: The measurement's reference window. The desk's own horizon cap, not a pick.
REF = MAX_HORIZON_SESSIONS
#: The forward-change window. The desk's own volatility window, not a pick.
FWD = ATR_PERIOD

#: Warm-up bars before the first close that can be measured (MA50 + pivots).
WARMUP = 60



def readings(bars, i, run_start, is_short, ind_cache):
    """Every reading on close i, from bars[:i+1] only -- nothing from ahead."""
    if i not in ind_cache:
        ind_cache[i] = compute_indicators("X", bars[max(0, i - 260): i + 1])
    ind = ind_cache[i]
    atr = ind.atr_14
    close = bars[i].close
    if atr is None or atr <= 0:
        return None
    window = bars[: i + 1]
    if i - 1 not in ind_cache and i > 0:
        ind_cache[i - 1] = compute_indicators("X", bars[max(0, i - 261): i])
    prev_ma20 = ind_cache[i - 1].ma_20 if i > 0 else None
    if is_short:
        pivot = _structural_pivot(_swing_highs(window), is_short=True)
        s_raw = pivot is not None and close >= pivot + BREAK_CONFIRMATION_ATR_MULTIPLE * atr
        ext = min(b.low for b in bars[run_start: i + 1])
        v = close >= ext + CHANDELIER_ATR_MULTIPLE * atr
        m20 = ind.ma_20 is not None and close > ind.ma_20
        m50 = ind.ma_50 is not None and close > ind.ma_50
        u = (ind.ma_20 is not None and prev_ma20 is not None
             and close < ind.ma_20 and ind.ma_20 < prev_ma20)
    else:
        pivot = _structural_pivot(_swing_lows(window), is_short=False)
        s_raw = pivot is not None and close <= pivot - BREAK_CONFIRMATION_ATR_MULTIPLE * atr
        ext = max(b.high for b in bars[run_start: i + 1])
        v = close <= ext - CHANDELIER_ATR_MULTIPLE * atr
        m20 = ind.ma_20 is not None and close < ind.ma_20
        m50 = ind.ma_50 is not None and close < ind.ma_50
        u = (ind.ma_20 is not None and prev_ma20 is not None
             and close > ind.ma_20 and ind.ma_20 > prev_ma20)
    return dict(s_raw=s_raw, v=v, m20=m20, m50=m50, u=u, atr=atr, pivot=pivot)


SHAPES = {
    "ALL3": lambda r, s2: s2 and r["v"] and r["m20"],
    "FASTPAIR": lambda r, s2: s2 and r["v"],
    "EITHER+VETO": lambda r, s2: (s2 or r["v"]) and not r["u"],
    "S2+VETO": lambda r, s2: s2 and not r["u"],
    "V+VETO": lambda r, s2: r["v"] and not r["u"],
    "S1PAIR": lambda r, s2: r["s_raw"] and r["v"],
    "S1PAIR+VETO": lambda r, s2: r["s_raw"] and r["v"] and not r["u"],
    "CHAND": lambda r, s2: r["v"],
    "S2": lambda r, s2: s2,
}


def walk(bars, start, end, is_short):
    cache: dict = {}
    prev_raw = False
    for i in range(start, end + 1):
        r = readings(bars, i, start, is_short, cache)
        if r is None:
            prev_raw = False
            continue
        s2 = bool(r["s_raw"] and prev_raw)
        prev_raw = bool(r["s_raw"])
        yield i, r, s2


def measure_two_years(allbars):
    agg = {k: {"gb": [], "gbr": [], "false": 0, "fwd": [], "n": 0, "years": 0.0} for k in SHAPES}
    measured = 0
    for _sym, bars in sorted(allbars.items()):
        if len(bars) < WARMUP + 20:
            continue
        measured += 1
        closes = [b.close for b in bars]
        years = (bars[-1].date - bars[WARMUP].date).days / 365.25
        cache: dict = {}
        prev_raw = False
        run_start = WARMUP
        active = {k: False for k in SHAPES}
        for i in range(WARMUP, len(bars)):
            r = readings(bars, i, run_start, False, cache)
            if r is None:
                prev_raw = False
                continue
            s2 = bool(r["s_raw"] and prev_raw)
            prev_raw = bool(r["s_raw"])
            for k, f in SHAPES.items():
                on = bool(f(r, s2))
                if on and not active[k]:
                    hi = max(closes[max(WARMUP, i - REF): i + 1])
                    agg[k]["gb"].append((hi - closes[i]) / hi * 100)
                    run_hi = max(closes[run_start: i + 1])
                    agg[k]["gbr"].append((run_hi - closes[i]) / run_hi * 100)
                    later = closes[i + 1: i + 1 + REF]
                    if later and max(later) > hi:
                        agg[k]["false"] += 1
                    if i + FWD < len(closes):
                        agg[k]["fwd"].append((closes[i + FWD] - closes[i]) / closes[i] * 100)
                    agg[k]["n"] += 1
                active[k] = on
            if r["v"]:
                run_start = i
        for k in SHAPES:
            agg[k]["years"] += years
    return agg, measured


# --------------------------------------------------------------------------
# self-test: prove the guards fire, offline
