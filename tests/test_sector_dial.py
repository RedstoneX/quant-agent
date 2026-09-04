"""Spec §10.3 — concentration scales size, it does not veto.

Sector crowding used to be a HARD BLOCK: a sector at `max_sector_pct` refused
the next trade in it outright, however good that trade was. The owner's
ratified framing (2026-09-01) inverts that — *"each trade opportunity is an
opportunity on its own, and it should be based on the merits of that
opportunity"* — so a high-conviction idea in an already-heavy sector is TAKEN,
SMALLER.

What these tests pin down, in the order the risk of getting it wrong runs:

  1. the dial works (accepted at reduced size, monotonically, never negative);
  2. the dial ENDS (an absolute ceiling, and a floor under which the honest
     answer is no trade rather than a token trade);
  3. the reason is visible to whoever reads the order;
  4. nothing ELSE moved — the correlation-cluster check and the per-trade /
     portfolio risk ceilings behave exactly as they did. That last group is
     the real regression risk in a change to a sizing path, so it is asserted
     explicitly rather than assumed from the other suites passing.
"""

from unittest.mock import patch

import pytest

from src.config import RiskConfig
from src.models import (
    Position, TargetPosition, TechAnalysisResult, TechReasoningChain,
    TradeDecision,
)
from src.pipeline import HARD_BLOCK_RULES
from src.portfolio_constructor import ConstructorConfig, PortfolioConstructor
from src.risk.rules import (
    RiskRuleEngine, sector_allowance_pct, sector_side_gross,
    sector_side_weights, sector_size_scale,
)

EQUITY = 100_000.0
SOFT = 40.0
HARD = 60.0


def _tech_rc() -> TechReasoningChain:
    return TechReasoningChain(
        trend="x", momentum="x", volatility="x",
        volume="x", support_resistance="x",
    )


def _analysis(symbol: str, entry: float, stop: float, target: float) -> TechAnalysisResult:
    """A realistic analyst result, including the two fields production sets in
    Python rather than asking the model for.

    `computed_levels` and `atr_14` are attached by `TechAnalystAgent` after
    parsing. They were absent when this file was written for §10.3 and the
    suite passed anyway, because the constructor then took the take-profit
    from the model's `reference_target`. Spec §10.4 (merged the same day)
    made the constructor DERIVE the target from structure and refuse without
    an ATR, so every order these tests build was refused at
    `no_volatility_reading` before the sector dial was ever consulted — the
    dial was not broken, it was unreachable. Same shape as
    `tests/test_portfolio_constructor.py::_analysis`, deliberately.

    The ATR sits just inside the noise band so the structural stop is left
    alone; these tests are about sizing, not stop widening. The long horizon
    is not decoration — reaching a target W away from a stop R away needs
    `sqrt(sessions) >= 0.9 * W/R` once the stop is held at 1.35 ATRs (it
    was 2.3 at 3.45 ATRs, before the 2026-09-04 floor change — see the same
    note in `tests/test_portfolio_constructor.py::_analysis`).
    """
    return TechAnalysisResult(
        symbol=symbol, rating="strong_buy", entry_price=entry,
        stop_loss=stop, reference_target=target, reasoning="test",
        support_levels=[stop], resistance_levels=[target],
        computed_levels=[stop, target],
        atr_14=abs(entry - stop) / 3.5,
        setup_type="range", expected_horizon_sessions=60,
        reasoning_chain=_tech_rc(),
    )


def _held(symbol: str, market_value: float, sector: str = "Technology") -> Position:
    """A held position worth `market_value`, priced so qty stays sane."""
    return Position(
        symbol=symbol, qty=market_value / 100.0, avg_entry=100.0,
        current_price=100.0, market_value=market_value,
        unrealized_pnl=0.0, sector=sector,
    )


def _constructor() -> PortfolioConstructor:
    return PortfolioConstructor(ConstructorConfig(
        max_sector_pct=SOFT, max_sector_hard_pct=HARD, min_order_usd=500.0,
    ))


def _build(sector_held_pct: float, target_weight_pct: float = 8.0,
           *, constructor: PortfolioConstructor | None = None):
    """Construct one BUY into a Technology sector already `sector_held_pct` full."""
    constructor = constructor or _constructor()
    positions = (
        [_held("AAPL", EQUITY * sector_held_pct / 100)] if sector_held_pct > 0 else []
    )
    with patch("src.execution.broker._get_sector", return_value="Technology"):
        return constructor.construct_orders(
            targets=[TargetPosition(
                symbol="NVDA", target_weight_pct=target_weight_pct,
                conviction="high", thesis="best setup on the board",
            )],
            positions=positions,
            analyses=[_analysis("NVDA", entry=100, stop=95, target=140)],
            total_value=EQUITY, price_map={"NVDA": 100.0},
        )


# ---------------------------------------------------------------------------
# 1. The dial — a crowded sector shrinks the trade instead of killing it
# ---------------------------------------------------------------------------

def test_high_conviction_in_an_overweight_sector_is_taken_smaller_not_refused():
    """The whole point of §10.3.

    Technology sits at 50% of equity — ten points OVER the diversification
    target that used to hard-block this order outright. The trade must still
    happen, at a reduced size.
    """
    decisions = _build(sector_held_pct=50.0, target_weight_pct=8.0)

    assert len(decisions) == 1, "a crowded sector must not veto the trade"
    d = decisions[0]
    assert d.action == "BUY"
    assert d.symbol == "NVDA"
    # Dial at 50% crowding is (60-50)/(60-40) = 0.5, so 8% is halved.
    assert d.allocation_pct == pytest.approx(4.0)
    assert 0 < d.allocation_pct < 8.0


def test_an_uncrowded_sector_pays_nothing_for_the_dial():
    """Below the diversification target the dial is inert.

    Guards against the change quietly shrinking every order in the book: at or
    under `max_sector_pct` the size must be byte-identical to what the
    constructor produced before §10.3, and the reasoning must carry no
    crowding note.
    """
    decisions = _build(sector_held_pct=10.0, target_weight_pct=8.0)

    assert len(decisions) == 1
    assert decisions[0].allocation_pct == pytest.approx(8.0)
    assert "scaled" not in decisions[0].reasoning


def test_heavier_sector_means_smaller_size_and_it_never_inverts():
    """Monotonicity, on the real construction path rather than the pure maths.

    A heavier sector may only ever produce a SMALLER position. Nothing in the
    sweep may go negative, and no step may hand back more than the step
    before it — an inversion would mean crowding rewarding itself.
    """
    sizes = []
    for held in (0, 10, 20, 30, 40, 42, 45, 48, 50, 52, 55):
        decisions = _build(sector_held_pct=float(held), target_weight_pct=8.0)
        size = decisions[0].allocation_pct if decisions else 0.0
        assert size >= 0.0, f"negative size at {held}% sector weight"
        sizes.append((held, size))

    for (prev_held, prev), (held, cur) in zip(sizes, sizes[1:]):
        assert cur <= prev + 1e-9, (
            f"size INVERTED: {prev_held}% sector → {prev}%, "
            f"but {held}% sector → {cur}%"
        )
    # And it genuinely moves — a "monotonic" constant would pass the above.
    assert sizes[0][1] > sizes[-1][1]


def test_the_pure_dial_functions_are_monotonic_and_non_negative():
    """The arithmetic underneath, swept finely enough to catch a discontinuity
    at the soft cap — where the two branches of `sector_allowance_pct` meet
    and an inversion would be easiest to introduce."""
    prev_scale = prev_allowance = float("inf")
    s = 0.0
    while s <= 80.0:
        scale = sector_size_scale(s, soft_cap_pct=SOFT, hard_cap_pct=HARD)
        allowance = sector_allowance_pct(s, soft_cap_pct=SOFT, hard_cap_pct=HARD)
        assert 0.0 <= scale <= 1.0, f"scale out of range at {s}"
        assert allowance >= 0.0, f"negative allowance at {s}"
        assert scale <= prev_scale + 1e-12, f"scale inverted at {s}"
        assert allowance <= prev_allowance + 1e-12, f"allowance inverted at {s}"
        prev_scale, prev_allowance = scale, allowance
        s += 0.1

    assert sector_size_scale(SOFT, soft_cap_pct=SOFT, hard_cap_pct=HARD) == 1.0
    assert sector_size_scale(HARD, soft_cap_pct=SOFT, hard_cap_pct=HARD) == 0.0
    assert sector_allowance_pct(HARD, soft_cap_pct=SOFT, hard_cap_pct=HARD) == 0.0


# ---------------------------------------------------------------------------
# 2. The dial ends — an absolute ceiling above, a minimum order below
# ---------------------------------------------------------------------------

def test_the_absolute_ceiling_still_refuses():
    """A dial with no end is not a dial.

    Past the hard ceiling no conviction buys more concentration, and the
    refusal must not be silent — a sector could otherwise grow without limit
    through an infinite series of ever-smaller additions.
    """
    assert _build(sector_held_pct=60.0) == []
    assert _build(sector_held_pct=75.0) == []


def test_a_trade_scaled_under_the_minimum_order_is_refused_not_placed_tiny():
    """A position shrunk to near-nothing still pays full commission, still
    consumes a slot, still needs a stop and still needs watching. It cannot
    pay for its own risk, so the honest answer is no trade."""
    # 59.5% crowding leaves an allowance of ~0.0125% of equity — about $12.
    decisions = _build(sector_held_pct=59.5, target_weight_pct=8.0)
    assert decisions == []


def test_the_minimum_order_floor_is_the_existing_threshold_not_a_new_one():
    """§10.3 reuses `cash_sweep.min_order_usd` rather than inventing a second
    notion of "too small to bother"; raising it must move the refusal."""
    generous = PortfolioConstructor(ConstructorConfig(
        max_sector_pct=SOFT, max_sector_hard_pct=HARD, min_order_usd=1.0,
    ))
    strict = PortfolioConstructor(ConstructorConfig(
        max_sector_pct=SOFT, max_sector_hard_pct=HARD, min_order_usd=5_000.0,
    ))
    # 55% crowding leaves 1.25% of equity = $1,250: over $1, under $5,000.
    assert _build(55.0, constructor=generous) != []
    assert _build(55.0, constructor=strict) == []


def test_the_hard_ceiling_is_configurable():
    """The ceiling is a judgement about how far a tilt may run, so it is a
    setting rather than a constant — and moving it must actually move the
    refusal boundary."""
    loose = PortfolioConstructor(ConstructorConfig(
        max_sector_pct=SOFT, max_sector_hard_pct=80.0, min_order_usd=500.0,
    ))
    assert _build(65.0) == []                       # refused at the 60% default
    assert _build(65.0, constructor=loose) != []    # permitted at 80%


# ---------------------------------------------------------------------------
# 3. The reason must be findable
# ---------------------------------------------------------------------------

def test_a_scaled_position_says_why_in_the_order_the_owner_reads():
    """An owner seeing a smaller position than expected must be able to find
    out why. The same audit path that carries the risk-budget and single-name
    cuts carries this one — a silent reduction reads to the AI Risk Manager
    as the PM contradicting itself (2026-08-20 full-plan veto)."""
    d = _build(sector_held_pct=50.0, target_weight_pct=8.0)[0]

    assert "sector" in d.reasoning.lower()
    assert "Technology" in d.reasoning
    assert "50.0%" in d.reasoning          # what the book actually held
    assert "8.00% → 4.00%" in d.reasoning  # the arithmetic, not just a claim
    assert "not PM inconsistency" in d.reasoning
    # The thesis survives alongside it.
    assert "best setup on the board" in d.reasoning


# ---------------------------------------------------------------------------
# 4. Batch behaviour — the second trade sees the first
# ---------------------------------------------------------------------------

def test_orders_in_one_batch_accumulate_sector_weight():
    """Without an accumulator, three targets in one crowded sector would each
    be sized against the same stale starting weight and collectively breach
    the ceiling — the same reason the pipeline's risk filter keeps
    `pending_sector_investment`."""
    constructor = _constructor()
    with patch("src.execution.broker._get_sector", return_value="Technology"):
        decisions = constructor.construct_orders(
            targets=[
                TargetPosition(symbol=s, target_weight_pct=10.0,
                               conviction="high", thesis="t")
                for s in ("NVDA", "AMD", "AVGO")
            ],
            positions=[_held("AAPL", EQUITY * 0.45)],
            analyses=[_analysis(s, entry=100, stop=95, target=140)
                      for s in ("NVDA", "AMD", "AVGO")],
            total_value=EQUITY, price_map={s: 100.0 for s in ("NVDA", "AMD", "AVGO")},
        )

    assert decisions, "a crowded sector must still trade"
    # Each successive order is sized against a heavier book than the last.
    sizes = [d.allocation_pct for d in decisions]
    assert sizes == sorted(sizes, reverse=True)
    # And the batch as a whole cannot breach the absolute ceiling.
    assert 45.0 + sum(sizes) <= HARD + 1e-6


def _short_analysis(symbol: str = "XOM") -> TechAnalysisResult:
    """A short-side analyst result, with the §10.4 Python-set fields."""
    return TechAnalysisResult(
        symbol=symbol, rating="sell", entry_price=100.0,
        stop_loss=105.0, reference_target=80.0, reasoning="test",
        support_levels=[80.0], resistance_levels=[105.0],
        # Spec §10.4 — see `_analysis` above. Without these the SHORT is
        # refused at `no_volatility_reading` and never reaches the dial.
        computed_levels=[80.0, 105.0],
        atr_14=5.0 / 3.5,
        setup_type="range", expected_horizon_sessions=60,
        reasoning_chain=_tech_rc(),
    )


def _short_into_energy(positions) -> list:
    """One SHORT of XOM into an Energy book of `positions`."""
    with patch("src.execution.broker._get_sector", return_value="Energy"):
        return _constructor().construct_orders(
            targets=[TargetPosition(
                symbol="XOM", target_weight_pct=8.0, direction="short",
                conviction="high", thesis="breaking down",
            )],
            positions=positions,
            analyses=[_short_analysis()],
            total_value=EQUITY, price_map={"XOM": 100.0},
        )


def _held_short(symbol: str, gross_value: float, sector: str) -> Position:
    """A HELD SHORT worth `gross_value` gross — negative qty AND negative
    market_value, which is how Alpaca reports one."""
    return Position(
        symbol=symbol, qty=-gross_value / 100.0, avg_entry=100.0,
        current_price=100.0, market_value=-gross_value,
        unrealized_pnl=0.0, sector=sector,
    )


def test_a_short_in_a_crowded_short_sector_is_scaled_not_refused():
    """A short crowds its own side exactly as a long crowds its own, and gets
    scaled for that crowding rather than vetoed.

    Spec §12.2 changed WHICH book it is measured against — the SHORT budget
    of that sector, not a shared gross bucket. The §10.3 behaviour under test
    here (dial, not gate) is unchanged.
    """
    decisions = _short_into_energy([_held_short("CVX", EQUITY * 0.5, "Energy")])

    assert len(decisions) == 1, "a crowded sector must not veto a short either"
    d = decisions[0]
    assert d.action == "SHORT"
    assert 0 < d.allocation_pct < 8.0
    assert "Energy" in d.reasoning


def test_a_crowded_LONG_book_does_not_shrink_a_short_into_the_same_sector():
    """DELIBERATE REVERSAL under spec §12.2 (owner-ratified 2026-09-01).

    This test previously asserted the opposite: that a 50%-long Energy book
    scaled a new Energy SHORT down, because "the sector budget is GROSS —
    direction-agnostic". §12.2 replaced that with SEPARATE long and short
    budgets, each measured against the same limit, neither offsetting or
    consuming the other.

    Owner's reasoning, which governs: *"A long and a short in the same sector
    is not a hedge... We are trading opportunities."* Gross summing was
    explicitly REJECTED because it charges one sector budget twice for two
    independent opportunities — see the pair-trade test below.

    So a heavy LONG book in Energy is not a reason to shrink a SHORT into
    Energy, and the order comes through at its full requested weight.
    """
    decisions = _short_into_energy([_held("CVX", EQUITY * 0.5, sector="Energy")])

    assert len(decisions) == 1
    d = decisions[0]
    assert d.action == "SHORT"
    assert d.allocation_pct == pytest.approx(8.0), (
        "the long side's crowding must not ration the short side's budget"
    )


def test_the_pair_trade_stays_legal_long_the_leader_short_the_laggard():
    """The trade §12.2 exists to keep legal: long the leader and short the
    laggard in the SAME hot sector, both permitted at full size.

    Under gross summing the second leg would be charged against exposure the
    first leg already consumed, and a hot sector would forbid exactly the
    trade a desk wants there most. Under §12.2 they are two opportunities
    that happen to share a label.
    """
    # Caps chosen so the two legs SUMMED (10 + 10 = 20) would sit past the
    # absolute ceiling and the second leg would be refused outright under
    # gross summing. Split by side, each leg is 10 against a 15 target and
    # neither is touched. 10% per leg is also the `max_single_short_pct`
    # ceiling, so the short is not silently clamped by an unrelated rule.
    constructor = PortfolioConstructor(ConstructorConfig(
        max_sector_pct=15.0, max_sector_hard_pct=18.0, min_order_usd=500.0,
    ))
    with patch("src.execution.broker._get_sector", return_value="Technology"):
        decisions = constructor.construct_orders(
            targets=[
                TargetPosition(symbol="NVDA", target_weight_pct=10.0,
                               conviction="high", thesis="the leader"),
                TargetPosition(symbol="INTC", target_weight_pct=10.0,
                               direction="short", conviction="high",
                               thesis="the laggard"),
            ],
            positions=[],
            analyses=[
                _analysis("NVDA", entry=100, stop=95, target=140),
                _short_analysis("INTC"),
            ],
            total_value=EQUITY,
            price_map={"NVDA": 100.0, "INTC": 100.0},
        )

    by_symbol = {d.symbol: d for d in decisions}
    assert set(by_symbol) == {"NVDA", "INTC"}, "both legs must survive"
    assert by_symbol["NVDA"].action == "BUY"
    assert by_symbol["INTC"].action == "SHORT"
    assert by_symbol["NVDA"].allocation_pct == pytest.approx(10.0)
    assert by_symbol["INTC"].allocation_pct == pytest.approx(10.0)
    # Neither leg was touched by the dial at all.
    assert "sector" not in by_symbol["NVDA"].reasoning.lower()
    assert "sector" not in by_symbol["INTC"].reasoning.lower()
    # The trade the owner wanted kept legal really would have been illegal
    # under gross summing: assert the arithmetic rather than trusting it.
    assert 10.0 + 10.0 > 18.0


# ---------------------------------------------------------------------------
# 5. The risk engine — advisory at the target, hard block at the ceiling
# ---------------------------------------------------------------------------

def _engine(**overrides) -> RiskRuleEngine:
    kwargs = dict(
        max_position_pct=30.0, max_total_position_pct=90.0,
        max_daily_loss_pct=3.0, max_sector_pct=SOFT,
        max_sector_hard_pct=HARD, require_stop_loss=True,
    )
    kwargs.update(overrides)
    return RiskRuleEngine(RiskConfig(**kwargs))


def _buy(symbol: str, allocation_pct: float) -> TradeDecision:
    return TradeDecision(
        action="BUY", symbol=symbol, allocation_pct=allocation_pct,
        entry_price=100.0, stop_loss=95.0, take_profit=140.0, reasoning="t",
    )


def test_the_diversification_target_is_advisory_not_a_hard_block():
    """`max_sector_pct` is deliberately no longer in HARD_BLOCK_RULES.

    That set membership IS the veto — leaving it there would reinstate
    exactly the behaviour §10.3 removes, so it is asserted directly rather
    than inferred from a pipeline outcome.
    """
    assert "max_sector_pct" not in HARD_BLOCK_RULES
    assert "max_sector_hard_pct" in HARD_BLOCK_RULES

    with patch("src.execution.broker._get_sector", return_value="Technology"):
        violations = _engine().check(
            _buy("NVDA", 10.0),
            positions=[_held("AAPL", EQUITY * 0.45)],
            total_value=EQUITY, daily_pnl=0.0,
        )
    rules = [v.rule for v in violations]
    # Still REPORTED — the book being over its target is real information.
    assert "max_sector_pct" in rules
    # But not blocked, and the message says so rather than reading as a veto.
    assert not [r for r in rules if r in HARD_BLOCK_RULES]
    msg = next(v.message for v in violations if v.rule == "max_sector_pct")
    assert "advisory" in msg


def test_the_engine_hard_blocks_past_the_absolute_ceiling():
    """The deterministic gate keeps the final word at the ceiling, so an order
    that never went through the constructor's sizing is still bounded."""
    with patch("src.execution.broker._get_sector", return_value="Technology"):
        violations = _engine().check(
            _buy("NVDA", 25.0),
            positions=[_held("AAPL", EQUITY * 0.55)],
            total_value=EQUITY, daily_pnl=0.0,
        )
    hard = [v for v in violations if v.rule in HARD_BLOCK_RULES]
    assert [v.rule for v in hard] == ["max_sector_hard_pct"]
    assert "60%" in hard[0].message


def test_a_constructor_sized_order_is_never_blocked_by_the_engine():
    """The relationship that makes the dial work: the constructor sizes with
    the SAME allowance function the engine gates on, so a scaled order arrives
    already inside it. A drift between the two would silently restore the
    veto for every crowded trade."""
    took_a_trade = False
    for held in (0, 25, 40, 45, 50, 55, 58, 59):
        decisions = _build(sector_held_pct=float(held), target_weight_pct=8.0)
        if not decisions:
            continue           # refused outright is a valid outcome, not a block
        took_a_trade = True
        positions = [_held("AAPL", EQUITY * held / 100)] if held else []
        with patch("src.execution.broker._get_sector", return_value="Technology"):
            violations = _engine().check(
                decisions[0], positions=positions,
                total_value=EQUITY, daily_pnl=0.0,
            )
        hard = [v.rule for v in violations if v.rule in HARD_BLOCK_RULES]
        assert not hard, (
            f"sector at {held}%: constructor sized {decisions[0].allocation_pct}% "
            f"but the engine hard-blocked it with {hard} — the two are reading "
            f"different books"
        )
    assert took_a_trade


def test_an_unknown_sector_is_exempt_exactly_as_it_was():
    """The engine has always skipped the sector cap for an unclassified
    symbol; the dial must not start rationing against exposure the gate does
    not measure."""
    with patch("src.execution.broker._get_sector", return_value="Unknown"):
        decisions = _constructor().construct_orders(
            targets=[TargetPosition(symbol="NVDA", target_weight_pct=8.0,
                                    conviction="high", thesis="t")],
            positions=[_held("AAPL", EQUITY * 0.75, sector="Unknown")],
            analyses=[_analysis("NVDA", entry=100, stop=95, target=140)],
            total_value=EQUITY, price_map={"NVDA": 100.0},
        )
    assert len(decisions) == 1
    assert decisions[0].allocation_pct == pytest.approx(8.0)


def test_a_hard_ceiling_below_the_target_is_rejected_as_config():
    """An incoherent pair would make the scaling band run backwards and fall
    back to gate behaviour silently."""
    with pytest.raises(ValueError, match="max_sector_hard_pct"):
        RiskConfig(
            max_position_pct=20, max_total_position_pct=90, max_daily_loss_pct=3,
            max_sector_pct=40, max_sector_hard_pct=30, require_stop_loss=True,
        )


def test_an_unset_ceiling_derives_from_the_target():
    """So an operator who moves the diversification target moves the ceiling
    with it instead of leaving the two inconsistent."""
    cfg = RiskConfig(
        max_position_pct=20, max_total_position_pct=90, max_daily_loss_pct=3,
        max_sector_pct=40, require_stop_loss=True,
    )
    assert cfg.sector_hard_ceiling_pct == 60.0


# ---------------------------------------------------------------------------
# 6. REGRESSION — nothing else moved. The main risk in this change.
# ---------------------------------------------------------------------------

def test_correlation_cluster_behaviour_is_unchanged():
    """A DIFFERENT mechanism (measured return correlation, not a sector
    label), and §10.3 must not have touched it: still fires at its own cap,
    still ADVISORY, still not a hard block.
    """
    positions = [
        _held("AAPL", 30_000.0, sector="Technology"),
        _held("MSFT", 25_000.0, sector="Technology"),
    ]
    corr = {
        "NVDA": {"AAPL": 0.95, "MSFT": 0.93},
        "AAPL": {"NVDA": 0.95, "MSFT": 0.9},
        "MSFT": {"NVDA": 0.93, "AAPL": 0.9},
    }
    with patch("src.execution.broker._get_sector", return_value="Technology"):
        violations = _engine().check(
            _buy("NVDA", 10.0), positions=positions,
            total_value=EQUITY, daily_pnl=0.0,
            correlation_matrix=corr, max_correlated_cluster_pct=50.0,
        )
    cluster = [v for v in violations if v.rule == "correlation_cluster"]
    assert len(cluster) == 1, "the cluster check must still fire"
    assert cluster[0].limit == 50.0
    assert "advisory" in cluster[0].message
    # It was never a hard block and must not have become one.
    assert "correlation_cluster" not in HARD_BLOCK_RULES

    # ...and it still fires on its own, with the sector cap wide open, which
    # is what proves the two mechanisms remain independent.
    with patch("src.execution.broker._get_sector", return_value="Technology"):
        violations = _engine(max_sector_pct=99.0, max_sector_hard_pct=100.0).check(
            _buy("NVDA", 10.0), positions=positions,
            total_value=EQUITY, daily_pnl=0.0,
            correlation_matrix=corr, max_correlated_cluster_pct=50.0,
        )
    assert [v.rule for v in violations] == ["correlation_cluster"]


def test_the_per_trade_and_single_name_ceilings_are_unchanged():
    """The 5% per-trade risk envelope and the 20% single-name notional cap are
    explicitly out of scope for §10.3 — only sector concentration changed from
    gate to dial."""
    # The single-name ceiling still HARD blocks, in an empty sector.
    with patch("src.execution.broker._get_sector", return_value="Technology"):
        violations = _engine(max_position_pct=20.0).check(
            _buy("NVDA", 25.0), positions=[],
            total_value=EQUITY, daily_pnl=0.0,
        )
    assert "max_position_pct" in [v.rule for v in violations]
    assert "max_position_pct" in HARD_BLOCK_RULES

    # And the constructor still sizes under the per-trade RISK budget, with
    # the sector dial inert, exactly as it did before: a 0.5% risk budget on
    # a $5-wide stop buys $500/$5 = 100 shares = $10k = 10% of equity, so a
    # 20% notional target is halved by the risk budget and by nothing else.
    constructor = PortfolioConstructor(ConstructorConfig(
        risk_budget_pct=0.5, max_position_pct=20.0,
        max_sector_pct=SOFT, max_sector_hard_pct=HARD,
    ))
    with patch("src.execution.broker._get_sector", return_value="Technology"):
        decisions = constructor.construct_orders(
            targets=[TargetPosition(symbol="NVDA", target_weight_pct=20.0,
                                    conviction="high", thesis="t")],
            positions=[], analyses=[_analysis("NVDA", entry=100, stop=95, target=140)],
            total_value=EQUITY, price_map={"NVDA": 100.0},
        )
    assert decisions[0].allocation_pct == pytest.approx(10.0)
    assert "risk budget" in decisions[0].reasoning
    assert "sector" not in decisions[0].reasoning.lower()


def test_the_portfolio_at_risk_ceiling_is_unchanged():
    """The 25% total at-risk ceiling is rationed by `allocate_risk_budget`,
    which §10.3 does not touch — the dial runs strictly after it and can only
    ever reduce further."""
    from src.risk.budget import allocate_risk_budget
    import inspect
    src = inspect.getsource(allocate_risk_budget)
    assert "sector" not in src.lower(), (
        "the risk budget allocator must stay free of sector logic — §10.3 "
        "lives in the constructor's sizing, not in the at-risk ceiling"
    )


def test_exits_are_untouched_by_the_dial():
    """Entries fail closed, exits fail open. A SELL or COVER must never be
    affected by how crowded its sector is."""
    for action in ("SELL", "COVER"):
        decision = TradeDecision(
            action=action, symbol="NVDA", allocation_pct=100.0,
            entry_price=0.0, stop_loss=0.0, take_profit=0.0, reasoning="exit",
        )
        with patch("src.execution.broker._get_sector", return_value="Technology"):
            violations = _engine().check(
                decision, positions=[_held("AAPL", EQUITY * 0.9)],
                total_value=EQUITY, daily_pnl=0.0,
            )
        assert violations == [], f"{action} must never be sector-gated"


# ---------------------------------------------------------------------------
# 7. Spec §12.2 — SEPARATE LONG AND SHORT SECTOR BUDGETS (the enforcement gate)
#
# Before §12.2 the gate summed SIGNED `market_value`, so a held SHORT made its
# sector look SMALLER and the long book could over-concentrate behind it. The
# comment above that summation said "gross ... unsigned"; the code was signed.
# ---------------------------------------------------------------------------

def _short(symbol: str, allocation_pct: float) -> TradeDecision:
    return TradeDecision(
        action="SHORT", symbol=symbol, allocation_pct=allocation_pct,
        entry_price=100.0, stop_loss=105.0, take_profit=80.0, reasoning="t",
    )


def test_a_held_short_no_longer_shrinks_its_sectors_measured_long_exposure():
    """THE §12.2 DEFECT, pinned directly.

    Technology holds 50% long and 20% short. Under the old SIGNED sum the
    sector measured 30%, so a further 15% BUY read as 45% — over the 40
    target here but comfortably inside the 60 ceiling. Measured on the LONG
    side alone it is 65%, past the ceiling, and refused.

    The short did not make the long book safer. It made it invisible.
    """
    positions = [
        _held("AAPL", EQUITY * 0.5, sector="Technology"),
        _held_short("INTC", EQUITY * 0.2, "Technology"),
    ]
    with patch("src.execution.broker._get_sector", return_value="Technology"):
        violations = _engine().check(
            _buy("NVDA", 15.0), positions=positions,
            total_value=EQUITY, daily_pnl=0.0,
        )

    advisory = next(v for v in violations if v.rule == "max_sector_pct")
    assert advisory.value == pytest.approx(65.0), (
        "the long side must measure 50 + 15, not 50 - 20 + 15"
    )
    assert "long" in advisory.message
    assert "max_sector_hard_pct" in [v.rule for v in violations]


def test_each_side_is_measured_against_the_limit_independently():
    """One sector, both sides held. Each is judged on its own budget, and a
    trade on the light side is not charged for the heavy one."""
    positions = [
        _held("AAPL", EQUITY * 0.5, sector="Technology"),
        _held_short("INTC", EQUITY * 0.1, "Technology"),
    ]
    with patch("src.execution.broker._get_sector", return_value="Technology"):
        long_violations = _engine().check(
            _buy("NVDA", 5.0), positions=positions,
            total_value=EQUITY, daily_pnl=0.0,
        )
        short_violations = _engine().check(
            _short("AMD", 5.0), positions=positions,
            total_value=EQUITY, daily_pnl=0.0,
        )

    # Long side: 50 + 5 = 55, over the 40 target (advisory), inside 60.
    long_advisory = next(v for v in long_violations if v.rule == "max_sector_pct")
    assert long_advisory.value == pytest.approx(55.0)
    # Short side: 10 + 5 = 15, nowhere near anything — untouched by the longs.
    assert [v.rule for v in short_violations] == []


def test_a_pending_short_does_not_consume_the_long_budget():
    """The batch accumulator is keyed `(sector, side)` too. A pending SHORT
    booked into the long bucket would ration the next BUY for crowding that
    is not there — and vice versa."""
    with patch("src.execution.broker._get_sector", return_value="Technology"):
        violations = _engine().check(
            _buy("NVDA", 20.0), positions=[],
            total_value=EQUITY, daily_pnl=0.0,
            pending_sector_investment={("Technology", "short"): EQUITY * 0.5},
        )
    assert [v.rule for v in violations] == [], (
        "a pending short is not long-side crowding"
    )


def test_the_gate_lets_the_pair_trade_through():
    """The owner's named case, at the gate rather than the constructor: long
    the leader and short the laggard in the same hot sector.

    Both legs at 35% — summed that is 70%, past the 60% ceiling this file
    configures, and gross summing would refuse the second leg. Split by side,
    each is one 35% position under a 40% target.

    The unrelated per-name and short-book ceilings are lifted here on purpose:
    they are separate controls with their own tests, and leaving them binding
    would let this test pass or fail for a reason that is not the sector split.
    """
    engine = _engine(
        max_position_pct=50.0, max_single_short_pct=50.0,
        max_gross_bearish_pct=100.0,
    )
    with patch("src.execution.broker._get_sector", return_value="Technology"):
        long_leg = engine.check(
            _buy("NVDA", 35.0), positions=[], total_value=EQUITY, daily_pnl=0.0,
        )
        short_leg = engine.check(
            _short("INTC", 35.0),
            positions=[_held("NVDA", EQUITY * 0.35, sector="Technology")],
            total_value=EQUITY, daily_pnl=0.0,
            pending_sector_investment={("Technology", "long"): EQUITY * 0.35},
        )
    assert [v.rule for v in long_leg] == []
    assert [v.rule for v in short_leg] == [], (
        "the long leg must not consume the short leg's budget — this is the "
        "trade gross summing was rejected for blocking"
    )


def test_an_inverse_etf_long_counts_as_LONG_side_exposure():
    """Spec §12.2 splits by POSITION SIDE, not by bullish/bearish thesis. A
    long position in an inverse ETF is long-side exposure in its sector; the
    separate `max_gross_bearish_pct` cap answers the directional question."""
    held = Position(
        symbol="SQQQ", qty=100, avg_entry=100.0, current_price=100.0,
        market_value=10_000.0, unrealized_pnl=0.0, sector="Broad",
    )
    by_side = sector_side_gross([held])
    # 3x leverage: $10k notional consumes $30k of sector budget, long side.
    assert by_side == {("Broad", "long"): 30_000.0}


# ---------------------------------------------------------------------------
# 8. Spec §12.2 — THE THREE IMPLEMENTATIONS MUST TELL THE SAME STORY
# ---------------------------------------------------------------------------

def test_gate_pm_facts_and_projection_report_the_same_sector_exposure():
    """Three independent implementations of "how much is this sector holding"
    is how the signed-vs-gross defect survived: the gate enforced one number,
    the PM read a second, and the pre-decision preview showed a third.

    They now share `sector_side_gross` / `sector_side_weights`. This test
    holds one book against all of them at once, so a future edit to any one
    fails here rather than in production.
    """
    from types import SimpleNamespace
    from src.pipeline import TradingPipeline

    positions = [
        _held("AAPL", EQUITY * 0.30, sector="Technology"),
        _held_short("INTC", EQUITY * 0.20, "Technology"),
        _held("XOM", EQUITY * 0.10, sector="Energy"),
    ]
    expected = {
        ("Technology", "long"): 30.0,
        ("Technology", "short"): 20.0,
        ("Energy", "long"): 10.0,
    }

    # (a) The gate. Read through the advisory violation it emits, which is the
    #     number the Risk Manager and the audit trail actually see. A 0%
    #     order adds nothing, so the figure is the held book alone.
    with patch("src.execution.broker._get_sector", return_value="Technology"):
        gate = _engine(max_sector_pct=1.0, max_sector_hard_pct=100.0).check(
            _buy("NVDA", 0.0), positions=positions,
            total_value=EQUITY, daily_pnl=0.0,
        )
    gate_long_tech = next(v for v in gate if v.rule == "max_sector_pct").value
    assert gate_long_tech == pytest.approx(expected[("Technology", "long")])

    # (b) The constructor's sizing view — the same helper, the same book.
    assert PortfolioConstructor._current_sector_weights(
        positions, EQUITY,
    ) == pytest.approx(expected)

    # (c) PMFacts — what the Portfolio Manager reads.
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.db = SimpleNamespace(
        compute_trade_calibration=lambda *a, **k: {},
        get_recent_agent_outputs=lambda *a, **k: [],
        get_recent_trades=lambda *a, **k: [],
    )
    facts = pipeline._build_pm_facts(
        positions=positions, analyses=[], total_value=EQUITY,
        cash=0.0, recent_performance={},
    )
    assert facts.sector_weights_long == {"Technology": 30.0, "Energy": 10.0}
    assert facts.sector_weights_short == {"Technology": 20.0}

    # (d) The projected-portfolio preview, before any candidate is added.
    pipeline._last_symbol_sectors = {}
    preview = pipeline._build_projected_portfolio(
        positions, [], total_value=EQUITY,
    )
    assert "Technology long 30%" in preview
    assert "Technology short 20%" in preview
    assert "Energy long 10%" in preview

    # And the raw shared helper agrees with all of them.
    assert sector_side_weights(positions, EQUITY) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# 9. Spec §12.3 — THE LIMIT IS 75, THE CEILING IS 90
# ---------------------------------------------------------------------------

def _production_risk_config() -> RiskConfig:
    """The real shipped `risk:` block, not a fixture — §12.3 is about the
    production number, so a hand-written 75 here would prove nothing.

    The YAML is parsed directly rather than through `load_config`, which also
    validates broker/LLM API keys a test environment does not have.
    """
    from pathlib import Path
    import yaml
    root = Path(__file__).resolve().parent.parent
    raw = yaml.safe_load((root / "config" / "settings.yaml").read_text())
    return RiskConfig(**raw["risk"])


def test_the_production_sector_limit_is_75_and_the_ceiling_is_90():
    """Spec §12.3: the owner chose 75 over the recommended 60.

    The 90 ceiling is NOT in the ratified §12.3 text — it was chosen when
    §12.3 was built, because 1.5x the target gives a meaningless 112.5 and a
    dial with no terminal bound bounds nothing. It is flagged for the owner
    to move, and asserted here so moving it is a deliberate act.
    """
    risk = _production_risk_config()
    assert risk.max_sector_pct == 75
    assert risk.sector_hard_ceiling_pct == 90


def test_the_derived_ceiling_is_capped_at_90_not_15x_the_target():
    """With the target at 75, 1.5x would give 112.5 — not a ceiling at all.
    The derivation is capped, and the old 40 -> 60 relationship is unchanged
    below the cap."""
    def _cfg(target: float) -> RiskConfig:
        return RiskConfig(
            max_position_pct=20, max_total_position_pct=90,
            max_daily_loss_pct=3, max_sector_pct=target, require_stop_loss=True,
        )
    assert _cfg(40).sector_hard_ceiling_pct == 60.0    # unchanged
    assert _cfg(75).sector_hard_ceiling_pct == 90.0    # capped, not 112.5
    assert _cfg(100).sector_hard_ceiling_pct == 100.0  # never below the target


def test_at_the_production_limit_crossing_75_scales_and_past_90_refuses():
    """§12.3's two behaviours in one book, at the shipped numbers.

    Sector at 80% long — over the 75 target, so the next long is SCALED, not
    refused. Sector at 92% long — past the 90 ceiling, so it is refused.
    """
    risk = _production_risk_config()
    constructor = PortfolioConstructor(ConstructorConfig(
        max_sector_pct=risk.max_sector_pct,
        max_sector_hard_pct=risk.sector_hard_ceiling_pct,
        min_order_usd=500.0,
    ))

    scaled = _build(80.0, target_weight_pct=8.0, constructor=constructor)
    assert len(scaled) == 1, "over the target is SCALED, never refused"
    assert 0 < scaled[0].allocation_pct < 8.0
    assert "75% concentration target" in scaled[0].reasoning

    refused = _build(92.0, target_weight_pct=8.0, constructor=constructor)
    assert refused == [], "past the 90% absolute ceiling the answer is no"


def test_the_engine_hard_blocks_past_90_at_the_production_numbers():
    """The gate, not just the constructor — an order that reaches the engine
    without constructor sizing is still refused past the ceiling."""
    risk = _production_risk_config()
    engine = _engine(
        max_sector_pct=risk.max_sector_pct,
        max_sector_hard_pct=risk.sector_hard_ceiling_pct,
    )
    with patch("src.execution.broker._get_sector", return_value="Technology"):
        violations = engine.check(
            _buy("NVDA", 10.0),
            positions=[_held("AAPL", EQUITY * 0.85, sector="Technology")],
            total_value=EQUITY, daily_pnl=0.0,
        )
    hard = [v for v in violations if v.rule == "max_sector_hard_pct"]
    assert hard, "95% long in one sector must be hard-blocked"
    assert hard[0].rule in HARD_BLOCK_RULES


def test_the_75_percent_cost_is_stated_where_a_decision_maker_reads_it():
    """§12.3 requires the cost be stated honestly rather than buried: at 75%
    in one sector an ordinary 20% sector drawdown costs 15% of equity.

    docs/WORK.md item 32 (2026-09-04): the daily-loss circuit breaker was
    rescaled from a flat 3% to 15% (= 3x the ratified 5% real per-trade
    risk unit), so the sector-drawdown cost now sits AT that breaker, not
    five times under it as it did when the breaker was still 3%. The prompt
    text was corrected to match rather than left stating a now-false
    relationship.

    Asserted in the PM prompt because that is where the decision is actually
    made — a consequence recorded only in a spec nobody reads at decision
    time is not stated.
    """
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    prompt = (root / "config" / "prompts" / "portfolio_manager.md").read_text()
    assert "15% of equity" in prompt
    assert "15% daily-loss circuit breaker" in prompt
    assert "not a hedge" in prompt, (
        "the PM must also be told long and short sector budgets are separate"
    )
