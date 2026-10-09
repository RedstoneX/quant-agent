"""Past earnings reports and the overnight gap each one produced. RECORD ONLY.

No threshold, percentile or trade use lives here. Every report that cannot be
measured is returned with a NAMED reason, never silently dropped.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Callable

from src.util.time import et_today

logger = logging.getLogger(__name__)

TIMING_BEFORE_OPEN = "before_open"
TIMING_AFTER_CLOSE = "after_close"

REASON_NO_TIMING = "no_timing"  # intraday stamp, or a bare midnight date with no time
REASON_NO_BARS = "no_bars"  # a needed session is missing from the daily bars
REASON_FUTURE_DATE = "future_date"  # report not yet happened
REASON_BAD_BARS = "bad_bars"  # reference close is zero or missing

# US equity regular session in Eastern time (exchange definition).
_SESSION_OPEN = time(9, 30)
_SESSION_CLOSE = time(16, 0)


@dataclass(frozen=True)
class GapRecord:
    date: date
    timing: str
    gap_pct: float


@dataclass(frozen=True)
class Unmeasured:
    date: date
    reason: str


def classify_timing(stamp: datetime) -> str | None:
    """before_open / after_close from the Eastern wall-clock time, else None.

    A bare midnight stamp means the source gave no time; midday is not an
    overnight event. Both are None (-> no_timing), never guessed.
    """
    clock = stamp.time()
    if clock == time(0, 0):
        return None
    if clock < _SESSION_OPEN:
        return TIMING_BEFORE_OPEN
    if clock >= _SESSION_CLOSE:
        return TIMING_AFTER_CLOSE
    return None


def measure_gap(stamp: datetime, bars: list, today: date) -> GapRecord | Unmeasured:
    """Gap across the correct session for one report. `bars` are daily OHLCV (.date/.open/.close)."""
    report_day = stamp.date()
    if report_day > today:
        return Unmeasured(report_day, REASON_FUTURE_DATE)
    timing = classify_timing(stamp)
    if timing is None:
        return Unmeasured(report_day, REASON_NO_TIMING)
    ordered = sorted(bars, key=lambda b: b.date)
    by_day = {b.date: i for i, b in enumerate(ordered)}
    if report_day not in by_day:
        return Unmeasured(report_day, REASON_NO_BARS)
    i = by_day[report_day]
    if timing == TIMING_BEFORE_OPEN:
        # open(report day) / close(previous trading day) - 1
        if i == 0:
            return Unmeasured(report_day, REASON_NO_BARS)
        ref_close, new_open = ordered[i - 1].close, ordered[i].open
    else:
        # open(next trading day after report) / close(report day) - 1; the next
        # trading day is read from the bar dates, so weekends/holidays are skipped.
        if i + 1 >= len(ordered):
            return Unmeasured(report_day, REASON_NO_BARS)
        ref_close, new_open = ordered[i].close, ordered[i + 1].open
    if not ref_close or not new_open:
        return Unmeasured(report_day, REASON_BAD_BARS)
    return GapRecord(report_day, timing, (new_open / ref_close - 1) * 100)


def build_history(report_stamps: list, bars: list, today: date | None = None):
    """(records, unmeasured), newest first."""
    today = today or et_today()
    records: list[GapRecord] = []
    unmeasured: list[Unmeasured] = []
    for stamp in sorted(report_stamps, reverse=True):
        out = measure_gap(stamp, bars, today)
        (unmeasured if isinstance(out, Unmeasured) else records).append(out)
    return records, unmeasured


def _eastern_naive(index_value) -> datetime:
    """yfinance stamps are tz-aware Eastern; keep the wall clock, drop the zone."""
    return index_value.to_pydatetime().replace(tzinfo=None)


def _yf_earnings_dates(symbol: str):
    import yfinance as yf

    # No `limit` passed: yfinance's own default is used.
    return yf.Ticker(symbol).get_earnings_dates()


def fetch_earnings_gap_history(
    symbol: str,
    get_ohlcv: Callable[[str, int], list] | None = None,
    get_earnings_dates: Callable[[str], object] | None = None,
):
    """Fetch past reports + daily bars and measure each gap. Returns (records, unmeasured)."""
    if get_earnings_dates is None:
        get_earnings_dates = _yf_earnings_dates
    if get_ohlcv is None:
        from src.data.market import MarketDataProvider

        get_ohlcv = MarketDataProvider().get_ohlcv
    frame = get_earnings_dates(symbol)
    if frame is None or frame.empty:
        return [], []
    stamps = [_eastern_naive(ix) for ix in frame.index]
    today = et_today()
    # Calendar days back to the oldest report, plus slack for the prior session.
    lookback = (today - min(stamps).date()).days + 7
    bars = get_ohlcv(symbol, lookback)
    return build_history(stamps, bars, today)
