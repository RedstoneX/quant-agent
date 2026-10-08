"""Self-test and the measurement run for the trend-alignment measurement."""
from __future__ import annotations

import statistics
from datetime import date

import trend_alignment  # noqa: F401  (puts the repo root on sys.path)
from trend_alignment.analysis import FWD, REF, SHAPES, WARMUP, measure_two_years, readings, walk  # noqa: F401
from trend_alignment.data import (  # noqa: F401
    MIN_BARS,
    MeasurementDataError,
    load_or_fetch,
    require_complete,
    require_nonempty_result,
    split_bulk_frame,
)


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

