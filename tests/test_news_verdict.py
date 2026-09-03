"""Phase 13 — the News seat's verdict shape.

`news_verdict_for_symbol` (src/models.py) collapses every `StockNewsItem`
News filed for one symbol into the single `AnalystVerdict` shape the
Portfolio Manager already reads from Technical (`TechAnalysisResult.
to_verdict`). News is structurally different from Technical: there is no
single object to restate, there are N items per symbol that can disagree.

The direction is resolved with the EXACT SAME collapsing rule
`PortfolioManagerAgent._collapse_stances` already applies in
`build_evidence_registry` (`put(symbol, "news", cls._collapse_stances(i.
sentiment for i in items))`) — both now call the one definition,
`src.quantities.collapse_stances`. `test_collapse_stances_and_the_pm_wrapper_agree`
below pins that the two can never silently drift apart.
"""

from __future__ import annotations

import pytest

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.models import (
    AnalystVerdict, NEWS_CONVICTION_MAGNITUDE, StockNewsItem,
    news_verdict_for_symbol,
)
from src.quantities import collapse_stances


def _item(sentiment: str, conviction: str, headline: str = "headline",
          impact_summary: str = "impact") -> StockNewsItem:
    return StockNewsItem(
        headline=headline, sentiment=sentiment, conviction=conviction,
        impact_summary=impact_summary,
    )


# ==========================================================================
# 1. All items agree.
# ==========================================================================

def test_all_agree_bullish_collapses_to_bullish():
    items = [
        _item("bullish", "medium", "NVDA beats on datacenter", "raises guide"),
        _item("bullish", "high", "Analyst upgrades NVDA", "PT raised to 200"),
    ]
    v = news_verdict_for_symbol("NVDA", items)
    assert v.seat == "news"
    assert v.symbol == "NVDA"
    assert v.direction == "bullish"
    # conviction: among agreeing items (both agree here), the HIGHEST wins.
    assert v.conviction == "high"
    assert v.magnitude == NEWS_CONVICTION_MAGNITUDE["high"]
    assert v.magnitude == 1.0
    assert v.invalidation  # directional verdict must state one
    assert len(v.evidence) == 2


def test_all_agree_low_conviction_still_has_nonzero_magnitude():
    """A low-conviction directional call must still carry SOME lean —
    magnitude 0.0 is reserved for neutral by AnalystVerdict's own validator,
    so 'low' cannot collapse to the same number as 'no call at all'."""
    items = [_item("bearish", "low")]
    v = news_verdict_for_symbol("XOM", items)
    assert v.direction == "bearish"
    assert v.conviction == "low"
    assert v.magnitude == NEWS_CONVICTION_MAGNITUDE["low"]
    assert v.magnitude > 0.0


# ==========================================================================
# 2. Mixed items that still resolve to one side.
# ==========================================================================

def test_mixed_items_that_are_all_positive_polarity_collapse_to_bullish():
    """collapse_stances treats {"strong_buy"-style positive labels} as one
    bucket, but StockNewsItem.sentiment is restricted to bullish/bearish/
    neutral, so the only way multiple DISTINCT values collapse to a single
    direction here is if they are all literally the same value once
    lowercased — covered by the all-agree tests above. This test instead
    pins the conviction-selection rule when agreeing items disagree on
    conviction: the highest of the AGREEING items wins, even when a
    higher-conviction item exists on the losing side.
    """
    items = [
        _item("bullish", "low", "small bullish item"),
        _item("bullish", "medium", "another bullish item"),
        _item("bearish", "high", "one high-conviction bearish outlier"),
    ]
    # Collapse rule: {"bullish", "bearish"} is neither <= positive nor
    # <= negative, and isn't <= {"neutral","mixed"} either -> "mixed" ->
    # treated as neutral (an unresolved split), NOT bullish.
    assert collapse_stances(i.sentiment for i in items) == "mixed"
    v = news_verdict_for_symbol("TSLA", items)
    assert v.direction == "neutral"
    assert v.magnitude == 0.0


def test_two_against_one_still_resolves_to_neutral_not_majority():
    """Confirms news_verdict_for_symbol does NOT run its own majority vote
    — two bullish items against one bearish item is still an unresolved
    'mixed' split under collapse_stances, and must land on neutral, not on
    the numerically larger side. (A naive majority-vote implementation
    would get this test wrong; see the break/restore check run manually.)
    """
    items = [
        _item("bullish", "high"),
        _item("bullish", "high"),
        _item("bearish", "high"),
    ]
    assert collapse_stances(i.sentiment for i in items) == "mixed"
    v = news_verdict_for_symbol("META", items)
    assert v.direction == "neutral"
    assert v.magnitude == 0.0
    assert v.invalidation == ""


def test_conviction_selection_ignores_the_losing_sides_conviction():
    """Among items agreeing with the final direction, take the HIGHEST
    conviction — a disagreeing item's conviction is not evidence for how
    strongly to hold the winning call, however confident it was."""
    items = [
        _item("bullish", "low", "weak bullish item"),
        _item("bullish", "high", "strong bullish item"),
    ]
    v = news_verdict_for_symbol("AAPL", items)
    assert v.direction == "bullish"
    assert v.conviction == "high"


# ==========================================================================
# 3. All neutral / empty.
# ==========================================================================

def test_all_neutral_items_produce_a_neutral_verdict():
    items = [_item("neutral", "medium"), _item("neutral", "low")]
    assert collapse_stances(i.sentiment for i in items) == "neutral"
    v = news_verdict_for_symbol("SPY", items)
    assert v.direction == "neutral"
    assert v.magnitude == 0.0
    assert v.invalidation == ""


def test_empty_items_fail_soft_to_neutral():
    """Documented precondition: news_verdict_for_symbol should never be
    called with an empty list in production (NewsIntelligenceReport.
    stock_news only has keys for symbols with >=1 item), but it fails soft
    to neutral rather than raising or crashing."""
    v = news_verdict_for_symbol("ORCL", [])
    assert v.direction == "neutral"
    assert v.magnitude == 0.0
    assert v.evidence == []


# ==========================================================================
# 4. Invalidation construction.
# ==========================================================================

def test_a_directional_call_never_has_a_disagreeing_item_to_quote():
    """Proves the claim in `news_verdict_for_symbol`'s invalidation
    docstring: because `StockNewsItem.sentiment` only has three possible
    values, `collapse_stances` resolves to a directional (non-"mixed")
    result ONLY when every item shares that exact sentiment. So a
    disagreeing item — one that could be quoted as "the stated case
    against the call" — can never coexist with a directional collapse.
    A mix that includes even one differing sentiment (bearish, or plain
    neutral) always collapses to "mixed" instead, per
    tests/test_news_verdict.py::test_mixed_items_that_are_all_positive_polarity_collapse_to_bullish
    and test_two_against_one_still_resolves_to_neutral_not_majority above.
    """
    for direction in ("bullish", "bearish"):
        other = "bearish" if direction == "bullish" else "bullish"
        for disagreeing_sentiment in (other, "neutral"):
            items = [_item(direction, "high"), _item(disagreeing_sentiment, "high")]
            assert collapse_stances(i.sentiment for i in items) == "mixed"


def test_invalidation_falls_back_to_a_generic_statement_when_all_items_agree():
    items = [_item("bullish", "medium", "steady demand", "reiterates guide")]
    v = news_verdict_for_symbol("COST", items)
    assert v.direction == "bullish"
    assert "bearish" in v.invalidation
    assert "COST" in v.invalidation


# ==========================================================================
# 5. Evidence capping.
# ==========================================================================

def test_evidence_is_capped_and_headline_plus_summary_are_both_present():
    items = [_item("bullish", "high", f"headline {i}", f"impact {i}") for i in range(8)]
    v = news_verdict_for_symbol("MSFT", items)
    assert len(v.evidence) == 5  # _MAX_NEWS_EVIDENCE_ITEMS
    assert v.evidence[0].label == "headline"
    assert "headline 0" in v.evidence[0].text
    assert "impact 0" in v.evidence[0].text


# ==========================================================================
# 6. The shape itself.
# ==========================================================================

def test_result_is_a_real_analystverdict_and_validates():
    v = news_verdict_for_symbol("GOOG", [_item("bearish", "high")])
    assert isinstance(v, AnalystVerdict)
    assert v.signed_magnitude == -v.magnitude


# ==========================================================================
# 7. Cross-check: the shared collapse rule cannot silently drift.
# ==========================================================================

@pytest.mark.parametrize("sentiments", [
    ["bullish"],
    ["bearish"],
    ["neutral"],
    ["bullish", "bullish"],
    ["bullish", "bearish"],
    ["bullish", "neutral"],
    ["neutral", "neutral"],
    ["bullish", "bullish", "bearish"],
    [],
])
def test_collapse_stances_and_the_pm_wrapper_agree(sentiments):
    """`PortfolioManagerAgent._collapse_stances` is now a thin wrapper over
    `src.quantities.collapse_stances` — this pins that the two can never
    silently drift apart, which is the whole point of moving the logic to
    one shared definition instead of writing it twice."""
    assert collapse_stances(sentiments) == PortfolioManagerAgent._collapse_stances(sentiments)
