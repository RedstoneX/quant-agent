"""Phase 14b — automatic opportunity-cost rotation, behind
`execution.rotation_enabled` (default OFF).

What these pin, in the order the owner asked for them:

  * flag OFF is a byte-for-byte no-op — no target appended, no event, and
    the structural-protection check is never even called;
  * a categorically-ineligible, already-unprotected holding IS rotated out
    (an ordinary zero-size target is appended) when the PM asked to buy the
    better candidate and there is no room;
  * the ranked-margin tier (both still eligible) is never executed;
  * a holding whose structural protection is intact is surfaced, never sold;
  * the sale's reason is built from measured facts, makes no regime /
  * bearish-state-change claim the holding-discipline checker could ever
    find false, and does not word itself past the midday keyword gate;
  * the appended target becomes an ordinary full SELL through the real
    `PortfolioConstructor._build_sell` — the same object RiskStage and
    ExecutionStage gate;
  * in-flight orders, a same-day BUY, a pending protection-restore WAL row
    or a pending re-peg on the held name all refuse the rotation, and an
    unanswerable in-flight question refuses it too (fail closed);
  * the owner alert carries the sold name, the replacement, the failed
    rules, the protection basis and the broker order id;
  * the BUY leg is dropped when the close was refused or did not fill (the
    freed room is not real), and every other BUY is untouched;
  * both legs' outcome is recorded durably, and a sold-but-not-bought
    rotation is paged;
  * the risk ceiling still binds the BUY: a close frees only its own risk.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import notifier
from src.models import (
    PortfolioDecision, Position, ReasoningChain, TargetPosition, TradeDecision,
)
from src.pipeline import TradingPipeline, _reason_cites_hard_trigger
from src.pipeline_context import RunContext
from src.pipeline_stages import (
    _alert_rotation_executed,
    _apply_rotation_execution,
    _drop_rotation_buy_if_room_not_freed,
    _record_rotation_buy_leg_outcome,
    _rotation_execution_enabled,
)
from src.portfolio_constructor import PortfolioConstructor
from src.risk.budget import RiskRequest, allocate_risk_budget
from src.risk.exit_guard import (
    StructuralProtectionCheck,
    claims_bearish_state_change,
    claims_regime_flip,
    holding_discipline_claim_check,
)
from src.rotation import RotationOpportunity, RotationPrecheck, rotation_sell_reason
from src.storage.db import Database

HELD_REASONS = ("R5 net evidence -1 if long — no rung",)
BROKEN_DETAIL = (
    "structural level 100.0 backing the stop has closed beyond it on two "
    "consecutive trading days: close 95.0 vs level 100.0 (break margin 0.05)"
)


def _opportunity(tier: str = "ineligible_hold") -> RotationOpportunity:
    return RotationOpportunity(
        new_symbol="NEW", new_score=1.8, held_symbol="OLD",
        held_score=None if tier == "ineligible_hold" else 0.9,
        tier=tier, reasons=HELD_REASONS if tier == "ineligible_hold" else (),
    )


def _precheck(opportunity=None) -> RotationPrecheck:
    return RotationPrecheck(
        opportunity=opportunity, headroom_pct=0.2, ceiling_pct=25.0,
        floor_pct=0.5,
    )


def _rc() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="m", news_check="n", earnings_check="e",
        signal_conflicts="s", sizing_logic="z", portfolio_balance="p",
        cash_target="c",
    )


def _decision(*targets: TargetPosition) -> PortfolioDecision:
    return PortfolioDecision(
        reasoning_chain=_rc(), portfolio_view="v", targets=list(targets),
    )


def _buy_new() -> TargetPosition:
    return TargetPosition(symbol="NEW", risk_allocation_pct=2.0, thesis="best idea")


def _pos(symbol="OLD", qty=10.0, price=95.0) -> Position:
    return Position(
        symbol=symbol, qty=qty, avg_entry=100.0, current_price=price,
        market_value=qty * price, unrealized_pnl=(price - 100.0) * qty,
        sector="Technology",
    )


class _ProtectionProbe:
    """Stands in for `TradingPipeline._structural_protection_for_holding`
    and counts calls, so a test can prove the check was never made."""

    def __init__(self, protected: bool, basis: str = "structural_level_broken",
                 detail: str = BROKEN_DETAIL):
        self.protected = protected
        self.basis = basis
        self.detail = detail
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> StructuralProtectionCheck:
        self.calls.append(kwargs)
        return StructuralProtectionCheck(
            protected=self.protected, basis=self.basis, detail=self.detail,
        )


def _pipeline(tmp_path, *, enabled=True, precheck=None, protected=False):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.db = db
    pipeline.config = SimpleNamespace(
        execution=SimpleNamespace(rotation_enabled=enabled),
    )
    pipeline.portfolio_manager = SimpleNamespace(
        last_rotation_precheck=precheck if precheck is not None else _precheck(_opportunity()),
    )
    probe = _ProtectionProbe(protected=protected)
    pipeline._structural_protection_for_holding = probe
    return pipeline, db, probe


def _ctx(run_id="run-1") -> RunContext:
    return RunContext(run_id=run_id, session="morning")


def _events(db, run_id="run-1") -> list[tuple[str | None, dict]]:
    import json
    rows = db.conn.execute(
        "SELECT symbol, evidence_json FROM specialist_evidence "
        "WHERE kind = 'pipeline_event' AND run_id = ? ORDER BY id", (run_id,),
    ).fetchall()
    return [(r["symbol"], json.loads(r["evidence_json"])) for r in rows]


def _rotation_events(db, run_id="run-1") -> list[tuple[str | None, dict]]:
    return [(s, e) for s, e in _events(db, run_id) if e.get("stage") == "rotation"]


HISTORY = {"OLD": {"thesis_invalid_if": "closes below 100", "entry_price": 100.0,
                   "stop_loss": 92.0}}


# ---------------------------------------------------------------------------
# Flag OFF — byte-for-byte the surfacing-only behaviour
# ---------------------------------------------------------------------------

def test_flag_off_is_a_no_op(tmp_path):
    pipeline, db, probe = _pipeline(tmp_path, enabled=False)
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    assert [t.symbol for t in decision.targets] == ["NEW"]
    assert ctx.rotation is None
    assert _events(db) == []
    assert probe.calls == [], "protection check must not even run with the flag off"


def test_flag_default_is_off_and_a_mock_config_never_reads_as_on():
    from src.config import ExecutionConfig
    assert ExecutionConfig().rotation_enabled is False
    # `_repeg_settings` convention: a MagicMock's truthy auto-attribute must
    # never read as "yes, close a real position on your own".
    assert _rotation_execution_enabled(SimpleNamespace(config=MagicMock())) is False
    assert _rotation_execution_enabled(SimpleNamespace(config=None)) is False
    assert _rotation_execution_enabled(
        SimpleNamespace(config=SimpleNamespace(execution=SimpleNamespace(rotation_enabled=1)))
    ) is False


# ---------------------------------------------------------------------------
# The categorical tier IS acted on, when the desk's own data says it is safe
# ---------------------------------------------------------------------------

def test_categorically_ineligible_unprotected_holding_is_rotated_out(tmp_path):
    pipeline, db, probe = _pipeline(tmp_path)
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)

    assert [t.symbol for t in decision.targets] == ["NEW", "OLD"]
    close = decision.targets[1]
    assert close.is_close and close.risk_allocation_pct == 0.0
    assert close.direction == "long"
    assert close.thesis.startswith("ROTATION (deterministic, src/rotation.py): OLD")
    assert close.thesis_invalid_if == "closes below 100"
    # The PM's own BUY is untouched.
    assert decision.targets[0].risk_allocation_pct == 2.0

    # Protection was checked with the position's OWN entry context, as a
    # long, under this run's id — the same call RiskStage makes.
    assert len(probe.calls) == 1
    assert probe.calls[0]["symbol"] == "OLD"
    assert probe.calls[0]["thesis_invalid_if"] == "closes below 100"
    assert probe.calls[0]["is_short"] is False
    assert probe.calls[0]["run_id"] == "run-1"

    assert ctx.rotation["held_symbol"] == "OLD"
    assert ctx.rotation["new_symbol"] == "NEW"
    assert ctx.rotation["protection_basis"] == "structural_level_broken"
    assert "sell_order_id" not in ctx.rotation  # nothing submitted yet

    # Durable audit record, not a log line.
    events = _rotation_events(db)
    assert len(events) == 1
    symbol, payload = events[0]
    assert symbol == "OLD"
    assert payload["outcome"] == "proposed"
    assert payload["new_symbol"] == "NEW"
    assert payload["held_reasons"] == list(HELD_REASONS)
    assert payload["protection_basis"] == "structural_level_broken"
    assert payload["reason"] == close.thesis


def test_ranked_margin_tier_is_surfaced_only_never_executed(tmp_path):
    pipeline, db, probe = _pipeline(
        tmp_path, precheck=_precheck(_opportunity("ranked_margin")),
    )
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    assert [t.symbol for t in decision.targets] == ["NEW"]
    assert ctx.rotation is None
    assert probe.calls == []
    (_, payload), = _rotation_events(db)
    assert payload["outcome"] == "skipped"
    assert payload["reason"] == "ranked_margin_tier_is_surfaced_only"


def test_nothing_surfaced_means_nothing_happens(tmp_path):
    pipeline, db, _ = _pipeline(tmp_path, precheck=_precheck(None))
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    assert [t.symbol for t in decision.targets] == ["NEW"]
    assert _events(db) == []


def test_structurally_protected_holding_is_surfaced_not_sold(tmp_path):
    """docs/WORK.md item 25: a position whose thesis level is intact is
    protected from a plain, no-real-trigger sale. Opportunity cost is not
    one of the three real triggers, so the rotation stops here."""
    pipeline, db, probe = _pipeline(tmp_path, protected=True)
    probe.basis, probe.detail = "structural_level_intact", "close 101 vs level 100"
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    assert [t.symbol for t in decision.targets] == ["NEW"]
    assert ctx.rotation is None
    (_, payload), = _rotation_events(db)
    assert payload["reason"] == "held_symbol_structurally_protected"
    assert payload["protection_basis"] == "structural_level_intact"


def test_protection_check_failure_fails_closed(tmp_path):
    pipeline, db, _ = _pipeline(tmp_path)

    def _boom(**kwargs):
        raise RuntimeError("bars feed down")

    pipeline._structural_protection_for_holding = _boom
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    assert [t.symbol for t in decision.targets] == ["NEW"]
    (_, payload), = _rotation_events(db)
    assert payload["reason"] == "protection_check_failed"


# ---------------------------------------------------------------------------
# The desk never invents the buy leg and never overrides the PM on the held name
# ---------------------------------------------------------------------------

def test_not_rotated_when_pm_did_not_target_the_new_candidate(tmp_path):
    pipeline, db, probe = _pipeline(tmp_path)
    ctx = _ctx()
    other = TargetPosition(symbol="OTHER", risk_allocation_pct=1.0, thesis="x")
    decision = _decision(other)
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    assert [t.symbol for t in decision.targets] == ["OTHER"]
    assert probe.calls == []
    (_, payload), = _rotation_events(db)
    assert payload["reason"] == "pm_did_not_target_new_candidate"


def test_a_zero_size_pm_target_for_the_new_name_does_not_count_as_wanting_it(tmp_path):
    pipeline, db, _ = _pipeline(tmp_path)
    ctx = _ctx()
    decision = _decision(TargetPosition(symbol="NEW", risk_allocation_pct=0.0, thesis="x"))
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    (_, payload), = _rotation_events(db)
    assert payload["reason"] == "pm_did_not_target_new_candidate"


def test_not_rotated_when_pm_already_targets_the_held_symbol(tmp_path):
    """Whether the PM closes OLD itself or adds to it, the model has spoken —
    no duplicate close, no override."""
    for pm_size in (0.0, 1.5):
        sub = tmp_path / f"s{pm_size}"
        sub.mkdir()
        pipeline, db, probe = _pipeline(sub)
        ctx = _ctx()
        decision = _decision(
            _buy_new(),
            TargetPosition(symbol="OLD", risk_allocation_pct=pm_size, thesis="pm call"),
        )
        _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
        assert [t.symbol for t in decision.targets] == ["NEW", "OLD"]
        assert decision.targets[1].thesis == "pm call"
        assert ctx.rotation is None
        assert probe.calls == []
        (_, payload), = _rotation_events(db)
        assert payload["reason"] == "pm_already_targets_held_symbol"


def test_a_short_is_never_rotated(tmp_path):
    pipeline, db, probe = _pipeline(tmp_path)
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos(qty=-10.0)], HISTORY)
    assert [t.symbol for t in decision.targets] == ["NEW"]
    assert probe.calls == []
    (_, payload), = _rotation_events(db)
    assert payload["reason"] == "held_symbol_is_not_a_long_position"


# ---------------------------------------------------------------------------
# In-flight orders, partial fills, same-day entries — never rotated
# ---------------------------------------------------------------------------

def test_in_flight_order_on_held_symbol_blocks_rotation(tmp_path):
    pipeline, db, probe = _pipeline(tmp_path)
    db.insert_trade(
        symbol="OLD", action="PARTIAL_SELL(50%)", qty=5.0, price=95.0,
        reasoning="midday trim", run_id="run-0", broker_order_id="ord-1",
        fill_status="submitted",
    )
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    assert [t.symbol for t in decision.targets] == ["NEW"]
    assert probe.calls == []
    (_, payload), = _rotation_events(db)
    assert payload["reason"] == "order_in_flight_on_held_symbol"
    assert "ord-1" in payload["detail"]


def test_held_symbol_bought_today_is_never_rotated(tmp_path):
    """A day-zero exit is what the exit noise band exists to prevent (OKLO
    2026-08-26: bought and sold at 0.67 ATR, never given one session's
    normal range). A same-day BUY — filled or not — refuses the rotation."""
    pipeline, db, probe = _pipeline(tmp_path)
    db.insert_trade(
        symbol="OLD", action="BUY", qty=10.0, price=100.0, reasoning="entry",
        run_id="run-0", broker_order_id="ord-b", fill_status="filled",
    )
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    assert [t.symbol for t in decision.targets] == ["NEW"]
    assert probe.calls == []
    (_, payload), = _rotation_events(db)
    assert payload["reason"] == "held_symbol_bought_today"


def test_pending_protection_restore_wal_row_blocks_rotation(tmp_path):
    """A pending_protection_restores row means a SELL on this name is
    already mid-flight (stops cancelled write-ahead, sale not yet
    finalized). Rotating on top of it would race the drain."""
    pipeline, db, probe = _pipeline(tmp_path)
    db.get_pending_protection_restores = lambda: [
        {"symbol": "OLD", "sell_order_id": "wal-9"},
    ]
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    assert [t.symbol for t in decision.targets] == ["NEW"]
    assert probe.calls == []
    (_, payload), = _rotation_events(db)
    assert payload["reason"] == "sell_already_in_flight_wal_row"
    assert payload["detail"] == "wal-9"


def test_pending_repeg_row_blocks_rotation(tmp_path):
    pipeline, db, probe = _pipeline(tmp_path)
    db.get_pending_repegs = lambda: [{"symbol": "OLD", "old_order_id": "rp-1"}]
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    assert [t.symbol for t in decision.targets] == ["NEW"]
    (_, payload), = _rotation_events(db)
    assert payload["reason"] == "entry_repeg_in_flight"


def test_unanswerable_in_flight_question_fails_closed(tmp_path):
    pipeline, db, probe = _pipeline(tmp_path)

    def _boom():
        raise RuntimeError("db locked")

    db.get_pending_repegs = _boom
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    assert [t.symbol for t in decision.targets] == ["NEW"]
    assert probe.calls == []
    (_, payload), = _rotation_events(db)
    assert payload["reason"] == "in_flight_check_failed"


# ---------------------------------------------------------------------------
# The reason is real and checkable, and the sale goes through the normal gates
# ---------------------------------------------------------------------------

def _reason() -> str:
    return rotation_sell_reason(
        _opportunity(), protection_basis="structural_level_broken",
        protection_detail=BROKEN_DETAIL, headroom_pct=0.2, ceiling_pct=25.0,
        floor_pct=0.5,
    )


def test_reason_names_every_measured_fact():
    reason = _reason()
    assert "OLD" in reason and "NEW" in reason
    assert "R5 net evidence -1 if long" in reason
    assert "structural_level_broken" in reason
    assert "closed beyond it" in reason
    assert "0.20%" in reason and "25.00%" in reason and "0.50%" in reason
    assert "score 1.80" in reason
    # `_build_sell` appends " (thesis_invalid_if: ...)" and then truncates
    # the whole reasoning at 500 — the measured clauses must all survive.
    assert len(reason) <= 420, len(reason)


def test_reason_makes_no_claim_the_holding_discipline_checker_could_find_false():
    """The rotation reason states only what was measured. It never asserts a
    regime flip or a bearish state change, so `holding_discipline_claim_check`
    — the deterministic gate RiskStage runs on every SELL — can never find
    it PROVABLY FALSE, and it never rides through on a claim it did not
    check. Verdict is 'ok' even against a protected position, which is
    exactly why `_apply_rotation_execution` refuses protected positions
    itself instead of relying on this check to."""
    reason = _reason()
    assert not claims_regime_flip(reason)
    assert not claims_bearish_state_change(reason)
    check = holding_discipline_claim_check(
        action="SELL", reason=reason, symbol="OLD", protected=True,
        macro_regime_today="risk-on", macro_status="ok", active_state_changes="",
    )
    assert check.verdict == "ok" and not check.blocks


def test_reason_does_not_word_itself_past_the_midday_keyword_gate():
    """`_reason_cites_hard_trigger` is the midday/close reviewer's phrase
    gate. A rotation reason built on a broken structural level or the noise
    band contains none of its keywords — this feature does not smuggle a
    trigger word in to pass a gate it does not go through (it takes the
    morning PM path, whose gates are RiskStage's). The one basis that DOES
    contain a keyword is `thesis_invalid_if_triggered`, and that is a real
    trigger by the desk's own definition, confirmed on two closes."""
    assert not _reason_cites_hard_trigger(_reason())
    noise = rotation_sell_reason(
        _opportunity(), protection_basis="noise_band_broken",
        protection_detail="adverse move 3.1 ATR from entry, outside the band",
        headroom_pct=0.2, ceiling_pct=25.0, floor_pct=0.5,
    )
    assert not _reason_cites_hard_trigger(noise)


def test_reason_builder_refuses_the_ranked_margin_tier():
    with pytest.raises(ValueError):
        rotation_sell_reason(
            _opportunity("ranked_margin"), protection_basis="noise_band_broken",
            protection_detail="x", headroom_pct=0.2, ceiling_pct=25.0, floor_pct=0.5,
        )


def test_injected_close_becomes_an_ordinary_full_sell_through_the_real_builder(tmp_path):
    """The appended target is built by `PortfolioConstructor._build_sell`
    like any PM close: a SELL for 100% carrying the rotation reason as its
    reasoning. That `TradeDecision` is what RiskStage (hard rules, Risk
    Manager refusal, holding-discipline check) and ExecutionStage
    (`_submit_protected_sell`) then gate — no separate path exists."""
    pipeline, _, _ = _pipeline(tmp_path)
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    close = decision.targets[1]
    trade = PortfolioConstructor._build_sell(close, _pos(), 10.0, 0.0)
    assert isinstance(trade, TradeDecision)
    assert trade.action == "SELL" and trade.allocation_pct == 100.0
    assert trade.reasoning.startswith("ROTATION (deterministic")
    assert "free room for NEW" in trade.reasoning, "measured clauses survive truncation"
    assert trade.thesis_invalid_if == "closes below 100"


# ---------------------------------------------------------------------------
# Owner alert, buy-leg discipline, durable outcome
# ---------------------------------------------------------------------------

def _capture_alerts(monkeypatch) -> list[tuple[str, list[str] | None]]:
    sent: list[tuple[str, list[str] | None]] = []

    def _fake(text, *, symbols=None):
        sent.append((text, symbols))
        return True

    monkeypatch.setattr(notifier, "send_owner_alert", _fake)
    return sent


def _rotation_dict(**extra) -> dict:
    base = {
        "held_symbol": "OLD", "new_symbol": "NEW", "new_score": 1.8,
        "held_reasons": list(HELD_REASONS),
        "protection_basis": "structural_level_broken",
        "protection_detail": BROKEN_DETAIL, "headroom_pct": 0.2,
        "ceiling_pct": 25.0, "reason": _reason(),
    }
    base.update(extra)
    return base


def test_owner_alert_fires_with_the_sale_and_the_measured_reason(monkeypatch):
    sent = _capture_alerts(monkeypatch)
    _alert_rotation_executed(
        rotation=_rotation_dict(), qty=10.0, limit_price=94.53, order_id="brk-42",
    )
    (text, symbols), = sent
    assert text.startswith("POSITION CLOSED AUTOMATICALLY")
    assert "OLD" in text and "NEW" in text
    assert "10 share(s)" in text and "$94.53" in text and "brk-42" in text
    assert "R5 net evidence -1 if long" in text
    assert "structural_level_broken" in text
    assert "score 1.80" in text
    assert symbols == ["OLD", "NEW"]


def test_owner_alert_never_raises(monkeypatch):
    def _boom(text, *, symbols=None):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(notifier, "send_owner_alert", _boom)
    _alert_rotation_executed(
        rotation=_rotation_dict(), qty=1.0, limit_price=1.0, order_id=None,
    )  # no exception


def _buy(symbol: str) -> TradeDecision:
    return TradeDecision(
        action="BUY", symbol=symbol, allocation_pct=5.0, entry_price=10.0,
        stop_loss=9.0, take_profit=12.0, reasoning="r",
    )


def test_buy_leg_dropped_when_the_rotation_close_did_not_fill(tmp_path):
    pipeline, db, _ = _pipeline(tmp_path)
    ctx = _ctx()
    ctx.rotation = _rotation_dict(sell_order_id="s1")
    buys = [_buy("NEW"), _buy("OTHER")]
    kept = _drop_rotation_buy_if_room_not_freed(
        pipeline, ctx, buys, {"s1": "canceled"},
    )
    assert [d.symbol for d in kept] == ["OTHER"], "only the rotation's own BUY is dropped"
    (skip,) = ctx.execution_skips
    assert skip["symbol"] == "NEW" and skip["reason"] == "rotation_room_not_freed"
    assert "canceled" in skip["detail"]


def test_buy_leg_dropped_when_the_rotation_close_was_refused_upstream(tmp_path):
    """No `sell_order_id` on the rotation record means the close never
    reached the broker — Risk Manager refusal, hard-rule drop, or a
    protected-sell skip. The freed room is not real; the BUY must not
    proceed on it."""
    pipeline, db, _ = _pipeline(tmp_path)
    ctx = _ctx()
    ctx.rotation = _rotation_dict()
    kept = _drop_rotation_buy_if_room_not_freed(pipeline, ctx, [_buy("NEW")], {})
    assert kept == []
    (skip,) = ctx.execution_skips
    assert skip["reason"] == "rotation_room_not_freed"
    assert "not submitted" in skip["detail"]


def test_buy_leg_kept_when_the_rotation_close_filled(tmp_path):
    pipeline, db, _ = _pipeline(tmp_path)
    ctx = _ctx()
    ctx.rotation = _rotation_dict(sell_order_id="s1")
    buys = [_buy("NEW"), _buy("OTHER")]
    assert _drop_rotation_buy_if_room_not_freed(pipeline, ctx, buys, {"s1": "filled"}) is buys
    assert ctx.execution_skips == []


def test_no_rotation_means_buy_list_untouched(tmp_path):
    pipeline, db, _ = _pipeline(tmp_path)
    ctx = _ctx()
    buys = [_buy("NEW")]
    assert _drop_rotation_buy_if_room_not_freed(pipeline, ctx, buys, {}) is buys


def test_sold_but_not_bought_is_recorded_and_paged(tmp_path, monkeypatch):
    sent = _capture_alerts(monkeypatch)
    pipeline, db, _ = _pipeline(tmp_path)
    ctx = _ctx()
    ctx.rotation = _rotation_dict(sell_order_id="s1")
    ctx.execution_skips.append({
        "symbol": "NEW", "reason": "insufficient_cash", "detail": "needs $500, has $20",
    })
    _record_rotation_buy_leg_outcome(pipeline, ctx, orders=[])
    (symbol, payload), = _rotation_events(db)
    assert symbol == "NEW" and payload["outcome"] == "buy_not_submitted"
    assert payload["reason"] == "insufficient_cash: needs $500, has $20"
    (text, symbols), = sent
    assert text.startswith("ROTATION INCOMPLETE")
    assert "insufficient_cash" in text and symbols == ["OLD", "NEW"]


def test_sold_and_bought_is_recorded_and_not_paged(tmp_path, monkeypatch):
    sent = _capture_alerts(monkeypatch)
    pipeline, db, _ = _pipeline(tmp_path)
    ctx = _ctx()
    ctx.rotation = _rotation_dict(sell_order_id="s1")
    _record_rotation_buy_leg_outcome(
        pipeline, ctx, orders=[{"id": "b1", "symbol": "NEW", "action": "BUY"}],
    )
    (symbol, payload), = _rotation_events(db)
    assert symbol == "NEW" and payload["outcome"] == "buy_submitted"
    assert sent == []


def test_outcome_is_not_recorded_when_no_rotation_sell_reached_the_broker(tmp_path, monkeypatch):
    sent = _capture_alerts(monkeypatch)
    pipeline, db, _ = _pipeline(tmp_path)
    ctx = _ctx()
    ctx.rotation = _rotation_dict()  # proposed, but refused upstream
    _record_rotation_buy_leg_outcome(pipeline, ctx, orders=[])
    assert _rotation_events(db) == [] and sent == []


# ---------------------------------------------------------------------------
# Every existing risk ceiling still binds
# ---------------------------------------------------------------------------

def test_a_close_frees_only_its_own_risk_and_the_ceiling_still_binds():
    """Closing OLD (4% of a book at 24.8%) leaves the other 20.8% committed:
    NEW asking 6% is capped at the 4.2% the ceiling actually leaves. The
    rotation is not a side door around `max_portfolio_risk_pct`."""
    allocation = allocate_risk_budget(
        [RiskRequest("OLD", 0.0), RiskRequest("NEW", 6.0)],
        existing_pct={"OLD": 4.0, "A": 10.4, "B": 10.4},
        ceiling_pct=25.0, floor_pct=0.5,
    )
    grant = allocation.grants["NEW"]
    assert grant.granted_pct == pytest.approx(4.2)
    assert grant.limited_by == "total_ceiling"


def test_same_session_close_now_reaches_the_allocator_as_a_zero_request():
    """The constructor fix this feature depends on: a full close is handed
    to `allocate_risk_budget` as the zero request it already documents, so
    the closed name's existing risk stops counting against the same
    session's BUY. Measured before the fix: NEW granted 0.00% here."""
    from tests.test_portfolio_constructor import _analysis

    constructor = PortfolioConstructor()
    targets = [
        TargetPosition(symbol="OLD", risk_allocation_pct=0.0, thesis="close"),
        TargetPosition(symbol="NEW", risk_allocation_pct=2.0, thesis="open"),
    ]
    analysis = _analysis("NEW", entry=100.0, stop=95.0, target=115.0)
    plans = constructor._plan_risk_targets(
        targets, analyses_by_sym={"NEW": analysis}, price_map={"NEW": 100.0},
        current_weights={"OLD": 10.0}, existing_risk_pct={"OLD": 24.8},
        clusters=None,
    )
    assert plans["OLD"].risk_pct == 0.0
    assert "NEW" in plans, "the BUY must not be denied for room the close frees"
    assert plans["NEW"].risk_pct == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# PM prompt wording
# ---------------------------------------------------------------------------

def test_pm_section_says_so_only_when_execution_is_enabled():
    from src.agents.portfolio_manager import PortfolioManagerAgent
    from src.verdicts import RankedCandidate

    kwargs = dict(
        ranked=[RankedCandidate(symbol="NEW", direction="bullish", score=1.8)],
        blocked={"OLD": list(HELD_REASONS)}, held_symbols={"OLD"},
        existing_risk_pct={"OLD": 24.8}, ceiling_pct=25.0,
    )
    off = PortfolioManagerAgent._render_rotation_section(**kwargs)
    on = PortfolioManagerAgent._render_rotation_section(**kwargs, execute_enabled=True)
    assert "AUTOMATIC ROTATION IS ENABLED" not in off
    assert "AUTOMATIC ROTATION IS ENABLED" in on
    assert on.startswith(off), "enabling only APPENDS a note; the comparison text is unchanged"
    # And the precheck the pipeline acts on is the one the prompt was built from.
    precheck = PortfolioManagerAgent.rotation_precheck(**kwargs)
    assert precheck.opportunity is not None
    assert precheck.opportunity.tier == "ineligible_hold"
    assert precheck.opportunity.held_symbol == "OLD"
    assert precheck.headroom_pct == pytest.approx(0.2)
