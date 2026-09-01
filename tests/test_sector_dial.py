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
    RiskRuleEngine, sector_allowance_pct, sector_size_scale,
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
    return TechAnalysisResult(
        symbol=symbol, rating="strong_buy", entry_price=entry,
        stop_loss=stop, reference_target=target, reasoning="test",
        support_levels=[stop], resistance_levels=[target],
        setup_type="range", expected_horizon_sessions=10,
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


def test_a_short_in_a_crowded_sector_is_also_scaled_not_refused():
    """The sector budget is GROSS — direction-agnostic — so a short crowds its
    sector exactly as a long does, and must be scaled for crowding exactly as
    a long is rather than vetoed."""
    constructor = _constructor()
    analysis = TechAnalysisResult(
        symbol="XOM", rating="sell", entry_price=100.0,
        stop_loss=105.0, reference_target=80.0, reasoning="test",
        support_levels=[80.0], resistance_levels=[105.0],
        setup_type="range", expected_horizon_sessions=10,
        reasoning_chain=_tech_rc(),
    )
    with patch("src.execution.broker._get_sector", return_value="Energy"):
        decisions = constructor.construct_orders(
            targets=[TargetPosition(
                symbol="XOM", target_weight_pct=8.0, direction="short",
                conviction="high", thesis="breaking down",
            )],
            positions=[_held("CVX", EQUITY * 0.5, sector="Energy")],
            analyses=[analysis],
            total_value=EQUITY, price_map={"XOM": 100.0},
        )

    assert len(decisions) == 1, "a crowded sector must not veto a short either"
    d = decisions[0]
    assert d.action == "SHORT"
    assert 0 < d.allocation_pct < 8.0
    assert "Energy" in d.reasoning


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
