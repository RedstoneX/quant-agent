#!/usr/bin/env python3
"""Measure what each candidate 'the trend is over' shape would have cost.

Supports board item 75 / PR #789 (`src/risk/trend_alignment.py`). Nothing here
picks a number: every reading is an already-ratified desk quantity, and the two
measurement windows are the desk's own `src.data.levels.MAX_HORIZON_SESSIONS`
and `src.data.technical.ATR_PERIOD`.

WHY THIS FILE IS SHAPED THE WAY IT IS — READ BEFORE TRUSTING ANY OUTPUT
-----------------------------------------------------------------------
The first version of this measurement REPORTED SUCCESS HAVING MEASURED
NOTHING, and its figures were relayed to the owner as fact and then withdrawn.
The reproduced cause (yfinance 1.6.0, verified 2026-09-30):

    yf.download([...19 symbols...], period="2y")

returns a column MultiIndex whose OUTER level is the price field and whose
INNER level is the ticker -- ('Close', 'META'), not ('META', 'Close'). Indexing
that frame by ticker (`df[sym]`) therefore raises `KeyError`, and code that
swallowed the per-symbol failure produced an empty bar set for every name. The
run then printed a full results table of zeros with "symbols: 0" and only died
later, at a direct `allbars["META"]`. The network was fine, the symbols were
right, nothing was rate limited, and no cache was involved -- the frame was
merely read with the wrong key.

So the invariant this file enforces is: NO TABLE IS EVER PRINTED BEFORE THE
BAR SET HAS BEEN PROVED COMPLETE. Every fetch failure is fatal and non-zero.

  * `fetch_bars` pulls one ticker at a time (`yf.Ticker(sym).history`), which
    has no ticker-vs-field ambiguity at all.
  * If the bulk path is ever reintroduced, `split_bulk_frame` is the only
    supported way to slice it and it asserts the level order first.
  * `require_complete` raises `MeasurementDataError` if ANY requested symbol is
    missing or returns fewer than `MIN_BARS` bars. `main` turns that into
    `sys.exit(2)` with the reason, BEFORE any analysis runs.
  * A second `require_nonempty_result` guard fails the run if the analysis
    somehow produced no fires at all across every shape.
  * `--self-test` proves both guards fire, without touching the network.

Exit codes: 0 = a real measurement. 2 = the data was not there; the numbers
you did not see are not zeros, they do not exist.

USAGE
    python scripts/measure_trend_alignment.py --positions <positions.json>
    python scripts/measure_trend_alignment.py --positions p.json --cache-only
    python scripts/measure_trend_alignment.py --self-test
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from datetime import date

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from src.models import OHLCV  # noqa: E402
from src.data.levels import MAX_HORIZON_SESSIONS  # noqa: E402
from src.data.technical import ATR_PERIOD, compute_indicators  # noqa: E402
from src.risk.exit_guard import BREAK_CONFIRMATION_ATR_MULTIPLE  # noqa: E402
from src.risk.trailing import (  # noqa: E402
    CHANDELIER_ATR_MULTIPLE,
    _structural_pivot,
    _swing_highs,
    _swing_lows,
)

#: Two years of US sessions is ~500. Anything under this is a partial fetch and
#: is refused rather than measured. Not a tuning knob: it is the completeness
#: floor for the window the measurement claims to cover.
MIN_BARS = 400

#: The measurement's reference window. The desk's own horizon cap, not a pick.
REF = MAX_HORIZON_SESSIONS
#: The forward-change window. The desk's own volatility window, not a pick.
FWD = ATR_PERIOD

#: Warm-up bars before the first close that can be measured (MA50 + pivots).
WARMUP = 60

DEFAULT_CACHE = os.environ.get(
    "ALIGN_CACHE", "/home/ubuntu/qamc-measurements/alignment_bars.json"
)


class MeasurementDataError(RuntimeError):
    """The bars needed for a claim were not obtained. Never a zero row."""


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------
def frame_to_bars(df) -> list[OHLCV]:
    out: list[OHLCV] = []
    for d, r in df.iterrows():
        try:
            o, h, lo, c = (float(r[k]) for k in ("Open", "High", "Low", "Close"))
        except (KeyError, TypeError, ValueError):
            continue
        if any(math.isnan(x) for x in (o, h, lo, c)):
            continue
        vol = r.get("Volume", 0)
        out.append(
            OHLCV(
                date=d.date(),
                open=o,
                high=h,
                low=lo,
                close=c,
                volume=int(vol) if vol == vol and vol is not None else 0,
            )
        )
    return out


def split_bulk_frame(df, sym: str):
    """Slice one ticker out of a bulk `yf.download` frame, correctly.

    THIS is the function whose absence produced the empty measurement. In
    yfinance 1.6.0 the bulk frame's columns are (price field, ticker), so
    `df[sym]` raises KeyError. `xs(..., level=-1)` takes the ticker level
    whichever way round the library puts it, and we prove the ticker really
    is on that level before slicing rather than returning an empty frame.
    """
    levels = [set(map(str, df.columns.get_level_values(i))) for i in range(df.columns.nlevels)]
    ticker_levels = [i for i, vals in enumerate(levels) if sym in vals]
    if not ticker_levels:
        raise MeasurementDataError(
            f"{sym} is on no column level of the bulk frame; levels are {levels}. "
            "The frame was fetched but cannot be keyed by ticker -- this is the "
            "exact failure that produced a table of zeros."
        )
    return df.xs(sym, axis=1, level=ticker_levels[-1])


def fetch_bars(syms: list[str], attempts: int = 3) -> dict[str, list[OHLCV]]:
    """One ticker at a time. A short history is retried, then fatal."""
    import yfinance as yf

    out: dict[str, list[OHLCV]] = {}
    for sym in syms:
        bars: list[OHLCV] = []
        for attempt in range(attempts):
            try:
                df = yf.Ticker(sym).history(period="2y", auto_adjust=False)
                bars = frame_to_bars(df.dropna(how="all")) if df is not None and len(df) else []
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                print(f"fetch {sym} attempt {attempt + 1} raised: {exc}", file=sys.stderr)
                bars = []
            if len(bars) >= MIN_BARS:
                break
            if attempt + 1 < attempts:
                print(
                    f"fetch {sym} attempt {attempt + 1} returned {len(bars)} bars "
                    f"(< {MIN_BARS}); retrying",
                    file=sys.stderr,
                )
                time.sleep(2 + 3 * attempt)
        out[sym] = bars
    return out


def require_complete(allbars: dict[str, list[OHLCV]], syms: list[str]) -> None:
    """Fail loudly unless every requested symbol has a full history."""
    missing = [s for s in syms if s not in allbars]
    short = {s: len(allbars[s]) for s in syms if s in allbars and len(allbars[s]) < MIN_BARS}
    if missing or short:
        parts = []
        if missing:
            parts.append(f"{len(missing)} symbol(s) returned nothing at all: {sorted(missing)}")
        if short:
            parts.append(
                f"{len(short)} symbol(s) returned fewer than {MIN_BARS} bars: "
                + ", ".join(f"{s}={n}" for s, n in sorted(short.items()))
            )
        raise MeasurementDataError(
            "THE FETCH FAILED -- NO MEASUREMENT WAS MADE AND NO TABLE WILL BE PRINTED. "
            + "; ".join(parts)
            + ". Check the network and yfinance first; if a bulk download was used, "
            "confirm the column MultiIndex level order (see split_bulk_frame)."
        )


def require_nonempty_result(agg: dict, measured_symbols: int) -> None:
    if measured_symbols == 0 or all(a["n"] == 0 for a in agg.values()):
        raise MeasurementDataError(
            f"NO SYMBOL PRODUCED A SINGLE FIRE across {measured_symbols} symbol(s). "
            "An all-zero table is not a finding; it is a broken run."
        )


def load_or_fetch(syms: list[str], cache: str, cache_only: bool) -> dict[str, list[OHLCV]]:
    if cache_only:
        if not os.path.exists(cache):
            raise MeasurementDataError(f"--cache-only given but {cache} does not exist")
        with open(cache) as fh:
            raw = json.load(fh)
        allbars = {
            s: [OHLCV(**{**b, "date": date.fromisoformat(b["date"])}) for b in raw[s]]
            for s in syms
            if s in raw
        }
        print(f"bars read from cache {cache}")
    else:
        allbars = fetch_bars(syms)
        os.makedirs(os.path.dirname(cache) or ".", exist_ok=True)
        with open(cache, "w") as fh:
            json.dump(
                {
                    s: [
                        dict(date=str(b.date), open=b.open, high=b.high, low=b.low,
                             close=b.close, volume=b.volume)
                        for b in bs
                    ]
                    for s, bs in allbars.items()
                },
                fh,
            )
        print(f"bars fetched live and cached to {cache}")
    require_complete(allbars, syms)
    return allbars


# --------------------------------------------------------------------------
# the readings
# --------------------------------------------------------------------------
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
# --------------------------------------------------------------------------
def self_test() -> int:
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            fn()
        except MeasurementDataError as exc:
            print(f"PASS {name}: {str(exc)[:80]}...")
            return
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {name}: wrong exception {type(exc).__name__}: {exc}")
            ok = False
            return
        print(f"FAIL {name}: no MeasurementDataError raised")
        ok = False

    check("empty fetch is fatal", lambda: require_complete({}, ["META", "AAPL"]))
    check("short fetch is fatal", lambda: require_complete({"META": [None] * 3}, ["META"]))
    check(
        "all-zero table is fatal",
        lambda: require_nonempty_result({k: {"n": 0} for k in SHAPES}, 19),
    )
    check("no symbol measured is fatal", lambda: require_nonempty_result({"X": {"n": 5}}, 0))

    class _Cols:
        nlevels = 2

        def get_level_values(self, i):
            return ["Close", "Open"] if i == 0 else ["AAPL", "AAPL"]

    class _Frame:
        columns = _Cols()

    check("bulk frame missing the ticker is fatal", lambda: split_bulk_frame(_Frame(), "META"))

    try:
        require_complete({"META": [None] * MIN_BARS}, ["META"])
        print("PASS a complete fetch is accepted")
    except MeasurementDataError as exc:
        print(f"FAIL a complete fetch was rejected: {exc}")
        ok = False

    print("SELF-TEST", "OK" if ok else "FAILED")
    return 0 if ok else 1


# --------------------------------------------------------------------------
def run(positions: dict, cache: str, cache_only: bool) -> int:
    if not positions:
        raise MeasurementDataError(
            "the positions set is EMPTY -- there is nothing to measure. Pass "
            "--positions <file>. (An empty default silently reduced the symbol "
            "set to one name in an earlier run of this measurement.)"
        )
    syms = sorted(set(positions) | {"META"})
    allbars = load_or_fetch(syms, cache, cache_only)

    first = min(b[0].date for b in allbars.values())
    last = max(b[-1].date for b in allbars.values())
    print(
        f"PROOF OF DATA: {len(allbars)} symbols, {sum(len(b) for b in allbars.values())} "
        f"daily bars, {first} .. {last}; per symbol: "
        + ", ".join(f"{s}={len(b)}" for s, b in sorted(allbars.items())),
        flush=True,
    )

    print("\n=== (1) DESK POSITIONS: peak-to-fire giveback per shape ===")
    for sym, info in positions.items():
        bars = allbars.get(sym) or []
        entry_d = date.fromisoformat(info["entry"])
        is_short = info["side"] == "short"
        idx = [i for i, b in enumerate(bars) if b.date >= entry_d]
        if not idx:
            print(f"{sym:6s} no bar at or after {entry_d}; skipped")
            continue
        start, end = idx[0], len(bars) - 1
        if info.get("exit"):
            end = max(start, max(i for i, b in enumerate(bars)
                                 if b.date <= date.fromisoformat(info["exit"])))
        closes = [b.close for b in bars]
        fires = {k: None for k in SHAPES}
        peak_at = {k: None for k in SHAPES}
        peak = closes[start]
        for i, r, s2 in walk(bars, start, end, is_short):
            peak = min(peak, closes[i]) if is_short else max(peak, closes[i])
            for k, f in SHAPES.items():
                if fires[k] is None and f(r, s2):
                    fires[k] = i
                    peak_at[k] = peak
        sign = -1 if is_short else 1
        entry_px = info["entry_px"]
        final = closes[end]
        line = (f"{sym:6s} {info['side']:5s} entry {entry_d} @{entry_px:.2f} "
                f"sessions={end - start + 1} peak={peak:.2f} last={final:.2f} "
                f"({sign * (final - entry_px) / entry_px * 100:+.1f}% vs entry)")
        if info.get("exit"):
            line += (f" ACTUAL EXIT {info['exit']} {info.get('exit_kind', '')} "
                     f"@{info.get('exit_px', 0):.2f}")
        print(line)
        for k in SHAPES:
            i = fires[k]
            if i is None:
                print(f"     {k:12s} never fired")
            else:
                gb = sign * (peak_at[k] - closes[i]) / peak_at[k] * 100
                print(f"     {k:12s} fired {bars[i].date} @{closes[i]:.2f}  "
                      f"giveback from peak {gb:.1f}%  "
                      f"({sign * (closes[i] - entry_px) / entry_px * 100:+.1f}% vs entry)")

    print(
        f"\n=== (2) SAME INSTRUMENTS, 2 YEARS: giveback from the {REF}-session high; "
        f"false-exit = that high exceeded again within {REF} sessions; "
        f"fwd = close change {FWD} sessions after the fire ===",
        flush=True,
    )
    agg, measured = measure_two_years(allbars)
    require_nonempty_result(agg, measured)

    print(f"{'shape':12s} {'fires':>6s} {'/yr/sym':>8s} {'gb60 med':>10s} {'p75':>6s} "
          f"{'gbRUN med':>10s} {'false-exit':>11s} {'median fwd':>11s}")
    for k, a in agg.items():
        gb = sorted(a["gb"])
        if not gb:
            print(f"{k:12s} {0:6d}  (never fired)")
            continue
        fwd = sorted(a["fwd"]) or [0.0]
        gbr = sorted(a["gbr"])
        print(f"{k:12s} {a['n']:6d} {a['n'] / a['years']:8.1f} "
              f"{statistics.median(gb):9.1f}% {gb[int(len(gb) * 0.75)]:5.1f}% "
              f"{statistics.median(gbr):9.1f}% {a['false'] / a['n'] * 100:10.0f}% "
              f"{statistics.median(fwd):+10.1f}%")
    print("symbols:", measured)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--positions", help="JSON file: {SYM: {entry, entry_px, side, ...}}")
    ap.add_argument("--positions-json", help="the same content inline")
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    ap.add_argument("--cache-only", action="store_true",
                    help="read the cache and refuse to fall back to a live fetch")
    ap.add_argument("--self-test", action="store_true",
                    help="prove the failure guards fire; no network")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    positions: dict = {}
    if args.positions_json:
        positions = json.loads(args.positions_json)
    elif args.positions:
        with open(args.positions) as fh:
            positions = json.load(fh)

    try:
        return run(positions, args.cache, args.cache_only)
    except MeasurementDataError as exc:
        print(f"\nFATAL: {exc}", file=sys.stderr)
        print("FATAL: exiting 2 with NO numbers. Do not quote anything from this run.",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
