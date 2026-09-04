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


def _statement_rows(n: int = 60) -> str:
    """A statements section's worth of line items.

    Real condensed financial statements carry hundreds of figures across the
    operations, balance-sheet and cash-flow tables. The fixture needs that
    density to represent what it claims to be: `_extract_text` now requires
    genuine financial content before it will accept a structured extraction,
    precisely because a few thousand words of prose containing the phrase
    "financial statements" is what the auditor's opinion letter looks like.
    """
    return " ".join(
        f"Line item {i}: ${1000 + i * 137:,} versus ${900 + i * 131:,} "
        f"({100 + i:,}) change {i * 3:,}."
        for i in range(n)
    )

def test_extract_text_compresses_standard_10q(tmp_path):
    """Full 10-Q with TOC + all standard sections → structured extraction path."""
    from src.data.earnings import EarningsDataProvider

    filler = "Lorem ipsum dolor sit amet consectetur. " * 50  # ~2000 chars
    html = f"""<html><body>
    <h1>Apple Inc. Q1 2026 Form 10-Q</h1>
    <div>Table of Contents: Item 1. Financial Statements ... Item 2. Management's Discussion and Analysis ... Item 1A. Risk Factors ...</div>
    {"Cover page filler filler filler. " * 400}
    <h2>CONDENSED CONSOLIDATED STATEMENTS OF OPERATIONS</h2>
    <p>Net sales: Products $113,743 Services $26,340. Total $140,083.
    Operating income $42,832. Diluted EPS $2.40. {_statement_rows()} {filler}</p>
    <h2>Item 2. Management's Discussion and Analysis of Financial Condition</h2>
    <p>Products revenue grew 8% YoY driven by iPhone. Services grew 14%.
    Gross margin expanded 120bps to 46.9%. Guidance implies mid-single-digit
    revenue growth in Q2. {filler}</p>
    <h2>Item 1A. Risk Factors</h2>
    <p>There have been no material changes to the risk factors disclosed in
    our 2025 Form 10-K. {filler}</p>
    </body></html>"""
    html_path = tmp_path / "test.html"
    html_path.write_bytes(html.encode())

    p = EarningsDataProvider(data_dir=str(tmp_path / "earnings"))
    out = p._extract_text(str(html_path), max_chars=30000)
    assert "=== FINANCIAL STATEMENTS ===" in out
    assert "=== MDNA ===" in out
    assert "=== RISK FACTORS ===" in out
    assert "113,743" in out
    assert "iPhone" in out
    assert len(out) < len(html) / 2


def test_extract_text_handles_smart_apostrophe(tmp_path):
    """SEC filings commonly use the curly apostrophe U+2019 — regex must match it."""
    from src.data.earnings import EarningsDataProvider

    html = (
        "<html><body>"
        + ("x " * 8000)  # push past TOC threshold
        + "\nItem 2.\nManagement\u2019s Discussion and Analysis\n"
        + "<p>Revenue up 10%. " + ("Lorem ipsum. " * 200) + "</p>"
        + "\nItem 3. Quantitative disclosures\n"
        + "</body></html>"
    )
    html_path = tmp_path / "curly.html"
    html_path.write_bytes(html.encode())

    p = EarningsDataProvider(data_dir=str(tmp_path / "earnings"))
    out = p._extract_text(str(html_path))
    # Whether structured or fallback path fires, the key content must be there.
    assert "Revenue up 10%" in out


def test_extract_text_falls_back_to_truncated_when_sections_sparse(tmp_path):
    """A filing with no recognizable section headers → fallback truncated text."""
    from src.data.earnings import EarningsDataProvider

    raw = "This is a filing without standard section markers. " * 4000
    html_path = tmp_path / "nohdr.html"
    html_path.write_bytes(f"<html><body>{raw}</body></html>".encode())

    p = EarningsDataProvider(data_dir=str(tmp_path / "earnings"))
    out = p._extract_text(str(html_path), max_chars=5000)
    # No section markers, fallback path
    assert "===" not in out
    assert len(out) <= 5100  # 5000 + small tail marker
    assert "[... truncated ...]" in out


def test_extract_text_skips_toc_for_financial_statements(tmp_path):
    """R6 audit (May 2026): logs showed 58 filings where the regex for
    'Consolidated Statements of Operations' matched the internal TOC /
    Index to Financial Statements before reaching the actual table.
    Result was ~553 chars of TOC entries vs 10,000+ chars of real data.

    Pin: when a TOC entry occurs BEFORE the real section, skip_toc
    strategy picks the later (real) one. Affected real-world filings:
    PG / SBUX / V / ABT / AMZN / CAT / COP / LLY 10-Qs."""
    from src.data.earnings import EarningsDataProvider

    # TOC entry (within first 15K chars) then later real section past 15K.
    # The early "Consolidated Statements of Operations" is a TOC pointer
    # with only ~200 chars of body before the next "Item" stop marker —
    # short enough to be dropped by the >=150 char threshold normally, but
    # NOT short enough if any "Item X" stop is found just after.
    early_toc = (
        "<html><body>"
        "<h1>Procter & Gamble 10-Q</h1>\n"
        "INDEX TO CONSOLIDATED FINANCIAL STATEMENTS\n"
        "Consolidated Statements of Operations\n"
        "(page 3)\n"
        # Filler to push the real section past 15K
        + ("Lorem cover-page boilerplate text padding the front. " * 350)
        # Real financial statement past the 15K threshold
        + "\nCONSOLIDATED STATEMENTS OF OPERATIONS\n"
        + ("Net sales $21,737 Cost of products sold $10,392 "
           "Operating income $5,148 Net earnings $4,103 Diluted EPS $1.66. " * 30)
        + "\nItem 2. Management's Discussion and Analysis\n"
        + ("Sales growth was driven by Beauty +6% and Health +8%. " * 30)
        + "\nItem 3. Quantitative disclosures\n"
        "</body></html>"
    )
    html_path = tmp_path / "pg.html"
    html_path.write_bytes(early_toc.encode())

    p = EarningsDataProvider(data_dir=str(tmp_path / "earnings"))
    out = p._extract_text(str(html_path), max_chars=30000)
    # The body of the financial_statements section must include the real
    # numerics, not just the TOC pointer.
    assert "Net sales $21,737" in out
    assert "Diluted EPS $1.66" in out
    assert "=== FINANCIAL STATEMENTS ===" in out


def test_extract_text_finds_financial_dense_region_on_fallback(tmp_path):
    """When structured extraction completely fails (filing layout that
    doesn't match any regex), fall back to the financial-data-rich
    region of the text rather than the front (which is typically
    cover page + XBRL boilerplate for iXBRL 10-Qs)."""
    from src.data.earnings import EarningsDataProvider

    # Front-loaded XBRL/cover boilerplate (no $-amount density), then a
    # rich financial table in the middle. No section headers anywhere
    # to defeat structured extraction entirely.
    boilerplate_front = (
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax "
        "contextRef=ctx_2026_q1 dimension=member dei:DocumentType "
        "ifrs-full:Assets fair-value-hierarchy Level1 Level2 Level3 "
    ) * 200
    rich_middle = (
        "Net sales $45,123 Operating income $12,456 Net income $9,876 "
        "Total assets $250,000 Cash $30,000 Diluted EPS $4.32 "
        "Free cash flow $11,234 Capital expenditures $(1,500) "
    ) * 100
    quiet_tail = "Signatures. Exhibit list. Other boilerplate. " * 200

    body = boilerplate_front + rich_middle + quiet_tail
    html_path = tmp_path / "ixbrl.html"
    html_path.write_bytes(f"<html><body>{body}</body></html>".encode())

    p = EarningsDataProvider(data_dir=str(tmp_path / "earnings"))
    out = p._extract_text(str(html_path), max_chars=10000)
    # Output must contain the rich-middle financial numbers, NOT the
    # XBRL boilerplate that dominated the front.
    assert "Net sales $45,123" in out
    assert "Diluted EPS $4.32" in out


def test_find_financial_dense_region_preserves_head_when_no_obvious_winner(tmp_path):
    """Tuning guard: if every chunk has similar low financial density
    (e.g., filing is genuinely all narrative — possibly a stub or an
    amendment with no statements), don't relocate. Returning 0 keeps
    backward compat behavior."""
    from src.data.earnings import EarningsDataProvider

    flat = "narrative text with no dollar figures or financial tables. " * 2000
    idx = EarningsDataProvider._find_financial_dense_region(flat, window=5000)
    assert idx == 0


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
    block to `text_excerpt`, not just have the method exist unused."""
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
        provider, "_get_xbrl_financial_facts",
        lambda cik, ticker, filing_date: (
            "=== STRUCTURED FINANCIAL FACTS (SEC XBRL, not text-extracted) ===\n"
            "Net Income: $31,778,000,000 (period ending 2026-03-31)\n"
        ),
    )

    report = provider._check_symbol("MSFT")

    assert report is not None
    assert "STRUCTURED FINANCIAL FACTS" in report.text_excerpt
    assert "Net Income: $31,778,000,000" in report.text_excerpt


def test_auditors_letter_is_not_mistaken_for_financial_statements(tmp_path):
    """The bug this guards, measured on production data 2026-08-28.

    `_extract_key_sections` matches the phrase "financial statements", which
    also appears inside the auditor's opinion letter — "...the related notes
    (collectively referred to as the financial statements)". That letter is
    prose thousands of characters long, so it cleared the old length-only
    acceptance test, and clearing it SUPPRESSED the density-seeking fallback
    that would have found the real tables.

    17 of the 68 filings cached on the production box were affected. Twelve of
    them — MSFT, AAPL, GOOGL, BAC, CVX, NFLX among them — reached the earnings
    analyst with ZERO financial figures. It failed silently: the analyst got a
    plausible document and reported on it.
    """
    from src.data.earnings import EarningsDataProvider

    auditors_letter = (
        "We have audited the accompanying consolidated balance sheets, and "
        "the related consolidated statements of income, comprehensive income, "
        "cash flows, and stockholders' equity, for each of the three years in "
        "the period ended June 30, 2026, and the related notes (collectively "
        "referred to as the financial statements). In our opinion, the "
        "financial statements present fairly, in all material respects, the "
        "financial position of the Company. " * 30
    )
    real_tables = _statement_rows(120)
    html = f"""<html><body>
    <h1>Form 10-K</h1>
    <h2>Item 8. Financial Statements and Supplementary Data</h2>
    <p>{auditors_letter}</p>
    <h2>CONSOLIDATED STATEMENTS OF OPERATIONS</h2>
    <p>Revenue $245,122 versus $211,915. Net income $88,136. {real_tables}</p>
    </body></html>"""
    html_path = tmp_path / "10k.html"
    html_path.write_bytes(html.encode())

    p = EarningsDataProvider(data_dir=str(tmp_path / "earnings"))
    out = p._extract_text(str(html_path), max_chars=30000)

    # Whatever path it takes, the analyst must end up holding actual numbers.
    import re
    from src.data.earnings import _FINANCIAL_FIGURE_RE
    assert len(_FINANCIAL_FIGURE_RE.findall(out)) >= 40, (
        "extraction returned narrative with no financial figures"
    )
    assert "245,122" in out or "88,136" in out


def test_a_genuinely_sparse_filing_still_returns_something(tmp_path):
    """Degrade, never blank. A shell filing has no tables to find, and the
    analyst is better served by the prose than by an empty string."""
    from src.data.earnings import EarningsDataProvider

    html = (
        "<html><body><h2>Item 1. Financial Statements</h2>"
        "<p>" + ("No material operations during the period. " * 200) + "</p>"
        "</body></html>"
    )
    html_path = tmp_path / "shell.html"
    html_path.write_bytes(html.encode())

    p = EarningsDataProvider(data_dir=str(tmp_path / "earnings"))
    out = p._extract_text(str(html_path), max_chars=30000)
    assert out.strip()
    assert "material operations" in out
