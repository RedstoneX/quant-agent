import json
from unittest.mock import MagicMock, patch

from src.agents.macro_analyst import MacroAnalystAgent


MACRO_SUMMARY = {
    "vix": {"current": 19.5, "mean_5d": 20.1, "trend": "falling", "staleness_days": 0},
    "treasury": {"us2y": 4.5, "us10y": 4.3, "spread_2_10": -0.2, "inverted": True, "staleness_days": 0},
    "fed_funds_rate": {"current": 3.60, "change_30d": 0.0, "staleness_days": 0},
    "inflation": {"headline_cpi_yoy": 3.0, "headline_cpi_mom": 0.2, "core_cpi_yoy": 2.8,
                  "core_cpi_mom": 0.25, "pce_yoy": 2.5, "staleness_days": 10},
    "unemployment": {"current": 4.1, "change_3m": 0.1, "change_12m": 0.3, "staleness_days": 15},
    "credit_spread": {"current_bps": 380, "change_30d_bps": 0, "staleness_days": 0},
}


@patch("anthropic.Anthropic")
def test_macro_analyze_parses_valid_response(mock_cls):
    response_json = json.dumps({
        "reasoning_chain": {
            "volatility_analysis": "VIX compressing.",
            "yield_curve_analysis": "Narrowing inversion.",
            "monetary_policy_analysis": "DFF flat.",
            "inflation_labor_credit": "Sticky core, benign labor, tight credit.",
            "cross_signal_synthesis": "Aligned risk-on with inflation caveat.",
            "sector_implications": "Tech, financials OW.",
        },
        "regime": "risk-on",
        "confidence": "medium",
        "equity_outlook": "bullish",
        "regime_shift": False,
        "shift_reason": "",
        "key_observations": [{"indicator": "VIX", "reading": "19.5", "interpretation": "OK"}],
        "sector_guidance": [{"sector": "Technology", "stance": "overweight", "reason": "AI"}],
        "risk_factors": ["Core CPI sticky"],
        "position_guidance": {
            "target_invested_pct": 75.0,
            "cash_recommendation_pct": 25.0,
            "reasoning": "Hold buffer.",
        },
        "bull_triggers": ["Core CPI MoM < 0.2% for 2m"],
        "bear_triggers": ["HY OAS > 450bps"],
        "alignment_with_news": "Consistent.",
        "summary": "Moderately supportive.",
    })
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=response_json)]
    mock_resp.usage.input_tokens = 1000
    mock_resp.usage.output_tokens = 500
    mock_client.messages.create.return_value = mock_resp
    mock_cls.return_value = mock_client

    agent = MacroAnalystAgent(api_key="test", model="claude-sonnet-4-6")
    analysis, result = agent.analyze(macro_summary=MACRO_SUMMARY, universe=["SPY"])

    assert analysis is not None
    # Phase 4 #7: analyze() returns a Pydantic MacroAnalysis object.
    assert analysis.regime == "risk-on"
    assert analysis.position_guidance.target_invested_pct == 75.0
    assert analysis.bull_triggers == ["Core CPI MoM < 0.2% for 2m"]
    assert analysis.reasoning_chain.cross_signal_synthesis.startswith("Aligned")


@patch("anthropic.Anthropic")
def test_macro_analyze_heals_alias_sector(mock_cls):
    """LLM emitting 'Financials' (common alias) is auto-canonicalized to 'Financial Services'
    instead of rejecting the whole analysis."""
    response = json.dumps({
        "reasoning_chain": {
            "volatility_analysis": "a", "yield_curve_analysis": "b",
            "monetary_policy_analysis": "c", "inflation_labor_credit": "d",
            "cross_signal_synthesis": "e", "sector_implications": "f",
        },
        "regime": "risk-on",
        "confidence": "medium",
        "equity_outlook": "bullish",
        "sector_guidance": [{"sector": "Financials", "stance": "overweight", "reason": "x"}],
        "position_guidance": {
            "target_invested_pct": 60, "cash_recommendation_pct": 40, "reasoning": "y"
        },
        "summary": "z",
    })
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=response)]
    mock_resp.usage.input_tokens = 100
    mock_resp.usage.output_tokens = 50
    mock_client.messages.create.return_value = mock_resp
    mock_cls.return_value = mock_client

    agent = MacroAnalystAgent(api_key="test", model="claude-sonnet-4-6")
    analysis, _ = agent.analyze(macro_summary=MACRO_SUMMARY)

    assert analysis is not None
    assert analysis.sector_guidance[0].sector == "Financial Services"


@patch("anthropic.Anthropic")
def test_macro_analyze_passes_last_state_and_news_to_prompt(mock_cls):
    """Verify the user message includes yesterday's regime and News tracker when provided."""
    response_json = json.dumps({
        "reasoning_chain": {"volatility_analysis": "a", "yield_curve_analysis": "b",
                            "monetary_policy_analysis": "c", "inflation_labor_credit": "d",
                            "cross_signal_synthesis": "e", "sector_implications": "f"},
        "regime": "risk-on", "confidence": "medium", "equity_outlook": "bullish",
        "sector_guidance": [],
        "position_guidance": {"target_invested_pct": 60, "cash_recommendation_pct": 40, "reasoning": "y"},
        "summary": "z",
    })
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=response_json)]
    mock_resp.usage.input_tokens = 100
    mock_resp.usage.output_tokens = 50
    mock_client.messages.create.return_value = mock_resp
    mock_cls.return_value = mock_client

    agent = MacroAnalystAgent(api_key="test", model="claude-sonnet-4-6")
    agent.analyze(
        macro_summary=MACRO_SUMMARY,
        last_state={"date": "2026-04-16", "regime": "transitional", "confidence": "low",
                    "equity_outlook": "neutral", "summary": "Choppy."},
        news_narrative={"current_regime": "Transitional",
                        "era_themes": ["AI supercycle"],
                        "key_state_tracker": {"fed_policy": "On hold"}},
    )

    sent_messages = mock_client.messages.create.call_args.kwargs["messages"]
    user_msg = sent_messages[0]["content"]
    assert "transitional" in user_msg.lower() or "Choppy" in user_msg
    assert "AI supercycle" in user_msg
    assert "fed_policy" in user_msg


# ---------------------------------------------------------------------------
# Per-entry isolation for key_observations (mirrors PR #73/#74 pattern)
# ---------------------------------------------------------------------------

def _valid_macro_json() -> dict:
    return {
        "reasoning_chain": {
            "volatility_analysis": "a", "yield_curve_analysis": "b",
            "monetary_policy_analysis": "c", "inflation_labor_credit": "d",
            "cross_signal_synthesis": "e", "sector_implications": "f",
        },
        "regime": "risk-on",
        "confidence": "medium",
        "equity_outlook": "bullish",
        "regime_shift": False,
        "shift_reason": "",
        "key_observations": [],
        "sector_guidance": [],
        "risk_factors": [],
        "position_guidance": {
            "target_invested_pct": 70.0,
            "cash_recommendation_pct": 30.0,
            "reasoning": "Hold buffer.",
        },
        "bull_triggers": [],
        "bear_triggers": [],
        "alignment_with_news": "",
        "summary": "Steady.",
    }


def test_drop_invalid_key_observations_strips_missing_fields_keeps_rest():
    """A MacroObservation missing the required `interpretation` field must be
    dropped individually instead of failing the whole MacroAnalysis. Without
    this, PM gets no regime / position_guidance / sector_guidance for the
    entire morning session."""
    parsed = _valid_macro_json()
    parsed["key_observations"] = [
        {"indicator": "VIX", "reading": "19.5", "interpretation": "compressing"},
        {"indicator": "DGS10", "reading": "4.3"},  # missing interpretation
        {"indicator": "DFF", "reading": "3.6", "interpretation": "flat"},
    ]
    out = MacroAnalystAgent._drop_invalid_key_observations(parsed)
    indicators = [o["indicator"] for o in out["key_observations"]]
    assert indicators == ["VIX", "DFF"], (
        f"DGS10 (missing interpretation) must be dropped; got {indicators}"
    )


def test_macro_analysis_constructs_after_dropping_bad_observation():
    """End-to-end: with the malformed observation stripped,
    MacroAnalysis(**parsed) must succeed — preserving regime, equity_outlook,
    position_guidance for the morning PM."""
    from src.models import MacroAnalysis

    parsed = _valid_macro_json()
    parsed["key_observations"] = [
        {"indicator": "VIX", "reading": "19.5", "interpretation": "compressing"},
        {"indicator": "BAD", "reading": "x"},  # missing interpretation
    ]
    cleaned = MacroAnalystAgent._drop_invalid_key_observations(parsed)
    analysis = MacroAnalysis(**cleaned)
    assert analysis.regime == "risk-on"
    assert len(analysis.key_observations) == 1
    assert analysis.key_observations[0].indicator == "VIX"


def test_drop_invalid_key_observations_handles_non_list_shape():
    parsed = _valid_macro_json()
    parsed["key_observations"] = "oops not a list"
    out = MacroAnalystAgent._drop_invalid_key_observations(parsed)
    assert out["key_observations"] == []


def test_drop_invalid_key_observations_drops_non_dict_items():
    parsed = _valid_macro_json()
    parsed["key_observations"] = [
        {"indicator": "VIX", "reading": "19.5", "interpretation": "ok"},
        "stray string the LLM hallucinated",
        None,
        {"indicator": "DFF", "reading": "3.6", "interpretation": "flat"},
    ]
    out = MacroAnalystAgent._drop_invalid_key_observations(parsed)
    indicators = [o["indicator"] for o in out["key_observations"]]
    assert indicators == ["VIX", "DFF"]


@patch("anthropic.Anthropic")
def test_macro_analyze_survives_one_malformed_observation(mock_cls):
    """End-to-end via analyze(): a bad observation in the LLM output no longer
    fails the whole report. Regression-pin: before this fix, the entire
    MacroAnalysis was lost when a single observation row was malformed."""
    payload = _valid_macro_json()
    payload["key_observations"] = [
        {"indicator": "VIX", "reading": "19.5", "interpretation": "ok"},
        {"indicator": "BAD"},  # missing reading + interpretation
    ]
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=json.dumps(payload))]
    mock_resp.usage.input_tokens = 100
    mock_resp.usage.output_tokens = 50
    mock_client.messages.create.return_value = mock_resp
    mock_cls.return_value = mock_client

    agent = MacroAnalystAgent(api_key="test", model="claude-sonnet-4-6")
    analysis, _ = agent.analyze(macro_summary=MACRO_SUMMARY)

    assert analysis is not None, "report must survive one bad observation"
    assert analysis.regime == "risk-on"
    assert len(analysis.key_observations) == 1
    assert analysis.key_observations[0].indicator == "VIX"


# ===========================================================================
# Sanity-check tests — _apply_sanity_checks soft floors
# ===========================================================================
#
# Prompt and code share per-cadence staleness semantics (see the docstring
# on `_apply_sanity_checks`): daily series stale past 3 business days,
# monthly series (CPI/PCE, UNRATE) only once a release cycle is missed —
# BLS/BEA prints are monthly, so their staleness_days is 20-51 on
# perfectly-normal cadence. The sanity check enforces only the two most
# flagrant violations the LLM occasionally makes:
#   - confidence='high' with stale/null indicators → downgrade to 'medium'
#   - regime_shift=True with < 2 fresh indicators → clear it
# Existing tests assert confidence='medium' with stale indicators stays
# unchanged — the sanity check must not regress that.

_ALL_FRESH_MACRO = {
    "vix":           {"current": 19.5, "mean_5d": 20.1, "trend": "falling",
                      "staleness_days": 0},
    "treasury":      {"us2y": 4.5, "us10y": 4.3, "spread_2_10": -0.2,
                      "inverted": True, "staleness_days": 0},
    "fed_funds_rate": {"current": 3.60, "change_30d": 0.0, "staleness_days": 0},
    "inflation":     {"headline_cpi_yoy": 3.0, "core_cpi_yoy": 2.8,
                      "staleness_days": 1},
    "unemployment":  {"current": 4.1, "change_3m": 0.1, "staleness_days": 1},
    "credit_spread": {"current_bps": 380, "change_30d_bps": 0,
                      "staleness_days": 0},
}


def _llm_response_dict(confidence: str, regime_shift: bool, shift_reason: str = ""):
    """Build a minimal valid LLM-emitted MacroAnalysis dict with the
    confidence + regime_shift values under test. Everything else is
    canned constants the schema accepts."""
    return {
        "reasoning_chain": {
            "volatility_analysis": "VIX low.",
            "yield_curve_analysis": "Curve normalizing.",
            "monetary_policy_analysis": "DFF flat.",
            "inflation_labor_credit": "Sticky but cooling.",
            "cross_signal_synthesis": "Aligned risk-on.",
            "sector_implications": "Tech OW.",
        },
        "regime": "risk-on",
        "confidence": confidence,
        "equity_outlook": "bullish",
        "regime_shift": regime_shift,
        "shift_reason": shift_reason,
        "key_observations": [
            {"indicator": "VIX", "reading": "19.5", "interpretation": "OK"}
        ],
        "sector_guidance": [
            {"sector": "Technology", "stance": "overweight", "reason": "AI"}
        ],
        "risk_factors": ["Core CPI sticky"],
        "position_guidance": {
            "target_invested_pct": 75.0,
            "cash_recommendation_pct": 25.0,
            "reasoning": "Hold buffer.",
        },
        "bull_triggers": ["Core CPI MoM < 0.2%"],
        "bear_triggers": ["HY OAS > 450bps"],
        "alignment_with_news": "Consistent.",
        "summary": "Moderately supportive.",
    }


def _mock_macro_llm(mock_cls, response_dict: dict):
    """Wire the Anthropic mock to return the given response dict."""
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=json.dumps(response_dict))]
    mock_resp.usage.input_tokens = 1000
    mock_resp.usage.output_tokens = 500
    mock_client.messages.create.return_value = mock_resp
    mock_cls.return_value = mock_client


@patch("anthropic.Anthropic")
def test_sanity_check_keeps_high_when_monthly_staleness_is_normal_cadence(mock_cls):
    """Per-cadence semantics (trading-utility recovery): inflation 10d /
    unemployment 15d is NORMAL monthly BLS/BEA cadence — the freshest
    print that exists. Under the old flat >3d gate this fixture
    downgraded every 'high', making high macro confidence structurally
    unreachable in production (and PM's evening-tilt sizing never saw
    one). Normal monthly cadence must NOT downgrade."""
    _mock_macro_llm(mock_cls, _llm_response_dict(confidence="high", regime_shift=False))

    agent = MacroAnalystAgent(api_key="test", model="claude-sonnet-4-6")
    analysis, _ = agent.analyze(macro_summary=MACRO_SUMMARY, universe=["SPY"])

    assert analysis is not None
    assert analysis.confidence == "high", (
        "monthly indicators at normal release cadence (10d/15d) must not "
        "downgrade 'high' — that was the unreachable-high bug"
    )


@patch("anthropic.Anthropic")
def test_sanity_check_downgrades_high_when_print_is_overdue(mock_cls, caplog):
    """The staleness that IS real: a daily series whose next print is past
    due by its own cadence and publication lag. Age is not the trigger —
    `staleness_days` here is the same 2 that a perfectly healthy day
    shows; what blocks 'high' is the provider reporting the print
    OVERDUE."""
    macro = {
        **MACRO_SUMMARY,
        "vix": {**MACRO_SUMMARY["vix"], "staleness_days": 2,
                "freshness": "overdue",
                "freshness_detail": "VIXCLS: OVERDUE — next print was due 6 days ago"},
    }
    _mock_macro_llm(mock_cls, _llm_response_dict(confidence="high", regime_shift=False))

    agent = MacroAnalystAgent(api_key="test", model="claude-sonnet-4-6")
    import logging
    with caplog.at_level(logging.WARNING):
        analysis, _ = agent.analyze(macro_summary=macro, universe=["SPY"])

    assert analysis is not None
    assert analysis.confidence == "medium"
    assert any(
        "confidence='high'" in r.message and "vix" in r.message
        for r in caplog.records
    ), "downgrade must log which indicators triggered it"


@patch("anthropic.Anthropic")
def test_sanity_check_keeps_high_when_daily_print_is_legitimately_old(mock_cls):
    """A daily indicator can be several days old and still be the only
    reading that exists (a long holiday weekend, a slow FRED update). The
    old gate downgraded 'high' past 3 business days regardless; age is no
    longer a test, so a `current` reading passes at any age."""
    macro = {
        **MACRO_SUMMARY,
        "vix": {**MACRO_SUMMARY["vix"], "staleness_days": 10,
                "freshness": "current"},
    }
    _mock_macro_llm(mock_cls, _llm_response_dict(confidence="high", regime_shift=False))

    agent = MacroAnalystAgent(api_key="test", model="claude-sonnet-4-6")
    analysis, _ = agent.analyze(macro_summary=macro, universe=["SPY"])

    assert analysis is not None
    assert analysis.confidence == "high"


@patch("anthropic.Anthropic")
def test_sanity_check_keeps_high_when_monthly_print_is_old_but_current(mock_cls):
    """A monthly series 60 business days back, reported `current`, is the
    freshest CPI that exists — the old `>55` bar called that a missed
    release cycle purely on arithmetic. Only the provider's own
    cadence-and-lag derivation can say a cycle was missed, and here it
    says it wasn't."""
    macro = {
        **MACRO_SUMMARY,
        "inflation": {**MACRO_SUMMARY["inflation"], "staleness_days": 60,
                      "freshness": "current"},
    }
    _mock_macro_llm(mock_cls, _llm_response_dict(confidence="high", regime_shift=False))

    agent = MacroAnalystAgent(api_key="test", model="claude-sonnet-4-6")
    analysis, _ = agent.analyze(macro_summary=macro, universe=["SPY"])

    assert analysis is not None
    assert analysis.confidence == "high"


@patch("anthropic.Anthropic")
def test_sanity_check_downgrades_high_when_monthly_release_is_overdue(mock_cls):
    """A monthly series whose next print really did not arrive (the
    provider flags it OVERDUE) still downgrades 'high' — the half of the
    old gate that was protecting something real."""
    macro = {
        **MACRO_SUMMARY,
        "inflation": {**MACRO_SUMMARY["inflation"], "staleness_days": 60,
                      "freshness": "overdue",
                      "freshness_detail": "CPIAUCSL: OVERDUE"},
    }
    _mock_macro_llm(mock_cls, _llm_response_dict(confidence="high", regime_shift=False))

    agent = MacroAnalystAgent(api_key="test", model="claude-sonnet-4-6")
    analysis, _ = agent.analyze(macro_summary=macro, universe=["SPY"])

    assert analysis is not None
    assert analysis.confidence == "medium"


@patch("anthropic.Anthropic")
def test_sanity_check_downgrades_high_when_series_returned_no_data(mock_cls):
    """Genuinely empty data must never pass as a real reading: a series
    the provider reports `empty` blocks 'high' exactly as a missing key
    does."""
    macro = {
        **MACRO_SUMMARY,
        "credit_spread": {"current_bps": None, "change_30d_bps": None,
                          "staleness_days": None, "freshness": "empty"},
    }
    _mock_macro_llm(mock_cls, _llm_response_dict(confidence="high", regime_shift=False))

    agent = MacroAnalystAgent(api_key="test", model="claude-sonnet-4-6")
    analysis, _ = agent.analyze(macro_summary=macro, universe=["SPY"])

    assert analysis is not None
    assert analysis.confidence == "medium"


@patch("anthropic.Anthropic")
def test_sanity_check_downgrades_high_confidence_when_indicator_null(mock_cls):
    """LLM emits 'high' but VIX is missing entirely. Treated as
    not-provably-fresh → downgrade to 'medium'."""
    macro = {**MACRO_SUMMARY}
    del macro["vix"]
    _mock_macro_llm(mock_cls, _llm_response_dict(confidence="high", regime_shift=False))

    agent = MacroAnalystAgent(api_key="test", model="claude-sonnet-4-6")
    analysis, _ = agent.analyze(macro_summary=macro, universe=["SPY"])

    assert analysis is not None
    assert analysis.confidence == "medium"


@patch("anthropic.Anthropic")
def test_sanity_check_leaves_medium_confidence_unchanged_with_stale(mock_cls):
    """LLM emits 'medium' with stale inflation/unemployment. The
    sanity check only acts on 'high' violations; medium passes through
    unchanged. This pins the soft interpretation of the prompt rule —
    the literal 'ANY stale → low' would peg every session at low
    because monthly indicators are usually stale.
    """
    _mock_macro_llm(mock_cls, _llm_response_dict(confidence="medium", regime_shift=False))

    agent = MacroAnalystAgent(api_key="test", model="claude-sonnet-4-6")
    analysis, _ = agent.analyze(macro_summary=MACRO_SUMMARY, universe=["SPY"])

    assert analysis is not None
    assert analysis.confidence == "medium", (
        "medium confidence with stale indicators must NOT be modified "
        "— the sanity check only catches the 'high' violation"
    )


@patch("anthropic.Anthropic")
def test_sanity_check_keeps_high_confidence_when_all_fresh(mock_cls):
    """Happy path: all indicators have staleness_days <= 3 and LLM
    emits 'high'. Sanity check leaves it alone."""
    _mock_macro_llm(mock_cls, _llm_response_dict(confidence="high", regime_shift=False))

    agent = MacroAnalystAgent(api_key="test", model="claude-sonnet-4-6")
    analysis, _ = agent.analyze(macro_summary=_ALL_FRESH_MACRO, universe=["SPY"])

    assert analysis is not None
    assert analysis.confidence == "high", (
        "all-fresh indicators must allow high confidence to pass through"
    )


@patch("anthropic.Anthropic")
def test_sanity_check_clears_regime_shift_when_indicators_missing_or_overdue(mock_cls, caplog):
    """A flip still needs >= 2 primary indicators you actually hold. Here
    four of the six are unusable for real reasons — two returned nothing
    and two are past due — leaving one usable, so the flip is cleared.

    Note what is NOT one of those reasons any more: age. Every indicator
    in this fixture that DOES count is several days old."""
    macro = {
        **MACRO_SUMMARY,
        "treasury": {"us2y": None, "us10y": None, "staleness_days": None,
                     "freshness": "empty"},
        "fed_funds_rate": {"current": None, "change_30d": None,
                           "staleness_days": None, "freshness": "empty"},
        "credit_spread": {**MACRO_SUMMARY["credit_spread"],
                          "freshness": "overdue"},
        "inflation": {**MACRO_SUMMARY["inflation"], "freshness": "overdue"},
        "unemployment": {**MACRO_SUMMARY["unemployment"], "freshness": "overdue"},
        "vix": {**MACRO_SUMMARY["vix"], "staleness_days": 2,
                "freshness": "current"},
    }
    _mock_macro_llm(
        mock_cls,
        _llm_response_dict(
            confidence="medium", regime_shift=True,
            shift_reason="VIX jumped from 17 to 23",
        ),
    )

    agent = MacroAnalystAgent(api_key="test", model="claude-sonnet-4-6")
    import logging
    with caplog.at_level(logging.WARNING):
        analysis, _ = agent.analyze(macro_summary=macro, universe=["SPY"])

    assert analysis is not None
    assert analysis.regime_shift is False, (
        "regime_shift=True must be cleared when < 2 indicators carry a "
        "usable latest reading"
    )
    assert analysis.shift_reason == "", (
        "shift_reason must also be cleared so PM doesn't read a flip "
        "narrative built on data we don't have"
    )
    assert any(
        "regime_shift=True" in r.message and "usable" in r.message
        for r in caplog.records
    ), "clear must log the gate that fired"


@patch("anthropic.Anthropic")
def test_sanity_check_keeps_regime_shift_with_two_fresh_indicators(mock_cls):
    """LLM declares regime_shift=True with 2+ fresh indicators in
    MACRO_SUMMARY (vix=0, treasury=0, fed_funds_rate=0, credit_spread=0
    — 4 fresh, well above the >= 2 threshold). Pass through."""
    _mock_macro_llm(
        mock_cls,
        _llm_response_dict(
            confidence="medium", regime_shift=True,
            shift_reason="VIX + HY both jumped today",
        ),
    )

    agent = MacroAnalystAgent(api_key="test", model="claude-sonnet-4-6")
    analysis, _ = agent.analyze(macro_summary=MACRO_SUMMARY, universe=["SPY"])

    assert analysis is not None
    assert analysis.regime_shift is True
    assert analysis.shift_reason == "VIX + HY both jumped today"


@patch("anthropic.Anthropic")
def test_regime_shift_survives_real_fred_publication_lag(mock_cls):
    """THE REGRESSION TEST for the defect this replaced (docs/WORK.md data
    quality audit item 6).

    The old gate required `staleness_days <= 1` on 2+ of the six primary
    indicators. FRED's real publication lag on its daily series is about 2
    business days: six production checkpoints (data/checkpoints/*-morning.json,
    2026-08-18..2026-08-27) show treasury and fed_funds_rate at
    `staleness_days=2` in 6/6 samples, never 1, and a live check against
    FRED's public fredgraph.csv endpoint on 2026-09-03 confirmed DGS10,
    DGS2, DFF, VIXCLS and BAMLH0A0HYM2 were ALL sitting at a real 2-day
    lag at query time. So the bar demanded a print that does not exist,
    and it cleared the seat's regime call on 14 of 27 retained production
    runs (52%, 2026-08-17..09-02).

    The fixture below is that ordinary day: every daily indicator at its
    normal 2-business-day lag, each one the latest published reading FRED
    has. The flip must now STAND. This test is the inverse of the one it
    replaces (`test_sanity_check_clears_regime_shift_under_realistic_fred_lag`),
    which pinned the defect deliberately while the replacement was an open
    owner decision.
    """
    ordinary_day = {
        key: {**value, "staleness_days": 2, "freshness": "current"}
        for key, value in MACRO_SUMMARY.items()
    }
    _mock_macro_llm(
        mock_cls,
        _llm_response_dict(
            confidence="medium", regime_shift=True,
            shift_reason="Curve steepened and credit tightened together",
        ),
    )

    agent = MacroAnalystAgent(api_key="test", model="claude-sonnet-4-6")
    analysis, _ = agent.analyze(macro_summary=ordinary_day, universe=["SPY"])

    assert analysis is not None
    assert analysis.regime_shift is True, (
        "a normal FRED day — every indicator at its real ~2-business-day "
        "publication lag and each one the latest print that exists — must "
        "NOT clear a regime shift; that was the 52%-of-runs defect"
    )
    assert analysis.shift_reason == (
        "Curve steepened and credit tightened together"
    )


@patch("anthropic.Anthropic")
def test_regime_shift_gate_reachable_when_freshness_unverified(mock_cls):
    """When FRED's release metadata does not come back, freshness is
    `unknown`: the reading we hold is still FRED's latest published
    observation (the fetch sets no `observation_end`), only the
    overdue-check is unverifiable. Unknown must therefore stay USABLE —
    refusing to act on unverifiable metadata is precisely how the old gate
    became unreachable."""
    unverified = {
        key: {**value, "freshness": "unknown"}
        for key, value in MACRO_SUMMARY.items()
    }
    _mock_macro_llm(
        mock_cls,
        _llm_response_dict(
            confidence="medium", regime_shift=True,
            shift_reason="HY OAS widened 40bps",
        ),
    )

    agent = MacroAnalystAgent(api_key="test", model="claude-sonnet-4-6")
    analysis, _ = agent.analyze(macro_summary=unverified, universe=["SPY"])

    assert analysis is not None
    assert analysis.regime_shift is True


def test_prompt_carries_no_calendar_day_freshness_threshold():
    """The prompt and the code must not drift back to a day count. The
    three thresholds the old design used (>3 daily, >55 monthly, <=1 for a
    regime shift) are gone from both sides; what replaced them is the
    latest-published / overdue language asserted below."""
    from src.agents.macro_analyst import PROMPT_PATH
    text = PROMPT_PATH.read_text()
    for gone in (
        "staleness_days > 7", "staleness_days > 3", "staleness_days > 55",
        "staleness_days ≤ 1", "staleness_days <= 1",
    ):
        assert gone not in text, (
            f"{gone!r} must not reappear — macro freshness is no longer a "
            f"calendar-day test (see src/data/macro.py::SeriesFreshness)"
        )
    assert "latest published reading" in text
    assert "OVERDUE" in text
