"""Phase 10.1 — one failing leg must die alone.

`RiskVerdict.approved` was a single bool for the whole plan, and
`RiskModification` could retune a symbol but not refuse one. So the only way
the risk manager could say "not this trade" was to say "not any trade".

Production evidence, run `run-64290730` (2026-09-01 morning): the verdict
rejected the entire plan citing XLE alone — constructed R/R 1.18, under the
1.5 floor — and CHPX died with it at R/R 3.03, a different sector and an
unrelated technical thesis. Zero trades that morning. CHPX was never judged;
it was standing next to XLE.

`rejected_symbols` gives the verdict a per-symbol outcome. The distinction
these tests exist to pin is which failures are which:

  - a PER-SYMBOL failure (R/R breach on one name, event risk on one name)
    refuses that name and nothing else;
  - a BOOK-level failure (correlation cluster, total exposure, drawdown) is a
    property of the whole account and STILL refuses everything.

Getting that second half wrong would be worse than the defect being fixed, so
half of this file is about the book-level veto continuing to work.

No threshold moves here. This is the granularity of refusal, nothing else.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.agents.base import AgentResult
from src.agents.risk_manager import RiskManagerAgent
from src.models import (
    PortfolioDecision, ReasoningChain, RiskReasoningChain, RiskVerdict,
    SymbolRejection, TradeDecision,
)
from src.pipeline_context import RunContext
from src.pipeline_stages import RiskStage

PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "prompts" / "risk_manager.md"
)

# The two legs of run-64290730, with the geometry that produced each R/R.
XLE_RR = "constructed R/R 1.18:1 is below the 1.5 floor and PM named no catalyst"
CHPX_RR = "R/R 3.03:1"


def _rc() -> RiskReasoningChain:
    return RiskReasoningChain(
        rr_audit="x", signal_fidelity="x", correlation_check="x",
        event_risk="x", sizing_sanity="x", overall="x",
    )


def _pm_rc() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="x", news_check="x", earnings_check="x",
        signal_conflicts="x", sizing_logic="x", portfolio_balance="x",
        cash_target="x",
    )


def _xle() -> TradeDecision:
    """R/R (90.11 - 87.40) / (87.40 - 85.10) = 1.18 — under the floor."""
    return TradeDecision(
        action="BUY", symbol="XLE", allocation_pct=5.0, entry_price=87.40,
        stop_loss=85.10, take_profit=90.11, reasoning="energy rotation",
    )


def _chpx() -> TradeDecision:
    """R/R (28.55 - 24.00) / (24.00 - 22.50) = 3.03 — comfortably passing."""
    return TradeDecision(
        action="BUY", symbol="CHPX", allocation_pct=6.0, entry_price=24.00,
        stop_loss=22.50, take_profit=28.55, reasoning="breakout, unrelated thesis",
    )


def _stage_pipeline(*, verdict, decisions):
    """A pipeline stubbed just far enough for RiskStage.run() to reach the
    verdict-application block, with an HONEST pass-through hard-risk filter:
    it returns whatever it is handed. Any symbol missing at the end was
    removed by the code under test, not by a mock."""
    from src.pipeline import TradingPipeline

    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.db = MagicMock()
    pipeline._sweeper = MagicMock(return_value=None)
    pipeline._filter_supported_symbols = MagicMock(return_value=(decisions, []))
    pipeline._clamp_queued_earnings_buys = MagicMock(return_value=decisions)
    pipeline._filter_hard_risk_decisions = MagicMock(
        side_effect=lambda d, *a, **kw: (list(d), [], []),
    )
    rm_result = MagicMock()
    rm_result.used_fallback = False
    rm_result.raw_text = "{}"
    pipeline.risk_manager = MagicMock()
    pipeline.risk_manager.review.return_value = (verdict, rm_result)
    return pipeline


def _ctx(decisions) -> RunContext:
    ctx = RunContext.start("morning")
    ctx.decision_id = f"{ctx.run_id}-dec-000001"
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(), decisions=decisions, portfolio_view="test",
    )
    return ctx


def _symbols(ctx) -> list[str]:
    return [d.symbol for d in ctx.portfolio_decision.decisions]


def _events(pipeline) -> list[tuple[str, str, str, str]]:
    """(symbol, stage, outcome, reason) for every pipeline_event written."""
    out = []
    for call in pipeline.db.insert_specialist_evidence.call_args_list:
        kwargs = call.kwargs
        if kwargs.get("kind") != "pipeline_event":
            continue
        payload = json.loads(kwargs["evidence_json"])
        out.append((
            kwargs.get("symbol"), payload.get("stage"),
            payload.get("outcome"), payload.get("reason"),
        ))
    return out


# ---------------------------------------------------------------------------
# The defect itself
# ---------------------------------------------------------------------------

def test_refusing_xle_no_longer_kills_chpx():
    """run-64290730, reproduced. One leg fails its own R/R floor; the other
    is in a different sector on an unrelated thesis and passes. Before this
    change the only lever available took both."""
    decisions = [_xle(), _chpx()]
    verdict = RiskVerdict(
        approved=True, reasoning_chain=_rc(), reason_category="rr_fail",
        rejected_symbols=[{"symbol": "XLE", "reason": XLE_RR}],
        reasoning=f"XLE refused on R/R. CHPX stands on its own at {CHPX_RR}.",
    )
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)
    ctx = _ctx(decisions)

    result = RiskStage(pipeline=pipeline).run(ctx)

    # None == "carry on to execution". A dict would be a terminal refusal.
    assert result is None
    assert _symbols(ctx) == ["CHPX"], (
        "the passing leg must survive a refusal aimed at a different symbol"
    )


def test_the_refused_symbol_carries_its_own_reason_not_the_runs():
    """The audit trail has to answer 'why did THIS name die', per name."""
    decisions = [_xle(), _chpx()]
    verdict = RiskVerdict(
        approved=True, reasoning_chain=_rc(), reason_category="rr_fail",
        rejected_symbols=[{"symbol": "XLE", "reason": XLE_RR}],
        reasoning="run-level narrative that is NOT the per-symbol reason",
    )
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)

    RiskStage(pipeline=pipeline).run(_ctx(decisions))

    risk_events = [e for e in _events(pipeline) if e[1] == "risk"]
    assert ("XLE", "risk", "rejected", XLE_RR) in risk_events
    assert not [e for e in risk_events if e[0] == "CHPX" and e[2] == "rejected"]

    # And a durable per-symbol evidence row, the same way a modification gets one.
    rejection_rows = [
        c.kwargs for c in pipeline.db.insert_specialist_evidence.call_args_list
        if c.kwargs.get("kind") == "rejection"
    ]
    assert len(rejection_rows) == 1
    assert rejection_rows[0]["symbol"] == "XLE"
    assert rejection_rows[0]["scope"] == "symbol"
    assert XLE_RR in rejection_rows[0]["evidence_json"]


def test_every_leg_refused_individually_still_ends_the_run():
    """Per-symbol refusal is not a way to trade something. When every leg is
    refused on its own merits the run is over — but each symbol carries its
    own reason rather than one shared sentence about a different symbol."""
    decisions = [_xle(), _chpx()]
    verdict = RiskVerdict(
        approved=True, reasoning_chain=_rc(), reason_category="rr_fail",
        rejected_symbols=[
            {"symbol": "XLE", "reason": XLE_RR},
            {"symbol": "CHPX", "reason": "stop sits inside the daily range"},
        ],
        reasoning="both refused, separately",
    )
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)

    result = RiskStage(pipeline=pipeline).run(_ctx(decisions))

    assert result["status"] == "rejected"
    assert result["orders"] == []
    assert XLE_RR in result["reason"]
    assert "stop sits inside the daily range" in result["reason"]


def test_refusal_naming_a_symbol_outside_the_plan_is_a_noop():
    decisions = [_chpx()]
    verdict = RiskVerdict(
        approved=True, reasoning_chain=_rc(), reason_category="rr_fail",
        rejected_symbols=[{"symbol": "XLE", "reason": XLE_RR}],
        reasoning="XLE is not in this plan",
    )
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)
    ctx = _ctx(decisions)

    assert RiskStage(pipeline=pipeline).run(ctx) is None
    assert _symbols(ctx) == ["CHPX"]


# ---------------------------------------------------------------------------
# What must NOT change: the book-level veto
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("category,reasoning", (
    ("correlation_risk",
     "XLE, CHPX and the held XOM are one energy cluster at 61% of the book"),
    ("concentration",
     "total exposure would reach 94% against a 90% ceiling"),
    ("oversized",
     "system is in drawdown; no new risk is appropriate today"),
))
def test_book_level_failure_still_refuses_every_leg(category, reasoning):
    """Correlation clusters, total exposure and drawdown state are properties
    of the WHOLE account. When the book is the problem, killing every leg
    remains the correct answer and this change must not soften it."""
    decisions = [_xle(), _chpx()]
    verdict = RiskVerdict(
        approved=False, reasoning_chain=_rc(), reason_category=category,
        reasoning=reasoning,
    )
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)
    ctx = _ctx(decisions)

    result = RiskStage(pipeline=pipeline).run(ctx)

    assert result == {"status": "rejected", "orders": [], "reason": reasoning}
    rejected = {e[0] for e in _events(pipeline) if e[1] == "risk" and e[2] == "rejected"}
    assert rejected == {"XLE", "CHPX"}


def test_book_level_veto_wins_over_a_per_symbol_list():
    """A verdict that sets both refuses everything. `approved=False` is
    evaluated first and is never narrowed by what the symbol list says."""
    decisions = [_xle(), _chpx()]
    verdict = RiskVerdict(
        approved=False, reasoning_chain=_rc(), reason_category="correlation_risk",
        rejected_symbols=[{"symbol": "XLE", "reason": XLE_RR}],
        reasoning="the book itself is the problem",
    )
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)
    ctx = _ctx(decisions)

    result = RiskStage(pipeline=pipeline).run(ctx)

    assert result["status"] == "rejected"
    assert _symbols(ctx) == ["XLE", "CHPX"], "no leg survives a book-level veto"


def test_scale_all_buys_still_applies_to_the_survivors():
    """Portfolio-level sizing is untouched by this change: it applies to
    whatever remains after a per-symbol refusal, exactly as before."""
    decisions = [_xle(), _chpx()]
    verdict = RiskVerdict(
        approved=True, reasoning_chain=_rc(), reason_category="rr_fail",
        rejected_symbols=[{"symbol": "XLE", "reason": XLE_RR}],
        scale_all_buys=0.5, reasoning="XLE refused; halve what is left",
    )
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)
    ctx = _ctx(decisions)

    assert RiskStage(pipeline=pipeline).run(ctx) is None
    survivors = ctx.portfolio_decision.decisions
    assert [d.symbol for d in survivors] == ["CHPX"]
    assert survivors[0].allocation_pct == pytest.approx(3.0)  # 6.0 x 0.5


def test_an_empty_verdict_behaves_exactly_as_before():
    """Every historical verdict, and any model that never emits the field,
    must replay with byte-identical behaviour."""
    decisions = [_xle(), _chpx()]
    verdict = RiskVerdict(
        approved=True, reasoning_chain=_rc(), reasoning="clean",
    )
    assert verdict.rejected_symbols == []
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)
    ctx = _ctx(decisions)

    assert RiskStage(pipeline=pipeline).run(ctx) is None
    assert _symbols(ctx) == ["XLE", "CHPX"]


# ---------------------------------------------------------------------------
# Schema: a refusal must never be lost to a formatting slip
# ---------------------------------------------------------------------------

def test_bare_symbol_string_is_normalized_into_a_refusal():
    """Dropping a malformed refusal is fail-OPEN — the refused name would
    trade. Anything that still names a symbol is normalized instead."""
    verdict = RiskVerdict(
        approved=True, reasoning_chain=_rc(), reasoning="r",
        rejected_symbols=["xle"],
    )
    assert list(verdict.rejections_by_symbol()) == ["XLE"]
    assert verdict.rejected_symbols[0].reason  # a stated absence, never empty


@pytest.mark.parametrize("raw", (
    {"XLE": "R/R below floor"},                     # mapping shorthand
    {"symbol": "XLE", "reason": "R/R below floor"},  # a single bare object
    "XLE",                                           # a bare string
))
def test_container_shorthands_still_refuse_the_symbol(raw):
    verdict = RiskVerdict(
        approved=True, reasoning_chain=_rc(), reasoning="r",
        rejected_symbols=raw,
    )
    assert "XLE" in verdict.rejections_by_symbol()


@pytest.mark.parametrize("raw", (
    [{"reason": "no symbol named"}],
    [{"symbol": ""}],
    7,
))
def test_a_refusal_naming_no_symbol_fails_the_verdict_closed(raw):
    """We know a refusal was intended and cannot tell which name it was for.
    Failing the whole verdict closed refuses everything — the conservative
    direction — instead of silently trading a name that was refused."""
    with pytest.raises(Exception) as exc:
        RiskVerdict(
            approved=True, reasoning_chain=_rc(), reasoning="r",
            rejected_symbols=raw,
        )
    assert any(e["loc"][0] == "rejected_symbols" for e in exc.value.errors())


def test_rejections_by_symbol_keeps_the_first_reason_per_name():
    verdict = RiskVerdict(
        approved=True, reasoning_chain=_rc(), reasoning="r",
        rejected_symbols=[
            SymbolRejection(symbol="XLE", reason="first"),
            SymbolRejection(symbol="xle", reason="second"),
        ],
    )
    assert verdict.rejections_by_symbol() == {"XLE": "first"}


# ---------------------------------------------------------------------------
# Repair path: a refusal is decision-bearing, not prose
# ---------------------------------------------------------------------------

_BASE_VERDICT = {
    "approved": True,
    "reasoning_chain": {
        "rr_audit": "XLE 1.18 below floor; CHPX 3.03 clean.",
        "signal_fidelity": "Both align with Tech.",
        "correlation_check": "No cluster — different sectors.",
        "event_risk": "No fetched date inside the window for either.",
    },
    "modifications": [],
    "rejected_symbols": [{"symbol": "XLE", "reason": XLE_RR}],
    "scale_all_buys": 1.0,
    "reason_category": "rr_fail",
    "reasoning": "XLE refused; CHPX proceeds.",
}


def _complete(payload: dict) -> dict:
    """The same verdict with the two omitted narrative fields supplied —
    a legitimate schema repair and nothing more."""
    out = json.loads(json.dumps(payload))
    out["reasoning_chain"]["sizing_sanity"] = "CHPX size proportional to R/R."
    out["reasoning_chain"]["overall"] = "One refusal, one trade."
    return out


def _rm() -> RiskManagerAgent:
    return RiskManagerAgent.__new__(RiskManagerAgent)


def _run_review(agent, first: str, repaired: str | None):
    """Drive `review()` with a canned first response and repair response."""
    calls: list[str] = []

    def fake_execute(user_message, **kwargs):
        calls.append(user_message)
        raw = first if len(calls) == 1 else repaired
        return AgentResult(raw_text=raw or "", tokens_used=0, model="t",
                           user_message=user_message)

    agent.run = lambda **kw: fake_execute("first")          # type: ignore[assignment]
    agent._execute = fake_execute                            # type: ignore[assignment]
    verdict, _ = agent.review(
        portfolio_decision=PortfolioDecision(
            reasoning_chain=_pm_rc(), targets=[], portfolio_view="v",
        ),
        positions=[], macro_summary={}, rule_violations=[],
    )
    return verdict, calls


def test_repair_preserving_the_refusal_is_accepted():
    agent = _rm()
    verdict, calls = _run_review(
        agent, json.dumps(_BASE_VERDICT), json.dumps(_complete(_BASE_VERDICT)),
    )
    assert verdict is not None
    assert verdict.rejections_by_symbol() == {"XLE": XLE_RR}
    assert len(calls) == 2, "one first call plus one bounded repair"


def test_repair_that_drops_the_refusal_fails_closed():
    """A repair that quietly reinstates a refused symbol has re-decided which
    trades die. That is not schema completion."""
    tampered = _complete(_BASE_VERDICT)
    tampered["rejected_symbols"] = []
    agent = _rm()
    verdict, _ = _run_review(agent, json.dumps(_BASE_VERDICT), json.dumps(tampered))
    assert verdict is None


def test_repair_that_adds_a_refusal_fails_closed():
    tampered = _complete(_BASE_VERDICT)
    tampered["rejected_symbols"].append({"symbol": "CHPX", "reason": "changed my mind"})
    agent = _rm()
    verdict, _ = _run_review(agent, json.dumps(_BASE_VERDICT), json.dumps(tampered))
    assert verdict is None


def test_repair_that_rewrites_a_refusal_reason_fails_closed():
    tampered = _complete(_BASE_VERDICT)
    tampered["rejected_symbols"][0]["reason"] = "a different reason entirely"
    agent = _rm()
    verdict, _ = _run_review(agent, json.dumps(_BASE_VERDICT), json.dumps(tampered))
    assert verdict is None


def test_malformed_refusal_skips_the_repair_call_entirely():
    """A validation failure rooted in `rejected_symbols` cannot be repaired
    without the model re-deciding — fail closed without spending the call."""
    broken = json.loads(json.dumps(_BASE_VERDICT))
    broken["rejected_symbols"] = [{"reason": "names no symbol"}]
    agent = _rm()
    verdict, calls = _run_review(agent, json.dumps(broken), json.dumps(_BASE_VERDICT))
    assert verdict is None
    assert len(calls) == 1, "no repair reprompt for a decision-bearing failure"


# ---------------------------------------------------------------------------
# The exit path gets the same split
# ---------------------------------------------------------------------------

def _exit_pipeline(verdict):
    """Mirrors tests/test_phase3_exit_rework.py's `_risk_pipeline`, kept local
    so the Phase 10.1 behaviour reads in one file."""
    from src.pipeline import TradingPipeline

    p = TradingPipeline.__new__(TradingPipeline)
    p.db = MagicMock()
    p.broker = MagicMock()
    p.broker.get_current_stop_price.return_value = None
    p._atr_for_symbol = MagicMock(return_value=2.0)
    p._build_portfolio_heat = MagicMock(return_value=None)
    p.risk_manager = MagicMock()
    p.risk_manager.review.return_value = (verdict, MagicMock(
        user_message="u", raw_text="r", model="m", tokens_used=1,
        input_tokens=1, output_tokens=1, cost_usd=0.0,
    ))
    return p


def _position(symbol):
    from src.models import Position

    return Position(
        symbol=symbol, qty=10, avg_entry=100.0, current_price=110.0,
        market_value=1100.0, unrealized_pnl=100.0, sector="Technology",
    )


def _two_exits():
    from src.models import PositionAction, PositionReasoningChain, PositionReview

    return PositionReview(
        reasoning_chain=PositionReasoningChain(
            macro_continuity_check="stable", thesis_progress_check="broken",
            thesis_integrity_check="invalidation hit",
            winners_discipline_check="n/a", session_disposition_check="midday",
            execution_rationale="exit",
        ),
        actions=[
            PositionAction(action="SELL", symbol="AAA",
                           reason="thesis_invalid triggered"),
            PositionAction(action="SELL", symbol="BBB",
                           reason="thesis_invalid triggered"),
        ],
        overall_assessment="two exits", risk_level="moderate",
    )


def test_a_per_symbol_refusal_vetoes_only_that_exit():
    verdict = RiskVerdict(
        approved=True, reasoning_chain=_rc(),
        rejected_symbols=[{"symbol": "AAA", "reason": "invalidation not confirmed"}],
        reasoning="BBB may exit",
    )
    pipeline = _exit_pipeline(verdict)

    vetoed, returned = pipeline._risk_review_exits(
        _two_exits(), [_position("AAA"), _position("BBB")],
        run_id="r1", total_value=100_000.0,
    )

    assert vetoed == {"AAA"}
    assert returned is verdict
    detail = pipeline.db.record_intraday_evaluation.call_args.kwargs["detail"]
    assert detail == "invalidation not confirmed", (
        "the vetoed exit must record ITS OWN reason, not the run narrative"
    )


def test_book_level_veto_still_holds_every_exit():
    verdict = RiskVerdict(
        approved=False, reasoning_chain=_rc(),
        reasoning="drawdown state — hold everything",
    )
    pipeline = _exit_pipeline(verdict)

    vetoed, _ = pipeline._risk_review_exits(
        _two_exits(), [_position("AAA"), _position("BBB")],
        run_id="r1", total_value=100_000.0,
    )

    assert vetoed == {"AAA", "BBB"}


# ---------------------------------------------------------------------------
# The model has to know the field exists
# ---------------------------------------------------------------------------

def test_prompt_teaches_the_field_and_the_book_level_distinction():
    """A schema the model does not know about produces malformed responses,
    which fail closed and trade nothing — the exact outcome being removed."""
    text = PROMPT_PATH.read_text()
    assert "`rejected_symbols`" in text
    assert '"rejected_symbols"' in text, "the JSON example must carry the field"
    # The distinction is the whole task; the prompt must state both halves.
    assert "Do NOT use it when the failure belongs to the BOOK" in text
    assert "refusing everything is still the correct answer" in text


def test_prompt_still_reserves_the_whole_plan_veto_for_the_book():
    text = PROMPT_PATH.read_text()
    assert "**Veto is nuclear.**" in text
    assert "Err on the side of capital preservation" in text
