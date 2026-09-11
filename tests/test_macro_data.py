import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
from src.data.macro import MacroDataProvider


@pytest.fixture
def mock_fred():
    mock = MagicMock()
    # VIX series
    mock.get_series.return_value = pd.Series(
        [18.5, 19.2, 17.8, 20.1, 18.0],
        index=pd.date_range("2026-04-01", periods=5, freq="B"),
    )
    return mock


@patch("src.data.macro.Fred")
def test_get_vix(mock_fred_cls, mock_fred):
    mock_fred_cls.return_value = mock_fred
    provider = MacroDataProvider(api_key="test-key")
    vix = provider.get_vix()
    assert vix["current"] == 18.0
    assert vix["mean_5d"] == pytest.approx(18.72, abs=0.01)
    assert "trend" in vix


@patch("src.data.macro.Fred")
def test_get_treasury_yields(mock_fred_cls, mock_fred):
    mock_fred_cls.return_value = mock_fred
    # Override for yield series
    mock_fred.get_series.side_effect = lambda series_id, **kw: pd.Series(
        [4.5] if series_id == "DGS2" else [4.2],
        index=pd.date_range("2026-04-07", periods=1),
    )
    provider = MacroDataProvider(api_key="test-key")
    yields = provider.get_treasury_yields()
    assert yields["us2y"] == 4.5
    assert yields["us10y"] == 4.2
    assert yields["spread_2_10"] == pytest.approx(-0.3, abs=0.01)
    assert yields["inverted"] is True


@patch("src.data.macro.Fred")
def test_get_macro_summary(mock_fred_cls, mock_fred):
    mock_fred_cls.return_value = mock_fred
    provider = MacroDataProvider(api_key="test-key")
    summary = provider.get_macro_summary()
    assert "vix" in summary
    assert "treasury" in summary
    assert "fed_funds_rate" in summary
    # New in 2026-04-17 refactor
    assert "inflation" in summary
    assert "unemployment" in summary
    assert "credit_spread" in summary


@patch("src.data.macro.Fred")
def test_get_fed_funds_rate_uses_dff_and_returns_dict(mock_fred_cls):
    """Switched from monthly FEDFUNDS to daily DFF; returns dict with current + 30d change."""
    mock = MagicMock()
    # 30 business days of DFF at 3.60% then stepping down to 3.35%
    mock.get_series.return_value = pd.Series(
        [3.60] * 15 + [3.35] * 15,
        index=pd.date_range("2026-03-15", periods=30, freq="B"),
    )
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(api_key="test-key")
    fed = provider.get_fed_funds_rate()

    assert fed["current"] == pytest.approx(3.35)
    assert fed["change_30d"] == pytest.approx(-0.25, abs=0.01)
    assert "staleness_days" in fed
    # Should have queried DFF, not FEDFUNDS
    assert mock.get_series.call_args_list[0][0][0] == "DFF"


@patch("src.data.macro.Fred")
def test_get_inflation(mock_fred_cls):
    """Headline + core CPI YoY and MoM; PCE YoY."""
    mock = MagicMock()
    # 14 monthly points so YoY (index[-1]/index[-13]) is defined.
    # Build a CPI series rising ~3% per year on headline, ~2.8% on core.
    # YoY is index[-1]/index[-13] − 1 (13 months back, not 14). Step sizes picked
    # so the ratio hits ~target: step such that (base + 13·step)/(base + step) ≈ 1 + target.
    def _fake_series(series_id, **kw):
        if series_id == "CPIAUCSL":
            vals = [300 + i * 0.75 for i in range(14)]   # ~3.0% YoY
        elif series_id == "CPILFESL":
            vals = [310 + i * 0.72 for i in range(14)]   # ~2.8% YoY
        elif series_id == "PCEPI":
            vals = [120 + i * 0.25 for i in range(14)]   # ~2.5% YoY
        else:
            vals = [0.0]
        return pd.Series(vals, index=pd.date_range("2025-03-01", periods=len(vals), freq="MS"))

    mock.get_series.side_effect = _fake_series
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(api_key="test-key")
    infl = provider.get_inflation()

    assert infl["headline_cpi_yoy"] == pytest.approx(3.0, abs=0.1)
    assert infl["core_cpi_yoy"] == pytest.approx(2.8, abs=0.1)
    assert infl["pce_yoy"] == pytest.approx(2.5, abs=0.1)
    assert infl["headline_cpi_mom"] is not None


@patch("src.data.macro.Fred")
def test_get_unemployment(mock_fred_cls):
    """UNRATE level + 3m and 12m changes."""
    mock = MagicMock()
    # Starting at 3.8%, ending at 4.1% over 13 months → +0.3pp 12m, last 3m +0.1pp
    vals = [3.8, 3.8, 3.9, 3.9, 3.9, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.1, 4.1]
    mock.get_series.return_value = pd.Series(
        vals, index=pd.date_range("2025-04-01", periods=13, freq="MS"),
    )
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(api_key="test-key")
    une = provider.get_unemployment()

    assert une["current"] == 4.1
    assert une["change_3m"] == pytest.approx(0.1, abs=0.01)
    assert une["change_12m"] == pytest.approx(0.3, abs=0.01)


@patch("src.data.macro.Fred")
def test_get_credit_spread(mock_fred_cls):
    """HY OAS returned in bps, 30-day change computed."""
    mock = MagicMock()
    # FRED returns HY OAS in percent (e.g. 3.80 = 380bps). Convert to bps.
    mock.get_series.return_value = pd.Series(
        [3.50, 3.60, 3.80],
        index=pd.date_range("2026-03-17", periods=3, freq="10B"),
    )
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(api_key="test-key")
    hy = provider.get_credit_spread()

    assert hy["current_bps"] == pytest.approx(380.0, abs=0.1)
    assert hy["change_30d_bps"] == pytest.approx(30.0, abs=0.1)


@patch("src.data.macro.Fred")
def test_empty_series_returns_safe_nulls(mock_fred_cls):
    """When FRED returns empty (network issue, stale holiday), fetchers don't crash."""
    mock = MagicMock()
    mock.get_series.return_value = pd.Series(dtype=float)
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(api_key="test-key")
    assert provider.get_fed_funds_rate()["current"] is None
    assert provider.get_inflation()["core_cpi_yoy"] is None
    assert provider.get_unemployment()["current"] is None
    assert provider.get_credit_spread()["current_bps"] is None


def test_staleness_uses_et_date_not_host_local():
    """CLAUDE.md invariant: any host TZ must produce the same data. Pre-fix
    used `date.today()` (host-local), so an SGT operator running before
    ET cutoff saw staleness ±1 day off vs the same data viewed from ET.

    Pin: with `et_today()` patched to a known ET date, staleness is computed
    relative to that, regardless of whatever the host calendar shows.
    """
    from datetime import date as _date

    series = pd.Series(
        [4.3, 4.4, 4.5],
        index=pd.date_range("2026-05-01", periods=3, freq="B"),
    )
    # series's latest observation = 2026-05-05 (the third business day from
    # 2026-05-01). With et_today=2026-05-08, staleness should be 3 calendar
    # days regardless of host TZ.
    with patch("src.data.macro.et_today", return_value=_date(2026, 5, 8)):
        days = MacroDataProvider._staleness_days(series)
    assert days == 3, f"expected 3 days, got {days}"


def test_macro_provider_rejects_empty_api_key():
    """MacroDataProvider must fail at construction when FRED_API_KEY is
    unset / empty / whitespace. Without this guard, every FRED series
    fetch silently fails inside macro_analyst.run, macro_summary becomes
    all-None, and PM decides with regime='unknown' — degraded data
    quietly contaminating decisions instead of crashing loud at startup.
    """
    import pytest

    with pytest.raises(ValueError, match="FRED_API_KEY"):
        MacroDataProvider(api_key="")
    with pytest.raises(ValueError, match="FRED_API_KEY"):
        MacroDataProvider(api_key="   ")  # whitespace alone counts as empty


def test_staleness_returns_zero_when_observation_is_today_in_et():
    """Same observation date as et_today → zero staleness, even if the host
    calendar would say it's tomorrow (e.g., SGT after midnight)."""
    from datetime import date as _date

    series = pd.Series(
        [4.5],
        index=pd.date_range("2026-05-08", periods=1),
    )
    with patch("src.data.macro.et_today", return_value=_date(2026, 5, 8)):
        days = MacroDataProvider._staleness_days(series)
    assert days == 0


def test_staleness_returns_none_for_empty_series():
    """No observations → can't compute staleness. Caller treats this as 'data unavailable'."""
    series = pd.Series(dtype=float)
    assert MacroDataProvider._staleness_days(series) is None


# ===========================================================================
# Transient-failure retry — trading-utility recovery (2026-08-20 incident:
# five series timed out in ONE run with no retry; the macro analyst read
# "critical missing data" and pinned the day to low-confidence / 55% cash).
# ===========================================================================

@patch("src.data.macro.time.sleep")
@patch("src.data.macro.Fred")
def test_transient_fred_timeout_recovers_on_retry(mock_fred_cls, mock_sleep):
    mock = MagicMock()
    good = pd.Series(
        [18.5, 19.2], index=pd.date_range("2026-04-01", periods=2, freq="B"),
    )
    mock.get_series.side_effect = [TimeoutError("The read operation timed out"), good]
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(api_key="test-key")
    vix = provider.get_vix()

    assert vix["current"] == 19.2, "one transient timeout must not blank the series"
    assert mock.get_series.call_count == 2
    mock_sleep.assert_called_once()


@patch("src.data.macro.time.sleep")
@patch("src.data.macro.Fred")
def test_persistent_failure_returns_empty_after_bounded_retry(mock_fred_cls, mock_sleep):
    """max_retries/breaker_after_failed_series are now operator settings
    (see src/config.py::MacroConfig) rather than module constants — pinned
    explicitly here to the pre-Phase-4.2 defaults so this test keeps
    verifying the MECHANISM (bounded retry, then degrade) independent of
    whatever the shipped defaults are tuned to. The new defaults themselves
    (more retries, jitter, a hard deadline ceiling) are covered in
    tests/test_macro_feed_resilience.py.
    """
    mock = MagicMock()
    mock.get_series.side_effect = TimeoutError("The read operation timed out")
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(api_key="test-key", max_retries=1)
    vix = provider.get_vix()

    assert vix["current"] is None
    assert mock.get_series.call_count == 2, "exactly one retry, then degrade"


@patch("src.data.macro.time.sleep")
@patch("src.data.macro.Fred")
def test_outage_breaker_stops_retrying_after_consecutive_failed_series(
    mock_fred_cls, mock_sleep,
):
    """Two series exhausting their retries looks like an outage, not a
    flake — later series must degrade after a single attempt so a full
    FRED outage can't multiply its own latency across all eight series.

    Pinned to max_retries=1/breaker_after_failed_series=2 (the
    pre-Phase-4.2 defaults) so this test verifies the breaker MECHANISM
    unchanged; the new default (breaker trips after just 1, since there
    are now fifteen series sharing one deadline budget) is covered in
    tests/test_macro_feed_resilience.py.
    """
    mock = MagicMock()
    mock.get_series.side_effect = TimeoutError("down")
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(
        api_key="test-key", max_retries=1, breaker_after_failed_series=2,
    )
    provider.get_vix()              # attempts 2 (1 + retry)
    provider.get_fed_funds_rate()   # attempts 2 (1 + retry) -> breaker arms
    calls_before = mock.get_series.call_count
    provider.get_unemployment()     # breaker armed: single attempt

    assert calls_before == 4
    assert mock.get_series.call_count == 5


@patch("src.data.macro.time.sleep")
@patch("src.data.macro.Fred")
def test_success_resets_outage_breaker(mock_fred_cls, mock_sleep):
    mock = MagicMock()
    good = pd.Series([1.0], index=pd.date_range("2026-04-01", periods=1))
    # fail, fail(retry) -> series 1 dead; then success resets the count.
    mock.get_series.side_effect = [
        TimeoutError("x"), TimeoutError("x"),   # series 1: dead after retry
        good,                                   # series 2: success -> reset
        TimeoutError("x"), good,                # series 3: retry still armed
    ]
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(
        api_key="test-key", max_retries=1, breaker_after_failed_series=2,
    )
    provider.get_vix()
    provider.get_fed_funds_rate()
    result = provider.get_unemployment()

    assert result["current"] == 1.0
    assert mock.get_series.call_count == 5


# ===========================================================================
# Freshness — latest-available-published-reading, NOT calendar age
# ===========================================================================
#
# Replaces the calendar-day freshness tests (2026-09-11). The old design
# asked "how old is this reading?" and blocked past a threshold; FRED's real
# publication lag meant the regime-shift gate demanded a print that does not
# exist and fired on 52% of production runs. The tests below pin the
# replacement: a reading counts when it is the latest FRED has published for
# that series and no newer print is overdue by that series' OWN measured
# cadence and publication lag. See src/data/macro.py::SeriesFreshness.

from datetime import date, timedelta  # noqa: E402

from src.data.macro import (  # noqa: E402
    FRESHNESS_CURRENT, FRESHNESS_EMPTY, FRESHNESS_OVERDUE, FRESHNESS_UNKNOWN,
)


def _info(observation_end: date, last_updated: date):
    """A minimal stand-in for FRED's /fred/series metadata response, which
    fredapi returns as a pandas Series of strings."""
    return pd.Series({
        "id": "X",
        "observation_end": observation_end.isoformat(),
        "last_updated": f"{last_updated.isoformat()} 08:31:05-05",
        "frequency_short": "D",
    })


def _daily_series(today: date, *, lag_days: int = 2, points: int = 10):
    """A business-daily series whose latest observation sits `lag_days`
    business days behind `today` — FRED's real, ordinary behaviour."""
    end = pd.Timestamp(today) - pd.tseries.offsets.BDay(lag_days)
    index = pd.bdate_range(end=end, periods=points)
    return pd.Series([4.2 + i * 0.01 for i in range(points)], index=index)


def _monthly_series(today: date, *, months_back: int = 1, points: int = 14):
    """A monthly series indexed at the reference-month start, with the most
    recent reference month `months_back` months before the current one —
    i.e. an ordinary CPI/UNRATE profile, weeks old by construction."""
    end = (pd.Timestamp(today).normalize().replace(day=1)
           - pd.DateOffset(months=months_back))
    index = pd.date_range(end=end, periods=points, freq="MS")
    return pd.Series([300.0 + i for i in range(points)], index=index)


@patch("src.data.macro.Fred")
def test_daily_series_at_real_fred_lag_is_current_not_stale(mock_fred_cls):
    """THE defect case, at provider level: a daily series two business days
    behind, published one day after its reference date. That is FRED being
    normal — it must read `current`, whatever its age."""
    today = date(2026, 9, 10)
    series = _daily_series(today, lag_days=2)
    obs_end = series.index[-1].date()
    mock = MagicMock()
    mock.get_series.return_value = series
    mock.get_series_info.return_value = _info(obs_end, obs_end + timedelta(days=1))
    mock_fred_cls.return_value = mock

    with patch("src.data.macro.et_today", return_value=today):
        provider = MacroDataProvider(api_key="test-key")
        vix = provider.get_vix()

    assert vix["freshness"] == FRESHNESS_CURRENT
    assert vix["staleness_days"] == 2, (
        "the age is still reported — it is context for the seat, no longer a gate"
    )


@patch("src.data.macro.Fred")
def test_monthly_series_weeks_old_is_current_not_stale(mock_fred_cls):
    """A monthly series whose newest reference month is last month, published
    ~2 weeks after that month began. Roughly six weeks 'old' and completely
    current: no newer CPI exists."""
    today = date(2026, 9, 10)
    series = _monthly_series(today, months_back=1)
    obs_end = series.index[-1].date()
    mock = MagicMock()
    mock.get_series.return_value = series
    mock.get_series_info.return_value = _info(obs_end, obs_end + timedelta(days=14))
    mock_fred_cls.return_value = mock

    with patch("src.data.macro.et_today", return_value=today):
        provider = MacroDataProvider(api_key="test-key")
        infl = provider.get_inflation()

    assert infl["freshness"] == FRESHNESS_CURRENT
    assert infl["staleness_days"] > 20, (
        "fixture must genuinely be weeks old, or it isn't testing the point"
    )


@patch("src.data.macro.Fred")
def test_monthly_series_is_overdue_once_a_cycle_is_actually_missed(mock_fred_cls):
    """Same monthly series, four months behind: the next print is long past
    due on the series' own 31-day cadence plus its own 14-day publication
    lag. That is real staleness and must be flagged."""
    today = date(2026, 9, 10)
    series = _monthly_series(today, months_back=4)
    obs_end = series.index[-1].date()
    mock = MagicMock()
    mock.get_series.return_value = series
    mock.get_series_info.return_value = _info(obs_end, obs_end + timedelta(days=14))
    mock_fred_cls.return_value = mock

    with patch("src.data.macro.et_today", return_value=today):
        provider = MacroDataProvider(api_key="test-key")
        infl = provider.get_inflation()

    assert infl["freshness"] == FRESHNESS_OVERDUE
    assert "due by" in infl["freshness_detail"]


@patch("src.data.macro.Fred")
def test_daily_series_is_overdue_when_publication_stalls(mock_fred_cls):
    """A daily series three weeks behind — a publication failure, a fetch
    failure, or a shutdown. Flagged, on the same derivation that leaves the
    two-day-lag case alone."""
    today = date(2026, 9, 10)
    series = _daily_series(today, lag_days=15)
    obs_end = series.index[-1].date()
    mock = MagicMock()
    mock.get_series.return_value = series
    mock.get_series_info.return_value = _info(obs_end, obs_end + timedelta(days=1))
    mock_fred_cls.return_value = mock

    with patch("src.data.macro.et_today", return_value=today):
        provider = MacroDataProvider(api_key="test-key")
        vix = provider.get_vix()

    assert vix["freshness"] == FRESHNESS_OVERDUE


@patch("src.data.macro.Fred")
def test_overdue_when_fred_holds_observations_the_fetch_did_not_return(mock_fred_cls):
    """FRED's own metadata says it has published past what we received. The
    query sets no observation_end, so this should be impossible — if it
    happens, what we hold is NOT the latest available reading, and that is a
    real problem rather than a normal release gap."""
    today = date(2026, 9, 10)
    series = _daily_series(today, lag_days=2)
    obs_end = series.index[-1].date() + timedelta(days=1)
    mock = MagicMock()
    mock.get_series.return_value = series
    mock.get_series_info.return_value = _info(obs_end, obs_end)
    mock_fred_cls.return_value = mock

    with patch("src.data.macro.et_today", return_value=today):
        provider = MacroDataProvider(api_key="test-key")
        vix = provider.get_vix()

    assert vix["freshness"] == FRESHNESS_OVERDUE
    assert "has published observations through" in vix["freshness_detail"]


@patch("src.data.macro.Fred")
def test_trailing_holiday_rows_with_no_value_are_not_overdue(mock_fred_cls):
    """FRED emits a row with value "." on a market holiday, which fredapi
    turns into NaN. That is a day with no print, not a missing print: the
    series' own observed gaps already include weekends, so the holiday must
    not read as overdue."""
    today = date(2026, 9, 10)
    series = _daily_series(today, lag_days=3)
    # One trailing no-value row the day after the last real reading.
    series = pd.concat([
        series,
        pd.Series([float("nan")], index=[series.index[-1] + pd.Timedelta(days=1)]),
    ])
    obs_end = series.index[-1].date()
    mock = MagicMock()
    mock.get_series.return_value = series
    mock.get_series_info.return_value = _info(obs_end, obs_end + timedelta(days=1))
    mock_fred_cls.return_value = mock

    with patch("src.data.macro.et_today", return_value=today):
        provider = MacroDataProvider(api_key="test-key")
        vix = provider.get_vix()

    assert vix["freshness"] == FRESHNESS_CURRENT


@patch("src.data.macro.Fred")
def test_freshness_unknown_when_metadata_unavailable(mock_fred_cls):
    """No metadata, no due-date derivation. The honest answer is `unknown`:
    the reading we hold is still FRED's latest published observation, but we
    cannot check whether a newer one should have arrived. Never reported as
    `current`, never as broken."""
    today = date(2026, 9, 10)
    mock = MagicMock()
    mock.get_series.return_value = _daily_series(today, lag_days=2)
    mock.get_series_info.side_effect = TimeoutError("metadata down")
    mock_fred_cls.return_value = mock

    with patch("src.data.macro.et_today", return_value=today):
        provider = MacroDataProvider(api_key="test-key")
        vix = provider.get_vix()

    assert vix["freshness"] == FRESHNESS_UNKNOWN
    assert vix["current"] is not None, "the reading itself is still usable"


@patch("src.data.macro.Fred")
def test_malformed_metadata_does_not_crash_and_reads_unknown(mock_fred_cls):
    """A redesigned response, a partial row, or a bare test double must
    degrade to `unknown` rather than raising inside a live trading
    session."""
    today = date(2026, 9, 10)
    mock = MagicMock()
    mock.get_series.return_value = _daily_series(today, lag_days=2)
    mock_fred_cls.return_value = mock
    for bad in (
        pd.Series({"id": "X"}),                                   # fields absent
        pd.Series({"observation_end": "not-a-date",
                   "last_updated": "also-not-a-date"}),           # unparseable
        pd.Series({"observation_end": "2026-09-08"}),             # half present
        "a string, not a metadata row",                           # wrong type
        None,
    ):
        mock.get_series_info.return_value = bad
        with patch("src.data.macro.et_today", return_value=today):
            provider = MacroDataProvider(api_key="test-key")
            vix = provider.get_vix()
        assert vix["freshness"] == FRESHNESS_UNKNOWN, bad


@patch("src.data.macro.Fred")
def test_empty_series_reports_empty_freshness_and_no_values(mock_fred_cls):
    """Genuinely missing data stays missing: `empty`, with every value None.
    It must never be handed on as a reading of any kind."""
    mock = MagicMock()
    mock.get_series.return_value = pd.Series(dtype=float)
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(api_key="test-key")
    vix = provider.get_vix()

    assert vix["freshness"] == FRESHNESS_EMPTY
    assert vix["current"] is None and vix["staleness_days"] is None
    assert mock.get_series_info.call_count == 0, (
        "no point asking for metadata about a series that returned nothing"
    )


@patch("src.data.macro.Fred")
def test_fetch_failure_reports_empty_freshness(mock_fred_cls):
    mock = MagicMock()
    mock.get_series.side_effect = TimeoutError("down")
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(api_key="test-key", max_retries=0)
    vix = provider.get_vix()

    assert vix["freshness"] == FRESHNESS_EMPTY
    assert vix["current"] is None


@patch("src.data.macro.Fred")
def test_single_observation_window_cannot_derive_cadence(mock_fred_cls):
    """One reading shows no gap, so the series' own cadence is
    underivable — `unknown`, not an assumed cadence."""
    today = date(2026, 9, 10)
    series = pd.Series([4.2], index=pd.DatetimeIndex([pd.Timestamp("2026-09-08")]))
    mock = MagicMock()
    mock.get_series.return_value = series
    mock.get_series_info.return_value = _info(
        date(2026, 9, 8), date(2026, 9, 9),
    )
    mock_fred_cls.return_value = mock

    with patch("src.data.macro.et_today", return_value=today):
        provider = MacroDataProvider(api_key="test-key")
        vix = provider.get_vix()

    assert vix["freshness"] == FRESHNESS_UNKNOWN
    assert "cadence" in vix["freshness_detail"]


@patch("src.data.macro.Fred")
def test_macro_summary_coverage_carries_overdue_series(mock_fred_cls):
    """An overdue print is a coverage-level fact the operator surface reads
    (`data_status["macro"] == "release_overdue"`), so it has to reach
    `last_coverage` and be named in `describe()`."""
    today = date(2026, 9, 10)
    mock = MagicMock()
    mock.get_series.return_value = _daily_series(today, lag_days=20)
    mock.get_series_info.side_effect = lambda sid: _info(
        _daily_series(today, lag_days=20).index[-1].date(),
        _daily_series(today, lag_days=20).index[-1].date() + timedelta(days=1),
    )
    mock_fred_cls.return_value = mock

    with patch("src.data.macro.et_today", return_value=today):
        provider = MacroDataProvider(api_key="test-key")
        provider.get_macro_summary()

    coverage = provider.last_coverage
    assert coverage is not None
    assert coverage.overdue_count > 0
    assert coverage.status == "ok", (
        "the fetch worked — overdue is a publication problem on a separate "
        "axis from coverage, not a fetch failure"
    )
    assert "OVERDUE" in coverage.describe()
