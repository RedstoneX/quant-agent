"""Board item 187 — after retries are exhausted the desk acts: it serves the
last-good cached series with its age, and says so in coverage. No live calls."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd

from src.data.macro import MacroDataProvider
from src.data.macro_series_cache import MacroSeriesCache
from src.data.fetch_coverage_record import build_row
from src.trading_calendar import et_now


def _seed(cache, sid, kwargs, days_old):
    cache.save(sid, kwargs, [("2026-09-01", 1.0), ("2026-09-02", 2.0)],
               None, None, et_now() - timedelta(days=days_old))


def _provider(tmp_path, fred_cls, side_effect):
    fred = MagicMock()
    fred.get_series.side_effect = side_effect
    fred_cls.return_value = fred
    cache = MacroSeriesCache(str(tmp_path))
    return MacroDataProvider(api_key="k", max_retries=2, series_cache=cache), fred, cache


@patch("src.data.macro.time.sleep")
@patch("src.data.macro.Fred")
def test_deadline_path_retries_then_serves_last_good_with_age(fred_cls, _s, tmp_path):
    p, fred, cache = _provider(tmp_path, fred_cls, TimeoutError("fetch_deadline_exceeded"))
    _seed(cache, "VIXCLS", {}, 5)
    out = p._safe_get_series("VIXCLS")
    assert fred.get_series.call_count == 3  # retried before falling back
    assert list(out) == [1.0, 2.0]  # fallback taken
    cov = p._run_failed[0]
    assert cov.series_id == "VIXCLS"
    assert "stale_last_good_served_age_5d" in cov.reason


@patch("src.data.macro.time.sleep")
@patch("src.data.macro.Fred")
def test_no_last_good_stays_a_named_failure(fred_cls, _s, tmp_path):
    p, _f, _c = _provider(tmp_path, fred_cls, TimeoutError("fetch_deadline_exceeded"))
    out = p._safe_get_series("VIXCLS")
    assert len(out) == 0
    assert p._run_failed[0].reason == "fetch_deadline_exceeded"


@patch("src.data.macro.time.sleep")
@patch("src.data.macro.Fred")
def test_incomplete_set_is_visible_to_consumers_and_counted(fred_cls, _s, tmp_path):
    p, _f, cache = _provider(tmp_path, fred_cls, TimeoutError("fetch_deadline_exceeded"))
    _seed(cache, "VIXCLS", {}, 3)
    p._safe_get_series("VIXCLS")
    from src.data.macro import MacroCoverage
    cov = MacroCoverage(configured=p._run_configured, succeeded=p._run_succeeded,
                        failed=list(p._run_failed))
    assert not cov.complete and cov.status == "failed"
    assert "stale_last_good_served_age_3d" in cov.describe()
    assert "VIXCLS" in cov.verdict_stamp()[1]
    row = build_row(1, cov, None)  # the counted durable row
    assert row["full_coverage"] == 0 and "VIXCLS" in row["series_failed"]


@patch("src.data.macro.Fred")
def test_prefetch_mode_never_serves_stale(fred_cls, tmp_path):
    p, _f, cache = _provider(tmp_path, fred_cls, TimeoutError("x"))
    _seed(cache, "VIXCLS", {}, 5)
    p._prefetch_mode = True
    assert len(p._safe_get_series("VIXCLS")) == 0
