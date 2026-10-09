"""Board item 63 — the census-to-forward-return join.

These pin the JOIN and the EXCLUSION accounting. They deliberately pin no
threshold, no band boundary and no weight: this module produces evidence,
and a test asserting a cut-off would be the fitted number the item forbids.
"""

from datetime import date

from src.insider_sale_measure import (
    EXCLUDED_BEFORE_BARS,
    EXCLUDED_NO_DATE,
    EXCLUDED_NO_SYMBOL,
    EXCLUDED_UNRESOLVED,
    join_forward_returns,
    summarize,
    summarize_joined,
)


class _Bar:
    def __init__(self, d, close):
        self.date = d
        self.close = close


def _bars(closes, start_day=1):
    return [_Bar(date(2026, 1, start_day + i), c) for i, c in enumerate(closes)]


def test_resolves_a_forward_return_at_an_explicit_horizon():
    rows = [
        {"symbol": "AAA", "transaction_date": "2026-01-01", "reference_price": 10.0, "holdings_fraction_band": "mid"}
    ]
    out = join_forward_returns(rows, {"AAA": _bars([100.0, 101.0, 110.0])}, 2)
    assert out["n_resolved"] == 1 and out["n_excluded"] == 0
    rec = out["records"][0]
    assert rec["forward_return_pct"] == 10.0
    assert rec["sessions_forward"] == 2
    assert rec["excluded_reason"] is None


def test_missing_symbol_missing_date_and_unresolved_window_are_excluded_by_reason():
    rows = [
        {"symbol": "ZZZ", "transaction_date": "2026-01-01"},
        {"symbol": "AAA", "transaction_date": ""},
        {"symbol": "AAA", "transaction_date": "2026-01-01"},
    ]
    out = join_forward_returns(rows, {"AAA": _bars([100.0, 101.0])}, 5)
    assert out["n_resolved"] == 0
    assert out["excluded_by_reason"] == {
        EXCLUDED_NO_SYMBOL: 1,
        EXCLUDED_NO_DATE: 1,
        EXCLUDED_UNRESOLVED: 1,
    }
    assert all(r["forward_return_pct"] is None for r in out["records"])


def test_a_transaction_older_than_the_bar_set_is_excluded_not_collapsed():
    """The entry rule takes the first bar ON OR AFTER the date, so without
    this guard a 2022 sale would report the move that began at the first
    bar years later — a substituted return wearing a real one's clothes."""
    rows = [{"symbol": "AAA", "transaction_date": "2022-01-01"}]
    out = join_forward_returns(rows, {"AAA": _bars([100.0, 110.0])}, 1)
    assert out["n_resolved"] == 0
    assert out["excluded_by_reason"] == {EXCLUDED_BEFORE_BARS: 1}


def test_excluded_rows_never_enter_a_summary():
    rows = [
        {"symbol": "AAA", "transaction_date": "2026-01-01", "holdings_fraction_band": "mid"},
        {"symbol": "ZZZ", "transaction_date": "2026-01-01", "holdings_fraction_band": "mid"},
    ]
    joined = join_forward_returns(rows, {"AAA": _bars([100.0, 110.0])}, 1)
    summary = summarize_joined(joined)
    assert summary["overall"]["n"] == 1
    assert summary["by_group"]["mid"]["n"] == 1
    assert summary["n_excluded"] == 1
    assert summary["excluded_by_reason"] == {EXCLUDED_NO_SYMBOL: 1}


def test_summary_reports_an_uncertainty_alongside_every_mean():
    s = summarize([1.0, -1.0, 3.0, -3.0])
    assert s["n"] == 4 and s["mean_pct"] == 0.0
    assert s["stderr_pct"] is not None and s["stderr_pct"] > 0
    assert s["share_negative_pct"] == 50.0
    assert summarize([])["mean_pct"] is None
