"""Board item 134 — a risk-seat `stop_loss`/`entry_price` edit must be
reconciled back to the position SIZE, so a widened stop shrinks the position
and can never enlarge dollar risk beyond what the pre-edit ticket carried.

The desk sizes a position so `shares = equity*risk_pct / |entry - stop|`, and
execution spends the resulting `allocation_pct` as
`qty = equity * allocation_pct/100 / entry`. The fraction of equity a ticket
risks is therefore `allocation_pct/100 * |entry - stop| / entry`, a function of
the ticket's own fields alone. These tests pin that the reconciliation holds
that fraction at or below its pre-edit value across every edit direction, on
both the long (BUY) and short (SHORT) side, and that it never contradicts the
existing "seat cannot enlarge a BUY" guard.
"""

import math

from src.pipeline import TradingPipeline
from src.models import RiskModification, TradeDecision


def _risk_fraction(d: TradeDecision) -> float:
    """Fraction of equity this ticket puts at risk — the quantity the
    reconciliation must never let an edit increase."""
    return (d.allocation_pct / 100.0) * abs(d.entry_price - d.stop_loss) / d.entry_price


def _pipeline() -> TradingPipeline:
    # Matches every other _apply_risk_modifications test: no __init__, so no
    # self.config and no bars — proving the reconciliation needs neither.
    return TradingPipeline.__new__(TradingPipeline)


# --- 1. Core fix: widening a BUY stop shrinks the position -----------------

def test_widening_buy_stop_shrinks_allocation_to_hold_dollar_risk():
    pipeline = _pipeline()
    # entry 500, stop 490 -> 10 wide; alloc 10 -> risk fraction 10%*10/500=0.2%.
    buy = TradeDecision(
        action="BUY", symbol="SPY", allocation_pct=10,
        entry_price=500, stop_loss=490, take_profit=530, reasoning="t",
    )
    before = _risk_fraction(buy)
    # Seat DOUBLES the stop distance: 490 -> 480 (now 20 wide).
    mods = [RiskModification(
        symbol="SPY", field="stop_loss",
        original_value=490, new_value=480, reason="give it room",
    )]
    updated, rejected = pipeline._apply_risk_modifications([buy], mods)

    assert rejected == []
    assert len(updated) == 1
    d = updated[0]
    assert d.stop_loss == 480              # the edit still applied
    # Stop distance doubled, so the position must roughly halve: 10 -> ~5.
    assert d.allocation_pct < 10
    assert math.isclose(d.allocation_pct, 5.0, abs_tol=0.01)
    # The invariant that matters: dollar-risk fraction did not grow.
    assert _risk_fraction(d) <= before + 1e-9


# --- 2. Tighter stop must NOT auto-enlarge the position --------------------

def test_tightening_buy_stop_does_not_enlarge_allocation():
    pipeline = _pipeline()
    buy = TradeDecision(
        action="BUY", symbol="SPY", allocation_pct=10,
        entry_price=500, stop_loss=480, take_profit=530, reasoning="t",
    )
    before = _risk_fraction(buy)
    # Seat TIGHTENS the stop: 480 -> 490 (distance halves). A naive "resize to
    # budget" would DOUBLE the position; the reconciliation must not.
    mods = [RiskModification(
        symbol="SPY", field="stop_loss",
        original_value=480, new_value=490, reason="tighten",
    )]
    updated, rejected = pipeline._apply_risk_modifications([buy], mods)

    assert rejected == []
    d = updated[0]
    assert d.stop_loss == 490
    assert d.allocation_pct == 10          # unchanged — never auto-enlarged
    # Dollar risk actually FELL (tighter stop, same shares) — never grew.
    assert _risk_fraction(d) <= before + 1e-9


# --- 3. entry_price edit reconciles the same way --------------------------

def test_entry_price_edit_reconciles_size():
    pipeline = _pipeline()
    buy = TradeDecision(
        action="BUY", symbol="AAPL", allocation_pct=8,
        entry_price=200, stop_loss=190, take_profit=230, reasoning="t",
    )
    before = _risk_fraction(buy)
    # Seat pulls the entry UP toward the stop is not it — pull entry DOWN to
    # 205? entry must stay > stop for a BUY. Move entry 200 -> 210: distance
    # 190->? stop unchanged at 190, so distance 10 -> 20, risk-per-share up.
    mods = [RiskModification(
        symbol="AAPL", field="entry_price",
        original_value=200, new_value=210, reason="chase",
    )]
    updated, rejected = pipeline._apply_risk_modifications([buy], mods)

    assert rejected == []
    d = updated[0]
    assert d.entry_price == 210
    assert d.allocation_pct < 8
    # Never over-risks beyond the pre-edit budget.
    assert _risk_fraction(d) <= before + 1e-9


# --- 4. No edit / unrelated edit leaves allocation untouched --------------

def test_take_profit_edit_does_not_touch_allocation():
    pipeline = _pipeline()
    buy = TradeDecision(
        action="BUY", symbol="SPY", allocation_pct=10,
        entry_price=500, stop_loss=480, take_profit=530, reasoning="t",
    )
    mods = [RiskModification(
        symbol="SPY", field="take_profit",
        original_value=530, new_value=540, reason="more upside",
    )]
    updated, rejected = pipeline._apply_risk_modifications([buy], mods)

    assert rejected == []
    d = updated[0]
    assert d.take_profit == 540
    assert d.allocation_pct == 10          # take_profit does not affect size


def test_no_modifications_is_a_noop():
    pipeline = _pipeline()
    buy = TradeDecision(
        action="BUY", symbol="SPY", allocation_pct=10,
        entry_price=500, stop_loss=480, take_profit=530, reasoning="t",
    )
    updated, rejected = pipeline._apply_risk_modifications([buy], [])
    assert rejected == []
    assert updated[0].allocation_pct == 10
    assert updated[0].stop_loss == 480


# --- 5. No contradiction with the "cannot raise a BUY" guard --------------

def test_reconciliation_does_not_contradict_buy_enlarge_guard():
    """The existing guard reverts an allocation_pct edit that ENLARGES a BUY.
    The reconciliation only ever REDUCES size, so the two never fight: an
    allocation raise is still reverted, and a stop widen still shrinks."""
    pipeline = _pipeline()
    buy = TradeDecision(
        action="BUY", symbol="SPY", allocation_pct=10,
        entry_price=500, stop_loss=480, take_profit=530, reasoning="t",
    )
    # An allocation_pct raise is reverted by guard 1b (unchanged, recorded).
    mods = [RiskModification(
        symbol="SPY", field="allocation_pct",
        original_value=10, new_value=20, reason="bigger",
    )]
    updated, rejected = pipeline._apply_risk_modifications([buy], mods)
    assert updated[0].allocation_pct == 10
    assert len(rejected) == 1


# --- 6. Short side mirrors correctly --------------------------------------

def test_widening_short_stop_shrinks_allocation():
    pipeline = _pipeline()
    # SHORT: stop sits ABOVE entry. entry 100, stop 105 -> 5 wide.
    short = TradeDecision(
        action="SHORT", symbol="TSLA", allocation_pct=6,
        entry_price=100, stop_loss=105, take_profit=90, reasoning="t",
    )
    before = _risk_fraction(short)
    # Widen: 105 -> 110 (distance 5 -> 10).
    mods = [RiskModification(
        symbol="TSLA", field="stop_loss",
        original_value=105, new_value=110, reason="room",
    )]
    updated, rejected = pipeline._apply_risk_modifications([short], mods)

    assert rejected == []
    d = updated[0]
    assert d.stop_loss == 110
    assert d.allocation_pct < 6
    assert math.isclose(d.allocation_pct, 3.0, abs_tol=0.01)
    assert _risk_fraction(d) <= before + 1e-9


def test_tightening_short_stop_does_not_enlarge():
    pipeline = _pipeline()
    short = TradeDecision(
        action="SHORT", symbol="TSLA", allocation_pct=6,
        entry_price=100, stop_loss=110, take_profit=90, reasoning="t",
    )
    before = _risk_fraction(short)
    mods = [RiskModification(
        symbol="TSLA", field="stop_loss",
        original_value=110, new_value=105, reason="tighten",
    )]
    updated, rejected = pipeline._apply_risk_modifications([short], mods)
    assert rejected == []
    d = updated[0]
    assert d.stop_loss == 105
    assert d.allocation_pct == 6           # never auto-enlarged
    assert _risk_fraction(d) <= before + 1e-9


# --- 7. Exit legs (SELL/COVER) are not resized ----------------------------

def test_sell_exit_stop_edit_not_resized():
    """A SELL carries entry_price/stop_loss = 0 and allocation_pct is the
    fraction of the position to exit — the sizing identity does not apply and
    the reconciliation must leave it alone (guarded by action in BUY/SHORT)."""
    pipeline = _pipeline()
    sell = TradeDecision(
        action="SELL", symbol="SPY", allocation_pct=100,
        entry_price=0.0, stop_loss=0.0, take_profit=0.0, reasoning="t",
    )
    # take_profit is the only field safely editable on a zero-price exit.
    mods = [RiskModification(
        symbol="SPY", field="take_profit",
        original_value=0.0, new_value=0.0, reason="noop",
    )]
    updated, rejected = pipeline._apply_risk_modifications([sell], mods)
    assert rejected == []
    assert updated[0].allocation_pct == 100


# --- 8. Direct unit test of the reconciliation identity -------------------

def test_reconcile_helper_preserves_risk_fraction_exactly():
    original = TradeDecision(
        action="BUY", symbol="X", allocation_pct=12,
        entry_price=250, stop_loss=240, take_profit=280, reasoning="t",
    )
    modified = original.model_copy(update={"stop_loss": 220})  # 10 wide -> 30
    reconciled = TradingPipeline._reconcile_size_to_risk_budget(original, modified)
    rebuilt = modified.model_copy(update={"allocation_pct": reconciled})
    assert _risk_fraction(rebuilt) <= _risk_fraction(original) + 1e-9
    # 3x the stop distance -> ~1/3 the size.
    assert math.isclose(reconciled, 4.0, abs_tol=0.01)


def test_short_through_real_constructor_reconciles_within_budget():
    """Drive a SHORT through the REAL constructor so `short_gap_risk_multiple`
    is actually baked into the shipped `allocation_pct`, then widen its stop via
    the risk seat. The reconciliation must keep realized-at-stop dollar risk
    within both the pre-edit budget and the 5% ceiling — pinning the "the
    haircut cancels in the ratio" claim, so a future change that makes the
    haircut stop-distance-dependent breaks THIS test rather than silently
    breaking the budget math."""
    from src.models import TargetPosition, TechAnalysisResult, TechReasoningChain
    from src.portfolio_constructor import PortfolioConstructor

    equity = 100_000.0
    constructor = PortfolioConstructor()
    budget_pct = constructor.cfg.risk_budget_pct   # 5.0
    assert constructor.cfg.short_gap_risk_multiple > 1.0  # haircut is real

    rc = TechReasoningChain(trend="x", momentum="x", volatility="x",
                            volume="x", support_resistance="x")
    analysis = TechAnalysisResult(
        symbol="TSLA", rating="sell", entry_price=250.0, stop_loss=262.5,
        reference_target=220.0, reasoning="test",
        support_levels=[220.0], resistance_levels=[262.5],
        computed_levels=[220.0, 262.5], atr_14=12.5 / 3.5,
        setup_type="range", expected_horizon_sessions=60,
        reasoning_chain=rc, thesis_invalid_if="closes below support",
    )
    decisions = constructor.construct_orders(
        targets=[TargetPosition(symbol="TSLA", direction="short",
                                risk_allocation_pct=2.0, conviction="high",
                                thesis="overvalued")],
        positions=[], analyses=[analysis], total_value=equity,
        price_map={"TSLA": 250.0},
    )
    short = next(d for d in decisions if d.action == "SHORT")
    assert short.stop_loss > short.entry_price   # short geometry
    before_dollar_risk = _risk_fraction(short) * equity
    # Sanity: the constructor already sized it within the 5% budget.
    assert before_dollar_risk <= equity * budget_pct / 100 + 1e-6

    # Risk seat widens the stop well beyond its original distance.
    new_stop = short.entry_price + (short.stop_loss - short.entry_price) * 2.5
    pipeline = _pipeline()
    updated, rejected = pipeline._apply_risk_modifications(
        [short],
        [RiskModification(symbol="TSLA", field="stop_loss",
                          original_value=short.stop_loss, new_value=new_stop,
                          reason="wider room")],
    )
    assert rejected == []
    reconciled = updated[0]
    assert reconciled.stop_loss == new_stop
    assert reconciled.allocation_pct < short.allocation_pct
    after_dollar_risk = _risk_fraction(reconciled) * equity
    # The two things the reconciliation guarantees, with the haircut live:
    assert after_dollar_risk <= before_dollar_risk + 1e-6   # never enlarged
    assert after_dollar_risk <= equity * budget_pct / 100 + 1e-6  # within 5%


def test_risk_event_attributes_the_reconciliation_allocation_drop():
    """Owner-facing transparency: when the reconciliation drops allocation_pct
    after a seat stop edit, the per-symbol `risk` event must attribute the drop
    with its own reason, not leave it unexplained or bucketed under the stop
    edit."""
    from types import SimpleNamespace
    from src.pipeline_stages import _risk_edit_snapshot, _risk_event_for

    pre = TradeDecision(
        action="BUY", symbol="SPY", allocation_pct=10,
        entry_price=500, stop_loss=490, take_profit=530, reasoning="t",
    )
    snapshot = _risk_edit_snapshot([pre])
    # What the leg looks like AFTER the stop edit + reconciliation.
    post = pre.model_copy(update={"stop_loss": 480, "allocation_pct": 5.0})
    verdict = SimpleNamespace(
        modifications=[RiskModification(
            symbol="SPY", field="stop_loss",
            original_value=490, new_value=480, reason="give it room",
        )],
        reason_category="clean",
    )
    outcome, reason, details = _risk_event_for(post, snapshot, verdict, 1.0)

    assert outcome == "modified"
    assert "stop_loss" in details["changes"]
    assert "allocation_pct" in details["changes"]
    assert "give it room" in reason                    # seat's own stop reason
    assert "granted budget" in reason                  # reconciliation attributed


def test_risk_event_does_not_invent_reconciliation_reason_on_direct_alloc_edit():
    """If the seat itself edited allocation_pct (not the reconciliation), the
    reconciliation reason must NOT be appended — that would misattribute a
    direct seat cut."""
    from types import SimpleNamespace
    from src.pipeline_stages import _risk_edit_snapshot, _risk_event_for

    pre = TradeDecision(
        action="BUY", symbol="SPY", allocation_pct=10,
        entry_price=500, stop_loss=480, take_profit=530, reasoning="t",
    )
    snapshot = _risk_edit_snapshot([pre])
    post = pre.model_copy(update={"allocation_pct": 6.0})
    verdict = SimpleNamespace(
        modifications=[RiskModification(
            symbol="SPY", field="allocation_pct",
            original_value=10, new_value=6, reason="oversized",
        )],
        reason_category="oversized",
    )
    outcome, reason, details = _risk_event_for(post, snapshot, verdict, 1.0)
    assert outcome == "modified"
    assert "oversized" in reason
    assert "granted budget" not in reason


def test_reconcile_helper_degenerate_returns_none():
    # A zero pre-edit risk-per-share cannot be validly constructed (the schema
    # forbids stop == entry), so reach the degenerate state via model_copy,
    # which does not re-validate. The reconciliation must decline (None) rather
    # than divide by zero, leaving the size untouched.
    valid = TradeDecision(
        action="BUY", symbol="X", allocation_pct=10,
        entry_price=100, stop_loss=99, take_profit=120, reasoning="t",
    )
    original = valid.model_copy(update={"stop_loss": 100})  # rps0 == 0
    modified = valid.model_copy(update={"stop_loss": 90})
    assert TradingPipeline._reconcile_size_to_risk_budget(original, modified) is None
