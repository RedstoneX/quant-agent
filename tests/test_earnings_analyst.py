import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.agents.base import AgentResult
from src.agents.earnings_analyst import EarningsAnalystAgent
from src.data.earnings import EarningsReport


def _valid_analysis(report: EarningsReport) -> dict:
    return {
        "symbol": report.symbol,
        "form_type": report.form_type,
        "filing_date": report.filing_date,
        "revenue": {
            "total": "$10.0 billion",
            "yoy_growth": "+5%",
            "segments": [{"name": "Core", "revenue": "$8.0 billion", "growth": "+4%"}],
        },
        "profitability": {
            "gross_margin": "45%",
            "operating_margin": "20%",
            "net_income": "$2.0 billion",
            "eps": "$1.00 diluted",
        },
        "cash_flow": {
            "operating_cf": "$3.0 billion",
            "free_cf": "$2.5 billion",
            "capex": "$0.5 billion",
        },
        "balance_sheet": {
            "cash_and_equivalents": "$4.0 billion",
            "total_debt": "$1.0 billion",
            "assessment": "Healthy balance sheet",
        },
        "management_highlights": ["Demand remained stable across core products"],
        "guidance": "Management did not provide numeric guidance",
        "strategic_direction": {
            "key_initiatives": ["Expanding into cloud services"],
            "capital_allocation": "50% buybacks, 30% R&D, 20% debt reduction",
            "competitive_positioning": "Market leader with 35% share in core segment",
        },
        "risk_flags": {
            "strategic_risks": ["Cloud expansion faces entrenched competitors"],
            "operational_risks": ["FX volatility remains a headwind"],
        },
        "strategy_consistency": "Consistent with prior quarter — cloud expansion on track",
        "investment_implications": {
            "sentiment": "bullish",
            "conviction": "medium",
            "reasoning_chain": {
                "fundamental_quality": "Revenue +5% with margin expansion",
                "growth_trajectory": "Operating leverage building QoQ",
                "strategic_risks": "Cloud competition is real but execution on track",
                "management_execution": "Guidance hit, capex on plan",
                "valuation_context": "Trades at a reasonable forward multiple",
            },
            "key_thesis": "Margins expanded while demand remained resilient",
            "bull_case": "Operating leverage continues",
            "bear_case": "FX pressure worsens",
        },
        "data_quality": "Filing text complete through the financial statements and MD&A.",
    }


@pytest.fixture
def agent():
    with patch("anthropic.Anthropic"):
        yield EarningsAnalystAgent(api_key="test-key", model="claude-sonnet-4-6-20250514")


@pytest.fixture
def report(tmp_path):
    return EarningsReport(
        symbol="AAPL",
        form_type="10-Q",
        filing_date="2026-03-15",
        filing_path=str(tmp_path / "10-Q.html"),
        analysis_path=str(tmp_path / "AAPL" / "analysis_10-Q_2026-03-15.md"),
        text_excerpt="Revenue was $10.0 billion and operating cash flow was $3.0 billion.",
        is_new=True,
    )













def test_earnings_analyst_accepts_valid_analysis(agent, report):
    agent.run = MagicMock(
        return_value=AgentResult(
            raw_text=json.dumps(_valid_analysis(report)),
            tokens_used=123,
            model="test-model",
        )
    )

    analysis, _ = agent._analyze_new(report)

    assert analysis is not None
    assert analysis["symbol"] == "AAPL"
    assert analysis["investment_implications"]["sentiment"] == "bullish"


def test_analyze_reports_wrapper_carries_analysis_path_to_full_extraction(agent, report):
    """Item 18 (2026-09-04): PM's prompt now gets a short verdict, not this
    whole report — but the full 8-field extraction must still be computed,
    saved to disk, and reachable via a pointer in the wrapper dict the
    pipeline hands to `PortfolioManagerAgent.build_user_message`. This is
    that plumbing: `analysis_path` on the wrapper must point at a real file
    that, when read back, still carries every one of the eight extraction
    fields — nothing was dropped, only what reaches PM's prompt changed.
    """
    agent.run = MagicMock(
        return_value=AgentResult(
            raw_text=json.dumps(_valid_analysis(report)),
            tokens_used=123,
            model="test-model",
        )
    )

    results = agent.analyze_reports([report])

    assert len(results) == 1
    wrapper = results[0]
    assert wrapper["analysis_path"] == report.analysis_path
    assert Path(wrapper["analysis_path"]).exists()

    on_disk = json.loads(
        Path(wrapper["analysis_path"]).read_text().split("```json\n")[1].split("\n```")[0]
    )
    for field in (
        "revenue", "profitability", "cash_flow", "balance_sheet",
        "strategic_direction", "risk_flags", "strategy_consistency",
        "data_quality",
    ):
        assert field in on_disk, f"full extraction lost field {field!r} on disk"


def test_existing_analysis_wrapper_also_carries_analysis_path(agent, report):
    """Same pointer, for the cached (not-new-filing) branch of `_analyze_one`
    — a symbol re-served from a prior session's cache still needs to be
    locatable by PM's short-verdict pointer, not only a freshly analyzed one.
    """
    report.is_new = False
    analysis_path = Path(report.analysis_path)
    analysis_path.parent.mkdir(parents=True, exist_ok=True)
    analysis_path.write_text(
        "# Cached\n\n```json\n" + json.dumps(_valid_analysis(report), indent=2) + "\n```\n"
    )

    results = agent.analyze_reports([report])

    assert len(results) == 1
    assert results[0]["analysis_path"] == report.analysis_path
    assert results[0]["is_new"] is False


def test_analyze_reports_carries_xbrl_facts_through_to_the_wrapper(agent, report):
    """The XBRL cross-check (`_classify_earnings_status`, src/pipeline_stages.py)
    reads `xbrl_facts` off the wrapper dict `analyze_reports` returns — it
    must actually be there, both for a freshly analyzed filing and for one
    served from the cache, not just present on the `EarningsReport` and
    silently dropped on the way to the results list."""
    report.xbrl_facts = {"revenue": 50_000_000_000.0, "net_income": 5_000_000_000.0}
    agent.run = MagicMock(
        return_value=AgentResult(
            raw_text=json.dumps(_valid_analysis(report)),
            tokens_used=123,
            model="test-model",
        )
    )

    results = agent.analyze_reports([report])

    assert len(results) == 1
    assert results[0]["xbrl_facts"] == {"revenue": 50_000_000_000.0, "net_income": 5_000_000_000.0}


def test_analyze_reports_xbrl_facts_defaults_empty_when_none_available(agent, report):
    """The ordinary case: no XBRL data was available for this filer/period
    (`_fetch_xbrl_raw` fails open to `{}`) — the wrapper must carry that
    through as `{}`, which the cross-check already reads as "nothing to
    compare," not as a mismatch."""
    agent.run = MagicMock(
        return_value=AgentResult(
            raw_text=json.dumps(_valid_analysis(report)),
            tokens_used=123,
            model="test-model",
        )
    )

    results = agent.analyze_reports([report])

    assert results[0]["xbrl_facts"] == {}


def test_earnings_analyst_rejects_metadata_mismatch(agent, report):
    bad = _valid_analysis(report)
    bad["symbol"] = "TSLA"
    agent.run = MagicMock(
        return_value=AgentResult(
            raw_text=json.dumps(bad),
            tokens_used=123,
            model="test-model",
        )
    )

    analysis, _ = agent._analyze_new(report)

    assert analysis is None


def test_earnings_analyst_rejects_invalid_cached_analysis(agent, report):
    bad = _valid_analysis(report)
    bad["filing_date"] = "2026-03-16"

    analysis_path = Path(report.analysis_path)
    analysis_path.parent.mkdir(parents=True, exist_ok=True)
    analysis_path.write_text(
        "# Cached analysis\n\n```json\n" + json.dumps(bad, indent=2) + "\n```\n"
    )

    assert agent._load_analysis(report) is None


# ===========================================================================
# Unsourced valuation claims — detection must actually close the loop, not
# just log it. `_flag_unsourced_valuation_claims` on its own is exercised in
# tests/test_agent_audit_2026_08_14.py; these cover the caller
# (`_validate_analysis`) that redacts and, for a cached hit, self-heals disk.
# ===========================================================================

def test_fresh_analysis_with_fabricated_valuation_is_redacted(agent, report):
    """Reproduces the live KO/MTZ shape: the model states a P/E or market cap
    despite being given filing text only. A fresh (source='llm') analysis
    must come back with the claim redacted, not passed through to the PM."""
    bad = _valid_analysis(report)
    bad["investment_implications"]["reasoning_chain"]["valuation_context"] = (
        "The filing provides no information on valuation multiples or market "
        "capitalization. The financial performance is strong, suggesting a "
        "reasonable P/E if sustained."
    )
    agent.run = MagicMock(
        return_value=AgentResult(
            raw_text=json.dumps(bad), tokens_used=123, model="test-model",
        )
    )

    analysis, _ = agent._analyze_new(report)

    assert analysis is not None, "a flagged claim must redact, never discard the analysis"
    vc = analysis["investment_implications"]["reasoning_chain"]["valuation_context"]
    assert "reasonable P/E if sustained" not in vc
    assert "removed" in vc.lower()
    # Everything else must be untouched — this is a redaction, not a rejection.
    assert analysis["investment_implications"]["sentiment"] == "bullish"
    assert analysis["revenue"]["total"] == "$10.0 billion"


def test_cached_analysis_with_fabricated_valuation_self_heals(agent, report):
    """The bug that actually shipped: KO and MTZ's cached analyses asserted a
    P/E or market cap on every run for days, because the old code only
    logged the detection and never touched the cache file. Loading a dirty
    cached analysis must both redact what's returned this run AND rewrite
    the cache file so the SAME run doesn't re-trigger the warning forever."""
    dirty = _valid_analysis(report)
    dirty["investment_implications"]["reasoning_chain"]["valuation_context"] = (
        "Trading at a market cap that looks rich versus peers given the "
        "growth profile disclosed."
    )
    analysis_path = Path(report.analysis_path)
    analysis_path.parent.mkdir(parents=True, exist_ok=True)
    analysis_path.write_text(
        "# Cached analysis\n\n```json\n" + json.dumps(dirty, indent=2) + "\n```\n"
    )

    loaded = agent._load_analysis(report)
    assert loaded is not None
    vc = loaded["investment_implications"]["reasoning_chain"]["valuation_context"]
    assert "market cap that looks rich" not in vc
    assert "removed" in vc.lower()

    # The cache file on disk must now be the redacted version too — a second
    # load must not re-flag the ORIGINAL claim, because it's gone from disk.
    reloaded_raw = analysis_path.read_text()
    assert "market cap that looks rich" not in reloaded_raw
    second_load = agent._load_analysis(report)
    assert second_load is not None
    vc2 = second_load["investment_implications"]["reasoning_chain"]["valuation_context"]
    assert "market cap that looks rich" not in vc2
    assert vc2 == vc, "second load of the self-healed cache must be stable, not re-redacted differently"


# ===========================================================================
# Atomic write tests — _save_analysis must not leave a half-written .md
# ===========================================================================

def test_save_analysis_writes_atomically_via_tmp_rename(agent, report, tmp_path):
    """The save path must use tmp+rename so a SIGKILL mid-write can never
    leave a half-written markdown that _load_analysis would treat as a
    corrupt cache → record_failure() → permanent abandonment after 3 ticks.

    Verifies: after _save_analysis completes, the target file exists and
    contains the expected content. A direct write_text would also pass
    this test — so we additionally verify the .tmp file is cleaned up.
    """
    final_path = tmp_path / "analysis_10-Q_2026-03-15.md"
    valid = _valid_analysis(report)

    agent._save_analysis(str(final_path), report, valid)

    assert final_path.exists()
    assert final_path.with_suffix(final_path.suffix + ".tmp").exists() is False, (
        "tmp file must be renamed away; leftover .tmp suggests non-atomic write"
    )
    body = final_path.read_text()
    assert "# AAPL 10-Q Analysis (2026-03-15)" in body
    assert "Sentiment: bullish" in body
    assert "```json" in body


def test_save_analysis_cleans_tmp_on_rename_failure(agent, report, tmp_path, monkeypatch):
    """If os.replace fails (disk full, permissions, racing rmdir), the
    tmp file must be cleaned up and the exception re-raised — never
    leave both the tmp file AND no canonical file on disk where the
    next session's manifest sync would fall into an ambiguous state.
    """
    final_path = tmp_path / "analysis_10-Q_2026-03-15.md"
    valid = _valid_analysis(report)

    real_replace = __import__("os").replace

    def boom(_src, _dst):
        raise OSError("simulated disk full")

    monkeypatch.setattr("src.agents.earnings_analyst.os.replace", boom)

    with pytest.raises(OSError, match="simulated disk full"):
        agent._save_analysis(str(final_path), report, valid)

    assert final_path.exists() is False, "final file must NOT be created on rename failure"
    assert final_path.with_suffix(final_path.suffix + ".tmp").exists() is False, (
        "tmp file must be cleaned up on failure; leftover tmp would confuse next run"
    )


# ===========================================================================
# XBRL structured financial facts — replaces the fragile text-regex matcher
# for the NUMBERS half of the filing. Root cause: `_extract_key_sections`
# recovered as little as 965 chars from a 184,000-char filing on ~20 of 67
# production filings (12 including MSFT/AAPL/GOOGL/BAC/CVX/NFLX extracted
# ZERO figures). This fetches the same numbers from SEC's structured XBRL
# API instead, independent of whether the text matcher succeeds.
# ===========================================================================

def _companyfacts(concept_entries: dict) -> bytes:
    """Build a minimal SEC companyfacts payload. `concept_entries` maps
    concept name -> unit key -> list of {end, val, form} dicts."""
    facts = {}
    for concept, units in concept_entries.items():
        facts[concept] = {"units": units}
    return json.dumps({"facts": {"us-gaap": facts}}).encode()


def test_xbrl_facts_picks_the_period_matching_the_filing(tmp_path, monkeypatch):
    """Basic case: one concept, one value, matching period."""
    from src.data.earnings import EarningsDataProvider

    provider = EarningsDataProvider(data_dir=str(tmp_path))
    monkeypatch.setattr(
        provider, "_sec_get",
        lambda url: _companyfacts({
            "NetIncomeLoss": {"USD": [
                {"end": "2026-03-31", "val": 8584000000, "form": "10-Q"},
            ]},
        }),
    )

    out = provider._get_xbrl_financial_facts("70858", "BAC", "2026-04-25")

    assert "Net Income: $8,584,000,000 (period ending 2026-03-31)" in out
    assert "STRUCTURED FINANCIAL FACTS" in out


def test_xbrl_facts_prefers_the_fresher_concept_over_a_stale_one(tmp_path, monkeypatch):
    """The real bug caught in manual testing: MSFT/AAPL have OLD entries
    under the `Revenues` tag (some from 2010/2018 — the tag was abandoned
    around ASC 606 adoption) and CURRENT entries under
    `RevenueFromContractWithCustomerExcludingAssessedTax`. Trying concepts
    in order and stopping at the first with ANY data picked the decade-old
    number. Must pick the freshest value across ALL given concept names."""
    from src.data.earnings import EarningsDataProvider

    provider = EarningsDataProvider(data_dir=str(tmp_path))
    monkeypatch.setattr(
        provider, "_sec_get",
        lambda url: _companyfacts({
            "Revenues": {"USD": [
                {"end": "2010-12-31", "val": 19953000000, "form": "10-Q"},
            ]},
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"USD": [
                {"end": "2026-03-31", "val": 82886000000, "form": "10-Q"},
            ]},
        }),
    )

    out = provider._get_xbrl_financial_facts("789019", "MSFT", "2026-04-30")

    assert "Total Revenue: $82,886,000,000 (period ending 2026-03-31)" in out
    assert "19,953,000,000" not in out
    assert "2010-12-31" not in out


def test_xbrl_facts_drops_a_field_stale_beyond_the_staleness_window(tmp_path, monkeypatch):
    """The second bug caught in manual testing: BAC's cash tag, CVX's
    long-term-debt tag, and NFLX's gross-profit tag were each years stale
    with no current alternative concept tried. A number that old is worse
    than no number — it's the exact 'PM sizes off an ungrounded field'
    failure this whole fix exists to close, just relocated from text
    extraction into XBRL. Must be omitted entirely, not shown as current."""
    from src.data.earnings import EarningsDataProvider

    provider = EarningsDataProvider(data_dir=str(tmp_path))
    monkeypatch.setattr(
        provider, "_sec_get",
        lambda url: _companyfacts({
            "NetIncomeLoss": {"USD": [
                {"end": "2026-03-31", "val": 2210000000, "form": "10-Q"},
            ]},
            "LongTermDebtNoncurrent": {"USD": [
                {"end": "2018-09-30", "val": 29854000000, "form": "10-K"},
            ]},
        }),
    )

    out = provider._get_xbrl_financial_facts("93410", "CVX", "2026-04-25")

    assert "Net Income: $2,210,000,000 (period ending 2026-03-31)" in out
    assert "Long-Term Debt" not in out
    assert "29,854,000,000" not in out


def test_xbrl_facts_fails_open_on_network_error(tmp_path, monkeypatch):
    """A SEC API hiccup must degrade to empty string, not crash the whole
    earnings check — the caller falls back to text-only extraction exactly
    as it did before this existed."""
    from src.data.earnings import EarningsDataProvider

    provider = EarningsDataProvider(data_dir=str(tmp_path))

    def boom(url):
        raise TimeoutError("SEC is down")
    monkeypatch.setattr(provider, "_sec_get", boom)

    out = provider._get_xbrl_financial_facts("789019", "MSFT", "2026-04-30")

    assert out == ""


def test_xbrl_facts_gets_prepended_to_extracted_text(tmp_path, monkeypatch):
    """End-to-end wiring: `_check_symbol` must actually attach the XBRL
    block to `text_excerpt`, not just have the method exist unused.

    Mocks `_fetch_xbrl_raw` — the one real fetch `_check_symbol` now makes
    (see its docstring: `_get_xbrl_financial_facts` and
    `_xbrl_comparable_values` both derive from this single call so a filing
    isn't fetched twice) — rather than `_get_xbrl_financial_facts` directly,
    so this test isn't silently making a real SEC network call while
    believing it's exercising the mock."""
    from src.data.earnings import EarningsDataProvider, FilingInfo

    provider = EarningsDataProvider(data_dir=str(tmp_path))
    monkeypatch.setattr(provider, "_get_cik", lambda ticker: "789019")
    monkeypatch.setattr(
        provider, "_get_recent_filings",
        lambda cik, ticker: [
            FilingInfo(
                symbol=ticker, form_type="10-Q", filing_date="2026-04-30",
                accession_number="0000789019-26-000001", primary_doc="doc.htm",
            ),
        ],
    )
    local_html = tmp_path / "filing.html"
    local_html.write_text("<html><body>Some filing text with no clean sections.</body></html>")
    monkeypatch.setattr(provider, "_download_filing", lambda cik, filing: str(local_html))
    monkeypatch.setattr(
        provider, "_fetch_xbrl_raw",
        lambda cik, ticker, filing_date: {"net_income": (31_778_000_000.0, "2026-03-31")},
    )

    report = provider._check_symbol("MSFT")

    assert report is not None
    assert "STRUCTURED FINANCIAL FACTS" in report.text_excerpt
    assert "Net Income: $31,778,000,000" in report.text_excerpt
    # And the same fetch's parsed value reaches the comparable-values dict
    # the XBRL cross-check (src/pipeline_stages.py) reads.
    assert report.xbrl_facts == {"net_income": 31_778_000_000.0}


def test_xbrl_comparable_values_keeps_only_the_one_to_one_fields(tmp_path):
    """`_xbrl_comparable_values` must expose ONLY the concepts that map
    one-to-one onto an `EarningsAnalysis` field (revenue, net_income, cash,
    eps) — not gross_profit/operating_income (the analyst reports MARGINS,
    a ratio it computes itself, not these raw dollar figures) and not
    long_term_debt (XBRL here is non-current debt only, while
    `total_debt` conventionally includes the current portion — comparing
    them would flag a definitional gap as a factual error)."""
    from src.data.earnings import EarningsDataProvider

    provider = EarningsDataProvider(data_dir=str(tmp_path))
    raw = {
        "revenue": (50_000_000_000.0, "2026-03-31"),
        "net_income": (5_000_000_000.0, "2026-03-31"),
        "gross_profit": (20_000_000_000.0, "2026-03-31"),
        "operating_income": (10_000_000_000.0, "2026-03-31"),
        "assets": (300_000_000_000.0, "2026-03-31"),
        "cash": (10_000_000_000.0, "2026-03-31"),
        "long_term_debt": (40_000_000_000.0, "2026-03-31"),
        "eps": (2.5, "2026-03-31"),
    }

    values = provider._xbrl_comparable_values(raw)

    assert values == {
        "revenue": 50_000_000_000.0,
        "net_income": 5_000_000_000.0,
        "cash": 10_000_000_000.0,
        "eps": 2.5,
    }


def test_xbrl_comparable_values_empty_when_nothing_fetched(tmp_path):
    """`_fetch_xbrl_raw` fails open to `{}` (no XBRL data for this
    filer/period) — `_xbrl_comparable_values` must pass that straight
    through as `{}`, not error or invent a value."""
    from src.data.earnings import EarningsDataProvider

    provider = EarningsDataProvider(data_dir=str(tmp_path))

    assert provider._xbrl_comparable_values({}) == {}




