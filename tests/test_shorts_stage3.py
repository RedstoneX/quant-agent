"""Stage 3 of short selling — shorts can be OPENED and COVERED.

Stage 1 (`tests/test_shorts_countable.py`) made a held short COUNTABLE.
Stage 2 (`tests/test_shorts_safe.py`) made its protective-stop geometry
correct. This stage lifts the last gate: the order path itself can now open
and cover a short, subject to three things that gate it — a borrow check
(D6), two exposure caps (D9), and a mandatory protective stop that escalates
to an immediate market cover on failure (D7) — and none of them is a master
switch (D11); together they ARE the control surface.

Surfaces covered: ``src/portfolio_constructor.py`` (signed targets, sign-
crossing refusal, direction-aware stop geometry/widening, the sizing
haircut, ``_build_short``/``_build_cover``), ``src/risk/rules.py`` (the two
short exposure caps, the cover-cash exemption), and
``src/pipeline_stages.py``'s ``ExecutionStage`` (the borrow gate, side-aware
order submission, and the protection-failure escalation).
"""

from unittest.mock import MagicMock

from src.config import RiskConfig
from src.models import (
    Position,
    PortfolioDecision,
    PositionAction,
    ReasoningChain,
    TargetPosition,
    TechAnalysisResult,
    TechReasoningChain,
    TradeDecision,
)
from src.pipeline import TradingPipeline
from src.pipeline_context import RunContext
from src.pipeline_stages import ExecutionStage
from src.portfolio_constructor import PortfolioConstructor
from src.risk.rules import RiskRuleEngine


# ==========================================================================
# Shared fixtures
# ==========================================================================

def _pos(symbol: str, qty: float, entry: float, price: float,
         sector: str = "Technology") -> Position:
    return Position(
        symbol=symbol, qty=qty, avg_entry=entry, current_price=price,
        market_value=qty * price, unrealized_pnl=qty * (price - entry),
        sector=sector,
    )


def _tech_rc() -> TechReasoningChain:
    return TechReasoningChain(
        trend="x", momentum="x", volatility="x",
        volume="x", support_resistance="x",
    )


def _pm_rc() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="x", news_check="x", earnings_check="x",
        signal_conflicts="x", sizing_logic="x",
        portfolio_balance="x", cash_target="x",
    )


def _long_analysis(symbol="NVDA", entry=250.0, stop=237.5, target=300.0,
                    atr_14=None) -> TechAnalysisResult:
    return TechAnalysisResult(
        symbol=symbol, rating="buy", entry_price=entry, stop_loss=stop,
        reference_target=target, reasoning="test",
        support_levels=[stop], resistance_levels=[target],
        setup_type="range", expected_horizon_sessions=10,
        reasoning_chain=_tech_rc(), atr_14=atr_14,
    )


def _short_analysis(symbol="TSLA", entry=250.0, stop=262.5, target=200.0,
                     atr_14=None) -> TechAnalysisResult:
    return TechAnalysisResult(
        symbol=symbol, rating="sell", entry_price=entry, stop_loss=stop,
        reference_target=target, reasoning="test",
        support_levels=[target], resistance_levels=[stop],
        setup_type="range", expected_horizon_sessions=10,
        reasoning_chain=_tech_rc(), atr_14=atr_14,
    )


def _short_target(symbol="TSLA", weight=5.0, suggested_stop=None) -> TargetPosition:
    return TargetPosition(
        symbol=symbol, direction="short", target_weight_pct=weight,
        conviction="high", thesis="overvalued",
        suggested_stop_price=suggested_stop,
    )


def _cfg(**kw) -> RiskConfig:
    base = dict(max_position_pct=20.0, max_total_position_pct=90.0,
                max_daily_loss_pct=3.0, max_sector_pct=40.0,
                require_stop_loss=True, allow_margin=False)
    base.update(kw)
    return RiskConfig(**base)


def _exec_pipeline() -> MagicMock:
    """A MagicMock pipeline wired with the benign defaults every
    ExecutionStage test needs — same convention as
    tests/test_pipeline_stages.py's inline setup, collected here since
    every test below needs the same baseline."""
    pipeline = MagicMock()
    pipeline._format_qty = lambda q: str(q)
    pipeline._order_accepted.return_value = True
    pipeline._full_sell_qty = TradingPipeline._full_sell_qty
    pipeline._reduce_sell_qty = TradingPipeline._reduce_sell_qty
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0}, [], {},
    )
    pipeline.risk_engine.check_daily_loss.return_value = None
    pipeline.broker.wait_for_order_terminal.return_value = "filled"
    pipeline.broker.get_shortability.return_value = {
        "shortable": True, "easy_to_borrow": True, "reason": "eligible",
    }
    return pipeline


def _ctx(decisions, positions=None, cash=50_000.0, total_value=100_000.0) -> RunContext:
    ctx = RunContext.start("morning")
    ctx.cash = cash
    ctx.total_value = total_value
    ctx.last_equity = total_value
    ctx.positions = positions or []
    ctx.portfolio_decision = PortfolioDecision(
        reasoning_chain=_pm_rc(), decisions=decisions, portfolio_view="test",
    )
    ctx.symbols_bars = {}
    return ctx


# ==========================================================================
# 1. Opening a short end to end (constructor + execution)
# ==========================================================================

def test_open_short_end_to_end_submits_sell_short_and_places_buy_stop_above_entry():
    """The headline Stage 3 property. The constructor builds a SHORT
    decision; ExecutionStage submits it with side='sell_short' and places
    the mandatory protective stop with side='sell_short' too (so
    `place_entry_protection` mirrors it to a BUY stop ABOVE entry)."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_short_target()], positions=[], analyses=[_short_analysis()],
        total_value=100_000, price_map={"TSLA": 250.0},
    )
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.action == "SHORT"
    assert decision.stop_loss > decision.entry_price

    pipeline = _exec_pipeline()
    pipeline.broker.get_latest_price.return_value = 250.0
    pipeline.broker.submit_order.return_value = {
        "id": "short-order-1", "status": "accepted", "symbol": "TSLA",
        "side": "sell_short", "pending_stop_price": decision.stop_loss,
    }
    pipeline.broker.get_order_fill_info.return_value = {"filled_qty": 20.0}
    pipeline.broker.place_entry_protection.return_value = {"id": "stop-1"}

    ctx = _ctx([decision])
    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    submit_kwargs = pipeline.broker.submit_order.call_args.kwargs
    assert submit_kwargs["side"] == "sell_short"
    assert submit_kwargs["stop_loss_price"] == decision.stop_loss

    pipeline.broker.place_entry_protection.assert_called_once()
    protect_kwargs = pipeline.broker.place_entry_protection.call_args.kwargs
    assert protect_kwargs["side"] == "sell_short"
    assert protect_kwargs["stop_price"] == decision.stop_loss
    assert protect_kwargs["stop_price"] > decision.entry_price, (
        "the protective stop for a short must sit ABOVE entry"
    )


# ==========================================================================
# 2. Covering a short — partial and full
# ==========================================================================

def test_cover_short_partial_buys_back_a_fraction():
    position = _pos("TSLA", qty=-40, entry=250, price=240)
    decision = TradeDecision(
        action="COVER", symbol="TSLA", allocation_pct=50.0,
        entry_price=0.0, stop_loss=0.0, take_profit=0.0,
        reasoning="trim the short",
    )
    pipeline = _exec_pipeline()
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0}, [position], {},
    )
    pipeline._submit_protected_sell.return_value = (
        {"id": "cover-1", "status": "accepted", "symbol": "TSLA", "side": "buy"},
        {"order_id": "cover-1", "symbol": "TSLA",
         "position_qty_before_sell": 40.0, "specs": [], "wal_row_id": None,
         "side": "buy"},
    )

    ctx = _ctx([decision], positions=[position])
    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    call_kwargs = pipeline._submit_protected_sell.call_args.kwargs
    assert call_kwargs["side"] == "buy"
    assert call_kwargs["qty"] == 20.0            # 50% of the 40-share short
    assert call_kwargs["position_qty_before_sell"] == 40.0
    assert call_kwargs["label"] == "PARTIAL_COVER(50%)"


def test_cover_short_full_buys_back_everything():
    position = _pos("TSLA", qty=-40, entry=250, price=240)
    decision = TradeDecision(
        action="COVER", symbol="TSLA", allocation_pct=100.0,
        entry_price=0.0, stop_loss=0.0, take_profit=0.0,
        reasoning="close the short",
    )
    pipeline = _exec_pipeline()
    pipeline._refresh_account_state.return_value = (
        {"cash": 50_000.0, "portfolio_value": 100_000.0}, [position], {},
    )
    pipeline._submit_protected_sell.return_value = (
        {"id": "cover-2", "status": "accepted", "symbol": "TSLA", "side": "buy"},
        {"order_id": "cover-2", "symbol": "TSLA",
         "position_qty_before_sell": 40.0, "specs": [], "wal_row_id": None,
         "side": "buy"},
    )

    ctx = _ctx([decision], positions=[position])
    orders = ExecutionStage(pipeline=pipeline).run(ctx)

    assert len(orders) == 1
    call_kwargs = pipeline._submit_protected_sell.call_args.kwargs
    assert call_kwargs["side"] == "buy"
    assert call_kwargs["qty"] == 40.0
    assert call_kwargs["label"] == "COVER"
    # No protective stop is ever placed for a COVER — it reduces risk, it
    # doesn't open any.
    pipeline.broker.place_entry_protection.assert_not_called()


# ==========================================================================
# 3. D3 — sign-crossing is refused; only the closing leg is emitted
# ==========================================================================

def test_long_to_short_target_emits_only_the_flattening_sell():
    """A held LONG with a SHORT target must not flip in one order. The
    constructor emits ONLY a full-close SELL this session."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[TargetPosition(symbol="NVDA", direction="short",
                                target_weight_pct=5.0, conviction="high",
                                thesis="reversal")],
        positions=[_pos("NVDA", qty=150, entry=100, price=100)],  # +15% long
        analyses=[_short_analysis(symbol="NVDA")],
        total_value=100_000, price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    assert decisions[0].action == "SELL"
    assert decisions[0].allocation_pct == 100.0


def test_short_to_long_target_emits_only_the_flattening_cover():
    """The mirror: a held SHORT with a LONG target emits ONLY a full COVER."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[TargetPosition(symbol="TSLA", direction="long",
                                target_weight_pct=5.0, conviction="high",
                                thesis="reversal")],
        positions=[_pos("TSLA", qty=-40, entry=250, price=250)],  # -10% short
        analyses=[_long_analysis(symbol="TSLA")],
        total_value=100_000, price_map={"TSLA": 250.0},
    )
    assert len(decisions) == 1
    assert decisions[0].action == "COVER"
    assert decisions[0].allocation_pct == 100.0


# ==========================================================================
# 4. D4 / D5 — direction-aware stop geometry and widening
# ==========================================================================

def test_short_stop_at_or_below_entry_is_rejected():
    """D4: a short's stop must sit strictly ABOVE entry. A PM-suggested
    stop below entry is refused, not silently accepted."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_short_target(suggested_stop=240.0)],  # BELOW entry $250
        positions=[], analyses=[_short_analysis()],
        total_value=100_000, price_map={"TSLA": 250.0},
    )
    assert decisions == []


def test_short_stop_inside_noise_band_is_widened_upward():
    """D5: a short's stop inside `min_stop_atr_multiple` ATRs of entry is
    pushed UP (away from entry) — the mirror of a long's stop being pushed
    DOWN. `_short_analysis`'s default setup_type='range' scales the base
    3.0x multiple by 1.15 (see `_stop_atr_multiple`) -> 3.45 x ATR(5.0) =
    17.25 -> band edge = entry 250 + 17.25 = 267.25. The R:R at that wider
    stop must still clear the floor."""
    constructor = PortfolioConstructor()
    widened = constructor._widen_stop_past_noise(
        "TSLA",
        _short_analysis(entry=250.0, stop=252.0, target=200.0, atr_14=5.0),
        entry_price=250.0, stop_loss=252.0, direction="short",
    )
    assert widened == 267.25
    assert widened > 252.0
    assert widened > 250.0


def test_short_widened_stop_failing_reward_risk_floor_is_rejected():
    """D5: when widening a short's stop to the noise-band edge collapses
    reward:risk below `min_reward_risk_after_widening` (default 1.5), the
    trade is rejected outright rather than taken at a worse payoff. Band
    edge = 265 (entry 250 + 3*5 ATR); target 235 → reward 15, risk 15 →
    R:R 1.0 < 1.5."""
    constructor = PortfolioConstructor()
    widened = constructor._widen_stop_past_noise(
        "TSLA",
        _short_analysis(entry=250.0, stop=252.0, target=235.0, atr_14=5.0),
        entry_price=250.0, stop_loss=252.0, direction="short",
    )
    assert widened is None


# ==========================================================================
# 5. D6 — the borrow gate: three distinct refusals
# ==========================================================================

def _borrow_gated_ctx_and_pipeline(borrow_result_or_exc):
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_short_target()], positions=[], analyses=[_short_analysis()],
        total_value=100_000, price_map={"TSLA": 250.0},
    )
    assert len(decisions) == 1

    pipeline = _exec_pipeline()
    pipeline.broker.get_latest_price.return_value = 250.0
    if isinstance(borrow_result_or_exc, Exception):
        pipeline.broker.get_shortability.side_effect = borrow_result_or_exc
    else:
        pipeline.broker.get_shortability.return_value = borrow_result_or_exc
    ctx = _ctx(decisions)
    return pipeline, ctx


def test_borrow_gate_refuses_a_not_shortable_symbol():
    pipeline, ctx = _borrow_gated_ctx_and_pipeline(
        {"shortable": False, "easy_to_borrow": True, "reason": "not_shortable"},
    )
    orders = ExecutionStage(pipeline=pipeline).run(ctx)
    assert orders == []
    pipeline.broker.submit_order.assert_not_called()
    assert ctx.execution_skips[0]["reason"] == "borrow_gate"
    assert ctx.execution_skips[0]["detail"] == "not_shortable"


def test_borrow_gate_refuses_a_hard_to_borrow_symbol():
    pipeline, ctx = _borrow_gated_ctx_and_pipeline(
        {"shortable": True, "easy_to_borrow": False, "reason": "hard_to_borrow"},
    )
    orders = ExecutionStage(pipeline=pipeline).run(ctx)
    assert orders == []
    pipeline.broker.submit_order.assert_not_called()
    assert ctx.execution_skips[0]["reason"] == "borrow_gate"
    assert ctx.execution_skips[0]["detail"] == "hard_to_borrow"


def test_borrow_gate_refuses_when_flags_are_unreadable():
    """A broker/API failure must fail CLOSED, not guess the short open."""
    pipeline, ctx = _borrow_gated_ctx_and_pipeline(RuntimeError("asset API down"))
    orders = ExecutionStage(pipeline=pipeline).run(ctx)
    assert orders == []
    pipeline.broker.submit_order.assert_not_called()
    assert ctx.execution_skips[0]["reason"] == "borrow_gate"
    assert ctx.execution_skips[0]["detail"] == "asset_lookup_failed"


# ==========================================================================
# 6. D7 — protective-stop failure on a short escalates to a market cover
# ==========================================================================

def test_protective_stop_failure_on_a_short_triggers_immediate_market_cover():
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[_short_target()], positions=[], analyses=[_short_analysis()],
        total_value=100_000, price_map={"TSLA": 250.0},
    )
    decision = decisions[0]

    pipeline = _exec_pipeline()
    pipeline.broker.get_latest_price.return_value = 250.0
    entry_order = {
        "id": "short-order-1", "status": "accepted", "symbol": "TSLA",
        "side": "sell_short", "pending_stop_price": decision.stop_loss,
    }
    cover_order = {
        "id": "cover-emergency-1", "status": "accepted", "symbol": "TSLA",
        "side": "buy",
    }
    pipeline.broker.submit_order.side_effect = [entry_order, cover_order]
    pipeline.broker.get_order_fill_info.return_value = {"filled_qty": 20.0}
    # The protective stop FAILS.
    pipeline.broker.place_entry_protection.return_value = None

    ctx = _ctx([decision])
    ExecutionStage(pipeline=pipeline).run(ctx)

    assert pipeline.broker.submit_order.call_count == 2, (
        "the entry AND the emergency cover must both have been submitted"
    )
    emergency_kwargs = pipeline.broker.submit_order.call_args_list[1].kwargs
    assert emergency_kwargs["symbol"] == "TSLA"
    assert emergency_kwargs["qty"] == 20.0
    assert emergency_kwargs["side"] == "buy"


# ==========================================================================
# 7. D8 — the gap-risk sizing haircut
# ==========================================================================

def test_short_gap_risk_haircut_produces_a_strictly_smaller_position_than_a_long():
    """Same risk allocation (0.5%), same $12.50/share stop distance, same
    entry $250 and gross multiplier (1x) for both a long and a short. The
    only difference is direction, and the only thing that should differ is
    the short's sizing haircut (default short_gap_risk_multiple=1.5)."""
    constructor = PortfolioConstructor()

    long_decisions = constructor.construct_orders(
        targets=[TargetPosition(symbol="LONGX", direction="long",
                                risk_allocation_pct=0.5, conviction="high",
                                thesis="breakout")],
        positions=[], analyses=[_long_analysis(symbol="LONGX", entry=250.0,
                                                stop=237.5, target=300.0)],
        total_value=100_000, price_map={"LONGX": 250.0},
    )
    short_decisions = constructor.construct_orders(
        targets=[TargetPosition(symbol="SHORTX", direction="short",
                                risk_allocation_pct=0.5, conviction="high",
                                thesis="breakdown")],
        positions=[], analyses=[_short_analysis(symbol="SHORTX", entry=250.0,
                                                 stop=262.5, target=200.0)],
        total_value=100_000, price_map={"SHORTX": 250.0},
    )
    assert len(long_decisions) == 1 and long_decisions[0].action == "BUY"
    assert len(short_decisions) == 1 and short_decisions[0].action == "SHORT"

    long_alloc = long_decisions[0].allocation_pct
    short_alloc = short_decisions[0].allocation_pct

    # THE numeric assertion: same risk allocation, same stop distance —
    # the short is sized at exactly long / short_gap_risk_multiple (1.5).
    assert long_alloc == 10.0
    assert short_alloc == round(long_alloc / 1.5, 2)
    assert short_alloc == 6.67
    assert short_alloc < long_alloc, (
        "a short must open strictly smaller than an equivalent long at the "
        "same risk allocation"
    )


# ==========================================================================
# 8. D9 — the two short exposure caps, hard blocks, never on a COVER
# ==========================================================================

def test_single_short_cap_hard_blocks_opening_too_large_a_short():
    engine = RiskRuleEngine(_cfg(max_single_short_pct=10.0))
    decision = TradeDecision(
        action="SHORT", symbol="XYZ", allocation_pct=15.0,  # 15% > 10% cap
        entry_price=100.0, stop_loss=110.0, take_profit=80.0, reasoning="t",
    )
    violations = engine.check(
        decision=decision, positions=[], total_value=100_000, daily_pnl=0.0,
    )
    rules = {v.rule for v in violations}
    assert "max_single_short_pct" in rules


def test_gross_short_cap_hard_blocks_a_second_short_pushing_the_book_over():
    engine = RiskRuleEngine(_cfg(max_gross_bearish_pct=20.0, max_single_short_pct=10.0))
    positions = [
        _pos("AAA", qty=-90, entry=100, price=100),   # -$9,000 = 9%
        _pos("BBB", qty=-95, entry=100, price=100),   # -$9,500 = 9.5%
    ]
    decision = TradeDecision(
        action="SHORT", symbol="CCC", allocation_pct=3.0,  # +3% -> 21.5% total
        entry_price=100.0, stop_loss=110.0, take_profit=80.0, reasoning="t",
    )
    violations = engine.check(
        decision=decision, positions=positions, total_value=100_000, daily_pnl=0.0,
    )
    rules = {v.rule for v in violations}
    assert "max_gross_bearish_pct" in rules
    assert "max_single_short_pct" not in rules, (
        "this decision alone (3%) must not trip the single-short cap — only "
        "the book-wide gross cap should fire"
    )


def test_neither_short_cap_blocks_a_cover():
    """D9 explicitly: neither cap may ever block a COVER, even reducing a
    position that is ALREADY over both ceilings."""
    engine = RiskRuleEngine(_cfg(max_single_short_pct=10.0, max_gross_bearish_pct=20.0))
    positions = [_pos("XYZ", qty=-250, entry=100, price=100)]  # -$25,000 = 25%
    decision = TradeDecision(
        action="COVER", symbol="XYZ", allocation_pct=50.0,
        entry_price=0.0, stop_loss=0.0, take_profit=0.0, reasoning="reduce",
    )
    violations = engine.check(
        decision=decision, positions=positions, total_value=100_000, daily_pnl=0.0,
    )
    assert violations == []


# ==========================================================================
# 9. D10 — a cover can never be blocked by the cash rule
# ==========================================================================

def test_cover_not_blocked_by_negative_cash_when_margin_disallowed():
    engine = RiskRuleEngine(_cfg(allow_margin=False))
    decision = TradeDecision(
        action="COVER", symbol="XYZ", allocation_pct=100.0,
        entry_price=0.0, stop_loss=0.0, take_profit=0.0, reasoning="close",
    )
    violations = engine.check(
        decision=decision, positions=[_pos("XYZ", qty=-40, entry=100, price=100)],
        total_value=100_000, daily_pnl=0.0, cash=-5_000.0,
    )
    assert violations == []


def test_buy_is_still_blocked_by_negative_cash_when_margin_disallowed():
    """Contrast case, proving the COVER exemption is real and not just an
    engine-wide bypass: an ordinary BUY under the same negative-cash
    conditions is still hard-blocked exactly as before."""
    engine = RiskRuleEngine(_cfg(allow_margin=False))
    decision = TradeDecision(
        action="BUY", symbol="XYZ", allocation_pct=10.0,
        entry_price=100.0, stop_loss=95.0, take_profit=120.0, reasoning="t",
    )
    violations = engine.check(
        decision=decision, positions=[], total_value=100_000, daily_pnl=0.0,
        cash=-5_000.0,
    )
    rules = {v.rule for v in violations}
    assert "cash_only" in rules


# ==========================================================================
# 10. Long-only regression proof
# ==========================================================================

def test_constructor_long_only_output_unchanged_with_no_shorts_anywhere():
    """No-op proof, literal-for-literal — same idiom as
    tests/test_shorts_countable.py's `*_long_only_unchanged` tests. A
    book with no `direction='short'` target and no short position anywhere
    produces exactly the pre-Stage-3 BUY."""
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[TargetPosition(symbol="NVDA", target_weight_pct=15.0,
                                conviction="high", thesis="add")],
        positions=[_pos("NVDA", qty=50, entry=100, price=100)],  # 5% held
        analyses=[_long_analysis(symbol="NVDA", entry=100.0, stop=95.0,
                                  target=115.0)],
        total_value=100_000, price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.action == "BUY"
    assert d.symbol == "NVDA"
    assert d.allocation_pct == 10.0    # 15% target - 5% held
    assert d.entry_price == 100.0
    assert d.stop_loss == 95.0
    assert d.take_profit == 115.0


def test_risk_engine_long_only_output_unchanged_with_no_shorts_anywhere():
    """No-op proof for RiskRuleEngine.check(): a plain BUY against a
    long-only book, run through the exact same `is_short=False` path the
    original single-position-size rule always used, produces the same
    violation it always did."""
    engine = RiskRuleEngine(_cfg(max_position_pct=20.0))
    decision = TradeDecision(
        action="BUY", symbol="XYZ", allocation_pct=25.0,  # 25% > 20% cap
        entry_price=100.0, stop_loss=95.0, take_profit=120.0, reasoning="t",
    )
    violations = engine.check(
        decision=decision, positions=[], total_value=100_000, daily_pnl=0.0,
    )
    assert len(violations) == 1
    v = violations[0]
    assert v.rule == "max_position_pct"
    assert v.value == 25.0
    assert v.limit == 20.0

    # And the clean case: well under every cap, zero violations.
    clean = TradeDecision(
        action="BUY", symbol="XYZ", allocation_pct=5.0,
        entry_price=100.0, stop_loss=95.0, take_profit=120.0, reasoning="t",
    )
    assert engine.check(
        decision=clean, positions=[], total_value=100_000, daily_pnl=0.0,
    ) == []


# ==========================================================================
# 11. Gap fix — an emergency close cancels a resting entry order on EITHER
#    side. Previously EMERGENCY_COVER (closing a short) left a resting
#    SELL-to-open entry order untouched — a fill on it would re-open the
#    exact short exposure the emergency close just cleared. The mechanism
#    itself (order-type-based filtering in AlpacaBroker.cancel_open_entry_
#    orders) is unit-tested in tests/test_broker.py; these two prove the
#    pipeline actually invokes it identically on both sides.
# ==========================================================================

def test_emergency_cover_cancels_the_symbols_resting_short_entry_order():
    """EMERGENCY_COVER must cancel that symbol's own resting entry order
    exactly as EMERGENCY_SELL does for a long."""
    p = TradingPipeline.__new__(TradingPipeline)
    p.broker = MagicMock()
    p.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    p._cancel_stops_with_write_ahead = MagicMock(return_value=(True, [], 7))
    p.db = MagicMock()

    p._submit_protected_sell(
        symbol="TSLA", qty=40, limit_price=252.5, reference_price=250.0,
        position_qty_before_sell=40, label="EMERGENCY_COVER", side="buy",
    )
    p.broker.cancel_open_entry_orders.assert_called_once_with(symbol="TSLA")


def test_emergency_sell_still_cancels_the_symbols_resting_long_entry_order():
    """Long-side regression proof — literal mirror of the test above. The
    pre-existing EMERGENCY_SELL behaviour is unaffected by the short-side
    fix (same assertion shape as tests/test_pipeline.py's
    test_full_exit_sell_cancels_same_symbol_entry_orders)."""
    p = TradingPipeline.__new__(TradingPipeline)
    p.broker = MagicMock()
    p.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    p._cancel_stops_with_write_ahead = MagicMock(return_value=(True, [], 7))
    p.db = MagicMock()

    p._submit_protected_sell(
        symbol="VST", qty=31, limit_price=150.0, reference_price=151.0,
        position_qty_before_sell=31, label="EMERGENCY_SELL",
    )
    p.broker.cancel_open_entry_orders.assert_called_once_with(symbol="VST")


# ==========================================================================
# 12. Gap fix — the midday/close reviewer can COVER a short
# ==========================================================================

def _mk_review_with_action(symbol: str, action: str, reason: str):
    """Minimal review-shaped object _midday_execute_llm_actions accepts —
    same helper shape as tests/test_position_reviewer.py's
    _mk_review_with_action, kept local so this file stands alone."""
    return MagicMock(actions=[PositionAction(
        action=action, symbol=symbol, reason=reason,
    )])


def _midday_pipeline_with_short(symbol: str, qty: float, current_price: float):
    """Pipeline scaffold sufficient to exercise _midday_execute_llm_actions
    on a single SHORT position. Mirrors tests/test_position_reviewer.py's
    _executor_pipeline_with_position, kept local for the same reason."""
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.broker = MagicMock()
    pipeline.broker.snapshot_protective_stops.return_value = (True, [])
    pipeline.broker.cancel_snapshotted_stops.return_value = True
    pipeline.broker.cancel_protective_stops.return_value = (True, [])
    pipeline.broker.submit_order.return_value = {
        "id": "cover-order", "status": "accepted", "symbol": symbol,
    }
    pipeline.broker.get_latest_price.return_value = current_price
    pipeline.broker.wait_for_order_terminal.return_value = "filled"
    pipeline.broker.get_order_fill_info.return_value = {
        "status": "filled", "filled_qty": str(qty),
        "filled_avg_price": str(current_price),
    }
    pipeline.db = MagicMock()
    pipeline.db.has_pending_action_for_symbol.return_value = False
    pipeline._order_accepted = MagicMock(return_value=True)
    pipeline._reprotect_residual_after_partial_sell = MagicMock()
    pipeline._format_qty = lambda q: str(q)
    return pipeline


def test_midday_reviewer_covers_a_short_end_to_end():
    """A held SHORT with a COVER action naming a hard trigger executes as a
    BUY-to-cover for the full absolute qty — the primary Gap 2 proof."""
    position = _pos("TSLA", qty=-40, entry=250.0, price=240.0)
    pipeline = _midday_pipeline_with_short("TSLA", 40.0, 240.0)
    review = _mk_review_with_action(
        "TSLA", "COVER",
        "thesis_invalid_if condition satisfied — guidance cut reversed the "
        "setup, bullish reversal confirmed above the defended level.",
    )

    orders = pipeline._midday_execute_llm_actions([position], review, run_id="r1")

    assert len(orders) == 1
    pipeline.broker.submit_order.assert_called_once()
    submit_kwargs = pipeline.broker.submit_order.call_args.kwargs
    assert submit_kwargs["side"] == "buy"
    assert submit_kwargs["qty"] == 40.0
    assert submit_kwargs["symbol"] == "TSLA"
    # Buy-to-cover limit sits ABOVE the reference (mirror of SELL's below).
    assert submit_kwargs["limit_price"] > 240.0


def test_midday_cover_refused_without_named_trigger():
    """A COVER whose reason names no recognised trigger is refused exactly
    as a SELL would be — the phrase gate applies identically."""
    position = _pos("TSLA", qty=-40, entry=250.0, price=240.0)
    pipeline = _midday_pipeline_with_short("TSLA", 40.0, 240.0)
    review = _mk_review_with_action(
        "TSLA", "COVER", "price fell a lot, prudent to lock in the gain.",
    )

    orders = pipeline._midday_execute_llm_actions([position], review, run_id="r1")

    assert orders == []
    pipeline.broker.submit_order.assert_not_called()


def test_midday_cover_not_blocked_by_negative_cash():
    """D10 mirrored at the executor: _midday_execute_llm_actions carries no
    cash or exposure gate on ANY exit action, COVER included — a closing
    action must never be blockable by the cash rule (being unable to close
    is strictly worse than being unable to open). Proven here by executing
    a COVER successfully with the pipeline's account state showing negative
    cash and margin disallowed; the executor doesn't even look at it."""
    position = _pos("TSLA", qty=-40, entry=250.0, price=240.0)
    pipeline = _midday_pipeline_with_short("TSLA", 40.0, 240.0)
    pipeline.config = MagicMock()
    pipeline.config.risk.allow_margin = False
    pipeline.cash = -5_000.0  # not read anywhere on this path — that's the point
    review = _mk_review_with_action(
        "TSLA", "COVER",
        "thesis_invalid_if condition satisfied — guidance cut reversed the setup.",
    )

    orders = pipeline._midday_execute_llm_actions([position], review, run_id="r1")

    assert len(orders) == 1
    pipeline.broker.submit_order.assert_called_once()


# ==========================================================================
# 13. Gap fix — a short position reaching the reviewer carries its side
# ==========================================================================

def test_short_position_reaches_reviewer_payload_with_its_side():
    """The reviewer payload must state a held short's side explicitly so it
    cannot read a winning short as a loser. Confirms the fix already lives
    in PositionReviewerAgent.build_user_message and actually reaches the
    reviewer path this change touches (see also the fuller pnl-sign proof
    in tests/test_position_reviewer.py)."""
    from unittest.mock import patch as _patch
    from src.agents.position_reviewer import PositionReviewerAgent

    with _patch("anthropic.Anthropic"):
        agent = PositionReviewerAgent(api_key="test", model="claude-sonnet-4-6")
        msg = agent.build_user_message(
            session_type="midday",
            positions=[_pos("TSLA", qty=-40, entry=250.0, price=240.0)],
            macro_summary={"vix": {"current": 18.0}},
            cash_balance=1_000.0,
            total_value=100_000.0,
        )

    assert "[SHORT]" in msg


# ==========================================================================
# 14. Long-only regression proof — both gap fixes leave a pure-long book
#    behaving exactly as before
# ==========================================================================

def test_long_only_midday_actions_unchanged_with_no_shorts_anywhere():
    """No COVER, no short position anywhere: SELL still executes exactly as
    pre-Stage-3 (side='sell', full qty, limit 0.5% below reference)."""
    position = _pos("VST", qty=31, entry=100.0, price=150.0)
    pipeline = _midday_pipeline_with_short("VST", 31.0, 150.0)
    review = _mk_review_with_action(
        "VST", "SELL",
        "thesis_invalid_if condition satisfied — thesis broken on filing.",
    )

    orders = pipeline._midday_execute_llm_actions([position], review, run_id="r1")

    assert len(orders) == 1
    submit_kwargs = pipeline.broker.submit_order.call_args.kwargs
    assert submit_kwargs["side"] == "sell"
    assert submit_kwargs["qty"] == 31.0
    assert submit_kwargs["limit_price"] == 149.25  # 150 * 0.995


def test_cancel_open_entry_orders_long_only_book_cancels_only_the_buy():
    """Long-only regression proof for the broker-level fix: a book with no
    short-side orders at all cancels exactly what it always did (see
    tests/test_broker.py's fuller mixed-book proof for the mechanism)."""
    p = TradingPipeline.__new__(TradingPipeline)
    p.broker = MagicMock()
    p.broker.submit_order.return_value = {"id": "o1", "status": "accepted"}
    p._cancel_stops_with_write_ahead = MagicMock(return_value=(True, [], 7))
    p.db = MagicMock()

    p._submit_protected_sell(
        symbol="VST", qty=31, limit_price=150.0, reference_price=151.0,
        position_qty_before_sell=31, label="SELL",
    )
    p.broker.cancel_open_entry_orders.assert_called_once_with(symbol="VST")
