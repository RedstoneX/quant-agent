"""Item 163 -- PM sizing NARRATIVE (`sizing_logic`) vs emitted risk numbers.

The per-symbol `TargetPosition.thesis` check lives in tests/test_models.py.
These cover the distinct surface this item filed: the whole-book `sizing_logic`
prose that names several symbols at once.
"""

from src.models import PortfolioDecision, ReasoningChain, TargetPosition
from src.risk_narrative_check import check_sizing_narrative


def _rc(sizing_logic: str) -> ReasoningChain:
    return ReasoningChain(
        macro_filter="m", news_check="n", earnings_check="e",
        signal_conflicts="s", sizing_logic=sizing_logic,
        portfolio_balance="b", cash_target="c",
    )


def _decision(sizing_logic: str, targets: list[TargetPosition]) -> PortfolioDecision:
    return PortfolioDecision(
        reasoning_chain=_rc(sizing_logic), portfolio_view="v", targets=targets,
    )


def _target(symbol: str, risk_pct: float | None) -> TargetPosition:
    kwargs = {"symbol": symbol, "conviction": "high", "thesis": "t"}
    if risk_pct is None:
        kwargs["target_weight_pct"] = 5.0
    else:
        kwargs["risk_allocation_pct"] = risk_pct
    return TargetPosition(**kwargs)


def test_filed_case_rsg_flagged_aapl_not():
    """The exact filed case: 'RSG and AAPL get 2.5% risk each' while RSG's
    emitted risk_allocation_pct is 0.5 -- RSG is flagged, AAPL (2.5%) is not."""
    decision = _decision(
        "RSG and AAPL get 2.5% risk each given the strong setups.",
        [_target("RSG", 0.5), _target("AAPL", 2.5)],
    )
    findings = check_sizing_narrative(decision)
    assert [f.symbol for f in findings] == ["RSG"]
    f = findings[0]
    assert f.prose_pct == 2.5
    assert f.field_pct == 0.5
    assert "RSG" in f.detail and "2.5%" in f.detail and "0.5%" in f.detail
    # authoritative field is never touched
    assert decision.targets[0].risk_allocation_pct == 0.5


def test_consistent_narrative_not_flagged():
    """Prose that agrees with the emitted number is not a mismatch."""
    decision = _decision(
        "NVDA carries 2% risk here on high conviction.",
        [_target("NVDA", 2.0)],
    )
    assert check_sizing_narrative(decision) == []


def test_within_tolerance_not_flagged():
    """A prose value within the shared tolerance of the field is not flagged."""
    decision = _decision(
        "NVDA is risking about 1.9% on this name.",
        [_target("NVDA", 2.0)],
    )
    assert check_sizing_narrative(decision) == []


def test_no_percentage_prose_passes():
    """Prose with no explicit risk-% claim never flags (no false positives)."""
    decision = _decision(
        "Sized to conviction; NVDA gets the most room, RSG the least.",
        [_target("NVDA", 2.0), _target("RSG", 0.5)],
    )
    assert check_sizing_narrative(decision) == []


def test_incidental_percentage_not_read_as_risk():
    """A target weight / stop distance / macro % near a symbol is not a risk
    claim, so it must not trip the check even though it differs from the field."""
    decision = _decision(
        "NVDA target weight 8%, stop 12% below entry; GDP grew 3% last quarter.",
        [_target("NVDA", 1.5)],
    )
    assert check_sizing_narrative(decision) == []


def test_ambiguous_two_values_one_sentence_skipped():
    """Two different risk percentages next to two symbols in one sentence
    cannot be paired with confidence -- the sentence is skipped, not flagged."""
    decision = _decision(
        "NVDA risk 3% and RSG risk 1% respectively.",
        # NVDA field disagrees with BOTH numbers, RSG too -- but the pairing is
        # ambiguous, so the honest-minimal rule stays silent.
        [_target("NVDA", 0.5), _target("RSG", 0.5)],
    )
    assert check_sizing_narrative(decision) == []


def test_symbol_only_flagged_when_named_in_the_claim_sentence():
    """A book-level risk figure in a sentence that does not name the symbol
    must not be pinned onto that symbol."""
    decision = _decision(
        "RSG is our smallest position. Overall the book is risking 2%.",
        [_target("RSG", 0.5)],
    )
    assert check_sizing_narrative(decision) == []


def test_legacy_target_without_risk_field_skipped():
    """A target carrying only target_weight_pct has no authoritative risk
    number, so its prose is never checked."""
    decision = _decision(
        "RSG gets 2.5% risk here.",
        [_target("RSG", None)],
    )
    assert check_sizing_narrative(decision) == []


def test_empty_sizing_logic_passes():
    decision = _decision("   ", [_target("RSG", 0.5)])
    # min_length=1 forbids "" so a single space is the empty-ish case.
    assert check_sizing_narrative(decision) == []
