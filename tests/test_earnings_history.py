"""Hermetic: fake bars + fake earnings frame."""

from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd

from src.data.event_calendar.earnings_history import (
    REASON_FUTURE_DATE,
    REASON_NO_BARS,
    REASON_NO_TIMING,
    build_history,
    fetch_earnings_gap_history,
)

TODAY = date(2026, 3, 2)


def bar(d, o, c):
    return SimpleNamespace(date=d, open=o, close=c)


# Thu 2026-02-05, Fri 02-06, (weekend), Mon 02-09
BARS = [
    bar(date(2026, 2, 5), 100, 100),
    bar(date(2026, 2, 6), 110, 120),
    bar(date(2026, 2, 9), 132, 130),
]


def test_before_open_uses_previous_close():
    recs, bad = build_history([datetime(2026, 2, 6, 7, 0)], BARS, TODAY)
    assert bad == []
    assert recs[0].timing == "before_open"
    assert abs(recs[0].gap_pct - 10.0) < 1e-9  # 110 / 100 - 1


def test_after_close_over_weekend_uses_next_bar_date():
    recs, bad = build_history([datetime(2026, 2, 6, 16, 30)], BARS, TODAY)
    assert bad == []
    assert recs[0].timing == "after_close"
    assert abs(recs[0].gap_pct - 10.0) < 1e-9  # Mon open 132 / Fri close 120 - 1


def test_named_reasons():
    stamps = [
        datetime(2026, 2, 6, 0, 0),  # no time
        datetime(2026, 2, 6, 12, 0),  # midday
        datetime(2026, 2, 9, 17, 0),  # no next bar
        datetime(2026, 2, 5, 8, 0),  # no previous bar
        datetime(2026, 4, 1, 8, 0),  # future
    ]
    recs, bad = build_history(stamps, BARS, TODAY)
    assert recs == []
    reasons = sorted(u.reason for u in bad)
    assert reasons == sorted([REASON_NO_TIMING] * 2 + [REASON_NO_BARS] * 2 + [REASON_FUTURE_DATE])


def test_fetch_with_fakes():
    idx = pd.DatetimeIndex([pd.Timestamp("2026-02-06 07:00", tz="America/New_York")])
    frame = pd.DataFrame({"x": [1]}, index=idx)
    seen = {}

    def fake_bars(sym, days):
        seen["days"] = days
        return BARS

    recs, bad = fetch_earnings_gap_history("ZZZ", fake_bars, lambda s: frame)
    assert len(recs) == 1 and bad == []
    assert seen["days"] > 0
