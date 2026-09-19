"""OWNER RULING 2026-09-19 — the risk seat is ADVISORY.

"Should the risk seat be allowed to block whole plans over guidelines, or
only over hard limits?" — "it shouldn't have to." Hard limits are enforced by
code. Every lever the seat's verdict carries is RECORDED with its reason and
NOT applied (`src/risk/risk_seat_advisory.py`).

The load-bearing assertion is the first test: whatever the seat says, the
plan that leaves `RiskStage` is byte-for-byte the plan that entered the
seat's review. The side doors the adversary review named each get their own
case, because each used to remove or change a trade without the seat ever
writing `approved: false`:

- `scale_all_buys = 0.0` dropped every entry;
- an edit that failed the order schema (e.g. allocation > 100%) dropped the
  name;
- a stop edit that failed the noise-band re-check dropped the trade;
- a cut below the minimum risk per trade.

The code-owned drop of a PROVABLY FALSE exit claim is NOT the seat and must
keep working — it shared the seat's drop shape, so it is pinned here too.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.models import (
    PortfolioDecision, RiskModification, RiskVerdict, TradeDecision,
)
from src.pipeline_stages import RiskStage
from tests.test_risk_verdict_per_symbol import (
    _chpx, _ctx, _rc, _stage_pipeline, _xle,
)

RULING = "not applied — owner ruling 2026-09-19"


def _sell() -> TradeDecision:
    return TradeDecision(
        action="SELL", symbol="XOM", allocation_pct=100.0, entry_price=0.0,
        stop_loss=0.0, take_profit=0.0, reasoning="thesis_invalid triggered",
    )


def _short() -> TradeDecision:
    return TradeDecision(
        action="SHORT", symbol="TSLA", allocation_pct=4.0, entry_price=200.0,
        stop_loss=212.0, take_profit=176.0, reasoning="breakdown",
        thesis_invalid_if="reclaims 210",
    )


def _plan() -> list[TradeDecision]:
    return [_xle(), _chpx(), _short(), _sell()]


def _dump(decisions) -> list[str]:
    return [d.model_dump_json() for d in decisions]


def _run_capturing_plan_seen_by_seat(verdict, decisions):
    """Run RiskStage; return (result, ctx, plan the seat was shown)."""
    pipeline = _stage_pipeline(verdict=verdict, decisions=decisions)
    shown: dict = {}
    original = pipeline.risk_manager.review.return_value

    def _review(**kwargs):
        shown["plan"] = _dump(kwargs["portfolio_decision"].decisions)
        return original

    pipeline.risk_manager.review.side_effect = _review
    ctx = _ctx(decisions)
    result = RiskStage(pipeline=pipeline).run(ctx)
    return result, ctx, shown["plan"], pipeline


def _every_lever() -> RiskVerdict:
    return RiskVerdict(
        approved=False, reasoning_chain=_rc(), reason_category="oversized",
        reasoning="the whole book is too big for this tape",
        rejected_symbols=[
            {"symbol": "XLE", "reason": "stop under no defended level"},
            {"symbol": "CHPX", "reason": "earnings inside the window"},
            {"symbol": "TSLA", "reason": "squeeze risk"},
            {"symbol": "XOM", "reason": "exit is premature"},
        ],
        modifications=[
            RiskModification(symbol="XLE", field="allocation_pct",
                             original_value=5.0, new_value=150.0, reason="x"),
            RiskModification(symbol="CHPX", field="stop_loss",
                             original_value=22.5, new_value=23.99, reason="x"),
            RiskModification(symbol="TSLA", field="allocation_pct",
                             original_value=4.0, new_value=0.001, reason="x"),
            RiskModification(symbol="XOM", field="allocation_pct",
                             original_value=100.0, new_value=0.0, reason="x"),
        ],
        scale_all_buys=0.0,
    )


def test_the_plan_leaving_the_seat_is_byte_for_byte_the_plan_it_was_shown():
    """Every lever at once: whole-plan veto, a refusal of every name, an
    edit on every name (schema-invalid, noise-band-failing stop, sub-minimum
    cut, a zeroed SELL) and `scale_all_buys=0.0`. The plan is unchanged, and
    it is also exactly the plan the portfolio manager produced (the stubbed
    pre-seat gates are pass-through here)."""
    pm_plan = _plan()
    pm_dump = _dump(pm_plan)

    result, ctx, shown, _ = _run_capturing_plan_seen_by_seat(_every_lever(), pm_plan)

    assert result is None
    assert shown == pm_dump
    assert _dump(ctx.portfolio_decision.decisions) == pm_dump


@pytest.mark.parametrize("name,verdict_kwargs", (
    ("scale_all_buys=0 used to drop every entry",
     {"scale_all_buys": 0.0}),
    ("an edit over 100% used to fail the schema and drop the name",
     {"modifications": [RiskModification(
         symbol="XLE", field="allocation_pct", original_value=5.0,
         new_value=150.0, reason="cut")]}),
    ("a stop edit inside the noise band used to drop the trade",
     {"modifications": [RiskModification(
         symbol="CHPX", field="stop_loss", original_value=22.5,
         new_value=23.99, reason="tighter")]}),
    ("a cut below the minimum risk per trade used to act as a refusal",
     {"modifications": [RiskModification(
         symbol="CHPX", field="allocation_pct", original_value=6.0,
         new_value=0.001, reason="token size")]}),
))
def test_each_side_door_is_closed(name, verdict_kwargs):
    pm_plan = [_xle(), _chpx()]
    pm_dump = _dump(pm_plan)
    verdict = RiskVerdict(
        approved=True, reasoning_chain=_rc(), reasoning="r", **verdict_kwargs,
    )
    pipeline = _stage_pipeline(verdict=verdict, decisions=pm_plan)
    # The REAL applier, so that if RiskStage ever calls it again the drop
    # it performs shows up here.
    from src.pipeline import TradingPipeline
    pipeline._apply_risk_modifications = (
        TradingPipeline._apply_risk_modifications.__get__(pipeline)
    )
    ctx = _ctx(pm_plan)

    assert RiskStage(pipeline=pipeline).run(ctx) is None, name
    assert _dump(ctx.portfolio_decision.decisions) == pm_dump, name
    objections = [
        json.loads(c.kwargs["evidence_json"])
        for c in pipeline.db.insert_specialist_evidence.call_args_list
        if c.kwargs.get("kind") == "pipeline_event"
        and json.loads(c.kwargs["evidence_json"])["outcome"] == "objection_not_applied"
    ]
    assert objections and all(o["reason"].endswith(RULING) for o in objections), name


def test_every_seat_evidence_row_is_marked_not_applied():
    _, _, _, pipeline = _run_capturing_plan_seen_by_seat(_every_lever(), _plan())
    rows = [
        c.kwargs for c in pipeline.db.insert_specialist_evidence.call_args_list
        if c.kwargs.get("agent_name") == "risk_manager"
    ]
    kinds = sorted({r["kind"] for r in rows})
    assert kinds == ["modification", "rejection", "verdict"]
    for r in rows:
        payload = json.loads(r["evidence_json"])
        assert payload["applied"] is False, r["kind"]
        assert payload["ruling"] == RULING


def test_an_unparseable_seat_no_longer_stops_the_plan():
    """The seat's garbage output used to be the widest side door of all: a
    malformed `rejected_symbols` entry or an out-of-range `scale_all_buys`
    fails the whole verdict, and a None verdict ended the run with zero
    orders. An advisory seat's absence removes no protection."""
    pm_plan = _plan()
    pipeline = _stage_pipeline(verdict=None, decisions=pm_plan)
    ctx = _ctx(pm_plan)

    assert RiskStage(pipeline=pipeline).run(ctx) is None
    assert _dump(ctx.portfolio_decision.decisions) == _dump(_plan())


# ---------------------------------------------------------------------------
# The code-owned drop the seat used to share a drop path with — still live
# ---------------------------------------------------------------------------

def test_a_provably_false_exit_claim_is_still_dropped_whatever_the_seat_says():
    """`holding_discipline_claim_check` is deterministic Python acting on a
    claim PROVEN false (a regime flip to risk-off, on a day the trusted macro
    read says risk-on). It used to share its drop shape with the seat's
    per-symbol refusal; removing the seat's refusal must not remove it. The
    seat here APPROVES the SELL outright."""
    from tests.test_holding_discipline_block import (
        _buy, _ctx as _hd_ctx, _sell as _hd_sell, _stage_pipeline as _hd_pipeline,
        _symbols,
    )

    decisions = [_hd_sell("ACME", "Regime flipped to risk-off today; cutting."),
                 _buy("CHPX")]
    pipeline = _hd_pipeline(decisions=decisions)
    ctx = _hd_ctx(decisions)

    with patch("src.notifier.send_owner_alert"):
        result = RiskStage(pipeline=pipeline).run(ctx)

    assert result is None
    assert _symbols(ctx) == ["CHPX"]


def test_every_leg_proven_false_still_ends_the_run():
    from tests.test_holding_discipline_block import (
        _ctx as _hd_ctx, _sell as _hd_sell, _stage_pipeline as _hd_pipeline,
    )

    decisions = [_hd_sell("ACME", "Regime flipped to risk-off today; cutting.")]
    pipeline = _hd_pipeline(decisions=decisions)
    ctx = _hd_ctx(decisions)

    with patch("src.notifier.send_owner_alert"):
        result = RiskStage(pipeline=pipeline).run(ctx)

    assert result is not None and result["status"] == "rejected"


# ---------------------------------------------------------------------------
# What the owner reads
# ---------------------------------------------------------------------------

def test_the_owner_facing_sentence():
    from src.risk.risk_seat_advisory import objection_text

    assert objection_text("earnings inside the window") == (
        "the risk reviewer objected: earnings inside the window; "
        "not applied — owner ruling 2026-09-19"
    )


def _feed_snapshot(tmp_path, monkeypatch, verdict: dict, mods=()):
    import src.trader_feed as trader_feed
    from tests.test_trader_feed import _evidence, _make_db

    db = _make_db(tmp_path, monkeypatch)
    run = "run-advisory"
    _evidence(db, run, "portfolio_manager", "reasoning", {"portfolio_view": "Plan."})
    _evidence(
        db, run, "portfolio_manager", "proposed_order",
        {"action": "BUY", "symbol": "NVDA", "allocation_pct": 10,
         "reasoning": "Setup qualifies"},
        symbol="NVDA",
    )
    _evidence(db, run, "risk_manager", "verdict", verdict)
    for mod in mods:
        _evidence(db, run, "risk_manager", "modification", mod, symbol=mod["symbol"])
    return trader_feed.format_session_result(
        "morning", {"status": "executed", "run_id": run, "orders": []}, 30.0,
    )


def test_telegram_describes_a_recorded_objection_truthfully(tmp_path, monkeypatch):
    msg = _feed_snapshot(tmp_path, monkeypatch, {
        "approved": False, "reason_category": "event_risk", "scale_all_buys": 0.0,
        "reasoning": "Earnings too close.",
        "rejected_symbols": [{"symbol": "NVDA", "reason": "earnings in 2 sessions"}],
        "applied": False, "ruling": RULING,
    }, mods=[{"symbol": "NVDA", "field": "allocation_pct", "original_value": 10,
              "new_value": 5, "reason": "pre-event", "applied": False}])

    assert "Risk reviewer: objected" in msg
    assert (
        "the risk reviewer objected: NVDA — earnings in 2 sessions; " + RULING
    ) in msg
    assert "whole plan — Earnings too close." in msg
    assert "Risk: REJECTED" not in msg
    assert "Blocked by risk manager" not in msg
    assert "Risk rejected" not in msg


def test_telegram_keeps_the_old_wording_for_a_pre_ruling_verdict(tmp_path, monkeypatch):
    """A verdict with no `applied` marker was written before the ruling and
    WAS applied; the feed must not rewrite history."""
    msg = _feed_snapshot(tmp_path, monkeypatch, {
        "approved": False, "reason_category": "event_risk", "scale_all_buys": 1.0,
        "reasoning": "Earnings too close.",
    })
    assert "Risk: REJECTED" in msg
