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


def test_same_number_restated_at_finer_precision_not_flagged():
    """The tolerance is HALF THE LAST PLACE OF THE EMITTED FIELD, read off the
    field itself rather than chosen. A field emitted as 2.0 asserts a
    tenth-of-a-point value, so prose saying 1.95% -- which rounds to 2.0 at
    that precision -- is the same number said more finely, not a mismatch."""
    decision = _decision(
        "NVDA is risking 1.95% on this name.",
        [_target("NVDA", 2.0)],
    )
    assert check_sizing_narrative(decision) == []


def test_difference_beyond_the_emitted_precision_is_flagged():
    """1.9% and an emitted 2.0 are different numbers at the precision 2.0 was
    written to, so they are a mismatch. The old borrowed 0.5pp tolerance --
    one `min_position_risk_pct` increment, a risk-BUDGET floor rather than a
    statement about the field's precision -- called this pair identical, and
    would equally have called prose '2.5% risk' and an emitted 2.0 identical."""
    decision = _decision(
        "NVDA is risking 1.9% on this name.",
        [_target("NVDA", 2.0)],
    )
    assert [f.symbol for f in check_sizing_narrative(decision)] == ["NVDA"]

    quarter_off = _decision(
        "NVDA is risking 2.5% on this name.",
        [_target("NVDA", 2.0)],
    )
    assert [f.symbol for f in check_sizing_narrative(quarter_off)] == ["NVDA"]


def test_finer_emitted_precision_narrows_the_band():
    """A field emitted to two places (2.25) asserts a hundredth, so its band is
    +/-0.005: 2.25% agrees, 2.3% does not. Nothing here is a chosen figure --
    both bands fall out of how the field itself was written."""
    agrees = _decision(
        "NVDA is risking 2.25% here.", [_target("NVDA", 2.25)],
    )
    assert check_sizing_narrative(agrees) == []
    differs = _decision(
        "NVDA is risking 2.3% here.", [_target("NVDA", 2.25)],
    )
    assert [f.symbol for f in check_sizing_narrative(differs)] == ["NVDA"]


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


def test_stored_run_601011e0_reproduces():
    """The filed case as the production database actually holds it (read-only
    replay, 2026-09-26): run-601011e0's stored `sizing_logic` against the six
    `portfolio_manager` / `target` evidence rows of the same run. RSG's emitted
    0.5 contradicts the narrative's 2.5% and is flagged; the four names that
    emitted what the prose says are not. ZS (prose 1.75%, emitted 0.5) is a
    deliberate MISS -- its clause never puts the word "risk" beside the number,
    so the narrow matcher stays silent rather than guess."""
    sizing_logic = (
        "RSG and AAPL get 2.5% risk each as the best breadth/diversification "
        "adds; NET gets 2.25% for high-conviction technical strength but "
        "neutral earnings; TSM gets 2.2% because its R/R is strong and it "
        "diversifies chip exposure; MRVL gets 2.0% on earnings plus technical "
        "support; ZS gets 1.75% as a smaller software add."
    )
    decision = _decision(sizing_logic, [
        _target("AAPL", 2.5), _target("RSG", 0.5), _target("NET", 2.25),
        _target("MRVL", 2.0), _target("TSM", 2.2), _target("ZS", 0.5),
    ])
    findings = check_sizing_narrative(decision)
    assert [f.symbol for f in findings] == ["RSG"]
    assert (findings[0].prose_pct, findings[0].field_pct) == (2.5, 0.5)
