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
    ExitReviewChain, PortfolioDecision, Position, ReasoningChain,
    TradeDecision,
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
    """What `_risk_review_exits` now builds: the reviewer's six real fields in
    an `ExitReviewChain`, every PM-only slot left unset because it does not
    exist here. `news_check` used to carry a placeholder string purely to
    satisfy the parent's `min_length=1`; the subclass removes that demand."""
    return PortfolioDecision(
        reasoning_chain=ExitReviewChain(
            macro_filter="Regime unchanged since morning: risk-on, VIX falling.",
            earnings_check="DIS thesis_progress -19.01%; V R=-1.09.",
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
    flat = " ".join(message.split())
    assert "NO `continuity_check` and NO `premortem_check`" in flat
    assert "not a skipped audit step" in flat
    # Row 330 tagged `data_degraded` off the false banner. Pin the correction.
    assert "do not tag `data_degraded` for it" in flat


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
    assert "do not refuse an exit for lacking Tech confirmation" in message


def test_absent_news_block_says_unknown_not_quiet():
    message = _render(_exit_proposal(),
                      review_mode=risk_review_mode.EXIT_REVIEW)
    assert "## News Intelligence\n(not provided)" not in message
    assert "Treat today's news as UNKNOWN rather than as quiet" in message


def test_exit_header_disapplies_the_checks_that_cannot_apply():
    message = _render(_exit_proposal(),
                      review_mode=risk_review_mode.EXIT_REVIEW)
    flat = " ".join(message.split())
    assert "## Review Mode: EXIT REVIEW" in message
    # The $0.0 geometry is explained rather than left as an apparent data bug.
    assert "`$0.0` entry, stop and target are structural" in flat
    # The veto's real-world direction is stated.
    assert "Refusing an exit leaves the position ON THE BOOK" in flat


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
    # And no `or "n/a"` / cross-reference / unused-slot placeholder is
    # written anywhere. `news_check` has no exit counterpart and is now left
    # genuinely empty rather than filled to satisfy a constraint.
    assert isinstance(chain, ExitReviewChain)
    assert chain.news_check == ""
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


# --------------------------------------------------------------------------
# 6. Regressions found in adversarial review of the first draft.
# --------------------------------------------------------------------------

def test_event_risk_checklist_cannot_be_read_as_a_reason_to_refuse_an_exit():
    """Checklist 4 says a fetched event inside the window means "downsize or
    reject". `_exit_event_risk_block` is what first gives this path a fetched
    date to trigger on, so the first draft ADDED refusal pressure that did not
    exist before it. On an entry, refusing carries less risk through the event;
    here it carries the position THROUGH the event. The instruction inverts."""
    message = _render(_exit_proposal(),
                      review_mode=risk_review_mode.EXIT_REVIEW)
    flat = " ".join(message.split())
    assert "Event proximity is **not a reason to refuse an exit**" in flat
    assert "refusing HERE carries the position THROUGH it" in flat
    # And it is no longer a mandatory output field on this path, so the seat
    # is not compelled to produce a paragraph about an inverted instruction.
    assert "`event_risk` are\nOPTIONAL here" in message
    assert "still answer `event_risk`" not in flat


def test_checklist_8_is_not_stood_down():
    """The four Python gates run AFTER this review — which is precisely the
    reason checklist 8 exists. The first draft used that downstream-ness as
    grounds to delete the instruction; the same fact cannot be both."""
    message = _render(_exit_proposal(),
                      review_mode=risk_review_mode.EXIT_REVIEW)
    flat = " ".join(message.split())
    assert "Checklist 8 still applies and is the substance of your job" in flat
    # Stood-down items are named explicitly; 8 must not be among them.
    disapplied = flat[flat.index("**Does not apply here.**"):
                      flat.index("**Checklist 8 still applies")]
    assert "Checklist 8" not in disapplied


def test_the_four_gates_are_described_with_their_real_limits():
    """The first draft listed them as coverage. Each abstains somewhere."""
    message = _render(_exit_proposal(),
                      review_mode=risk_review_mode.EXIT_REVIEW)
    flat = " ".join(message.split())
    assert "after you speak" in flat
    assert "checks only that the reason says recognised words" in flat
    assert "bypassed whenever the reason cites external information" in flat
    assert "does not run at all without recorded prior metrics" in flat
    assert "passes every unverifiable claim by design" in flat
    assert "None of them can catch a plausibly-worded" in flat


def test_seat_is_told_refusal_is_its_only_live_lever():
    """`_apply_risk_modifications` is called only from the morning stage, and
    `_risk_review_exits`' verdict is consumed for `rejected_symbols` alone. So
    `modifications` and `scale_all_buys` do nothing here — and since
    2026-09-14 they are not fields of `ExitRiskVerdict` at all. Telling the
    seat how to edit `allocation_pct` on this path is the same class of false
    statement this change exists to remove."""
    message = _render(_exit_proposal(),
                      review_mode=risk_review_mode.EXIT_REVIEW)
    flat = " ".join(message.split())
    assert "Refusal is your only lever" in flat
    assert (
        "`modifications` and `scale_all_buys` are **not fields of this "
        "path's output**" in flat
    )
    assert "do not size it and do not comment on it" in flat


def test_exit_header_stays_under_its_size_budget():
    """Archived exit prompts ran 7,775-8,428 characters. A header that grows
    the message by half again is a real cost with no rig able to measure the
    benefit, so its size is pinned rather than left to drift."""
    header = risk_review_mode.mode_header(risk_review_mode.EXIT_REVIEW)
    assert 0 < len(header) <= 3000, len(header)
    assert risk_review_mode.mode_header(risk_review_mode.MORNING_PLAN) == ""


# --------------------------------------------------------------------------
# 6. The SCHEMA, not just the prompt (2026-09-14).
#
# PR #343 gave the exit path its own header and its own chain rows, so the
# PROMPT had diverged. Both seats still answered ONE schema, which meant the
# exit seat was still being ASKED for the things that header had just told it
# were inapplicable. Two classes of residue:
#
#   - `modifications` / `scale_all_buys` — applied only by
#     `_apply_risk_modifications`, called only from the morning `RiskStage`.
#     Emitted, parsed, stored, discarded.
#   - `rr_audit` / `sizing_sanity` / `event_risk` — `min_length=1`, i.e. a
#     DEMAND for an answer, on the three steps the exit header stands down or
#     inverts. Measured harm: archived row 330 (2026-09-01, `close-0e9129f1`)
#     answered `event_risk` "next earnings dates were NOT FETCHED this run"
#     and set `reason_category: "data_degraded"` on that basis — a label
#     `portfolio_manager` reads back to self-calibrate.
#
# Every test below is paired with a morning-path assertion, because the
# morning path uses all of it for real.
# --------------------------------------------------------------------------

def _exit_verdict_json(**over) -> dict:
    body = {
        "approved": True,
        "reasoning_chain": {
            "signal_fidelity": "Reviewer's trigger matches the news block.",
            "correlation_check": "Closing both leaves no cluster behind.",
            "overall": "Both exits hold up; approved.",
        },
        "reasoning": "Exits are sound.",
    }
    body.update(over)
    return body


def test_exit_verdict_has_no_levers_that_nothing_applies():
    from src.models import ExitRiskVerdict, RiskVerdict
    assert "modifications" not in ExitRiskVerdict.model_fields
    assert "scale_all_buys" not in ExitRiskVerdict.model_fields
    # The morning verdict keeps both — `_apply_risk_modifications` and the
    # constructor's BUY scaling are real consumers there.
    assert "modifications" in RiskVerdict.model_fields
    assert "scale_all_buys" in RiskVerdict.model_fields


def test_only_the_morning_stage_applies_modifications():
    """The load-bearing fact under the whole change. If a second call site
    ever applies them, the exit schema must grow them back."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    callers = sorted(
        p.name
        for p in (root / "src").rglob("*.py")
        if "_apply_risk_modifications(" in p.read_text()
    )
    # Definition site + the single caller. Nothing else.
    assert callers == ["pipeline.py", "pipeline_stages.py"], callers


def test_exit_chain_does_not_demand_the_steps_its_prompt_stands_down():
    from src.models import ExitRiskReasoningChain, RiskReasoningChain
    stood_down = ("rr_audit", "sizing_sanity", "event_risk")
    for name in stood_down:
        assert not ExitRiskReasoningChain.model_fields[name].is_required(), name
        # ... and all three stay MANDATORY on the morning plan, where the
        # entry geometry, the sizing and the event window are all real.
        assert RiskReasoningChain.model_fields[name].is_required(), name
    # The three that are live questions on an exit stay mandatory here too.
    for name in ("signal_fidelity", "correlation_check", "overall"):
        assert ExitRiskReasoningChain.model_fields[name].is_required(), name


def test_exit_chain_omitting_the_stood_down_steps_validates():
    from src.models import ExitRiskReasoningChain, RiskReasoningChain
    chain = ExitRiskReasoningChain(
        signal_fidelity="a", correlation_check="b", overall="c",
    )
    assert chain.rr_audit == "" and chain.sizing_sanity == ""
    assert chain.event_risk == ""
    # Same payload against the morning chain must still fail — the morning
    # BUY path is not weakened by any of this.
    with pytest.raises(Exception):
        RiskReasoningChain(signal_fidelity="a", correlation_check="b",
                           overall="c")


def test_exit_review_parses_into_the_exit_verdict_shape():
    from src.models import ExitRiskVerdict
    agent = _agent()
    with patch.object(RiskManagerAgent, "run") as run:
        run.return_value = MagicMock(
            parse_json=lambda: _exit_verdict_json(),
        )
        verdict, _ = agent.review(
            portfolio_decision=_exit_proposal(), positions=_positions(),
            macro_summary={}, rule_violations=[], total_value=9817.0,
            review_mode=risk_review_mode.EXIT_REVIEW,
        )
    assert isinstance(verdict, ExitRiskVerdict)
    assert verdict.approved is True
    assert verdict.rejections_by_symbol() == {}


def test_a_lever_the_exit_seat_emits_anyway_is_dropped_not_stored():
    """Belt and braces: the prompt says do not emit them, but a model that
    emits them anyway must not have them recorded as if they meant something."""
    agent = _agent()
    with patch.object(RiskManagerAgent, "run") as run:
        run.return_value = MagicMock(parse_json=lambda: _exit_verdict_json(
            scale_all_buys=0.0,
            modifications=[{"symbol": "DIS", "field": "allocation_pct",
                            "original_value": 50.0, "new_value": 10.0,
                            "reason": "x"}],
        ))
        verdict, _ = agent.review(
            portfolio_decision=_exit_proposal(), positions=_positions(),
            macro_summary={}, rule_violations=[], total_value=9817.0,
            review_mode=risk_review_mode.EXIT_REVIEW,
        )
    dumped = verdict.model_dump()
    assert "modifications" not in dumped and "scale_all_buys" not in dumped


def test_exit_refusal_still_reaches_the_caller_per_symbol():
    """Refusal is the ONE lever this path has. Removing the other two must
    not touch it, and doctrine requires a durable per-symbol reason."""
    agent = _agent()
    with patch.object(RiskManagerAgent, "run") as run:
        run.return_value = MagicMock(parse_json=lambda: _exit_verdict_json(
            rejected_symbols=[{"symbol": "dis",
                               "reason": "trigger cites a filing that is not "
                                         "in the news block"}],
            reason_category="signal_fidelity",
        ))
        verdict, _ = agent.review(
            portfolio_decision=_exit_proposal(), positions=_positions(),
            macro_summary={}, rule_violations=[], total_value=9817.0,
            review_mode=risk_review_mode.EXIT_REVIEW,
        )
    assert verdict.rejections_by_symbol() == {
        "DIS": "trigger cites a filing that is not in the news block",
    }
    assert verdict.reason_category == "signal_fidelity"


def test_morning_review_still_parses_into_the_full_verdict():
    """The regression that matters most: the BUY path must be untouched."""
    from src.models import RiskVerdict
    agent = _agent()
    payload = {
        "approved": True,
        "reasoning_chain": {
            "rr_audit": "a", "signal_fidelity": "b", "correlation_check": "c",
            "event_risk": "d", "sizing_sanity": "e", "overall": "f",
        },
        "modifications": [{"symbol": "NVDA", "field": "allocation_pct",
                           "original_value": 15.0, "new_value": 7.5,
                           "reason": "earnings inside the window"}],
        "scale_all_buys": 0.5,
        "reason_category": "event_risk",
        "reasoning": "trimmed",
    }
    with patch.object(RiskManagerAgent, "run") as run:
        run.return_value = MagicMock(parse_json=lambda: payload)
        verdict, _ = agent.review(
            portfolio_decision=_morning_proposal(), positions=_positions(),
            macro_summary={}, rule_violations=[], total_value=9817.0,
        )
    assert isinstance(verdict, RiskVerdict)
    assert verdict.scale_all_buys == 0.5
    assert len(verdict.modifications) == 1
    assert verdict.modifications[0].new_value == 7.5


def test_exit_repair_is_not_failed_closed_over_a_lever_that_does_nothing():
    """`_decision_fields_unchanged` fails a verdict CLOSED when a repair
    changes decision-bearing content. On the exit path `modifications` and
    `scale_all_buys` decide nothing, so drift there must not kill the verdict
    — failing closed here leaves a broken-thesis position on the book, the
    exact asymmetry `_risk_review_exits` fails OPEN for."""
    unchanged = RiskManagerAgent._decision_fields_unchanged
    original = {"approved": True, "rejected_symbols": [],
                "reason_category": "clean", "scale_all_buys": 1.0}
    repaired = {"approved": True, "rejected_symbols": [],
                "reason_category": "clean", "scale_all_buys": 0.0,
                "modifications": [{"symbol": "DIS", "field": "allocation_pct",
                                   "original_value": 1.0, "new_value": 2.0,
                                   "reason": "x"}]}
    assert unchanged(original, repaired,
                     fields=RiskManagerAgent._EXIT_DECISION_FIELDS) is True
    # Morning default: the same drift IS an unauthorized re-decision.
    assert unchanged(original, repaired) is False


def test_exit_repair_still_fails_closed_on_a_changed_refusal():
    """The lever that DOES do something on this path keeps its guard."""
    unchanged = RiskManagerAgent._decision_fields_unchanged
    original = {"approved": True, "reason_category": "signal_fidelity",
                "rejected_symbols": [{"symbol": "DIS", "reason": "r"}]}
    repaired = {"approved": True, "reason_category": "signal_fidelity",
                "rejected_symbols": []}
    assert unchanged(original, repaired,
                     fields=RiskManagerAgent._EXIT_DECISION_FIELDS) is False


def test_exit_input_chain_needs_no_placeholder_for_a_pm_only_slot():
    """`news_check` has no exit counterpart and no rendered row. It was being
    filled with a marker string only to satisfy the parent's `min_length=1`."""
    chain = ExitReviewChain(
        macro_filter="a", earnings_check="b", signal_conflicts="c",
        sizing_logic="d", portfolio_balance="e", cash_target="f",
    )
    assert chain.news_check == ""
    # The morning chain still refuses the same omission.
    with pytest.raises(Exception):
        ReasoningChain(
            macro_filter="a", earnings_check="b", signal_conflicts="c",
            sizing_logic="d", portfolio_balance="e", cash_target="f",
        )
