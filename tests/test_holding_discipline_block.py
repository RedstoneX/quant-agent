"""Spec item 25, 2026-09-04 escalation — a PROVEN-FALSE holding-discipline
claim now BLOCKS the exit and alerts the owner; an UNVERIFIABLE one still
only logs.

The check itself (`exit_guard.holding_discipline_false_claim`) shipped
log-only: it recorded a finding when a SELL/REDUCE/COVER on a structurally
protected position gave a justification real recorded data contradicted, and
then let the trade through anyway. The owner approved the escalation to a
real veto, with one hard constraint restated here because it is the whole
design: only an AFFIRMATIVELY CONTRADICTED claim may block. A claim the desk
merely could not check this run (macro seat untrusted, or no same-day
state-change row names the symbol at all) must keep the old behaviour
exactly — logged, not blocked, not alerted. Absence of proof is not proof.

This file pins all three verdicts, both at the unit level
(`holding_discipline_claim_check`) and end-to-end through `RiskStage.run`,
where the veto and the alert actually have to happen. `RiskStage` is stubbed
with the same honest pass-through harness `test_risk_verdict_per_symbol.py`
uses: the hard-risk filter returns whatever it is handed, so a decision
missing at the end was dropped by the code under test, not by a mock.
"""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

from src.models import (
    PortfolioDecision, ReasoningChain, RiskReasoningChain, RiskVerdict,
    TradeDecision,
)
from src.pipeline_context import RunContext
from src.pipeline_stages import RiskStage
from src.risk.exit_guard import (
    StructuralProtectionCheck,
    holding_discipline_claim_check,
    holding_discipline_false_claim,
)

TODAY = "2026-09-04"


def _asc(date_str: str, event: str, symbols: dict[str, str]) -> str:
    """One `active_state_changes` row in the exact format
    `TradingPipeline._build_active_state_changes` renders."""
    syms = ", ".join(f"{sym}({direction})" for sym, direction in symbols.items())
    return f"- [{date_str}] {event} → {syms}"


# ---------------------------------------------------------------------------
# Unit level — the three-valued verdict
# ---------------------------------------------------------------------------

def test_contradicted_regime_claim_is_verdict_false_and_blocks():
    """Real, trusted macro read says risk-ON; the reasoning asserts a flip to
    risk-off. That is a claim the desk's own data affirmatively denies."""
    check = holding_discipline_claim_check(
        action="SELL",
        reason="Cutting ACME — the macro regime flipped to risk-off today.",
        symbol="ACME",
        protected=True,
        macro_regime_today="risk-on",
        macro_status="ok",
        active_state_changes="",
    )
    assert check.verdict == "false"
    assert check.blocks is True
    assert "risk-on" in (check.finding or "")
    assert "BLOCKED" in (check.finding or "")
    assert len(check.reasons) == 1


def test_uncheckable_regime_claim_is_verdict_unverifiable_and_does_not_block():
    """The macro seat failed this run. The identical sentence is now
    unverifiable, NOT false — this is the distinction the veto rests on."""
    check = holding_discipline_claim_check(
        action="SELL",
        reason="Cutting ACME — the macro regime flipped to risk-off today.",
        symbol="ACME",
        protected=True,
        macro_regime_today=None,
        macro_status="failed",
        active_state_changes="",
    )
    assert check.verdict == "unverifiable"
    assert check.blocks is False
    assert check.finding is not None
    assert "NOT blocked" in check.finding


def test_missing_state_change_row_is_unverifiable_not_false():
    """No same-day row names the symbol either way. The news pipeline can
    simply not have logged a real catalyst yet."""
    check = holding_discipline_claim_check(
        action="SELL",
        reason="High-conviction bearish state change on ACME today.",
        symbol="ACME",
        protected=True,
        macro_regime_today="risk-on",
        macro_status="ok",
        active_state_changes="",
        asof=date.fromisoformat(TODAY),
    )
    assert check.verdict == "unverifiable"
    assert check.blocks is False


def test_state_change_row_with_the_wrong_direction_is_false():
    """A same-day row DOES name ACME, recorded bullish. Checkable and wrong."""
    check = holding_discipline_claim_check(
        action="SELL",
        reason="Selling ACME on a high-conviction bearish state change.",
        symbol="ACME",
        protected=True,
        macro_regime_today="risk-on",
        macro_status="ok",
        active_state_changes=_asc(TODAY, "Guidance raise", {"ACME": "bullish"}),
        asof=date.fromisoformat(TODAY),
    )
    assert check.verdict == "false"
    assert check.blocks is True


def test_confirmed_claim_is_verdict_ok():
    """Macro really did flip to risk-off. Nothing to say at all."""
    check = holding_discipline_claim_check(
        action="SELL",
        reason="Regime flipped to risk-off today per Macro; cutting risk.",
        symbol="ACME",
        protected=True,
        macro_regime_today="risk-off",
        macro_status="ok",
        active_state_changes="",
    )
    assert check.verdict == "ok"
    assert check.blocks is False
    assert check.finding is None


def test_a_contradiction_outranks_a_co_occurring_unverifiable_clause():
    """Regime claim provably false; state-change claim uncheckable in the
    same sentence. One provable contradiction is enough to block."""
    check = holding_discipline_claim_check(
        action="SELL",
        reason="Regime flipped to risk-off and there is a high-conviction "
               "bearish state change on ACME today.",
        symbol="ACME",
        protected=True,
        macro_regime_today="risk-on",
        macro_status="ok",
        active_state_changes="",      # state-change claim unverifiable
        asof=date.fromisoformat(TODAY),
    )
    assert check.verdict == "false"
    assert len(check.reasons) == 1
    assert "regime flip" in check.reasons[0]


def test_legacy_wrapper_still_returns_only_proven_false_findings():
    """`holding_discipline_false_claim` keeps its old contract exactly: a
    string for a proven-false claim, None for everything else INCLUDING the
    unverifiable case it now has a name for."""
    assert holding_discipline_false_claim(
        action="SELL",
        reason="Regime flipped to risk-off today.",
        symbol="ACME", protected=True,
        macro_regime_today="risk-on", macro_status="ok",
    ) is not None
    assert holding_discipline_false_claim(
        action="SELL",
        reason="Regime flipped to risk-off today.",
        symbol="ACME", protected=True,
        macro_regime_today=None, macro_status="failed",
    ) is None


# ---------------------------------------------------------------------------
# End to end through RiskStage — where the veto and the alert live
# ---------------------------------------------------------------------------

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


def _sell(symbol: str, reasoning: str) -> TradeDecision:
    """A full exit. `allocation_pct` on a SELL is the fraction OF THE CURRENT
    POSITION to sell (see `RiskManagerAgent.build_user_message`), so 100.0 is
    "close it", not "100% of book"."""
    return TradeDecision(
        action="SELL", symbol=symbol, allocation_pct=100.0,
        entry_price=100.0, stop_loss=95.0, take_profit=115.0,
        reasoning=reasoning,
    )


def _buy(symbol: str) -> TradeDecision:
    """A passing BUY that must survive any exit-side block — the veto is
    per-decision, exactly like a per-symbol RM refusal."""
    return TradeDecision(
        action="BUY", symbol=symbol, allocation_pct=6.0, entry_price=24.00,
        stop_loss=22.50, take_profit=28.55, reasoning="unrelated breakout",
    )


def _stage_pipeline(*, decisions, protected=True, active_state_changes=""):
    """RiskStage stubbed just far enough to reach the holding-discipline
    block, with a pass-through hard-risk filter."""
    from src.pipeline import TradingPipeline

    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.db = MagicMock()
    pipeline._sweeper = MagicMock(return_value=None)
    pipeline._filter_supported_symbols = MagicMock(return_value=(decisions, []))
    pipeline._clamp_queued_earnings_buys = MagicMock(return_value=decisions)
    pipeline._filter_hard_risk_decisions = MagicMock(
        side_effect=lambda d, *a, **kw: (list(d), [], []),
    )
    pipeline._build_active_state_changes = MagicMock(
        return_value=active_state_changes,
    )
    pipeline._structural_protection_for_holding = MagicMock(
        return_value=StructuralProtectionCheck(
            protected=protected,
            basis="structural_level_intact",
            detail="level intact on the close",
        ),
    )
    verdict = RiskVerdict(
        approved=True, reasoning_chain=_rc(), reason_category="clean",
        reasoning="no objection at the book level",
    )
    rm_result = MagicMock()
    rm_result.used_fallback = False
    rm_result.raw_text = "{}"
    pipeline.risk_manager = MagicMock()
    pipeline.risk_manager.review.return_value = (verdict, rm_result)
    return pipeline


def _ctx(decisions, *, macro_regime="risk-on", macro_status="ok") -> RunContext:
    ctx = RunContext.start("morning")
    ctx.decision_id = f"{ctx.run_id}-dec-000001"
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.macro_analysis = {"regime": macro_regime}
    ctx.data_status = {"macro": macro_status}
    ctx.position_history = {
        d.symbol: {"entry_price": 100.0, "stop_loss": 95.0}
        for d in decisions
    }
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(), decisions=decisions, portfolio_view="test",
    )
    return ctx


def _symbols(ctx) -> list[str]:
    return [d.symbol for d in ctx.portfolio_decision.decisions]


def _events(pipeline) -> list[tuple[str, str, str, str]]:
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


def test_proven_false_claim_blocks_the_sell_and_alerts_the_owner():
    """The headline case. ACME is structurally protected; the SELL asserts a
    regime flip to risk-off; today's trusted macro read says risk-on. The
    decision must not survive to order construction, and the owner must be
    told in a message of its own."""
    decisions = [_sell("ACME", "Regime flipped to risk-off today; cutting."),
                 _buy("CHPX")]
    pipeline = _stage_pipeline(decisions=decisions)
    ctx = _ctx(decisions)

    with patch("src.notifier.send_owner_alert") as alert:
        result = RiskStage(pipeline=pipeline).run(ctx)

    assert result is None, "the unrelated BUY still stands, so not terminal"
    assert _symbols(ctx) == ["CHPX"], "the false-justification SELL was dropped"

    outcomes = [e[2] for e in _events(pipeline) if e[0] == "ACME"]
    assert "rejected" in outcomes, (
        "must reuse the existing per-symbol rejection event, not a new one"
    )
    assert "holding_discipline_claim_false" in outcomes

    assert alert.call_count == 1
    body = alert.call_args.args[0]
    assert "ACME" in body
    assert "BLOCKED" in body
    assert "risk-off" in body and "risk-on" in body
    assert alert.call_args.kwargs["symbols"] == ["ACME"]


def test_unverifiable_claim_is_logged_but_never_blocked_or_alerted():
    """Same sentence, macro seat failed. Old behaviour, unchanged."""
    decisions = [_sell("ACME", "Regime flipped to risk-off today; cutting.")]
    pipeline = _stage_pipeline(decisions=decisions)
    ctx = _ctx(decisions, macro_regime=None, macro_status="failed")

    with patch("src.notifier.send_owner_alert") as alert:
        result = RiskStage(pipeline=pipeline).run(ctx)

    assert result is None
    assert _symbols(ctx) == ["ACME"], "an unverifiable claim must NOT block"
    outcomes = [e[2] for e in _events(pipeline) if e[0] == "ACME"]
    assert "holding_discipline_claim_unverified" in outcomes
    assert "rejected" not in outcomes
    assert alert.call_count == 0


def test_a_true_claim_passes_through_with_no_event_and_no_alert():
    """Macro really did flip. Nothing unusual happens at all."""
    decisions = [_sell("ACME", "Regime flipped to risk-off today; cutting.")]
    pipeline = _stage_pipeline(decisions=decisions)
    ctx = _ctx(decisions, macro_regime="risk-off")

    with patch("src.notifier.send_owner_alert") as alert:
        result = RiskStage(pipeline=pipeline).run(ctx)

    assert result is None
    assert _symbols(ctx) == ["ACME"]
    hd_events = [
        e for e in _events(pipeline)
        if e[2] in ("holding_discipline_claim_false",
                    "holding_discipline_claim_unverified")
    ]
    assert hd_events == []
    assert alert.call_count == 0


def test_an_unprotected_position_is_never_blocked():
    """Structural protection has already broken. A plain exit there needs no
    special justification, so a false (b)/(c) claim is out of scope."""
    decisions = [_sell("ACME", "Regime flipped to risk-off today; cutting.")]
    pipeline = _stage_pipeline(decisions=decisions, protected=False)
    ctx = _ctx(decisions)

    with patch("src.notifier.send_owner_alert") as alert:
        result = RiskStage(pipeline=pipeline).run(ctx)

    assert result is None
    assert _symbols(ctx) == ["ACME"]
    assert alert.call_count == 0


def test_blocking_every_remaining_leg_returns_the_terminal_rejected_status():
    """When the false-claim SELL was the only decision left, the stage must
    end the run the same way the per-symbol RM refusal path does — status
    'rejected', no orders, each symbol carrying its own reason."""
    decisions = [_sell("ACME", "Regime flipped to risk-off today; cutting.")]
    pipeline = _stage_pipeline(decisions=decisions)
    ctx = _ctx(decisions)

    with patch("src.notifier.send_owner_alert"):
        result = RiskStage(pipeline=pipeline).run(ctx)

    assert isinstance(result, dict)
    assert result["status"] == "rejected"
    assert result["orders"] == []
    assert "ACME" in result["reason"]


def test_an_alert_failure_cannot_break_the_trading_path():
    """The block must still happen if Telegram is down — an alerting bug must
    never be able to break the thing it reports on."""
    decisions = [_sell("ACME", "Regime flipped to risk-off today; cutting."),
                 _buy("CHPX")]
    pipeline = _stage_pipeline(decisions=decisions)
    ctx = _ctx(decisions)

    with patch("src.notifier.send_owner_alert", side_effect=RuntimeError("down")):
        result = RiskStage(pipeline=pipeline).run(ctx)

    assert result is None
    assert _symbols(ctx) == ["CHPX"]
