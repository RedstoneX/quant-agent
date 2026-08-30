"""Spec §9.3 — "disagreement must be adjudicated, not merely logged".

An unadjudicated `conflicts` provenance entry on a target that OPENS or
INCREASES exposure must drop THAT ONE TARGET and leave the rest of the
session's decision intact — never join `validate_grounding`'s error list,
which fails the entire session (see `PortfolioManagerAgent.decide()`'s
`_semantic_failure` contract). Exits and reductions are exempt.

This enforces SPECIFICITY OF REFERENCE (the symbol and the conflicting
source must both be named in `signal_conflicts`), not quality of reasoning.
"""

import json

from unittest.mock import MagicMock, patch

from src.agents.portfolio_manager import (
    CONFLICT_UNADJUDICATED_STATUS, PortfolioManagerAgent,
)
from src.models import PortfolioDecision, Position, TechAnalysisResult, TechReasoningChain


def _tech_rc() -> TechReasoningChain:
    return TechReasoningChain(
        trend="x", momentum="x", volatility="x", volume="x",
        support_resistance="x",
    )


def _analysis(symbol: str, rating: str = "buy") -> TechAnalysisResult:
    buy = rating in {"buy", "strong_buy"}
    return TechAnalysisResult(
        symbol=symbol, rating=rating, conviction="medium", entry_price=100,
        stop_loss=95 if buy else 105, reference_target=112 if buy else 88,
        support_levels=[95] if buy else [88],
        resistance_levels=[112] if buy else [105],
        setup_type="range", expected_horizon_sessions=10,
        reasoning="validated production-like trend and momentum evidence",
        reasoning_chain=_tech_rc(),
    )


def _decision(targets: list[dict], conflicts: str) -> PortfolioDecision:
    return PortfolioDecision.model_validate({
        "reasoning_chain": {
            "macro_filter": "Macro checked.", "news_check": "News checked.",
            "earnings_check": "Earnings checked.", "signal_conflicts": conflicts,
            "sizing_logic": "Sizing checked.", "portfolio_balance": "Book checked.",
            "cash_target": "Cash checked.",
        },
        "targets": targets, "portfolio_view": "Test decision.",
    })


def _buy_target(symbol: str, *, conflict_source: str | None = "macro") -> dict:
    """A BUY (opening) target with a `technical` supports claim and,
    optionally, one `conflicts` claim from `conflict_source`."""
    provenance = [{
        "source": "technical", "observed_stance": "buy",
        "relationship": "supports", "evidence": "current-run buy rating",
    }]
    if conflict_source:
        provenance.append({
            "source": conflict_source,
            "observed_stance": "bearish" if conflict_source != "technical" else "sell",
            "relationship": "conflicts", "evidence": "named disagreement",
        })
    return {
        "symbol": symbol, "risk_allocation_pct": 3.0, "conviction": "medium",
        "thesis": f"{symbol} setup.", "provenance": provenance,
    }


def _close_target(symbol: str, *, conflict_source: str = "earnings") -> dict:
    """A full-close (risk_allocation_pct=0) target carrying an unaddressed
    conflict — must be EXEMPT regardless."""
    return {
        "symbol": symbol, "risk_allocation_pct": 0.0, "conviction": "low",
        "thesis": f"Close {symbol}.",
        "provenance": [{
            "source": conflict_source, "observed_stance": "bearish",
            "relationship": "conflicts", "evidence": "named disagreement",
        }],
    }


def _held(symbol: str, qty: float = 10.0) -> Position:
    return Position(
        symbol=symbol, qty=qty, avg_entry=100.0, current_price=105.0,
        market_value=qty * 105.0, unrealized_pnl=50.0, sector="Technology",
    )


# --------------------------------------------------------------------------
# Direct unit tests of `_drop_unadjudicated_conflicts`
# --------------------------------------------------------------------------

def test_unadjudicated_conflict_drops_only_its_own_target():
    """THE key test: one bad target must not take the rest of the book
    with it. NVDA carries an unaddressed macro conflict; AAPL is clean."""
    decision = _decision(
        [_buy_target("NVDA", conflict_source="macro"), _buy_target("AAPL", conflict_source=None)],
        conflicts="AAPL: available=technical=buy. Conflict: none. Resolution: n/a.",
    )
    result = PortfolioManagerAgent._drop_unadjudicated_conflicts(
        decision, positions=[], total_value=100_000,
    )
    symbols = {t.symbol for t in result.targets}
    assert symbols == {"AAPL"}, f"expected only AAPL to survive, got {symbols}"


def test_adjudicated_conflict_naming_symbol_and_source_passes():
    """Naming BOTH the symbol and the conflicting source lets the target
    through untouched."""
    decision = _decision(
        [_buy_target("NVDA", conflict_source="macro")],
        conflicts=(
            "NVDA: available=technical=buy, macro=bearish. Conflict: macro "
            "underweight. Resolution: overriding on the earnings catalyst."
        ),
    )
    result = PortfolioManagerAgent._drop_unadjudicated_conflicts(
        decision, positions=[], total_value=100_000,
    )
    assert {t.symbol for t in result.targets} == {"NVDA"}


def test_conflict_on_close_is_never_blocked():
    """Exits/reductions are exempt — an unresolved conflict on a CLOSE
    target must never be dropped."""
    decision = _decision(
        [_close_target("EPD", conflict_source="earnings")],
        conflicts="No mention of EPD or earnings at all.",
    )
    result = PortfolioManagerAgent._drop_unadjudicated_conflicts(
        decision, positions=[_held("EPD")], total_value=100_000,
    )
    assert {t.symbol for t in result.targets} == {"EPD"}


def test_conflict_on_reduce_of_legacy_weight_target_is_never_blocked():
    """A legacy target_weight_pct-based reduction (weight below current)
    is classified `sell` by `_target_intent` and is exempt too."""
    target = {
        "symbol": "XLF", "target_weight_pct": 2.0, "conviction": "low",
        "thesis": "Trim XLF.",
        "provenance": [{
            "source": "news", "observed_stance": "bearish",
            "relationship": "conflicts", "evidence": "named disagreement",
        }],
    }
    decision = _decision(
        [target], conflicts="No mention of XLF or news at all.",
    )
    # Held at 10% weight; target of 2% is a reduction.
    position = Position(
        symbol="XLF", qty=100, avg_entry=50.0, current_price=100.0,
        market_value=10_000.0, unrealized_pnl=5_000.0, sector="Financial Services",
    )
    result = PortfolioManagerAgent._drop_unadjudicated_conflicts(
        decision, positions=[position], total_value=100_000,
    )
    assert {t.symbol for t in result.targets} == {"XLF"}


def test_symbol_substring_cannot_satisfy_the_match():
    """Word-boundary matching: "V" appearing only inside "AVGO"/"INVALID"
    must NOT satisfy the match for a conflict on symbol "V"."""
    decision = _decision(
        [_buy_target("V", conflict_source="macro")],
        conflicts=(
            "AVGO: available=technical=buy. Conflict: none. "
            "INVALID data ignored. macro regime is risk-on."
        ),
    )
    result = PortfolioManagerAgent._drop_unadjudicated_conflicts(
        decision, positions=[], total_value=100_000,
    )
    assert result.targets == [], (
        "a substring match on 'V' inside 'AVGO'/'INVALID' incorrectly "
        "satisfied the conflict-naming requirement"
    )


def test_symbol_as_a_real_word_boundary_does_satisfy_the_match():
    """The mirror case: "V" as its OWN word (not a substring) does count,
    proving the gate isn't over-strict either."""
    decision = _decision(
        [_buy_target("V", conflict_source="macro")],
        conflicts="V: available=technical=buy, macro=bearish. Conflict named; overriding on catalyst.",
    )
    result = PortfolioManagerAgent._drop_unadjudicated_conflicts(
        decision, positions=[], total_value=100_000,
    )
    assert {t.symbol for t in result.targets} == {"V"}


def test_smart_money_source_alias_matches_plain_english():
    """`smart_money` is commonly written "smart money" in prose — the
    alias list must recognise that without a second guessing rule."""
    decision = _decision(
        [_buy_target("CEG", conflict_source="smart_money")],
        conflicts="CEG: smart money is bearish/conflicts; overriding on the breakout.",
    )
    result = PortfolioManagerAgent._drop_unadjudicated_conflicts(
        decision, positions=[], total_value=100_000,
    )
    assert {t.symbol for t in result.targets} == {"CEG"}


def test_multiple_conflicting_sources_all_must_be_named():
    """A target with TWO conflicting sources is only adjudicated when
    BOTH are individually named."""
    provenance = [
        {"source": "technical", "observed_stance": "buy", "relationship": "supports",
         "evidence": "buy rating"},
        {"source": "macro", "observed_stance": "bearish", "relationship": "conflicts",
         "evidence": "underweight"},
        {"source": "news", "observed_stance": "bearish", "relationship": "conflicts",
         "evidence": "adverse coverage"},
    ]
    target = {
        "symbol": "DIS", "risk_allocation_pct": 3.0, "conviction": "medium",
        "thesis": "DIS setup.", "provenance": provenance,
    }
    # Only macro named — news left unaddressed.
    decision = _decision(
        [target], conflicts="DIS: macro conflict noted; overriding on catalyst.",
    )
    result = PortfolioManagerAgent._drop_unadjudicated_conflicts(
        decision, positions=[], total_value=100_000,
    )
    assert result.targets == []

    # Both named — passes.
    decision2 = _decision(
        [target],
        conflicts=(
            "DIS: macro conflict and news conflict both noted; overriding "
            "on the earnings catalyst."
        ),
    )
    result2 = PortfolioManagerAgent._drop_unadjudicated_conflicts(
        decision2, positions=[], total_value=100_000,
    )
    assert {t.symbol for t in result2.targets} == {"DIS"}


def test_no_conflicts_provenance_is_unaffected():
    """A target with no `conflicts` claim at all is untouched regardless
    of `signal_conflicts` content."""
    decision = _decision(
        [_buy_target("MSFT", conflict_source=None)],
        conflicts="Nothing relevant mentioned.",
    )
    result = PortfolioManagerAgent._drop_unadjudicated_conflicts(
        decision, positions=[], total_value=100_000,
    )
    assert {t.symbol for t in result.targets} == {"MSFT"}


# --------------------------------------------------------------------------
# Full decide() integration — proves the drop happens inside the real
# parse/validate pipeline and does not fail the session.
# --------------------------------------------------------------------------

def _pm_response(targets: list[dict], conflicts: str) -> str:
    return json.dumps({
        "reasoning_chain": {
            "macro_filter": "checked", "news_check": "checked",
            "earnings_check": "checked", "signal_conflicts": conflicts,
            "sizing_logic": "checked", "portfolio_balance": "checked",
            "cash_target": "checked",
        },
        "targets": targets,
        "portfolio_view": "Two candidates, one carries an unaddressed conflict.",
    })


@patch("anthropic.Anthropic")
def test_decide_drops_only_the_unadjudicated_target_end_to_end(mock_cls):
    response_text = _pm_response(
        [_buy_target("NVDA", conflict_source="macro"), _buy_target("AAPL", conflict_source=None)],
        conflicts="AAPL: available=technical=buy. Conflict: none. Resolution: n/a.",
    )
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=response_text)]
    mock_response.usage.input_tokens = 500
    mock_response.usage.output_tokens = 200
    mock_client.messages.create.return_value = mock_response
    mock_cls.return_value = mock_client

    agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6-20250725")
    decision, result = agent.decide(
        analyses=[_analysis("NVDA"), _analysis("AAPL")],
        positions=[], macro_analysis=None, cash_balance=50_000,
        total_value=100_000,
        allowed_buy_symbols={"NVDA", "AAPL"},
    )
    assert decision is not None, f"decide() failed closed: {result.semantic_error}"
    assert {t.symbol for t in decision.targets} == {"AAPL"}
    assert result.semantic_status in (None, "success")


@patch("anthropic.Anthropic")
def test_decide_still_fails_closed_when_grounding_genuinely_breaks(mock_cls, caplog):
    """Sanity check that the new step doesn't mask a REAL grounding
    failure unrelated to conflict adjudication (e.g. a fabricated source)."""
    bad_target = {
        "symbol": "AAPL", "risk_allocation_pct": 3.0, "conviction": "medium",
        "thesis": "AAPL setup.",
        "provenance": [{
            "source": "earnings", "observed_stance": "bullish",
            "relationship": "supports", "evidence": "fabricated — no earnings coverage exists",
        }],
    }
    response_text = _pm_response(
        [bad_target], conflicts="AAPL: available=technical=buy. Conflict: none.",
    )
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=response_text)]
    mock_response.usage.input_tokens = 500
    mock_response.usage.output_tokens = 200
    mock_client.messages.create.return_value = mock_response
    mock_cls.return_value = mock_client

    agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6-20250725")
    decision, result = agent.decide(
        analyses=[_analysis("AAPL")], positions=[], macro_analysis=None,
        cash_balance=50_000, total_value=100_000,
        allowed_buy_symbols={"AAPL"},
    )
    assert decision is None
    assert result.semantic_status == "pm_grounding_error"


def test_conflict_unadjudicated_status_is_a_stable_greppable_constant():
    assert CONFLICT_UNADJUDICATED_STATUS == "pm_conflict_unadjudicated"
