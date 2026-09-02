import pytest
from src.risk.rules import RiskRuleEngine, RiskViolation
from src.models import TradeDecision, Position
from src.config import RiskConfig


@pytest.fixture
def risk_config():
    return RiskConfig(
        max_position_pct=20,
        max_total_position_pct=90,
        max_daily_loss_pct=3,
        max_sector_pct=40,
        require_stop_loss=True,
    )


@pytest.fixture
def engine(risk_config):
    return RiskRuleEngine(risk_config)


def test_position_size_within_limit(engine):
    decision = TradeDecision(
        action="BUY", symbol="SPY", allocation_pct=15.0,
        entry_price=500.0, stop_loss=485.0, take_profit=530.0,
        reasoning="Test",
    )
    violations = engine.check(decision, positions=[], total_value=10000.0, daily_pnl=0.0)
    assert len(violations) == 0


def test_position_size_exceeds_limit(engine):
    decision = TradeDecision(
        action="BUY", symbol="SPY", allocation_pct=25.0,
        entry_price=500.0, stop_loss=485.0, take_profit=530.0,
        reasoning="Test",
    )
    violations = engine.check(decision, positions=[], total_value=10000.0, daily_pnl=0.0)
    assert any(v.rule == "max_position_pct" for v in violations)


def test_existing_position_plus_new_buy_exceeds_limit(engine):
    positions = [
        Position(
            symbol="SPY",
            qty=3,
            avg_entry=500.0,
            current_price=500.0,
            market_value=1500.0,
            unrealized_pnl=0.0,
            sector="ETF",
        )
    ]
    decision = TradeDecision(
        action="BUY", symbol="SPY", allocation_pct=10.0,
        entry_price=500.0, stop_loss=485.0, take_profit=530.0,
        reasoning="Add to winner",
    )

    violations = engine.check(decision, positions=positions, total_value=10000.0, daily_pnl=0.0)
    assert any(v.rule == "max_position_pct" for v in violations)


def test_pending_same_symbol_buy_exceeds_limit(engine):
    decision = TradeDecision(
        action="BUY", symbol="SPY", allocation_pct=15.0,
        entry_price=500.0, stop_loss=485.0, take_profit=530.0,
        reasoning="Second leg",
    )

    violations = engine.check(
        decision,
        positions=[],
        total_value=10000.0,
        daily_pnl=0.0,
        pending_symbol_investment={"SPY": 1500.0},
    )
    assert any(v.rule == "max_position_pct" for v in violations)


def test_total_exposure_exceeds_limit(engine):
    positions = [
        Position(symbol="AAPL", qty=10, avg_entry=180.0, current_price=190.0,
                 market_value=1900.0, unrealized_pnl=100.0, sector="Technology"),
        Position(symbol="MSFT", qty=10, avg_entry=400.0, current_price=410.0,
                 market_value=4100.0, unrealized_pnl=100.0, sector="Technology"),
        Position(symbol="GOOGL", qty=5, avg_entry=170.0, current_price=175.0,
                 market_value=875.0, unrealized_pnl=25.0, sector="Technology"),
    ]
    decision = TradeDecision(
        action="BUY", symbol="NVDA", allocation_pct=25.0,
        entry_price=850.0, stop_loss=810.0, take_profit=920.0,
        reasoning="Test",
    )
    violations = engine.check(decision, positions=positions, total_value=10000.0, daily_pnl=0.0)
    assert any(v.rule == "max_total_position_pct" for v in violations)


def test_daily_loss_limit(engine):
    decision = TradeDecision(
        action="BUY", symbol="SPY", allocation_pct=10.0,
        entry_price=500.0, stop_loss=485.0, take_profit=530.0,
        reasoning="Test",
    )
    violations = engine.check(decision, positions=[], total_value=10000.0, daily_pnl=-350.0)
    assert any(v.rule == "max_daily_loss_pct" for v in violations)


def test_check_daily_loss_rule_handles_nan_daily_pnl(engine, caplog):
    """Per-BUY daily-loss rule mirrors the standalone check_daily_loss
    NaN guard. A non-finite daily_pnl (Alpaca portfolio_value glitches
    propagate into total_value - last_equity) used to make
    `abs(NaN / baseline * 100) > limit` evaluate False, silently
    disabling rule 3 INSIDE the per-BUY pipeline path while the
    standalone breaker remained protected. Audit 2026-05-27.

    Contract: NaN does not violate (we don't know the actual loss), and
    the engine does not crash. force_delever + check_daily_loss handle
    the disabling-on-NaN class of failure elsewhere."""
    import logging
    decision = TradeDecision(
        action="BUY", symbol="SPY", allocation_pct=10.0,
        entry_price=500.0, stop_loss=485.0, take_profit=530.0,
        reasoning="Test",
    )
    caplog.set_level(logging.WARNING, logger="src.risk.rules")
    violations = engine.check(
        decision, positions=[], total_value=10000.0, daily_pnl=float("nan"),
    )
    # rule 3 must NOT spuriously violate on NaN (it's not "we crossed
    # the loss limit" — it's "we don't know").
    assert not any(v.rule == "max_daily_loss_pct" for v in violations)
    # And the bypass must be logged so the operator sees it.
    assert any(
        "non-finite" in r.getMessage() and "SPY" in r.getMessage()
        for r in caplog.records
    ), "expected a WARNING about non-finite daily_pnl"


def test_no_stop_loss(engine):
    decision = TradeDecision(
        action="BUY", symbol="SPY", allocation_pct=10.0,
        entry_price=500.0, stop_loss=0.0, take_profit=530.0,
        reasoning="Test",
    )
    violations = engine.check(decision, positions=[], total_value=10000.0, daily_pnl=0.0)
    assert any(v.rule == "require_stop_loss" for v in violations)


def test_sector_concentration(engine):
    positions = [
        Position(symbol="AAPL", qty=10, avg_entry=180.0, current_price=190.0,
                 market_value=1900.0, unrealized_pnl=100.0, sector="Technology"),
        Position(symbol="MSFT", qty=5, avg_entry=400.0, current_price=410.0,
                 market_value=2050.0, unrealized_pnl=50.0, sector="Technology"),
    ]
    decision = TradeDecision(
        action="BUY", symbol="NVDA", allocation_pct=15.0,
        entry_price=850.0, stop_loss=810.0, take_profit=920.0,
        reasoning="Test",
    )
    # Sector is now auto-detected from _get_sector(symbol)
    from unittest.mock import patch
    with patch("src.execution.broker._get_sector", return_value="Technology"):
        violations = engine.check(
            decision, positions=positions, total_value=10000.0, daily_pnl=0.0,
        )
    assert any(v.rule == "max_sector_pct" for v in violations)


def test_sell_decision_skips_buy_rules(engine):
    decision = TradeDecision(
        action="SELL", symbol="SPY", allocation_pct=0,
        entry_price=0, stop_loss=0, take_profit=0,
        reasoning="Take profit",
    )
    violations = engine.check(decision, positions=[], total_value=10000.0, daily_pnl=0.0)
    assert len(violations) == 0


# ===========================================================================
# NaN-guard tests — check_daily_loss must NOT silently disable on NaN
# ===========================================================================

def test_check_daily_loss_nan_baseline_does_not_disable_silently(engine, caplog):
    """Alpaca has been observed to return NaN portfolio_value during
    market-open glitches; that propagates to last_equity → baseline.
    Pre-fix: `NaN <= 0` is False → falls through → `abs(NaN/NaN*100)`
    is NaN → `NaN > limit` is False → no violation → circuit breaker
    silently disabled on exactly the kind of broken-snapshot day where
    it's most valuable.

    Fix: NaN baseline returns None (same as the "no signal" path) but
    LOGS a warning so the operator can see the breaker was bypassed,
    AND force_delever downstream catches the actual cash deficit.
    """
    import logging
    import math
    with caplog.at_level(logging.WARNING):
        v = engine.check_daily_loss(baseline=float("nan"), daily_pnl=-100.0)
    assert v is None
    assert any(
        "non-finite" in r.message and "baseline" in r.message
        for r in caplog.records
    ), "non-finite baseline must log a warning so the bypass is visible"


def test_check_daily_loss_nan_daily_pnl_does_not_disable_silently(engine, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        v = engine.check_daily_loss(baseline=10000.0, daily_pnl=float("nan"))
    assert v is None
    assert any(
        "non-finite" in r.message and "daily_pnl" in r.message
        for r in caplog.records
    )


def test_check_daily_loss_inf_baseline_treated_as_non_finite(engine):
    """Defense-in-depth: +/- inf is also not a usable baseline."""
    assert engine.check_daily_loss(baseline=float("inf"), daily_pnl=-100.0) is None
    assert engine.check_daily_loss(baseline=float("-inf"), daily_pnl=-100.0) is None


def test_check_daily_loss_finite_inputs_still_fire_breaker(engine):
    """Sanity: the NaN guard must not regress the legitimate breach
    detection. 4% loss with 3% cap → violation."""
    v = engine.check_daily_loss(baseline=10000.0, daily_pnl=-400.0)
    assert v is not None
    assert v.rule == "max_daily_loss_pct"


# ===========================================================================
# Zero / NaN total_value guard — must NOT silently approve BUYs
# ===========================================================================

def test_check_zero_total_value_emits_blocking_violation(engine):
    """Alpaca portfolio_value=0 during a market-open glitch must NOT be
    treated as 'all checks passed'. Pre-fix: early return `[]` had the
    same shape as the no-violations path → BUYs sailed through with
    every cap (cash_only, position, sector, daily-loss) bypassed.
    Now: synthesizes a HARD_BLOCK_RULES violation so the pipeline
    filter blocks the BUY until the next snapshot reads non-zero.
    """
    decision = TradeDecision(
        action="BUY", symbol="NVDA", allocation_pct=10.0,
        entry_price=500.0, stop_loss=485.0, take_profit=530.0,
        reasoning="Test", reasoning_chain={"setup": "x", "rr": "x", "alignment": "x", "risk": "x", "thesis": "x"} if False else "Test",
    ) if False else TradeDecision(
        action="BUY", symbol="NVDA", allocation_pct=10.0,
        entry_price=500.0, stop_loss=485.0, take_profit=530.0, reasoning="Test",
    )
    violations = engine.check(decision, positions=[], total_value=0.0, daily_pnl=0.0)
    assert len(violations) == 1
    # Must be in HARD_BLOCK_RULES so _filter_hard_risk_decisions blocks
    from src.pipeline import HARD_BLOCK_RULES
    assert violations[0].rule in HARD_BLOCK_RULES
    assert "not a valid equity" in violations[0].message


def test_check_nan_total_value_emits_blocking_violation(engine):
    """NaN total_value (also seen during market-open glitches) must
    block the BUY same as zero — pre-fix `NaN <= 0` was False, so the
    early-return guard didn't even fire, and the rest of the check
    propagated NaN comparisons that all returned False."""
    decision = TradeDecision(
        action="BUY", symbol="NVDA", allocation_pct=10.0,
        entry_price=500.0, stop_loss=485.0, take_profit=530.0, reasoning="Test",
    )
    violations = engine.check(decision, positions=[], total_value=float("nan"), daily_pnl=0.0)
    assert len(violations) == 1
    from src.pipeline import HARD_BLOCK_RULES
    assert violations[0].rule in HARD_BLOCK_RULES


def test_check_negative_total_value_emits_blocking_violation(engine):
    """Defense-in-depth: negative equity (extremely unlikely but
    possible during paper-trading reset) must also block, not bypass."""
    decision = TradeDecision(
        action="BUY", symbol="NVDA", allocation_pct=10.0,
        entry_price=500.0, stop_loss=485.0, take_profit=530.0, reasoning="Test",
    )
    violations = engine.check(decision, positions=[], total_value=-100.0, daily_pnl=0.0)
    assert len(violations) == 1


# ===========================================================================
# GUARD 3 (2026-09-02 operational safety guard) — INVESTIGATION: can a halt
# or hard-block risk rule refuse a risk-reducing order and trap a position?
#
# Answer, from the code: NO. `RiskRuleEngine.check`'s very first statement
# is `if decision.action in ("SELL", "COVER"): return []` — before ANY of
# HARD_BLOCK_RULES (max_daily_loss_pct, max_total_position_pct,
# max_position_pct, require_stop_loss, max_sector_hard_pct, cash_only,
# drawdown_buy_cap, max_single_short_pct, max_gross_bearish_pct) is even
# evaluated. These tests pin that property directly: SELL/COVER return no
# violations even when EVERY other check would fail if it ran.
#
# A trade that flips a long into a short is NOT expressible as a single
# SELL here — src/portfolio_constructor.py's `_build_sell`/`_build_cover`
# clamp the sold/covered fraction to at most the CURRENTLY HELD size
# (`alloc = max(1.0, min(99.0, ...))` or `100.0` for a full close), so the
# exemption below can never be handed a disguised flip; a flip requires a
# SEPARATE BUY/SHORT decision afterward, which is NOT exempt (see the last
# test below).
# ===========================================================================

def _breaching_positions():
    """A book that, if SELL/COVER were not exempt, would fail
    max_position_pct, max_total_position_pct AND max_sector_hard_pct at
    once: one name at 500% of a $10 book, all in one sector."""
    return [
        Position(
            symbol="MEGA", qty=500, avg_entry=100.0, current_price=100.0,
            market_value=50_000.0, unrealized_pnl=0.0, sector="Technology",
        ),
    ]


def test_sell_bypasses_every_hard_block_even_at_extreme_breach(engine):
    """total_value <= 0 alone is HARD_BLOCK_RULES-worthy for a BUY (see
    RiskRuleEngine.check's own synthetic total_value violation) — a SELL
    must sail through regardless."""
    decision = TradeDecision(
        action="SELL", symbol="MEGA", allocation_pct=100.0,
        entry_price=0.0, stop_loss=0.0, take_profit=0.0,
        reasoning="closing the position",
    )
    violations = engine.check(
        decision, positions=_breaching_positions(),
        total_value=-1.0,          # would hard-block a BUY outright
        daily_pnl=-999_999.0,      # would blow max_daily_loss_pct
        cash=float("nan"),         # would hard-block cash_only
        in_drawdown=True,
    )
    assert violations == []


def test_cover_bypasses_every_hard_block_even_at_extreme_breach(engine):
    decision = TradeDecision(
        action="COVER", symbol="MEGA", allocation_pct=100.0,
        entry_price=0.0, stop_loss=0.0, take_profit=0.0,
        reasoning="covering the short",
    )
    violations = engine.check(
        decision, positions=_breaching_positions(),
        total_value=float("nan"),
        daily_pnl=-999_999.0,
        cash=float("nan"),
        in_drawdown=True,
    )
    assert violations == []


def test_buy_is_NOT_exempt_under_the_same_breaching_state(engine):
    """Sanity check that the exemption is action-specific, not something
    that accidentally short-circuits the whole engine: a BUY under
    identical conditions must still be hard-blocked."""
    decision = TradeDecision(
        action="BUY", symbol="MEGA", allocation_pct=10.0,
        entry_price=100.0, stop_loss=95.0, take_profit=115.0,
        reasoning="adding more",
    )
    violations = engine.check(
        decision, positions=_breaching_positions(),
        total_value=-1.0, daily_pnl=0.0,
    )
    assert len(violations) > 0


def test_apply_gross_ceiling_never_blocks_sell_or_cover_on_unusable_equity():
    """The SIZING half of the ladder (apply_gross_ceiling) has its own
    fail-closed branch for non-finite/zero/negative equity — pinned here
    to confirm it refuses new BUY/SHORT exposure ONLY, never a SELL/COVER
    already in the decision list."""
    from src.risk.rules import GrossCeiling, apply_gross_ceiling

    ceiling = GrossCeiling(
        ceiling_x=2.0, base_x=2.0, drawdown_pct=None,
        alert_owner=False, rung="unknown", reason="test",
    )
    decisions = [
        TradeDecision(action="SELL", symbol="AAA", allocation_pct=100.0,
                      entry_price=0.0, stop_loss=0.0, take_profit=0.0,
                      reasoning="exit"),
        TradeDecision(action="COVER", symbol="BBB", allocation_pct=100.0,
                      entry_price=0.0, stop_loss=0.0, take_profit=0.0,
                      reasoning="cover"),
        TradeDecision(action="BUY", symbol="CCC", allocation_pct=10.0,
                      entry_price=50.0, stop_loss=45.0, take_profit=60.0,
                      reasoning="new risk"),
    ]
    positions = [
        Position(symbol="AAA", qty=10, avg_entry=100.0, current_price=100.0,
                 market_value=1000.0, unrealized_pnl=0.0, sector="Technology"),
        Position(symbol="BBB", qty=-10, avg_entry=100.0, current_price=100.0,
                 market_value=-1000.0, unrealized_pnl=0.0, sector="Technology"),
    ]
    for bad_equity in (float("nan"), 0.0, -5_000.0):
        outcome = apply_gross_ceiling(
            [d.model_copy(deep=True) for d in decisions], positions, bad_equity, ceiling,
        )
        by_symbol = {d.symbol: d for d in outcome.decisions}
        assert by_symbol["AAA"].allocation_pct == 100.0, "SELL must be untouched"
        assert by_symbol["BBB"].allocation_pct == 100.0, "COVER must be untouched"
        assert by_symbol["CCC"].allocation_pct == 0.0, "only new risk (BUY) is refused"
        assert "AAA" not in outcome.blocked
        assert "BBB" not in outcome.blocked


def test_apply_gross_ceiling_never_blocks_sell_or_cover_when_book_is_over_ceiling():
    """A book massively over its ceiling still must not refuse an exit —
    it can only BLOCK new BUY/SHORT and, separately, TRIM the held book
    (which itself only ever emits new SELL/COVER-shaped orders, never
    withholds a proposed one).

    ZZZ is held but untouched by any decision, so it alone keeps the
    post-trade book over the $5,000 ceiling (equity $10,000 x 0.5x)
    regardless of AAA's full exit — the genuine "no headroom" case DDD's
    new SHORT should be rationed against.
    """
    from src.risk.rules import GrossCeiling, apply_gross_ceiling

    ceiling = GrossCeiling(
        ceiling_x=0.5, base_x=2.0, drawdown_pct=-30.0,
        alert_owner=True, rung="-20%", reason="test",
    )
    decisions = [
        TradeDecision(action="SELL", symbol="AAA", allocation_pct=100.0,
                      entry_price=0.0, stop_loss=0.0, take_profit=0.0,
                      reasoning="exit"),
        TradeDecision(action="SHORT", symbol="DDD", allocation_pct=5.0,
                      entry_price=50.0, stop_loss=55.0, take_profit=40.0,
                      reasoning="new short"),
    ]
    positions = [
        Position(symbol="AAA", qty=1000, avg_entry=100.0, current_price=100.0,
                 market_value=100_000.0, unrealized_pnl=0.0, sector="Technology"),
        Position(symbol="ZZZ", qty=500, avg_entry=100.0, current_price=100.0,
                 market_value=50_000.0, unrealized_pnl=0.0, sector="Healthcare"),
    ]
    outcome = apply_gross_ceiling(decisions, positions, 10_000.0, ceiling)
    by_symbol = {d.symbol: d for d in outcome.decisions}
    assert by_symbol["AAA"].allocation_pct == 100.0, "the exit must still go through in full"
    assert by_symbol["DDD"].allocation_pct == 0.0, "new SHORT exposure has no headroom and is rationed to 0"
    assert "AAA" not in outcome.blocked
