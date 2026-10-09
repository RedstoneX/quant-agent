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
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trend_alignment.analysis import *  # noqa: E402,F401,F403
from trend_alignment.analysis import FWD, REF, SHAPES, WARMUP  # noqa: E402,F401
from trend_alignment.data import *  # noqa: E402,F401,F403
from trend_alignment.data import (  # noqa: E402,F401
    DEFAULT_CACHE,
    MIN_BARS,
    MeasurementDataError,
    require_complete,
    require_nonempty_result,
    split_bulk_frame,
)
from trend_alignment.report import run, self_test  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--positions", help="JSON file: {SYM: {entry, entry_px, side, ...}}")
    ap.add_argument("--positions-json", help="the same content inline")
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    ap.add_argument("--cache-only", action="store_true", help="read the cache and refuse to fall back to a live fetch")
    ap.add_argument("--self-test", action="store_true", help="prove the failure guards fire; no network")
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
        print("FATAL: exiting 2 with NO numbers. Do not quote anything from this run.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
