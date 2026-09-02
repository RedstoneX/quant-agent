"""The sub-floor reward:risk catalyst gate (2026-09-02).

The prompt has always permitted a below-floor pick that names a catalyst.
Benchmarked 2026-09-01 on the real opportunity set of the zero-trade day
(`run-64290730`), both candidate models picked NVDA at R/R 1.03 in 9 of 9
runs and passed over GEV, which cleared the floor — WITHOUT DISOBEYING.
Every sub-floor pick named a catalyst, cut size, and stated the ratio was
below floor; the rule-compliance grader passed them all.

The hole was that the catalyst was asserted, never checked. For a mega-cap
the news feed always carries one, so the exception was a null constraint on
exactly the names it needed to bind — and the desk's own
`active_state_changes` block was feeding the PM the catalyst it then used to
justify the exception.

So the fix is deterministic and lives AFTER the PM submits:
  * a sub-floor pick's `catalyst` must RESOLVE to an `active_state_changes`
    row — cited by that row's ISO date, and the row must name the symbol —
    or the target is dropped;
  * one that does resolve is capped at the smallest starter size.

These tests pin both halves, the exemptions, and the bypasses that were
deliberately closed.
"""

import json

from unittest.mock import MagicMock, patch

import pytest

from src.agents.portfolio_manager import (
    SUBFLOOR_CATALYST_UNVERIFIED_STATUS,
    SUBFLOOR_SIZE_CAPPED_STATUS,
    PortfolioManagerAgent,
)
from src.models import (
    PortfolioDecision, Position, TechAnalysisResult, TechReasoningChain,
)
from src.risk.constants import REWARD_RISK_FLOOR, STARTER_POSITION_RISK_PCT


# The block as `TradingPipeline._build_active_state_changes` renders it —
# these five rows are copied verbatim from the run-64290730 fixture
# (`ops/model_policy/fixtures/run_64290730_pm_input.json`,
# `memory.active_state_changes`) so the format under test is the production
# one, not a test-local invention.
ACTIVE_STATE_CHANGES = (
    "- [2026-09-01] Global bond selloff lifts yields and rising crude "
    "pressures equities to start September → SPY, QQQ, DIA, SMH, SOXX, NVDA\n"
    "- [2026-08-31] US oil firms designated to take over Venezuelan "
    "oilfields, Exxon expanding footprint → XOM, CVX, XLE\n"
    "- [2026-08-31] Anthropic signs $35 billion cloud deal with "
    "Nvidia-backed Lambda → NVDA\n"
    "- [2026-08-27] Nvidia revenue forecast of 70% growth → NVDA, SMH, "
    "SOXX, AMD, AVGO, TSM\n"
    "- [2026-08-27] Salesforce beats Q2 earnings and expands AI "
    "partnership → CRM, MSFT, GOOGL, AMZN, ORCL, ZS"
)

# The catalyst the LIVE 2026-09-01 desk actually recorded for its NVDA
# target, read out of production `agent_logs` for decision
# `run-64290730-dec-f25433`. It is concrete, specific and entirely
# unverifiable: no state-change row mentions an SB Energy investment.
LIVE_NVDA_CATALYST = (
    "HIGH bullish stock-specific news: strategic $3B investment into SB "
    "Energy reinforces long-term AI infrastructure ecosystem lock-in."
)


def _tech_rc() -> TechReasoningChain:
    return TechReasoningChain(
        trend="x", momentum="x", volatility="x", volume="x",
        support_resistance="x",
    )


def _analysis(
    symbol: str, *, rating: str = "buy", target: float | None = 112.0,
) -> TechAnalysisResult:
    """Entry 100 / stop 95 for a long, 100 / 105 for a short.

    `target=112` gives R/R 2.4 (clears the floor); `target=104` gives 0.8
    (fails it). A `neutral` rating gives `risk_reward is None`, which the
    gate also treats as sub-floor.
    """
    buy = rating in {"buy", "strong_buy"}
    return TechAnalysisResult(
        symbol=symbol, rating=rating, conviction="medium", entry_price=100,
        stop_loss=95 if buy else 105, reference_target=target,
        support_levels=[95] if buy else [88],
        resistance_levels=[112] if buy else [105],
        setup_type="range", expected_horizon_sessions=10,
        reasoning="validated production-like trend and momentum evidence",
        reasoning_chain=_tech_rc(),
    )


def _decision(targets: list[dict]) -> PortfolioDecision:
    return PortfolioDecision.model_validate({
        "reasoning_chain": {
            "macro_filter": "Macro checked.", "news_check": "News checked.",
            "earnings_check": "Earnings checked.",
            "signal_conflicts": "None material.",
            "sizing_logic": "Sizing checked.",
            "portfolio_balance": "Book checked.",
            "cash_target": "Cash checked.",
        },
        "targets": targets, "portfolio_view": "Test decision.",
    })


def _target(
    symbol: str, *, risk: float | None = 3.0, catalyst: str = "",
    direction: str = "long", weight: float | None = None,
) -> dict:
    row: dict = {
        "symbol": symbol, "conviction": "medium", "direction": direction,
        "thesis": f"{symbol} setup.", "catalyst": catalyst,
        "provenance": [{
            "source": "technical",
            "observed_stance": "buy" if direction == "long" else "sell",
            "relationship": "supports", "evidence": "current-run rating",
        }],
    }
    if risk is not None:
        row["risk_allocation_pct"] = risk
    if weight is not None:
        row["target_weight_pct"] = weight
    return row


def _apply(decision, analyses, *, positions=None, asc=ACTIVE_STATE_CHANGES):
    return PortfolioManagerAgent._apply_subfloor_catalyst_rule(
        decision, analyses=analyses, positions=positions or [],
        total_value=100_000, active_state_changes=asc,
        rr_floor=REWARD_RISK_FLOOR,
        starter_risk_pct=STARTER_POSITION_RISK_PCT,
    )


def _held(symbol: str, qty: float = 10.0) -> Position:
    return Position(
        symbol=symbol, qty=qty, avg_entry=100.0, current_price=105.0,
        market_value=qty * 105.0, unrealized_pnl=50.0, sector="Technology",
    )


# --------------------------------------------------------------------------
# The rendered-block parser
# --------------------------------------------------------------------------

def test_parser_maps_each_row_date_to_the_symbols_it_names():
    by_date = PortfolioManagerAgent._state_change_symbols_by_date(
        ACTIVE_STATE_CHANGES,
    )
    assert by_date["2026-09-01"] == {"SPY", "QQQ", "DIA", "SMH", "SOXX", "NVDA"}
    # Two rows share 2026-08-31, and two share 2026-08-27. Same-date rows are
    # UNIONED: a citation proves "a HIGH-conviction state change affecting
    # this symbol was recorded on this date", which is the checkable claim.
    assert by_date["2026-08-31"] == {"XOM", "CVX", "XLE", "NVDA"}
    assert by_date["2026-08-27"] == {
        "NVDA", "SMH", "SOXX", "AMD", "AVGO", "TSM",
        "CRM", "MSFT", "GOOGL", "AMZN", "ORCL", "ZS",
    }


def test_parser_skips_rows_with_no_affected_symbols_and_unparseable_lines():
    """`_build_active_state_changes` writes an em dash when the news analyst
    attached no symbols. A market-wide row names nobody, so it can back
    nobody — and a line that does not parse must narrow what can be cited,
    never widen it or raise."""
    by_date = PortfolioManagerAgent._state_change_symbols_by_date(
        "- [2026-08-30] Broad risk-off with no named exposure → —\n"
        "not a row at all\n"
        "- [2026-08-29] Missing the arrow entirely\n"
        "- [not-a-date] Something → NVDA\n"
        "- [2026-08-28] Yields fall → TLT",
    )
    assert by_date == {"2026-08-28": {"TLT"}}


def test_parser_splits_on_the_last_arrow_so_event_prose_may_contain_one():
    by_date = PortfolioManagerAgent._state_change_symbols_by_date(
        "- [2026-08-31] Regime moved risk-off → risk-on overnight → SPY, QQQ",
    )
    assert by_date == {"2026-08-31": {"SPY", "QQQ"}}


def test_parser_tolerates_an_empty_block():
    assert PortfolioManagerAgent._state_change_symbols_by_date("") == {}


# --------------------------------------------------------------------------
# The citation check
# --------------------------------------------------------------------------

@pytest.mark.parametrize("catalyst,symbol,expected", [
    ("2026-08-31: Anthropic/Lambda cloud deal", "NVDA", True),
    # Same date, a symbol that row does not name.
    ("2026-08-31: Anthropic/Lambda cloud deal", "GEV", False),
    # Right symbol, a date with no row.
    ("2026-08-26: something happened", "NVDA", False),
    # Concrete, specific, and citing nothing — the whole failure mode.
    (LIVE_NVDA_CATALYST, "NVDA", False),
    ("", "NVDA", False),
    ("   ", "NVDA", False),
    # A row whose symbols include the target, cited among other prose.
    ("Energy takeover per the 2026-08-31 state change", "XLE", True),
])
def test_catalyst_resolution(catalyst, symbol, expected):
    by_date = PortfolioManagerAgent._state_change_symbols_by_date(
        ACTIVE_STATE_CHANGES,
    )
    assert PortfolioManagerAgent._catalyst_cites_state_change(
        catalyst, symbol, by_date,
    ) is expected


def test_the_live_2026_09_01_nvda_catalyst_does_not_resolve():
    """THE regression this whole gate exists for, using the exact text the
    production desk recorded rather than a paraphrase of it."""
    decision = _decision([_target("NVDA", risk=0.75, catalyst=LIVE_NVDA_CATALYST)])
    result = _apply(decision, [_analysis("NVDA", target=104.0)])
    assert [t.symbol for t in result.targets] == []


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------

def test_subfloor_pick_with_unverifiable_catalyst_is_dropped_alone():
    """One decorative catalyst must not take the rest of the book with it —
    same punishment-fits-offence contract as §9.3's conflict prune."""
    decision = _decision([
        _target("NVDA", catalyst=LIVE_NVDA_CATALYST),
        _target("GEV", risk=2.0),
    ])
    result = _apply(
        decision,
        [_analysis("NVDA", target=104.0), _analysis("GEV", target=112.0)],
    )
    assert {t.symbol for t in result.targets} == {"GEV"}
    assert result.targets[0].risk_allocation_pct == 2.0, (
        "a qualifying pick must be left entirely alone"
    )


def test_subfloor_pick_with_no_catalyst_at_all_is_dropped():
    decision = _decision([_target("NVDA", catalyst="")])
    result = _apply(decision, [_analysis("NVDA", target=104.0)])
    assert result.targets == []


def test_subfloor_pick_with_a_resolving_citation_survives_capped():
    decision = _decision([
        _target("NVDA", risk=3.0, catalyst="2026-08-31 Anthropic/Lambda deal"),
    ])
    result = _apply(decision, [_analysis("NVDA", target=104.0)])
    assert [t.symbol for t in result.targets] == ["NVDA"]
    assert result.targets[0].risk_allocation_pct == STARTER_POSITION_RISK_PCT


def test_the_cap_only_ever_reduces():
    """A sub-floor pick already at or under the starter size keeps its own
    number — the rule is a ceiling, never a floor that sizes up."""
    decision = _decision([
        _target("NVDA", risk=0.25, catalyst="2026-08-31 Anthropic/Lambda deal"),
    ])
    result = _apply(decision, [_analysis("NVDA", target=104.0)])
    assert result.targets[0].risk_allocation_pct == 0.25


def test_a_pick_sitting_exactly_on_the_floor_clears_it():
    """The floor is inclusive — `>= rr_floor`, matching
    `PortfolioConstructor`'s own `reward_risk < floor` rejection. Entry 100 /
    stop 95 / target 107.5 is R/R 1.50 exactly, and must trade at full size
    with no catalyst at all."""
    analysis = _analysis("GEV", target=107.5)
    assert analysis.risk_reward == REWARD_RISK_FLOOR
    decision = _decision([_target("GEV", risk=4.0, catalyst="")])
    result = _apply(decision, [analysis])
    assert [t.symbol for t in result.targets] == ["GEV"]
    assert result.targets[0].risk_allocation_pct == 4.0


def test_a_pick_clearing_the_floor_keeps_its_full_size():
    decision = _decision([_target("GEV", risk=4.0)])
    result = _apply(decision, [_analysis("GEV", target=112.0)])
    assert result.targets[0].risk_allocation_pct == 4.0


def test_missing_reward_risk_counts_as_subfloor():
    """`risk_reward` is None for a neutral rating or malformed geometry. The
    prompt already says "R/R n/a — treat as low-R/R", and a target with no
    computable payoff is exactly what a checkable catalyst has to justify."""
    neutral = _analysis("NVDA", rating="neutral", target=112.0)
    assert neutral.risk_reward is None
    decision = _decision([_target("NVDA", catalyst=LIVE_NVDA_CATALYST)])
    assert _apply(decision, [neutral]).targets == []
    # ... and it is not simply always dropped: a resolving citation still
    # buys it a capped starter.
    cited = _decision([
        _target("NVDA", catalyst="2026-08-31 Anthropic/Lambda deal"),
    ])
    kept = _apply(cited, [neutral]).targets
    assert [t.symbol for t in kept] == ["NVDA"]
    assert kept[0].risk_allocation_pct == STARTER_POSITION_RISK_PCT


def test_shorts_are_gated_on_the_same_terms_as_longs():
    """A qualified short is worth exactly as much as a qualified long, and an
    unqualified one costs exactly as much."""
    decision = _decision([
        _target("MSFT", direction="short", catalyst="no citation here"),
        _target("CRM", direction="short",
                catalyst="2026-08-27 Salesforce Q2 beat"),
    ])
    analyses = [
        _analysis("MSFT", rating="sell", target=96.0),   # R/R 0.8
        _analysis("CRM", rating="sell", target=96.0),    # R/R 0.8
    ]
    result = _apply(decision, analyses)
    assert {t.symbol for t in result.targets} == {"CRM"}
    assert result.targets[0].risk_allocation_pct == STARTER_POSITION_RISK_PCT


def test_exits_and_closes_are_exempt():
    """Mirrors §3.4 and §9.3's asymmetry: this desk must never find it harder
    to cut risk than to add it. A close carries no catalyst and its symbol's
    R/R is irrelevant."""
    decision = _decision([_target("NVDA", risk=0.0, catalyst="")])
    result = _apply(
        decision, [_analysis("NVDA", target=104.0)], positions=[_held("NVDA")],
    )
    assert [t.symbol for t in result.targets] == ["NVDA"]
    assert result.targets[0].risk_allocation_pct == 0.0


def test_an_empty_state_change_block_makes_the_exception_unavailable():
    """With no rows to cite there is nothing to check, so the exception
    cannot be claimed. Deliberate: on such a day the qualified candidates are
    the whole opportunity set."""
    decision = _decision([
        _target("NVDA", catalyst="2026-08-31 Anthropic/Lambda deal"),
    ])
    result = _apply(decision, [_analysis("NVDA", target=104.0)], asc="")
    assert result.targets == []


def test_legacy_notional_target_cannot_bypass_the_cap():
    """A sub-floor pick that supplies only the legacy `target_weight_pct`
    would otherwise be sized by the constructor's notional path and escape a
    risk-denominated cap entirely. It is converted onto the risk path at the
    starter size instead — the constructor prefers risk whenever both are
    present, so this is the field that actually binds."""
    decision = _decision([
        _target("NVDA", risk=None, weight=8.0,
                catalyst="2026-08-31 Anthropic/Lambda deal"),
    ])
    result = _apply(decision, [_analysis("NVDA", target=104.0)])
    assert [t.symbol for t in result.targets] == ["NVDA"]
    assert result.targets[0].risk_allocation_pct == STARTER_POSITION_RISK_PCT


def test_a_symbol_with_no_analysis_at_all_is_treated_as_subfloor():
    """`validate_grounding` independently refuses an increase with no
    current-run technical read, but this rule must not be the thing that
    waves one through if that check ever moves."""
    decision = _decision([_target("ZZZZ", catalyst=LIVE_NVDA_CATALYST)])
    result = _apply(decision, [_analysis("NVDA", target=112.0)])
    assert result.targets == []


# --------------------------------------------------------------------------
# End to end through `decide()`
# --------------------------------------------------------------------------

def _pm_response(targets: list[dict]) -> str:
    return json.dumps({
        "reasoning_chain": {
            "macro_filter": "checked", "news_check": "checked",
            "earnings_check": "checked", "signal_conflicts": "none material",
            "sizing_logic": "checked", "portfolio_balance": "checked",
            "cash_target": "checked",
        },
        "targets": targets,
        "portfolio_view": "One sub-floor pick, one qualifying pick.",
    })


def _mock_agent(mock_cls, response_text: str) -> PortfolioManagerAgent:
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=response_text)]
    mock_response.usage.input_tokens = 500
    mock_response.usage.output_tokens = 200
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response
    mock_cls.return_value = mock_client
    return PortfolioManagerAgent(api_key="test", model="claude-opus-4-6-20250725")


@patch("anthropic.Anthropic")
def test_decide_drops_the_live_nvda_pick_and_keeps_the_qualifying_one(mock_cls):
    """The 2026-09-01 book in miniature: the famous name at R/R 0.8 with the
    catalyst the live desk actually wrote, alongside a candidate that clears
    the floor. Only the qualifying one may survive."""
    agent = _mock_agent(mock_cls, _pm_response([
        _target("NVDA", risk=0.75, catalyst=LIVE_NVDA_CATALYST),
        _target("GEV", risk=2.0),
    ]))
    decision, result = agent.decide(
        analyses=[_analysis("NVDA", target=104.0), _analysis("GEV", target=112.0)],
        positions=[], macro_analysis=None, cash_balance=50_000,
        total_value=100_000,
        active_state_changes=ACTIVE_STATE_CHANGES,
        allowed_buy_symbols={"NVDA", "GEV"},
    )
    assert decision is not None, f"decide() failed closed: {result.semantic_error}"
    assert {t.symbol for t in decision.targets} == {"GEV"}
    assert result.semantic_status in (None, "success")


@patch("anthropic.Anthropic")
def test_decide_caps_a_verified_subfloor_pick_rather_than_dropping_it(mock_cls):
    """The capability is preserved. NVDA cites a real row that names it, so
    it trades — at the smallest size the desk can hold."""
    agent = _mock_agent(mock_cls, _pm_response([
        _target("NVDA", risk=3.0,
                catalyst="2026-08-31: Anthropic/Lambda $35bn cloud deal"),
    ]))
    decision, _ = agent.decide(
        analyses=[_analysis("NVDA", target=104.0)],
        positions=[], macro_analysis=None, cash_balance=50_000,
        total_value=100_000,
        active_state_changes=ACTIVE_STATE_CHANGES,
        allowed_buy_symbols={"NVDA"},
    )
    assert decision is not None
    assert [t.symbol for t in decision.targets] == ["NVDA"]
    assert decision.targets[0].risk_allocation_pct == STARTER_POSITION_RISK_PCT


@patch("anthropic.Anthropic")
def test_decide_defaults_to_the_production_thresholds(mock_cls):
    """A caller that threads no config — the model-policy harness, most
    tests — must gate on exactly the numbers production uses, not on a
    second opinion about them."""
    agent = _mock_agent(mock_cls, _pm_response([
        _target("NVDA", risk=3.0, catalyst=LIVE_NVDA_CATALYST),
    ]))
    decision, _ = agent.decide(
        analyses=[_analysis("NVDA", target=104.0)],
        positions=[], macro_analysis=None, cash_balance=50_000,
        total_value=100_000,
        active_state_changes=ACTIVE_STATE_CHANGES,
        allowed_buy_symbols={"NVDA"},
    )
    assert decision is not None
    assert decision.targets == []


# --------------------------------------------------------------------------
# Single-definition guards
# --------------------------------------------------------------------------

def test_status_keys_are_stable_greppable_constants():
    assert SUBFLOOR_CATALYST_UNVERIFIED_STATUS == "pm_subfloor_catalyst_unverified"
    assert SUBFLOOR_SIZE_CAPPED_STATUS == "pm_subfloor_size_capped"


def test_the_gate_reuses_the_risk_configs_own_numbers():
    """The floor the PM is gated on must be the floor the constructor
    enforces, and the cap must be the size the risk budget will actually
    grant — one definition each, not a second opinion that can drift."""
    from src.config import RiskConfig

    fields = RiskConfig.model_fields
    assert fields["min_reward_risk_after_widening"].default == REWARD_RISK_FLOOR
    assert fields["min_position_risk_pct"].default == STARTER_POSITION_RISK_PCT
