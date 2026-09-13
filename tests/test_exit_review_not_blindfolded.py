"""The AI Risk Manager must not be lied to about what it can see on the exit path.

Defect (fixed 2026-09-13): `pipeline._risk_review_exits` reused the morning
plan's renderer verbatim. Two consequences, both false statements to the seat:

1. The synthetic `ReasoningChain` it built never set `continuity_check` or
   `premortem_check` — fields that exist only in the PORTFOLIO MANAGER's
   schema, not the position reviewer's — so the renderer stamped both with
   "[MISSING — ... Treat the audit step as NOT PERFORMED]". The standing prompt
   then tells the seat that a missing audit step means "do not extend the plan
   the benefit of the doubt elsewhere". On every exit review ever run, the seat
   was told the analyst had skipped both mandatory red-team steps. It had not:
   those steps do not exist on that path.
2. It passed no news, no earnings, no cash, no drawdown state, no holding ages
   and no event-risk block, while the checklist told the seat to "check the
   News and Tech blocks yourself for the trigger PM claims".

Archive evidence: row id 330 of the pre-reset database (2026-09-01 19:32, run
`close-0e9129f1`) shows both banners, `Tech Analyst Signals (not provided)`,
`News Intelligence (not provided)`, event risk NOT FETCHED on all three
sub-blocks, and `System performance: not provided`.

Why the direction matters: a veto here does not stop a purchase, it stops a
SALE. A position whose thesis has broken stays on the book overnight with only
the broker stop behind it.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.agents import risk_review_mode
from src.agents.risk_manager import RiskManagerAgent
from src.models import (
    PortfolioDecision, Position, ReasoningChain, TradeDecision,
)

#: The exact banner text the exit path must never carry.
NOT_PERFORMED = "Treat the audit step as NOT PERFORMED"


def _agent() -> RiskManagerAgent:
    return RiskManagerAgent(api_key="k", model="m")


def _positions() -> list[Position]:
    return [
        Position(symbol="DIS", qty=3.0, avg_entry=108.09, current_price=106.21,
                 market_value=318.63, unrealized_pnl=-5.65,
                 sector="Communication Services"),
        Position(symbol="V", qty=1.0, avg_entry=380.33, current_price=373.70,
                 market_value=373.70, unrealized_pnl=-6.63,
                 sector="Financial Services"),
    ]


def _exit_decisions() -> list[TradeDecision]:
    return [
        TradeDecision(action="SELL", symbol="DIS", allocation_pct=50.0,
                      entry_price=0.0, stop_loss=0.0, take_profit=0.0,
                      reasoning="thesis_progress=-19.01% and nearing stop"),
        TradeDecision(action="SELL", symbol="V", allocation_pct=100.0,
                      entry_price=0.0, stop_loss=0.0, take_profit=0.0,
                      reasoning="R=-1.09, distance_to_stop=2.98%"),
    ]


def _exit_proposal() -> PortfolioDecision:
    """What `_risk_review_exits` now builds: the reviewer's five real fields,
    the two PM-only audit fields left unset because they do not exist here."""
    return PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter="Regime unchanged since morning: risk-on, VIX falling.",
            earnings_check="DIS thesis_progress -19.01%; V R=-1.09.",
            news_check=risk_review_mode.UNUSED_SLOT,
            signal_conflicts="Both cite deterioration confirmed against stops.",
            sizing_logic="DIS half, V full close before the session ends.",
            portfolio_balance="MSFT held; winners are not being trimmed.",
            cash_target="Close session — reduce overnight exposure.",
        ),
        decisions=_exit_decisions(),
        portfolio_view="EXIT REVIEW (position reviewer): mixed book.",
    )


def _morning_proposal() -> PortfolioDecision:
    return PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter="risk-on", news_check="nothing adverse",
            earnings_check="none inside window", signal_conflicts="none",
            sizing_logic="proportional", portfolio_balance="balanced",
            cash_target="90% invested",
        ),
        decisions=[
            TradeDecision(action="BUY", symbol="SPY", allocation_pct=10.0,
                          entry_price=507.0, stop_loss=490.0,
                          take_profit=530.0, reasoning="uptrend"),
        ],
        portfolio_view="Bullish",
    )


def _render(proposal: PortfolioDecision, **kwargs) -> str:
    base = dict(
        portfolio_decision=proposal, positions=_positions(),
        macro_summary={}, rule_violations=[], total_value=9817.0,
    )
    base.update(kwargs)
    return _agent().build_user_message(**base)


# --------------------------------------------------------------------------
# 1. The banner for steps that do not exist on this path.
# --------------------------------------------------------------------------

def test_exit_review_carries_no_not_performed_banner():
    """THE defect. The exit message must not accuse the reviewer of skipping
    two audit steps its schema has never had."""
    message = _render(_exit_proposal(),
                      review_mode=risk_review_mode.EXIT_REVIEW)
    assert NOT_PERFORMED not in message
    assert "Continuity check" not in message
    assert "Pre-mortem check" not in message


def test_exit_review_says_those_fields_belong_to_a_different_schema():
    """Silence would leave the seat to wonder. It is told why they are gone."""
    message = _render(_exit_proposal(),
                      review_mode=risk_review_mode.EXIT_REVIEW)
    assert "NO `continuity_check` and NO `premortem_check`" in message
    assert "not a skipped audit step" in message


def test_exit_chain_is_labelled_as_the_position_reviewers_not_pms():
    message = _render(_exit_proposal(),
                      review_mode=risk_review_mode.EXIT_REVIEW)
    assert "## Position Reviewer Reasoning Chain" in message
    assert "## PM Reasoning Chain" not in message
    # The reviewer's own field names, not PM's.
    for label in ("Macro continuity check", "Thesis progress check",
                  "Thesis integrity check", "Execution rationale",
                  "Winners discipline check", "Session disposition check"):
        assert f"- {label}:" in message, label
    # PM's labels must not appear on this path — auditing "sizing logic"
    # against a sentence about execution is what the relabel fixes.
    for label in ("Sizing logic", "Portfolio balance", "Cash target",
                  "News check", "Earnings check", "Macro filter"):
        assert f"- {label}:" not in message, label


# --------------------------------------------------------------------------
# 2. Blocks that are absent by construction are described as such.
# --------------------------------------------------------------------------

def test_absent_tech_block_is_unavailable_by_design_not_not_provided():
    """No TechAnalyst call runs on the midday/close loop. Saying '(not
    provided)' invites the seat to read it as an omission and refuse."""
    message = _render(_exit_proposal(),
                      review_mode=risk_review_mode.EXIT_REVIEW)
    assert "## Tech Analyst Signals\n(not provided)" not in message
    assert "UNAVAILABLE BY DESIGN ON THIS PATH" in message
    assert "do NOT refuse an exit for lacking Tech confirmation" in message


def test_absent_news_block_says_unknown_not_quiet():
    message = _render(_exit_proposal(),
                      review_mode=risk_review_mode.EXIT_REVIEW)
    assert "## News Intelligence\n(not provided)" not in message
    assert "Treat today's news as UNKNOWN rather than as quiet" in message


def test_exit_header_disapplies_the_checks_that_cannot_apply():
    message = _render(_exit_proposal(),
                      review_mode=risk_review_mode.EXIT_REVIEW)
    assert "## Review Mode: EXIT REVIEW" in message
    # The $0.0 geometry is explained rather than left as an apparent data bug.
    assert "structural zero" in message
    # The veto's real-world direction is stated.
    assert "A wrongly-refused exit" in message
    # The deterministic gates that already cover checklist item 8.
    assert "holding_discipline_claim_check" in message
    assert "named-trigger gate" in message


# --------------------------------------------------------------------------
# 3. Inputs newly passed actually appear.
# --------------------------------------------------------------------------

def test_newly_passed_inputs_reach_the_message():
    message = _render(
        _exit_proposal(),
        review_mode=risk_review_mode.EXIT_REVIEW,
        cash=1234.0,
        recent_performance={"rolling_5d_pct": -1.4, "rolling_20d_pct": 2.2,
                            "in_drawdown": False, "trailing_days": 20},
        position_history={"DIS": {"days_held": 6}, "V": {"days_held": 11}},
        earnings_analyses=[{"symbol": "DIS", "queued": True,
                            "form_type": "8-K", "filing_date": "2026-09-01"}],
    )
    assert "Cash (deployable this session): $1,234" in message
    assert "System performance: not provided" not in message
    assert "in_drawdown=false" in message
    assert "held: 6d" in message and "held: 11d" in message
    assert "held: unknown" not in message
    assert "## Earnings" in message


def test_no_fabricated_placeholder_reaches_an_empty_chain_field():
    """The call site used to write `or "n/a"` into six fields. A fabricated
    "n/a" reads as a real answer; `[EMPTY]` reads as what it is."""
    n = risk_review_mode.NOT_AUTHORED
    empty = PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter=n, earnings_check=n, news_check=n,
            signal_conflicts=n, sizing_logic=n,
            portfolio_balance=n, cash_target=n,
        ),
        decisions=_exit_decisions(),
        portfolio_view="EXIT REVIEW (position reviewer): x",
    )
    message = _render(empty, review_mode=risk_review_mode.EXIT_REVIEW)
    assert "- Macro continuity check: [NOT AUTHORED" in message
    assert "NOT that the reviewer skipped a step" in message
    assert NOT_PERFORMED not in message
    assert ": n/a" not in message


# --------------------------------------------------------------------------
# 4. The morning plan path is untouched.
# --------------------------------------------------------------------------

def test_morning_plan_rendering_is_unchanged_by_default():
    """No `review_mode` argument at all — every pre-existing call site."""
    message = _render(_morning_proposal())
    assert "## PM Reasoning Chain — PM's CLAIMS about its own plan" in message
    assert "## Review Mode: EXIT REVIEW" not in message
    assert "- Macro filter:" in message
    assert "- Continuity check:" in message
    assert "- Pre-mortem check:" in message
    assert "## Tech Analyst Signals\n(not provided)" in message


def test_morning_plan_still_banners_a_genuinely_skipped_pm_audit_step():
    """The banner is CORRECT on the morning path and must survive. PM's own
    prompt makes both fields mandatory while the schema defaults them to ""."""
    message = _render(_morning_proposal(),
                      review_mode=risk_review_mode.MORNING_PLAN)
    assert message.count(NOT_PERFORMED) == 2


def test_explicit_morning_mode_is_byte_identical_to_no_mode_at_all():
    proposal = _morning_proposal()
    assert _render(proposal) == _render(
        proposal, review_mode=risk_review_mode.MORNING_PLAN
    )


@pytest.mark.parametrize("bogus", ["", None, "exit", "exit review", "nonsense"])
def test_unrecognised_mode_degrades_to_the_strictest_rendering(bogus):
    """An unknown string must never abort a risk review, and must never
    accidentally suppress the morning path's real banners."""
    assert risk_review_mode.normalize(bogus) == risk_review_mode.MORNING_PLAN


def test_exit_review_mode_is_recognised_case_insensitively():
    assert risk_review_mode.is_exit_review("EXIT_REVIEW")
    assert risk_review_mode.is_exit_review(" exit_review ")
    assert risk_review_mode.is_exit_review(risk_review_mode.EXIT_REVIEW)
    assert not risk_review_mode.is_exit_review(risk_review_mode.MORNING_PLAN)


# --------------------------------------------------------------------------
# 5. The call site passes the mode and the inputs.
# --------------------------------------------------------------------------

def _pipeline_double():
    """A bare pipeline object with only what `_risk_review_exits` touches."""
    from src.pipeline import TradingPipeline

    pipe = TradingPipeline.__new__(TradingPipeline)
    pipe.risk_manager = MagicMock()
    pipe.db = MagicMock()
    pipe.market = None          # forces the labelled NOT FETCHED earnings form
    pipe.config = MagicMock()
    return pipe


class _ReviewAction:
    def __init__(self, symbol, action, reason):
        self.symbol, self.action, self.reason = symbol, action, reason


class _ReviewChain:
    macro_continuity_check = "regime unchanged"
    thesis_progress_check = "DIS -19.01%"
    thesis_integrity_check = "both cite named triggers"
    execution_rationale = "close before the bell"
    winners_discipline_check = "MSFT held"
    session_disposition_check = "close session"


class _Review:
    overall_assessment = "mixed"
    reasoning_chain = _ReviewChain()
    actions = [_ReviewAction("DIS", "REDUCE", "thesis broken"),
               _ReviewAction("V", "SELL", "stop proximity")]


def test_call_site_declares_exit_review_mode_and_passes_the_evidence():
    pipe = _pipeline_double()
    verdict = MagicMock()
    verdict.approved = True
    verdict.rejections_by_symbol.return_value = {}
    verdict.reasoning = "fine"
    pipe.risk_manager.review.return_value = (verdict, MagicMock())

    with patch.object(type(pipe), "_build_portfolio_heat",
                      return_value=None, create=True), \
         patch.object(type(pipe), "_build_position_history",
                      return_value={"DIS": {"days_held": 6}}, create=True):
        vetoed, got = pipe._risk_review_exits(
            _Review(), _positions(), run_id="r1", total_value=9817.0,
            macro_summary={"vix": {"current": 14.9}},
            news_intel=None, earnings_analyses=[], cash=1234.0,
            reserve_balance=0.0, recent_performance={"in_drawdown": False},
        )

    assert vetoed == set() and got is verdict
    kwargs = pipe.risk_manager.review.call_args.kwargs
    assert kwargs["review_mode"] == risk_review_mode.EXIT_REVIEW
    assert kwargs["cash"] == 1234.0
    assert kwargs["recent_performance"] == {"in_drawdown": False}
    assert kwargs["position_history"] == {"DIS": {"days_held": 6}}
    assert kwargs["event_risk_block"].strip()
    # The two PM-only audit fields are left unset — never invented.
    chain = kwargs["portfolio_decision"].reasoning_chain
    assert chain.continuity_check == ""
    assert chain.premortem_check == ""
    # And no `or "n/a"` / cross-reference placeholder is written anywhere.
    assert chain.news_check == risk_review_mode.UNUSED_SLOT
    assert chain.macro_filter == "regime unchanged"
    assert chain.earnings_check == "DIS -19.01%"


def test_exit_call_site_message_is_clean_end_to_end():
    """Render what the call site actually builds, through the real renderer."""
    pipe = _pipeline_double()
    captured = {}

    def _fake_review(**kwargs):
        captured["message"] = _agent().build_user_message(
            portfolio_decision=kwargs["portfolio_decision"],
            positions=kwargs["positions"],
            macro_summary=kwargs["macro_summary"],
            rule_violations=kwargs["rule_violations"],
            total_value=kwargs["total_value"],
            cash=kwargs["cash"],
            recent_performance=kwargs["recent_performance"],
            position_history=kwargs["position_history"],
            event_risk_block=kwargs["event_risk_block"],
            review_mode=kwargs["review_mode"],
        )
        verdict = MagicMock()
        verdict.approved = True
        verdict.rejections_by_symbol.return_value = {}
        verdict.reasoning = "ok"
        return verdict, MagicMock()

    pipe.risk_manager.review.side_effect = _fake_review
    with patch.object(type(pipe), "_build_portfolio_heat",
                      return_value=None, create=True), \
         patch.object(type(pipe), "_build_position_history",
                      return_value={"DIS": {"days_held": 6},
                                    "V": {"days_held": 11}}, create=True):
        pipe._risk_review_exits(
            _Review(), _positions(), run_id="r1", total_value=9817.0,
            macro_summary={}, cash=1234.0,
            recent_performance={"in_drawdown": False, "rolling_5d_pct": -1.0},
        )

    message = captured["message"]
    assert NOT_PERFORMED not in message
    assert "## Review Mode: EXIT REVIEW" in message
    assert "held: unknown" not in message
    assert "System performance: not provided" not in message
    assert "## Tech Analyst Signals\n(not provided)" not in message


def test_verdict_none_still_fails_open():
    """Unchanged, and load-bearing: refusing an exit because a model is
    unavailable leaves a broken thesis on the book overnight."""
    pipe = _pipeline_double()
    pipe.risk_manager.review.return_value = (None, MagicMock())
    with patch.object(type(pipe), "_build_portfolio_heat",
                      return_value=None, create=True), \
         patch.object(type(pipe), "_build_position_history",
                      return_value={}, create=True):
        vetoed, verdict = pipe._risk_review_exits(
            _Review(), _positions(), run_id="r1", total_value=9817.0,
            macro_summary={},
        )
    assert vetoed == set() and verdict is None
