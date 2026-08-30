"""Phase 4.2 — FRED feed resilience, six new series, and coverage visibility.

Two verified production defects being fixed here:

1. The FRED fetch gave up too easily. Production log evidence
   (2026-08-26 17:01:29-17:03:49 UTC): ALL NINE configured series failed
   in one run with "The read operation timed out", under a
   single-retry / flat-2s-backoff policy. `src/data/macro.py` now takes
   more retries, exponential backoff with jitter, and a hard wall-clock
   deadline as operator settings (`src/config.py::MacroConfig`,
   `config/settings.yaml`'s `macro:` block) rather than module constants.

2. Nothing told anyone it happened. `MacroCoverage` (this module) mirrors
   `src.data.news.NewsCoverage` (the 2026-08-28 news fix) exactly: it
   tracks configured/succeeded/failed FRED series per run and feeds
   `data_status["macro"]` (ok/partial/failed) the same way NewsCoverage
   feeds `data_status["news"]` — which trader_feed.py / notifier.py
   already render as the "Data degraded" banner with no changes of
   their own required.

Also covers the six new FRED series added alongside the resilience work
(docs/AGENT_ROLE_AUDIT.md §2.3: DFII10, T10YIE, DGS3MO, DTWEXBGS,
BAMLC0A0CM; plus ICSA for a timely labor read) — verified live against
FRED (see the Phase 4.2 fetch-verification report) before being wired in.

No live network calls anywhere in this file — every Fred.get_series call
is mocked.
"""

import time
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.agents.macro_analyst import MacroAnalystAgent
from src.data.macro import MacroCoverage, MacroDataProvider, SeriesFailure
from src.pipeline_context import RunContext
from src.pipeline_stages import MorningResearchStage


def _series(values, start="2026-08-01", freq="B"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq=freq))


def _minimal_macro_analysis():
    from src.models import MacroAnalysis, MacroPositionGuidance, MacroReasoningChain

    return MacroAnalysis(
        reasoning_chain=MacroReasoningChain(
            volatility_analysis="a", yield_curve_analysis="b",
            monetary_policy_analysis="c", inflation_labor_credit="d",
            cross_signal_synthesis="e", sector_implications="f",
        ),
        regime="risk-on", confidence="medium", equity_outlook="bullish",
        position_guidance=MacroPositionGuidance(
            target_invested_pct=70, cash_recommendation_pct=30, reasoning="y",
        ),
        summary="z",
    )


# ===========================================================================
# Retry / backoff / deadline mechanism
# ===========================================================================

@patch("src.data.macro.time.sleep")
@patch("src.data.macro.Fred")
def test_new_default_survives_two_consecutive_transient_failures(mock_fred_cls, mock_sleep):
    """The pre-Phase-4.2 shipped policy (max_retries=1) could not survive
    TWO consecutive timeouts on the same series — exactly the 2026-08-26
    failure mode (network trouble outlasting a single 2s-backoff retry).
    The new shipped default (max_retries=2) must recover from this."""
    mock = MagicMock()
    good = _series([18.5, 19.2])
    mock.get_series.side_effect = [
        TimeoutError("The read operation timed out"),
        TimeoutError("The read operation timed out"),
        good,
    ]
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(api_key="test-key")  # shipped defaults, no overrides
    vix = provider.get_vix()

    assert vix["current"] == 19.2, "two transient timeouts must not blank the series"
    assert mock.get_series.call_count == 3
    assert mock_sleep.call_count == 2


@patch("src.data.macro.Fred")
def test_one_series_total_failure_degrades_coverage_to_partial(mock_fred_cls):
    """One of fifteen series times out completely (exhausts its retries);
    the other fourteen return data. MacroCoverage must read 'partial' —
    the whole point of tracking coverage instead of a log-only warning."""
    mock = MagicMock()

    def _side_effect(series_id, **kw):
        if series_id == "BAMLC0A0CM":
            raise TimeoutError("down")
        return _series([1.0, 1.1, 1.2])

    mock.get_series.side_effect = _side_effect
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(api_key="test-key", max_retries=0)
    summary = provider.get_macro_summary()

    coverage = provider.last_coverage
    assert coverage is not None
    assert coverage.configured == 15
    assert coverage.succeeded == 14
    assert coverage.status == "partial"
    assert coverage.failed == [SeriesFailure(series_id="BAMLC0A0CM", reason="down")]
    assert "BAMLC0A0CM" in coverage.describe()
    # The one failed series still degrades to safe nulls, never crashes.
    assert summary["ig_credit_spread"]["current_bps"] is None


@patch("src.data.macro.Fred")
def test_all_series_failure_degrades_coverage_to_failed(mock_fred_cls):
    """Reproduces the verified 2026-08-26 17:01:29-17:03:49 UTC incident
    shape: every configured FRED series fails in one run. MacroCoverage
    must read 'failed', never 'ok' or 'partial'."""
    mock = MagicMock()
    mock.get_series.side_effect = TimeoutError("The read operation timed out")
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(api_key="test-key", max_retries=0)
    summary = provider.get_macro_summary()

    coverage = provider.last_coverage
    assert coverage.configured == 15
    assert coverage.succeeded == 0
    assert coverage.status == "failed"
    assert coverage.failed_count == 15
    # Every fetcher, old and new, degrades to safe nulls — never raises.
    assert summary["vix"]["current"] is None
    assert summary["real_rates"]["real_10y"] is None
    assert summary["jobless_claims"]["current"] is None
    assert summary["dollar_index"]["current"] is None


def test_backoff_and_deadline_bound_worst_case_wall_clock():
    """No time.sleep mocking here — this measures REAL elapsed wall clock
    to prove total_fetch_deadline_s is an actual ceiling (requests and
    backoff sleeps clipped to remaining budget), not merely an implication
    of retry-count x timeout arithmetic. A full-outage fetch with a
    generous per-series retry budget must still finish close to the
    deadline, never meaningfully past it — this is what keeps a live
    trading session from stalling on a FRED outage."""
    mock = MagicMock()
    mock.get_series.side_effect = TimeoutError("down")

    with patch("src.data.macro.Fred") as mock_fred_cls:
        mock_fred_cls.return_value = mock
        provider = MacroDataProvider(
            api_key="test-key",
            request_timeout_s=1.0,
            max_retries=5,
            retry_backoff_base_s=0.05,
            retry_backoff_max_s=0.2,
            retry_backoff_jitter_s=0.05,
            breaker_after_failed_series=1,
            total_fetch_deadline_s=0.5,
        )
        start = time.monotonic()
        provider.get_macro_summary()
        elapsed = time.monotonic() - start

    # Unbounded retry x timeout arithmetic here would be 15 series x up to
    # 6 attempts x 1.0s = tens of seconds; the deadline must keep this
    # close to 0.5s regardless. Generous slack (2x) absorbs scheduling /
    # logging overhead without hiding a real regression.
    assert elapsed <= 1.0, (
        f"get_macro_summary() took {elapsed:.2f}s against a "
        f"total_fetch_deadline_s=0.5s ceiling — the deadline is not "
        f"actually bounding wall-clock"
    )
    assert provider.last_coverage.status == "failed"


@patch("src.data.macro.Fred")
def test_deadline_already_expired_skips_remaining_series_without_attempting(mock_fred_cls):
    """A provider whose fetch deadline has already elapsed (e.g. an
    earlier series in the same get_macro_summary() call burned the whole
    budget) must skip a later series without even calling Fred — the
    mechanism that keeps the worst case bounded regardless of how many
    series are configured."""
    mock = MagicMock()
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(api_key="test-key")
    provider._deadline = time.monotonic() - 1.0  # already expired

    result = provider.get_vix()

    assert result["current"] is None
    mock.get_series.assert_not_called()


# ===========================================================================
# The six new series — parsed correctly, reach the summary dict
# ===========================================================================

@patch("src.data.macro.Fred")
def test_get_treasury_yields_includes_3m_10y_curve(mock_fred_cls):
    """DGS3MO added alongside the existing 2Y/10Y curve."""
    mock = MagicMock()
    fixed = {"DGS3MO": _series([3.84]), "DGS2": _series([4.20]), "DGS10": _series([4.67])}
    mock.get_series.side_effect = lambda sid, **kw: fixed[sid]
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(api_key="test-key")
    treasury = provider.get_treasury_yields()

    assert treasury["us3mo"] == 3.84
    assert treasury["us2y"] == 4.20
    assert treasury["us10y"] == 4.67
    assert treasury["spread_2_10"] == pytest.approx(0.47, abs=0.001)
    assert treasury["inverted"] is False
    assert treasury["spread_3m_10y"] == pytest.approx(0.83, abs=0.001)
    assert treasury["inverted_3m_10y"] is False


@patch("src.data.macro.Fred")
def test_get_real_yield_and_breakeven(mock_fred_cls):
    mock = MagicMock()
    fixed = {"DFII10": _series([2.34]), "T10YIE": _series([2.31])}
    mock.get_series.side_effect = lambda sid, **kw: fixed[sid]
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(api_key="test-key")
    real_rates = provider.get_real_yield_and_breakeven()

    assert real_rates["real_10y"] == 2.34
    assert real_rates["breakeven_10y"] == 2.31
    assert "staleness_days" in real_rates


@patch("src.data.macro.Fred")
def test_get_dollar_index(mock_fred_cls):
    mock = MagicMock()
    mock.get_series.return_value = _series([117.0, 117.5, 118.06], freq="D")
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(api_key="test-key")
    dollar = provider.get_dollar_index()

    assert dollar["current"] == 118.06
    assert "change_30d" in dollar
    assert "staleness_days" in dollar


@patch("src.data.macro.Fred")
def test_get_ig_credit_spread(mock_fred_cls):
    mock = MagicMock()
    # FRED returns % — 0.79% = 79bps, same convention as the existing HY OAS fetcher.
    mock.get_series.return_value = _series([0.75, 0.77, 0.79])
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(api_key="test-key")
    ig = provider.get_ig_credit_spread()

    assert ig["current_bps"] == pytest.approx(79.0, abs=0.1)
    assert "change_30d_bps" in ig


@patch("src.data.macro.Fred")
def test_get_jobless_claims_weekly(mock_fred_cls):
    mock = MagicMock()
    mock.get_series.return_value = pd.Series(
        [210000, 208000, 205000, 204000, 203000],
        index=pd.date_range("2026-07-25", periods=5, freq="W"),
    )
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(api_key="test-key")
    claims = provider.get_jobless_claims()

    assert claims["current"] == 203000
    assert claims["change_4w"] == pytest.approx(203000 - 210000, abs=1)
    assert claims["trend"] == "falling"


@patch("src.data.macro.Fred")
def test_get_macro_summary_includes_all_new_keys(mock_fred_cls):
    mock = MagicMock()
    mock.get_series.return_value = _series([1.0, 1.05, 1.1, 1.15, 1.2])
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(api_key="test-key")
    summary = provider.get_macro_summary()

    for key in ("real_rates", "dollar_index", "ig_credit_spread", "jobless_claims"):
        assert key in summary
    assert "us3mo" in summary["treasury"]
    assert "spread_3m_10y" in summary["treasury"]
    assert provider.last_coverage.configured == 15


def test_build_user_message_renders_new_series_and_coverage():
    """The agent PAYLOAD, not just the fetch — a series that is fetched
    but never explained to the model is dead weight."""
    agent = MacroAnalystAgent(api_key="test-key", model="claude-sonnet-4-6")
    macro_summary = {
        "vix": {"current": 18.0, "mean_5d": 18.5, "trend": "falling", "staleness_days": 0},
        "treasury": {
            "us3mo": 3.84, "us2y": 4.2, "us10y": 4.67,
            "spread_2_10": 0.47, "inverted": False,
            "spread_3m_10y": 0.83, "inverted_3m_10y": False,
            "staleness_days": 0,
        },
        "real_rates": {"real_10y": 2.34, "breakeven_10y": 2.31, "staleness_days": 0},
        "dollar_index": {"current": 118.06, "change_30d": -0.5, "staleness_days": 6},
        "ig_credit_spread": {"current_bps": 79.0, "change_30d_bps": 2.0, "staleness_days": 0},
        "jobless_claims": {"current": 203000, "change_4w": -7000, "trend": "falling", "staleness_days": 6},
    }
    coverage = MacroCoverage(
        configured=15, succeeded=14,
        failed=[SeriesFailure(series_id="ICSA", reason="timed out")],
    )

    msg = agent.build_user_message(macro_summary=macro_summary, macro_coverage=coverage)

    assert "Macro Data Coverage" in msg
    assert "14/15" in msg
    assert "ICSA (timed out)" in msg
    assert "Real 10Y Yield: 2.34" in msg
    assert "10Y Breakeven Inflation: 2.31" in msg
    assert "3-Month: 3.84" in msg
    assert "3M-10Y Spread: 0.83" in msg
    assert "Initial Jobless Claims" in msg
    assert "IG Credit Spread" in msg
    assert "Dollar Index" in msg


# ===========================================================================
# No-op wall: existing fields unchanged from pre-Phase-4.2 behavior
# ===========================================================================

@patch("src.data.macro.Fred")
def test_no_op_existing_fields_unchanged_when_everything_succeeds(mock_fred_cls):
    """With every series succeeding, the ORIGINAL nine series' fields must
    compute byte-identical to pre-Phase-4.2 behavior (values mirror
    tests/test_macro_data.py's per-series fixtures exactly) — this phase
    only ADDS fields/series, it must never change what an existing
    consumer (risk_manager, position_reviewer, evening_analyst,
    macro_analyst's own prompt) already reads."""
    mock = MagicMock()
    fixed = {
        "VIXCLS": _series([18.5, 19.2, 17.8, 20.1, 18.0]),
        "DGS3MO": _series([3.84]),
        "DGS2": _series([4.5]),
        "DGS10": _series([4.2]),
        "DFF": pd.Series(
            [3.60] * 15 + [3.35] * 15,
            index=pd.date_range("2026-03-15", periods=30, freq="B"),
        ),
        "CPIAUCSL": pd.Series(
            [300 + i * 0.75 for i in range(14)],
            index=pd.date_range("2025-03-01", periods=14, freq="MS"),
        ),
        "CPILFESL": pd.Series(
            [310 + i * 0.72 for i in range(14)],
            index=pd.date_range("2025-03-01", periods=14, freq="MS"),
        ),
        "PCEPI": pd.Series(
            [120 + i * 0.25 for i in range(14)],
            index=pd.date_range("2025-03-01", periods=14, freq="MS"),
        ),
        "UNRATE": pd.Series(
            [3.8, 3.8, 3.9, 3.9, 3.9, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.1, 4.1],
            index=pd.date_range("2025-04-01", periods=13, freq="MS"),
        ),
        "BAMLH0A0HYM2": _series([3.50, 3.60, 3.80], freq="10B"),
        "DFII10": _series([2.34]),
        "T10YIE": _series([2.31]),
        "DTWEXBGS": _series([118.0]),
        "BAMLC0A0CM": _series([0.75, 0.77, 0.79]),
        "ICSA": pd.Series(
            [210000, 205000, 203000, 202000, 201000],
            index=pd.date_range("2026-07-25", periods=5, freq="W"),
        ),
    }
    mock.get_series.side_effect = lambda sid, **kw: fixed[sid]
    mock_fred_cls.return_value = mock

    provider = MacroDataProvider(api_key="test-key")
    summary = provider.get_macro_summary()

    # Pre-existing fields — same math as tests/test_macro_data.py's own
    # fixtures for these series (test_get_vix, test_get_treasury_yields,
    # test_get_fed_funds_rate_uses_dff_and_returns_dict, test_get_inflation,
    # test_get_unemployment, test_get_credit_spread).
    assert summary["vix"]["current"] == 18.0
    assert summary["treasury"]["us2y"] == 4.5
    assert summary["treasury"]["us10y"] == 4.2
    assert summary["treasury"]["spread_2_10"] == pytest.approx(-0.3, abs=0.01)
    assert summary["treasury"]["inverted"] is True
    assert summary["fed_funds_rate"]["current"] == pytest.approx(3.35)
    assert summary["fed_funds_rate"]["change_30d"] == pytest.approx(-0.25, abs=0.01)
    assert summary["inflation"]["headline_cpi_yoy"] == pytest.approx(3.0, abs=0.1)
    assert summary["inflation"]["core_cpi_yoy"] == pytest.approx(2.8, abs=0.1)
    assert summary["inflation"]["pce_yoy"] == pytest.approx(2.5, abs=0.1)
    assert summary["unemployment"]["current"] == 4.1
    assert summary["unemployment"]["change_3m"] == pytest.approx(0.1, abs=0.01)
    assert summary["unemployment"]["change_12m"] == pytest.approx(0.3, abs=0.01)
    assert summary["credit_spread"]["current_bps"] == pytest.approx(380.0, abs=0.1)
    assert summary["credit_spread"]["change_30d_bps"] == pytest.approx(30.0, abs=0.1)

    # New fields present ALONGSIDE, not replacing, the above.
    assert summary["treasury"]["us3mo"] == 3.84
    assert summary["real_rates"]["real_10y"] == 2.34
    assert summary["real_rates"]["breakeven_10y"] == 2.31
    assert summary["dollar_index"]["current"] == 118.0
    assert summary["ig_credit_spread"]["current_bps"] == pytest.approx(79.0, abs=0.1)
    assert summary["jobless_claims"]["current"] == 201000
    assert provider.last_coverage.status == "ok"
    assert provider.last_coverage.configured == 15
    assert provider.last_coverage.succeeded == 15


# ===========================================================================
# MorningResearchStage integration — data_status["macro"] visibility
# (mirrors the equivalent NewsCoverage tests in test_pipeline_stages.py)
# ===========================================================================

def _macro_coverage_stage(macro_coverage, macro_analysis):
    """Minimal MorningResearchStage wiring shared by the tests below —
    mirrors test_pipeline_stages.py's _news_coverage_stage exactly, but
    varies the macro branch (self.macro.last_coverage) instead of news."""
    mock_config = MagicMock()
    mock_config.trading.universe = ["AAPL"]
    mock_config.trading.lookback_days = 30

    market = MagicMock()
    market.get_ohlcv.return_value = []  # skip tech entirely, not under test here

    macro_provider = MagicMock()
    macro_provider.get_macro_summary.return_value = {
        "vix": {"current": 18.0}, "credit_spread": {"current_bps": 300},
        "inflation": {"core_cpi_yoy": 3.0}, "unemployment": {"current": 4.2},
    }
    macro_provider.last_coverage = macro_coverage

    macro_store = MagicMock()
    macro_store.load_last_state.return_value = None
    news_store = MagicMock()
    news_store.load_macro_narrative.return_value = None
    macro_agent = MagicMock()
    macro_agent.analyze.return_value = (macro_analysis, MagicMock(
        user_message="m", raw_text="{}", tokens_used=1, model="t",
        input_tokens=1, output_tokens=1, cost_usd=0.0,
    ))

    return MorningResearchStage(
        config=mock_config,
        db=MagicMock(),
        market=market,
        macro=macro_provider,
        news_provider=MagicMock(),
        news_store=news_store,
        macro_store=macro_store,
        tech_store=MagicMock(),
        earnings_provider=MagicMock(),
        macro_analyst=macro_agent,
        news_analyst=MagicMock(),
        tech_analyst=MagicMock(),
        earnings_analyst=MagicMock(),
        has_actionable_signal_fn=lambda *args, **kw: False,
        run_news_update_fn=lambda run_id, session: (None, None),
        load_earnings_analyses_fn=lambda run_id, session, ctx=None: ([], []),
    )


def test_morning_research_stage_macro_partial_coverage_marks_status_partial():
    """One of fifteen configured FRED series down, fourteen survivors were
    enough for the analyst to produce a valid MacroAnalysis. Before this
    fix, data_status['macro'] was 'ok' purely because the LLM call parsed
    — this asserts it is now 'partial'."""
    coverage = MacroCoverage(
        configured=15, succeeded=14,
        failed=[SeriesFailure(series_id="ICSA", reason="timed out")],
    )
    stage = _macro_coverage_stage(coverage, _minimal_macro_analysis())

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.macro_coverage is coverage
    assert result_ctx.data_status["macro"] == "partial"
    assert result_ctx.data_status["macro"] != "ok"


def test_morning_research_stage_macro_total_failure_marks_status_failed_even_when_analysis_parses():
    """The exact scenario the fix targets, reproducing the verified
    2026-08-26 17:01-17:03 UTC incident at the pipeline layer: ALL fifteen
    FRED series fail, yet the analyst still returns a technically-valid
    MacroAnalysis (built on all-None indicators). Coverage failure must
    dominate — this must read as 'failed', never 'ok', regardless of
    whether the LLM call itself succeeded on empty input."""
    coverage = MacroCoverage(
        configured=15, succeeded=0,
        failed=[SeriesFailure(series_id=f"S{i}", reason="timed out") for i in range(15)],
    )
    stage = _macro_coverage_stage(coverage, _minimal_macro_analysis())

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["macro"] == "failed"
    assert result_ctx.data_status["macro"] != "ok"


def test_morning_research_stage_macro_full_coverage_marks_status_ok():
    """Control case: 15/15 series returned data and the analysis parsed —
    the one scenario that legitimately reads as 'ok'."""
    coverage = MacroCoverage(configured=15, succeeded=15, failed=[])
    stage = _macro_coverage_stage(coverage, _minimal_macro_analysis())

    ctx = RunContext.start("morning")
    ctx.positions = []
    result_ctx = stage.run(ctx)

    assert result_ctx.data_status["macro"] == "ok"
