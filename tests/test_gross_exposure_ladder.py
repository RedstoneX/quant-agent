"""Spec §11.2 — the gross-exposure ceiling and the de-levering ladder.

**This file is the owner's gate.** The 90% sector hard-refusal ceiling
(§12.3) was ratified CONDITIONAL on the ladder being proven to actually step
down: *"The 90% works if you've got the ladder, so ensure the ladder works."*
So these are not existence tests. They assert that the ceiling CHANGES at
every threshold, that new exposure is refused BEFORE anything is sold, that
the ladder is applied EXACTLY ONCE, and that none of it depends on the
Portfolio Manager returning a usable book.

The ratified table (owner, 2026-09-01):

    peak-to-trough drawdown   gross exposure ceiling
     0% to  -8%                2.0x
    -8% to -15%                1.5x
   -15% to -20%                1.0x
    worse than -20%            0.5x, and the owner is alerted
"""

import pytest

from src.models import Position, TradeDecision
from src.risk.rules import (
    GROSS_EXPOSURE_RULE,
    GROSS_LADDER,
    RiskRuleEngine,
    apply_drawdown_scale,
    apply_gross_ceiling,
    distance_to_forced_liquidation_pct,
    gross_exposure,
    peak_to_trough_pct,
    resolve_gross_ceiling,
)
from src.config import RiskConfig


EQUITY = 10_000.0
BASE_X = 2.0


def _position(symbol="NVDA", qty=10.0, avg_entry=100.0, current_price=100.0,
              sector="Technology") -> Position:
    return Position(
        symbol=symbol, qty=qty, avg_entry=avg_entry,
        current_price=current_price,
        market_value=qty * current_price,
        unrealized_pnl=qty * (current_price - avg_entry),
        sector=sector,
    )


def _buy(symbol="NVDA", alloc=10.0) -> TradeDecision:
    return TradeDecision(
        action="BUY", symbol=symbol, allocation_pct=alloc,
        entry_price=100.0, stop_loss=95.0, take_profit=115.0,
        reasoning="high conviction breakout",
    )


def _risk_config(**overrides) -> RiskConfig:
    fields = dict(
        max_position_pct=100, max_total_position_pct=400,
        max_daily_loss_pct=3, max_sector_pct=100, require_stop_loss=False,
        allow_margin=True, max_gross_exposure_x=BASE_X,
    )
    fields.update(overrides)
    return RiskConfig(**fields)


# ===========================================================================
# THE GATE, PART 1 — the ladder STEPS. Every rung, both sides of every edge.
# ===========================================================================

#: (drawdown_pct, expected ceiling). Each boundary is probed from BOTH
#: sides, plus the boundary value itself. Ties resolve to the TIGHTER rung —
#: the deliberate fail-closed choice, pinned here so nobody "fixes" it.
LADDER_CASES = [
    #  --- rung 1: 0% to -8% -> 2.0x --------------------------------------
    (0.0, 2.0),
    (-0.01, 2.0),
    (-7.99, 2.0),
    #  --- the -8% edge ---------------------------------------------------
    (-8.0, 1.5),      # exactly on the edge: tighter rung wins
    (-8.01, 1.5),
    #  --- rung 2: -8% to -15% -> 1.5x ------------------------------------
    (-11.0, 1.5),
    (-14.99, 1.5),
    #  --- the -15% edge --------------------------------------------------
    (-15.0, 1.0),
    (-15.01, 1.0),
    #  --- rung 3: -15% to -20% -> 1.0x -----------------------------------
    (-17.5, 1.0),
    (-19.99, 1.0),
    #  --- the -20% edge --------------------------------------------------
    (-20.0, 0.5),
    (-20.01, 0.5),
    #  --- rung 4: worse than -20% -> 0.5x --------------------------------
    (-35.0, 0.5),
    (-99.0, 0.5),
]


@pytest.mark.parametrize("drawdown_pct,expected_x", LADDER_CASES)
def test_the_ladder_steps_at_every_ratified_threshold(drawdown_pct, expected_x):
    """THE GATE. The ceiling must CHANGE at each of the four thresholds.

    A test that only proved the ladder exists would pass on a constant. This
    one fails on a constant, on an off-by-one boundary, and on any rung whose
    value drifts from the ratified table.
    """
    ceiling = resolve_gross_ceiling(drawdown_pct, base_x=BASE_X)
    assert ceiling.ceiling_x == expected_x, (
        f"at {drawdown_pct}% drawdown the ceiling must be {expected_x}x, "
        f"got {ceiling.ceiling_x}x"
    )


def test_the_ladder_actually_moves_rather_than_returning_one_number():
    """Guards the failure a per-case test cannot see: a ladder that is
    accidentally constant still passes every individual assertion above if
    the constant happens to be right. Four DISTINCT ceilings must exist."""
    ceilings = {
        resolve_gross_ceiling(dd, base_x=BASE_X).ceiling_x
        for dd in (-1.0, -10.0, -17.0, -25.0)
    }
    assert ceilings == {2.0, 1.5, 1.0, 0.5}


def test_each_step_is_strictly_tighter_than_the_one_above_it():
    """Monotonicity: a deeper drawdown may never buy a HIGHER ceiling."""
    probes = [0.0, -8.0, -15.0, -20.0, -40.0]
    ceilings = [resolve_gross_ceiling(p, base_x=BASE_X).ceiling_x for p in probes]
    assert ceilings == sorted(ceilings, reverse=True)
    assert len(set(ceilings)) == 4, "four rungs must produce four values"


def test_the_deepest_rung_alerts_the_owner_and_the_others_do_not():
    assert resolve_gross_ceiling(-20.0, base_x=BASE_X).alert_owner is True
    assert resolve_gross_ceiling(-25.0, base_x=BASE_X).alert_owner is True
    assert resolve_gross_ceiling(-19.99, base_x=BASE_X).alert_owner is False
    assert resolve_gross_ceiling(-8.0, base_x=BASE_X).alert_owner is False
    assert resolve_gross_ceiling(0.0, base_x=BASE_X).alert_owner is False


def test_the_ladder_can_only_tighten_the_configured_cap_never_raise_it():
    """An operator who lowers the standing cap lowers every rung with it —
    the ladder is a `min`, not a lookup that overrides config."""
    for drawdown in (0.0, -9.0, -16.0, -30.0):
        assert resolve_gross_ceiling(drawdown, base_x=1.0).ceiling_x <= 1.0
    assert resolve_gross_ceiling(-30.0, base_x=1.0).ceiling_x == 0.5


def test_the_ratified_rungs_are_the_ones_in_the_table():
    """If someone edits GROSS_LADDER, this is what tells them the owner
    ratified these specific numbers on 2026-09-01."""
    assert GROSS_LADDER == ((-8.0, 1.5), (-15.0, 1.0), (-20.0, 0.5))


# ===========================================================================
# THE GATE, PART 2 — new exposure is blocked BEFORE anything is trimmed.
# ===========================================================================

def test_new_exposure_is_refused_and_nothing_is_sold_to_make_room():
    """A book at its ceiling refuses the next BUY. It does not sell a held
    position to fund it. This ordering is the whole reason the ladder is not
    a panic-selling mechanism."""
    positions = [_position("NVDA", qty=200.0, current_price=100.0)]  # $20k = 2.0x
    ceiling = resolve_gross_ceiling(0.0, base_x=BASE_X)
    decisions = [_buy("AMD", 10.0)]

    outcome = apply_gross_ceiling(
        decisions, positions, EQUITY, ceiling, min_order_usd=500.0,
    )

    assert outcome.decisions[0].allocation_pct == 0.0, "the BUY must be refused"
    assert outcome.blocked == ["AMD"]
    assert outcome.trims == [], (
        "nothing may be sold to make room for a new position — the held book "
        "is exactly AT its ceiling, not over it"
    )


def test_a_drawdown_blocks_first_and_only_then_trims_the_excess():
    """The full ordering, in one run. The book is $20k gross on $10k equity
    (2.0x). A -16% drawdown drops the ceiling to 1.0x = $10k. So:

      1. the proposed BUY is refused outright (no headroom), and
      2. the held book, which is over on its own, is trimmed by $10k.

    Step 2 must be caused by the HELD book alone. Remove the BUY and the
    trim is identical — asserted directly below.
    """
    positions = [
        _position("NVDA", qty=100.0, current_price=100.0),   # $10k, +0
        _position("AMD", qty=100.0, current_price=100.0, avg_entry=140.0),  # $10k, -4k
    ]
    ceiling = resolve_gross_ceiling(-16.0, base_x=BASE_X)
    assert ceiling.ceiling_x == 1.0

    with_buy = apply_gross_ceiling(
        [_buy("TSLA", 5.0)], positions, EQUITY, ceiling, min_order_usd=500.0,
    )
    assert with_buy.decisions[0].allocation_pct == 0.0
    assert with_buy.blocked == ["TSLA"]
    assert with_buy.trims, "a book at 2.0x under a 1.0x ceiling must de-lever"

    without_buy = apply_gross_ceiling(
        [], list(positions), EQUITY, ceiling, min_order_usd=500.0,
    )
    assert [(t.symbol, t.action, t.allocation_pct) for t in without_buy.trims] == \
           [(t.symbol, t.action, t.allocation_pct) for t in with_buy.trims], (
        "the trim must be identical with and without a proposed BUY — if a "
        "proposal can change what gets sold, new exposure is not being "
        "blocked first"
    )
    # Biggest loser first: AMD is down $4k, NVDA is flat.
    assert without_buy.trims[0].symbol == "AMD"


def test_an_entry_that_still_fits_is_shrunk_rather_than_dropped():
    """A ceiling that only refuses produces no-trade sessions. Where headroom
    exists, the order is taken smaller."""
    positions = [_position("NVDA", qty=150.0, current_price=100.0)]  # $15k
    ceiling = resolve_gross_ceiling(0.0, base_x=BASE_X)   # $20k ceiling
    decision = _buy("AMD", 100.0)                          # wants $10k

    outcome = apply_gross_ceiling(
        [decision], positions, EQUITY, ceiling, min_order_usd=500.0,
    )

    assert outcome.blocked == []
    assert decision.allocation_pct == pytest.approx(50.0)  # $5k of headroom
    assert outcome.projected_gross <= outcome.ceiling_usd + 1e-6
    assert GROSS_EXPOSURE_RULE in decision.reasoning


def test_a_planned_exit_frees_headroom_before_entries_are_judged():
    """Exits count first — a book already being reduced is judged on what it
    will hold, not on what it holds now. Otherwise a legitimate rotation
    would be refused."""
    positions = [
        _position("NVDA", qty=200.0, current_price=100.0),   # $20k = the ceiling
    ]
    sell = TradeDecision(
        action="SELL", symbol="NVDA", allocation_pct=50.0,
        entry_price=0.0, stop_loss=0.0, take_profit=0.0, reasoning="rotate",
    )
    buy = _buy("AMD", 50.0)                                  # wants $5k
    ceiling = resolve_gross_ceiling(0.0, base_x=BASE_X)

    outcome = apply_gross_ceiling(
        [sell, buy], positions, EQUITY, ceiling, min_order_usd=500.0,
    )

    assert buy.allocation_pct == 50.0, "the freed $10k must fund the rotation"
    assert outcome.trims == []


def test_a_remnant_below_the_minimum_order_is_refused_not_placed():
    """§10.3's floor. A position shrunk to near-nothing still pays commission
    and still needs watching — the honest answer is no trade."""
    positions = [_position("NVDA", qty=199.0, current_price=100.0)]  # $19.9k
    ceiling = resolve_gross_ceiling(0.0, base_x=BASE_X)              # $20k
    decision = _buy("AMD", 20.0)

    outcome = apply_gross_ceiling(
        [decision], positions, EQUITY, ceiling, min_order_usd=500.0,
    )

    assert decision.allocation_pct == 0.0
    assert outcome.blocked == ["AMD"]


# ===========================================================================
# THE GATE, PART 3 — the ladder is applied EXACTLY ONCE.
# ===========================================================================

def test_the_ceiling_is_a_level_so_applying_it_twice_changes_nothing():
    """Double-application is a live failure mode: the PM prompt tells the
    model the ladder is the engine's arithmetic and never its own, so if the
    engine applied it twice — or the PM applied it and the engine applied it
    again — the book would de-lever twice as hard as intended.

    The defence is structural: the ceiling is a LEVEL, not a multiplier.
    Enforcing a level twice is the same level. This test is what would fail
    if anyone reintroduced a compounding factor.
    """
    positions = [_position("NVDA", qty=150.0, current_price=100.0)]  # $15k
    ceiling = resolve_gross_ceiling(-10.0, base_x=BASE_X)            # 1.5x = $15k
    decision = _buy("AMD", 100.0)

    first = apply_gross_ceiling(
        [decision], positions, EQUITY, ceiling, min_order_usd=500.0,
    )
    after_first = decision.allocation_pct
    trims_first = [(t.symbol, t.allocation_pct) for t in first.trims]

    second = apply_gross_ceiling(
        [decision], positions, EQUITY, ceiling, min_order_usd=500.0,
    )

    assert decision.allocation_pct == after_first, (
        "a second pass must not shrink the order again"
    )
    assert [(t.symbol, t.allocation_pct) for t in second.trims] == trims_first, (
        "a second pass must not trim the book again"
    )


def test_an_order_that_fits_is_never_scaled_by_the_rung():
    """The specific double-application shape to guard against: treating the
    rung as a MULTIPLIER on size rather than a ceiling on the book.

    A 1.5x rung must not become "x0.75 on every order". If it did, an order
    that comfortably fits under the ceiling would still be shrunk — and
    shrunk AGAIN on a second pass, which is the "de-levers twice as hard as
    intended" failure. The book here is at 0.5x with a 1.5x ceiling, so
    there is ample headroom and any change at all is a bug.
    """
    positions = [_position("NVDA", qty=50.0, current_price=100.0)]   # $5k
    ceiling = resolve_gross_ceiling(-10.0, base_x=BASE_X)            # 1.5x = $15k
    decision = _buy("AMD", 20.0)                                     # $2k, fits

    apply_gross_ceiling([decision], positions, EQUITY, ceiling)
    assert decision.allocation_pct == 20.0, (
        "an order inside the ceiling must pass through untouched — the rung "
        "is a ceiling on the book, not a multiplier on the order"
    )
    apply_gross_ceiling([decision], positions, EQUITY, ceiling)
    assert decision.allocation_pct == 20.0, (
        "and a second pass must still leave it alone"
    )
    assert GROSS_EXPOSURE_RULE not in decision.reasoning


def test_resolving_the_ladder_twice_yields_the_same_rung():
    """`resolve_gross_ceiling` is a pure function of drawdown. It cannot
    accumulate, because it reads no state of its own."""
    first = resolve_gross_ceiling(-16.0, base_x=BASE_X)
    second = resolve_gross_ceiling(first.drawdown_pct, base_x=BASE_X)
    third = resolve_gross_ceiling(-16.0, base_x=first.ceiling_x)
    assert first.ceiling_x == second.ceiling_x == 1.0
    # Feeding a rung back in as the base cannot step it down a second time.
    assert third.ceiling_x == 1.0


def test_a_de_levered_book_trimmed_once_is_not_trimmed_again():
    """The realistic double-application shape: the preamble trims, the book
    is re-measured, and the second pass must find nothing left to do."""
    ceiling = resolve_gross_ceiling(-16.0, base_x=BASE_X)   # 1.0x = $10k
    positions = [_position("NVDA", qty=200.0, current_price=100.0)]  # $20k

    first = apply_gross_ceiling([], positions, EQUITY, ceiling)
    assert first.trims, "the over-levered book must be trimmed once"

    # The book that now exists after that trim executes.
    sold_fraction = first.trims[0].allocation_pct / 100.0
    remaining = [
        _position("NVDA", qty=200.0 * (1 - sold_fraction), current_price=100.0),
    ]
    second = apply_gross_ceiling([], remaining, EQUITY, ceiling)
    assert second.trims == [], (
        "the ladder must not de-lever a book that already fits its ceiling"
    )


def test_the_drawdown_halve_and_the_ceiling_are_separate_arithmetic():
    """`apply_drawdown_scale` halves ALLOCATIONS; the ladder sets a CEILING.
    Passing the resolved ceiling to the halving must not double-scale — the
    ceiling is there to be NAMED in the note, not multiplied in."""
    ceiling = resolve_gross_ceiling(-16.0, base_x=BASE_X)
    with_ceiling = apply_drawdown_scale(
        [_buy("NVDA", 12.0)], in_drawdown=True, ceiling=ceiling,
    )[0][0]
    without_ceiling = apply_drawdown_scale(
        [_buy("NVDA", 12.0)], in_drawdown=True,
    )[0][0]
    assert with_ceiling.allocation_pct == without_ceiling.allocation_pct == 6.0
    assert "1.0x" in with_ceiling.reasoning, (
        "the note should tell the reader which rung is in force"
    )


# ===========================================================================
# THE GATE, PART 4 — none of this may depend on the Portfolio Manager.
# ===========================================================================

def test_a_blank_portfolio_manager_session_still_de_levers():
    """**A session where the Portfolio Manager returns nothing at all, with
    the account in a drawdown deep enough to demand a lower ceiling, still
    de-levers.**

    This is not hypothetical. One candidate model was measured returning an
    empty response on 1 run in 10, truncating mid-JSON at the token limit. A
    blank PM session is a silent no-trade session — which costs a day of
    trading today, and with borrowing enabled would mean the desk stays
    levered at exactly the moment it should be shedding exposure.

    So: no decisions, no targets, no PM output of any kind. The ceiling must
    still have a correct value and the held book must still be reduced.
    """
    positions = [
        _position("NVDA", qty=120.0, current_price=100.0),               # $12k
        _position("AMD", qty=80.0, current_price=100.0, avg_entry=150.0),  # $8k
    ]
    # -16% peak-to-trough demands 1.0x. The book is at 2.0x.
    ceiling = resolve_gross_ceiling(-16.0, base_x=BASE_X)
    assert ceiling.ceiling_x == 1.0, "the ceiling must not need a PM to resolve"

    outcome = apply_gross_ceiling([], positions, EQUITY, ceiling)

    assert outcome.decisions == [], "there is nothing the PM proposed"
    assert outcome.blocked == [], "there is no new exposure to block"
    # Blocking must HOLD on such a run, not merely be vacuous. The ceiling
    # the blank session resolved refuses new exposure at the execution gate
    # exactly as it would on a session the PM answered.
    engine = RiskRuleEngine(_risk_config())
    assert GROSS_EXPOSURE_RULE in [
        v.rule for v in engine.check(
            decision=_buy("TSLA", 5.0), positions=positions,
            total_value=EQUITY, daily_pnl=0.0, gross_ceiling=ceiling,
        )
    ], "the blank-PM ceiling must still refuse new exposure"
    assert outcome.trims, (
        "THE FAILURE THIS TEST EXISTS FOR: a blank PM response must not leave "
        "the desk levered through a drawdown"
    )
    freed = sum(
        abs(p.market_value) * (t.allocation_pct / 100.0)
        for t in outcome.trims
        for p in positions if p.symbol == t.symbol
    )
    assert outcome.held_gross - freed <= outcome.ceiling_usd + 1e-6, (
        "the trim must actually bring the book under the ceiling"
    )


def test_the_de_lever_runs_in_the_preamble_before_any_agent_is_called():
    """Wiring, not just arithmetic. A correct `apply_gross_ceiling` that is
    only ever reached from inside the decision path would still leave the
    desk levered on a blank-PM run, because the decision path returns early
    when the Portfolio Manager produces nothing.

    So the de-lever must be invoked BEFORE the decision stage in both trading
    entry points. This asserts that ordering directly against the source.
    """
    import inspect
    from src.pipeline import TradingPipeline

    for entry_point in (TradingPipeline.run_morning, TradingPipeline.run_position_review):
        source = inspect.getsource(entry_point)
        assert "_enforce_gross_ceiling" in source, (
            f"{entry_point.__name__} must de-lever in its preamble"
        )

    morning = inspect.getsource(TradingPipeline.run_morning)
    assert morning.index("_enforce_gross_ceiling") < morning.index("_decision_stage"), (
        "the de-lever must run BEFORE the Portfolio Manager is called, so a "
        "blank or truncated model response cannot skip it"
    )


def test_the_preamble_de_lever_submits_sells_with_no_pm_decision_present():
    """The pipeline method itself, exercised against an over-levered book and
    no Portfolio Manager output whatsoever."""
    from unittest.mock import MagicMock
    from src.config import CashSweepConfig
    from src.pipeline import TradingPipeline
    from src.pipeline_context import RunContext

    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.config = MagicMock()
    pipeline.config.risk = _risk_config()
    pipeline.config.cash_sweep = CashSweepConfig(enabled=False)
    pipeline.db = MagicMock()
    # A book that fell 16% from its high: the ladder demands 1.0x.
    pipeline.db.get_daily_pnl.return_value = [{"total_value": EQUITY / 0.84}]
    pipeline.broker = MagicMock()
    pipeline.broker.get_account.return_value = {
        "cash": 0.0, "portfolio_value": EQUITY, "last_equity": EQUITY,
    }
    pipeline.broker.get_positions.return_value = []
    pipeline.cash_sweeper = None
    pipeline._compute_deployable_cash = MagicMock(return_value=0.0)
    pipeline._finalize_pending_protections = MagicMock()
    pipeline._submit_protected_sell = MagicMock(
        return_value=({"id": "order-1", "symbol": "NVDA"}, {}),
    )

    ctx = RunContext(run_id="test-run", session="morning")
    ctx.positions = [_position("NVDA", qty=200.0, current_price=100.0)]  # 2.0x
    ctx.total_value = EQUITY

    orders = pipeline._enforce_gross_ceiling(ctx)

    assert orders, "an over-levered book must be reduced with no PM involved"
    assert pipeline._submit_protected_sell.called
    assert ctx.leverage["ceiling_x"] == 1.0
    assert ctx.leverage["drawdown_pct"] == pytest.approx(-16.0, abs=0.1)
    assert ctx.leverage["distance_to_forced_liquidation_pct"] is not None


def test_the_ceiling_is_computed_from_account_state_alone():
    """Signature-level proof of the decoupling: resolving the ceiling takes a
    drawdown and a configured cap. There is no parameter through which a
    Portfolio Manager decision could reach it."""
    import inspect
    parameters = set(inspect.signature(resolve_gross_ceiling).parameters)
    assert parameters == {"drawdown_pct", "base_x"}


def test_peak_to_trough_is_measured_from_the_equity_curve_not_from_agents():
    curve = [10_000.0, 12_000.0, 11_000.0]
    assert peak_to_trough_pct(curve, 9_600.0) == -20.0
    assert peak_to_trough_pct(curve, 12_000.0) == 0.0
    # A new high is not a drawdown.
    assert peak_to_trough_pct(curve, 15_000.0) == 0.0
    # No history at all is honestly unknown, never a fabricated zero.
    assert peak_to_trough_pct([], None) is None


def test_an_unknown_drawdown_holds_the_standing_cap_and_trims_nothing():
    """A fresh account with no equity history genuinely has no drawdown.
    Forcing it to the deepest rung would refuse every trade on day one and
    force-liquidate a book that never fell."""
    ceiling = resolve_gross_ceiling(None, base_x=BASE_X)
    assert ceiling.ceiling_x == BASE_X
    assert ceiling.alert_owner is False
    assert ceiling.drawdown_pct is None


# ===========================================================================
# The measurement itself — gross, the cash park, and the margin-call distance
# ===========================================================================

def test_gross_is_longs_plus_the_magnitude_of_shorts():
    positions = [
        _position("NVDA", qty=50.0, current_price=100.0),     # +$5k long
        _position("TSLA", qty=-30.0, current_price=100.0),    # -$3k short
    ]
    assert gross_exposure(positions) == pytest.approx(8_000.0)


def test_the_cash_park_is_not_exposure():
    """SGOV is parked cash. Counting it would consume the entire leverage
    allowance doing nothing."""
    positions = [
        _position("NVDA", qty=50.0, current_price=100.0),
        _position("SGOV", qty=100.0, current_price=100.0, sector="Cash"),
    ]
    assert gross_exposure(positions, cash_park_symbol="SGOV") == pytest.approx(5_000.0)
    # And it is never sold to satisfy the ceiling.
    ceiling = resolve_gross_ceiling(-30.0, base_x=BASE_X)   # 0.5x = $5k
    outcome = apply_gross_ceiling(
        [], positions, EQUITY, ceiling, cash_park_symbol="SGOV",
    )
    assert all(t.symbol != "SGOV" for t in outcome.trims)


def test_the_park_symbol_comes_from_config_not_a_hardcoded_ticker():
    positions = [_position("BIL", qty=100.0, current_price=100.0, sector="Cash")]
    assert gross_exposure(positions, cash_park_symbol="BIL") == 0.0
    assert gross_exposure(positions, cash_park_symbol="SGOV") == pytest.approx(10_000.0)


def test_distance_to_forced_liquidation_reproduces_the_ratified_figures():
    """The §11.2 spec entry publishes ~33% at 2.0x and ~55% at 1.5x. These
    are reproduced from the maintenance-margin arithmetic, not restated."""
    equity = 9_825.0
    assert distance_to_forced_liquidation_pct(2.0 * equity, equity) == pytest.approx(33.3, abs=0.1)
    assert distance_to_forced_liquidation_pct(1.5 * equity, equity) == pytest.approx(55.6, abs=0.1)
    # An unlevered book cannot be margin-called.
    assert distance_to_forced_liquidation_pct(equity, equity) == 100.0
    assert distance_to_forced_liquidation_pct(0.0, equity) == 100.0


# ===========================================================================
# The EXECUTION gate — the same ceiling, enforced again where orders leave
# ===========================================================================

def test_the_execution_gate_hard_blocks_a_breach():
    engine = RiskRuleEngine(_risk_config())
    positions = [_position("NVDA", qty=190.0, current_price=100.0)]   # $19k
    decision = _buy("AMD", 30.0)                                      # +$3k -> 2.2x

    violations = engine.check(
        decision=decision, positions=positions, total_value=EQUITY,
        daily_pnl=0.0, gross_ceiling=resolve_gross_ceiling(0.0, base_x=BASE_X),
    )

    rules = [v.rule for v in violations]
    assert GROSS_EXPOSURE_RULE in rules
    message = next(v.message for v in violations if v.rule == GROSS_EXPOSURE_RULE)
    # Operator-readable, and it names the rule and both numbers.
    assert "gross exposure" in message
    assert "2.00x ceiling" in message


def test_the_execution_gate_moves_with_the_ladder():
    """The same order passes at 2.0x and is blocked at 1.0x. If this test
    ever passes with identical outcomes at both rungs, the execution gate is
    not reading the ladder."""
    engine = RiskRuleEngine(_risk_config())
    positions = [_position("NVDA", qty=90.0, current_price=100.0)]    # $9k
    order = _buy("AMD", 20.0)                                         # +$2k -> 1.1x

    undrawn = engine.check(
        decision=order, positions=positions, total_value=EQUITY, daily_pnl=0.0,
        gross_ceiling=resolve_gross_ceiling(0.0, base_x=BASE_X),
    )
    drawn = engine.check(
        decision=order, positions=positions, total_value=EQUITY, daily_pnl=0.0,
        gross_ceiling=resolve_gross_ceiling(-16.0, base_x=BASE_X),
    )

    assert GROSS_EXPOSURE_RULE not in [v.rule for v in undrawn]
    assert GROSS_EXPOSURE_RULE in [v.rule for v in drawn]


def test_the_execution_gate_is_a_hard_block_not_an_advisory():
    from src.pipeline import HARD_BLOCK_RULES
    assert GROSS_EXPOSURE_RULE in HARD_BLOCK_RULES


def test_the_execution_gate_falls_back_to_the_configured_cap():
    """A caller that forgets to pass the ladder still gets A ceiling — never
    none."""
    engine = RiskRuleEngine(_risk_config(max_gross_exposure_x=1.0))
    positions = [_position("NVDA", qty=100.0, current_price=100.0)]   # $10k = 1.0x
    violations = engine.check(
        decision=_buy("AMD", 20.0), positions=positions,
        total_value=EQUITY, daily_pnl=0.0,
    )
    assert GROSS_EXPOSURE_RULE in [v.rule for v in violations]


def test_a_short_consumes_the_ceiling_exactly_like_a_long():
    """Gross is direction-agnostic. A short is not free leverage."""
    engine = RiskRuleEngine(_risk_config(max_single_short_pct=100))
    positions = [_position("NVDA", qty=190.0, current_price=100.0)]
    short = TradeDecision(
        action="SHORT", symbol="TSLA", allocation_pct=30.0,
        entry_price=100.0, stop_loss=110.0, take_profit=80.0,
        reasoning="breakdown",
    )
    violations = engine.check(
        decision=short, positions=positions, total_value=EQUITY, daily_pnl=0.0,
        gross_ceiling=resolve_gross_ceiling(0.0, base_x=BASE_X),
    )
    assert GROSS_EXPOSURE_RULE in [v.rule for v in violations]


# ===========================================================================
# Refusals must read as English, naming the rule that fired
# ===========================================================================

def test_every_refusal_says_which_rule_fired_in_plain_language():
    positions = [_position("NVDA", qty=200.0, current_price=100.0)]
    ceiling = resolve_gross_ceiling(-16.0, base_x=BASE_X)
    outcome = apply_gross_ceiling([_buy("AMD", 10.0)], positions, EQUITY, ceiling)

    assert outcome.notes, "a refusal with no explanation is not a refusal"
    for note in outcome.notes:
        assert GROSS_EXPOSURE_RULE in note, "every note names the rule by name"
        assert "below its equity high" in note, (
            "and says, in words, why the ceiling is where it is"
        )


def test_the_de_lever_order_explains_itself_to_the_operator():
    positions = [_position("NVDA", qty=200.0, current_price=100.0)]
    ceiling = resolve_gross_ceiling(-16.0, base_x=BASE_X)
    trim = apply_gross_ceiling([], positions, EQUITY, ceiling).trims[0]
    assert "New exposure was blocked first" in trim.reasoning
    assert "ceiling" in trim.reasoning


# ===========================================================================
# Fail-closed on a broken broker snapshot
# ===========================================================================

def test_a_nan_market_value_blocks_new_exposure_and_forbids_trimming():
    """`NaN > ceiling` is False, so an unguarded comparison would switch the
    ceiling OFF on exactly the broken-snapshot day it matters most. And
    trimming on a snapshot we cannot read is destructive — block, do not
    sell."""
    broken = _position("NVDA", qty=100.0, current_price=100.0)
    object.__setattr__(broken, "market_value", float("nan"))
    decision = _buy("AMD", 10.0)

    outcome = apply_gross_ceiling(
        [decision], [broken], EQUITY, resolve_gross_ceiling(0.0, base_x=BASE_X),
    )

    assert decision.allocation_pct == 0.0
    assert outcome.trims == []
    assert outcome.measurable is False


def test_an_unusable_equity_figure_refuses_every_new_position():
    decision = _buy("AMD", 10.0)
    outcome = apply_gross_ceiling(
        [decision], [], 0.0, resolve_gross_ceiling(0.0, base_x=BASE_X),
    )
    assert decision.allocation_pct == 0.0
    assert outcome.blocked == ["AMD"]
    assert outcome.trims == []
