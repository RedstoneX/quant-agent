"""Bar acquisition and completeness guards for the trend-alignment measurement.

See the docstring of scripts/measure_trend_alignment.py for why every failure is fatal.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import date

from src.models import OHLCV  # noqa: E402


#: Two years of US sessions is ~500. Anything under this is a partial fetch and
#: is refused rather than measured. Not a tuning knob: it is the completeness
#: floor for the window the measurement claims to cover.
MIN_BARS = 400

DEFAULT_CACHE = os.environ.get("ALIGN_CACHE", "/home/ubuntu/qamc-measurements/alignment_bars.json")


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
                    f"fetch {sym} attempt {attempt + 1} returned {len(bars)} bars (< {MIN_BARS}); retrying",
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
        allbars = {s: [OHLCV(**{**b, "date": date.fromisoformat(b["date"])}) for b in raw[s]] for s in syms if s in raw}
        print(f"bars read from cache {cache}")
    else:
        allbars = fetch_bars(syms)
        os.makedirs(os.path.dirname(cache) or ".", exist_ok=True)
        with open(cache, "w") as fh:
            json.dump(
                {
                    s: [
                        dict(date=str(b.date), open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume)
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
