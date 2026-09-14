"""ops/model_policy/scenarios.py seat exams: fixture admissibility,
re-derivation by today's live code, and grader discrimination.

Offline: no network, no LLM calls. Every `invoke` in this file is bypassed —
these tests exercise the data-rebuild functions the invokes call, and the
`grade` functions directly, never `agent.analyze` / `agent.analyze_batch`.
"""
from __future__ import annotations

import json

import pytest

from ops.model_policy import fixture_policy as fp
from ops.model_policy import scenarios as sc

RUNNABLE = ["earnings_filing", "smart_money_form4", "tech_batch", "macro_stress", "news_intel"]
QUARANTINED = ["pm_selection"]


# --- quarantine list, from the scenario registry's own point of view -------

@pytest.mark.parametrize("key", RUNNABLE)
def test_runnable_scenario_is_not_refused(key):
    scen = sc.SCENARIOS_BY_KEY[key]
    assert sc.refusal_reason(scen) is None


@pytest.mark.parametrize("key", QUARANTINED)
def test_quarantined_scenario_is_refused(key):
    scen = sc.SCENARIOS_BY_KEY[key]
    reason = sc.refusal_reason(scen)
    assert reason is not None
    assert key in reason or "QUARANTINED" in reason


def test_scenario_with_no_fixture_and_no_blocked_reason_would_be_a_policy_hole():
    """Every scenario must declare its own admissibility one way or the
    other — a scenario with neither `fixture` nor `blocked_reason` bypasses
    `refusal_reason` entirely and would run unchecked. Catches that
    regression for every entry in the registry at once, present and future.
    """
    unions = {"pm_constrained", "pm_production_scale"}  # named, owner-flagged exception (scenarios.py "Not done")
    for scen in sc.SCENARIOS:
        if scen.key in unions:
            continue
        assert scen.fixture or scen.blocked_reason, (
            f"{scen.key} has neither `fixture` nor `blocked_reason` — "
            f"refusal_reason() would silently admit it"
        )


# --- re-derivation: raw fixture facts -> today's live code -----------------

def test_tech_fixture_bars_are_recomputed_by_todays_indicators():
    data = sc.tech_exam_symbols_data()
    expected = set(json.loads(
        (fp.FIXTURES_DIR / sc._TECH_FIXTURE).read_text()
    )["symbols"])
    assert {d["symbol"] for d in data} == expected
    for row in data:
        assert row["indicators"] is not None
        # `indicators` is computed HERE, by today's compute_indicators, from
        # the pinned raw bars — never itself stored in the fixture manifest
        # (fixture_policy.OLD_CODE_DERIVED_KEYS would refuse that).
        assert row["bars"], f"{row['symbol']} has no raw bars to derive from"


def test_macro_fixture_series_are_recomputed_by_todays_provider():
    summary = sc._public_macro_summary()
    # Every top-level key get_macro_summary() promises, all computed just
    # now from the pinned raw FRED observations (fixture_policy would
    # refuse a stored `macro_summary` key outright).
    for key in ("vix", "treasury", "fed_funds_rate", "inflation", "unemployment",
                "credit_spread", "real_rates", "dollar_index", "ig_credit_spread",
                "jobless_claims"):
        assert key in summary
    assert summary["vix"]["current"] is not None
    assert summary["vix"]["freshness"] in ("current", "overdue", "unknown", "empty")


def test_news_fixture_bytes_are_reparsed_by_todays_provider():
    news_text, stock_mentions, coverage = sc._public_news_report()
    manifest = json.loads((fp.FIXTURES_DIR / sc._NEWS_FIXTURE).read_text())
    assert coverage.succeeded == len(manifest["rss_feeds"])
    assert coverage.failed == []
    assert isinstance(news_text, str) and len(news_text) > 0
    assert isinstance(stock_mentions, dict)
    # Every mentioned symbol must be one of the exam's own universe —
    # tag_symbol_mentions filters to the universe it is given.
    assert set(stock_mentions) <= set(sc._PUBLIC_NEWS_UNIVERSE)


def test_earnings_fixture_text_is_extracted_by_todays_provider():
    report = sc.earnings_exam_report()
    assert report.symbol == "MRVL"
    assert report.text_excerpt  # extracted just now from the pinned HTML blob
    assert report.xbrl_facts  # computed just now from the pinned companyfacts blob


def test_smart_money_fixture_is_reclassified_by_todays_provider():
    observations = sc.smart_money_exam_observations()
    assert observations, "today's SECForm4Provider found no observations in the pinned submissions"


# --- grader discrimination: None fails, a minimally-valid object passes ----

def test_tech_grader_fails_closed_on_empty_dict():
    checks = sc._tech_grade({})
    assert not checks[0].passed  # all_symbols_resolved: {} != expected non-empty set


def test_macro_grader_discriminates_none_vs_valid():
    from src.models import MacroAnalysis

    assert not sc._public_macro_grade(None)[0].passed

    # Reuse the already-validated MacroAnalysis fixture `pm_production_scale`
    # is built on (tests/test_benchmark_budget.py::
    # test_pm_production_macro_fixture_validates pins its shape) rather than
    # hand-rolling another one against this schema's many required fields;
    # it just needs `sector_guidance` added (that fixture predates this
    # grader and has none).
    payload = {
        **sc._PM_PRODUCTION_MACRO,
        "sector_guidance": [
            {"sector": "Technology", "stance": "overweight", "reason": "r1"},
            {"sector": "Energy", "stance": "underweight", "reason": "r2"},
        ],
    }
    valid = MacroAnalysis.model_validate(payload)
    checks = sc._public_macro_grade(valid)
    assert all(c.passed for c in checks), checks


def test_news_grader_discriminates_none_vs_valid_and_catches_invented_tickers():
    from src.models import MacroNarrative, NewsIntelligenceReport, StockNewsItem

    assert not sc._public_news_grade(None)[0].passed

    valid = NewsIntelligenceReport(
        macro_narrative=MacroNarrative(
            last_updated="2026-09-14", era_themes=["theme"],
            current_regime="risk-on, broad based",
        ),
        stock_news={"AAPL": [StockNewsItem(
            headline="h", sentiment="bullish", conviction="medium",
            impact_summary="s",
        )]},
        pm_briefing="briefing", market_sentiment="bullish", confidence="medium",
    )
    checks = sc._public_news_grade(valid)
    by_name = {c.name: c for c in checks}
    assert by_name["parsed"].passed
    assert by_name["no_invented_stock_news_symbols"].passed  # AAPL is in the universe

    invented = valid.model_copy(update={"stock_news": {"ZZZZ_NOT_REAL": []}})
    bad_checks = {c.name: c for c in sc._public_news_grade(invented)}
    assert not bad_checks["no_invented_stock_news_symbols"].passed
