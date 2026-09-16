"""Item 60 — one owner of exit refusal, one uncertainty fail direction.

The measured defect was not under-refusal. It was two layers that could
refuse a sale, failing in opposite directions when they could not judge:
a dead Risk Manager failed OPEN (owner-ratified 2026-08-27); a hard-trigger
substring miss failed CLOSED. This file pins the reconciliation.

What did not move: NOISE_BAND_ATR_MULTIPLE, absolute_min_stop_atr_multiple,
repeg, shorts, profit-taking. Item 70 stays open.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import yaml

from src.pipeline import TradingPipeline, _reason_cites_hard_trigger
from src.risk.exit_guard import NOISE_BAND_ATR_MULTIPLE
from src.risk.exit_refusal import (
    CODE_AI_RISK_REJECT,
    CODE_AI_RISK_UNAVAILABLE,
    CODE_HARD_TRIGGER_UNCERTAIN,
    CODE_UNRECOGNIZED_TRIGGER,
    EXIT_REFUSAL_KIND,
    REFUSAL_OWNER,
    UNCERTAINTY_FAIL,
    classify_trigger_reason,
    load_exit_refusals,
    record_exit_refusal,
)
from src.storage.db import Database


def _position(symbol="AAA", qty=10, avg_entry=100.0, current_price=110.0):
    from src.models import Position
    return Position(
        symbol=symbol, qty=qty, avg_entry=avg_entry, current_price=current_price,
        market_value=qty * current_price,
        unrealized_pnl=qty * (current_price - avg_entry), sector="Technology",
    )


def _review_with(action="SELL", symbol="AAA", reason="thesis_invalid triggered"):
    from src.models import PositionAction, PositionReasoningChain, PositionReview
    return PositionReview(
        reasoning_chain=PositionReasoningChain(
            macro_continuity_check="stable", thesis_progress_check="broken",
            thesis_integrity_check="invalidation hit", winners_discipline_check="n/a",
            session_disposition_check="midday", execution_rationale="exit",
        ),
        actions=[PositionAction(action=action, symbol=symbol, reason=reason)],
        overall_assessment="one exit", risk_level="moderate",
    )


def _verdict(approved: bool, reasoning="because"):
    from src.models import RiskReasoningChain, RiskVerdict
    return RiskVerdict(
        approved=approved,
        reasoning_chain=RiskReasoningChain(
            rr_audit="n/a", signal_fidelity="ok", correlation_check="ok",
            event_risk="none", sizing_sanity="ok", overall="ok",
        ),
        reasoning=reasoning,
    )


def _risk_pipeline(verdict=None, raises=False):
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.db = MagicMock()
    pipeline.broker = MagicMock()
    pipeline.risk_manager = MagicMock()
    if raises:
        pipeline.risk_manager.review.side_effect = RuntimeError("provider down")
    else:
        pipeline.risk_manager.review.return_value = (verdict, MagicMock(
            user_message="u", raw_text="r", model="m", tokens_used=1,
            input_tokens=1, output_tokens=1, cost_usd=0.0,
        ))
    pipeline._build_portfolio_heat = MagicMock(return_value=None)
    pipeline._atr_for_symbol = MagicMock(return_value=2.0)
    pipeline._build_position_history = MagicMock(return_value={})
    pipeline._exit_event_risk_block = MagicMock(return_value="")
    return pipeline


def _payloads(pipeline):
    out = []
    for call in pipeline.db.insert_specialist_evidence.call_args_list:
        kw = call.kwargs
        if kw.get("kind") != EXIT_REFUSAL_KIND:
            continue
        payload = json.loads(kw["evidence_json"])
        payload["symbol"] = kw.get("symbol")
        out.append(payload)
    return out


# ---------------------------------------------------------------------------
# Policy pins
# ---------------------------------------------------------------------------

def test_deterministic_python_owns_refusal_and_uncertainty_fails_open():
    assert REFUSAL_OWNER == "deterministic"
    assert UNCERTAINTY_FAIL == "open"


def test_item_70_noise_band_and_stop_floor_untouched():
    """Item 60 must not quietly retune the two 1.0s that item 70 owns."""
    assert NOISE_BAND_ATR_MULTIPLE == 1.0
    from pathlib import Path
    settings = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config" / "settings.yaml")
        .read_text()
    )
    assert settings["risk"]["absolute_min_stop_atr_multiple"] == 1.0


# ---------------------------------------------------------------------------
# Classifier — completed no vs uncertainty
# ---------------------------------------------------------------------------

def test_present_reason_without_keywords_is_unnamed_not_uncertain():
    assert classify_trigger_reason(
        "momentum cooling, prudent to harvest",
        cites=_reason_cites_hard_trigger,
    ) == "unnamed"


def test_named_trigger_is_named():
    assert classify_trigger_reason(
        "thesis_invalid_if triggered — guidance cut",
        cites=_reason_cites_hard_trigger,
    ) == "named"


def test_empty_string_is_a_completed_unnamed_judgment():
    """An empty reason field is not 'could not tell'. Nothing was named.
    That remains a drop, same as before this item."""
    assert classify_trigger_reason("", cites=_reason_cites_hard_trigger) == "unnamed"


def test_non_string_reason_is_unnamed_not_uncertain():
    """A missing reason is a completed no, not a new fail-open. PositionAction
    requires a string; if a non-string ever arrived it must still drop."""
    assert classify_trigger_reason(None, cites=_reason_cites_hard_trigger) == "unnamed"
    assert classify_trigger_reason(123, cites=_reason_cites_hard_trigger) == "unnamed"


def test_matcher_exception_is_uncertain():
    def boom(_reason):
        raise RuntimeError("matcher exploded")
    assert classify_trigger_reason("thesis_invalid", cites=boom) == "uncertain"


# ---------------------------------------------------------------------------
# Durable recording — append-only, same symbol+run does not clobber
# ---------------------------------------------------------------------------

def test_two_drop_reasons_for_the_same_symbol_and_run_both_survive(tmp_path):
    db = Database(str(tmp_path / "refusals.db"))
    db.initialize()
    try:
        record_exit_refusal(
            db, symbol="V", run_id="r-same", action="SELL",
            code=CODE_UNRECOGNIZED_TRIGGER, dropped=True,
            detail="no named trigger", layer="hard_trigger",
        )
        record_exit_refusal(
            db, symbol="V", run_id="r-same", action="SELL",
            code=CODE_AI_RISK_REJECT, dropped=True,
            detail="challenge seat said no", layer="ai_risk",
        )
        # The cooldown ledger WOULD have kept only the second row.
        db.record_intraday_evaluation(
            symbol="V", run_id="r-same", status="exit_blocked_no_named_trigger",
            detail="first",
        )
        db.record_intraday_evaluation(
            symbol="V", run_id="r-same", status="exit_vetoed_by_ai_risk",
            detail="second",
        )
        ledger = db.conn.execute(
            "SELECT status FROM intraday_evaluations WHERE symbol='V' AND run_id='r-same'"
        ).fetchall()
        assert [row["status"] for row in ledger] == ["exit_vetoed_by_ai_risk"]

        rows = load_exit_refusals(db, symbol="V", run_id="r-same")
        assert [row["code"] for row in rows] == [
            CODE_UNRECOGNIZED_TRIGGER, CODE_AI_RISK_REJECT,
        ]
        assert all(row["dropped"] is True for row in rows)
        assert all(row["owner"] == REFUSAL_OWNER for row in rows)
        assert all(row["uncertainty_fail"] == "open" for row in rows)
    finally:
        db.close()


def test_recording_failure_does_not_raise():
    class Boom:
        def insert_specialist_evidence(self, **_kw):
            raise RuntimeError("disk full")
    record_exit_refusal(
        Boom(), symbol="AAA", run_id="r1", action="SELL",
        code=CODE_UNRECOGNIZED_TRIGGER, dropped=True,
        detail="x", layer="hard_trigger",
    )


# ---------------------------------------------------------------------------
# Fail-direction coherence on the live pair
# ---------------------------------------------------------------------------

def test_dead_risk_manager_does_not_veto_a_named_trigger():
    """2026-08-27 kept: uncertainty on the challenge seat fails OPEN."""
    pipeline = _risk_pipeline(verdict=None)
    vetoed, verdict = pipeline._risk_review_exits(
        _review_with(), [_position("AAA")], run_id="r1", total_value=100_000.0,
    )
    assert vetoed == set()
    assert verdict is None
    payloads = _payloads(pipeline)
    assert any(
        p["code"] == CODE_AI_RISK_UNAVAILABLE and p["dropped"] is False
        and p["symbol"] == "AAA"
        for p in payloads
    )


def test_risk_manager_exception_also_fails_open_and_is_recorded():
    pipeline = _risk_pipeline(raises=True)
    vetoed, _ = pipeline._risk_review_exits(
        _review_with(), [_position("AAA")], run_id="r1", total_value=100_000.0,
    )
    assert vetoed == set()
    assert any(p["code"] == CODE_AI_RISK_UNAVAILABLE and p["dropped"] is False
               for p in _payloads(pipeline))


def test_unnamed_trigger_is_not_sent_to_the_risk_manager():
    """Owner refuses first. A dead model therefore cannot fail-OPEN a sale
    the phrase gate already refused — the silent disagreement is gone."""
    pipeline = _risk_pipeline(_verdict(True))
    vetoed, verdict = pipeline._risk_review_exits(
        _review_with(reason="momentum cooling, prudent to harvest"),
        [_position("AAA")], run_id="r1", total_value=100_000.0,
    )
    assert vetoed == set()
    assert verdict is None
    pipeline.risk_manager.review.assert_not_called()


def test_ai_approval_cannot_override_an_unnamed_trigger_drop():
    """Deterministic owns refusal. Challenge-seat approval does not save it."""
    pipeline = _risk_pipeline(_verdict(True))
    pipeline.broker.submit_order.return_value = {
        "id": "ord-1", "status": "accepted", "symbol": "AAA",
    }
    pipeline._format_qty = lambda q: str(q)
    pipeline._full_sell_qty = lambda q: q
    orders = pipeline._midday_execute_llm_actions(
        positions=[_position("AAA")],
        review=_review_with(reason="TARGET_BREACH — up 16%, valuation stretched"),
        run_id="r1",
        risk_vetoed_symbols=set(),
    )
    assert orders == []
    pipeline.broker.submit_order.assert_not_called()
    statuses = [
        c.kwargs.get("status")
        for c in pipeline.db.record_intraday_evaluation.call_args_list
    ]
    assert "exit_blocked_no_named_trigger" in statuses
    payloads = _payloads(pipeline)
    assert any(
        p["code"] == CODE_UNRECOGNIZED_TRIGGER and p["dropped"] is True
        for p in payloads
    )


def test_parseable_ai_reject_of_a_cover_records_cover_not_sell():
    pipeline = _risk_pipeline(_verdict(False, "cover is not justified"))
    vetoed, verdict = pipeline._risk_review_exits(
        _review_with(action="COVER", reason="thesis_invalid_if triggered"),
        [_position("AAA", qty=-10)], run_id="r1", total_value=100_000.0,
    )
    assert vetoed == {"AAA"}
    payloads = _payloads(pipeline)
    assert any(
        p["code"] == CODE_AI_RISK_REJECT and p["dropped"] is True
        and p.get("action") == "COVER"
        for p in payloads
    )


def test_unnamed_skip_is_recorded_before_the_risk_manager_call():
    pipeline = _risk_pipeline(_verdict(True))
    pipeline._risk_review_exits(
        _review_with(reason="momentum cooling, prudent to harvest"),
        [_position("AAA")], run_id="r1", total_value=100_000.0,
    )
    pipeline.risk_manager.review.assert_not_called()
    payloads = _payloads(pipeline)
    assert any(
        p["code"] == CODE_UNRECOGNIZED_TRIGGER and p["dropped"] is True
        and p["symbol"] == "AAA"
        for p in payloads
    )


def test_executor_still_skips_a_symbol_vetoed_by_ai_risk():
    pipeline = _risk_pipeline(_verdict(True))
    pipeline._format_qty = lambda q: str(q)
    pipeline._full_sell_qty = lambda q: q
    orders = pipeline._midday_execute_llm_actions(
        positions=[_position("AAA")], review=_review_with(), run_id="r1",
        risk_vetoed_symbols={"AAA"},
    )
    assert orders == []
    pipeline.broker.submit_order.assert_not_called()


def test_matcher_exception_fails_open_on_the_phrase_gate(monkeypatch):
    """The only uncertainty this layer can produce is the matcher raising.
    That fails OPEN, matching a dead Risk Manager. A missing/empty reason
    is a completed no and is not this test."""
    def boom(_reason):
        raise RuntimeError("matcher exploded")
    monkeypatch.setattr("src.pipeline._reason_cites_hard_trigger", boom)
    pipeline = _risk_pipeline(_verdict(True))
    pipeline._format_qty = lambda q: str(q)
    pipeline._full_sell_qty = lambda q: q
    pipeline._submit_protected_sell = MagicMock(return_value=None)
    orders = pipeline._midday_execute_llm_actions(
        positions=[_position("AAA")],
        review=_review_with(reason="thesis_invalid_if triggered"),
        run_id="r1",
    )
    payloads = _payloads(pipeline)
    assert any(
        p["code"] == CODE_HARD_TRIGGER_UNCERTAIN and p["dropped"] is False
        for p in payloads
    )
    statuses = [
        c.kwargs.get("status")
        for c in pipeline.db.record_intraday_evaluation.call_args_list
    ]
    assert "exit_blocked_no_named_trigger" not in statuses
    del orders


def test_dead_risk_manager_plus_unnamed_trigger_still_drops_at_execute():
    """Reconciliation of 2026-08-27 with the phrase gate: the owner's drop
    still happens when the challenge seat is dead, because unnamed exits
    never reached it."""
    pipeline = _risk_pipeline(verdict=None)
    pipeline._format_qty = lambda q: str(q)
    pipeline._full_sell_qty = lambda q: q
    review = _review_with(reason="price looks tired")
    vetoed, _ = pipeline._risk_review_exits(
        review, [_position("AAA")], run_id="r1", total_value=100_000.0,
    )
    assert vetoed == set()
    pipeline.risk_manager.review.assert_not_called()
    orders = pipeline._midday_execute_llm_actions(
        positions=[_position("AAA")], review=review, run_id="r1",
        risk_vetoed_symbols=vetoed,
    )
    assert orders == []
    pipeline.broker.submit_order.assert_not_called()
    payloads = _payloads(pipeline)
    assert any(
        p["code"] == CODE_UNRECOGNIZED_TRIGGER and p["dropped"] is True
        for p in payloads
    )
    assert not any(
        p["code"] == CODE_AI_RISK_UNAVAILABLE for p in payloads
    )
