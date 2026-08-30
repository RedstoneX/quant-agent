"""Gross BEARISH exposure ceiling (2026-08-30).

The defect: `RiskRuleEngine.check`'s book-wide short-exposure cap
(`max_short_gross_pct`, now `max_gross_bearish_pct`) summed only true
shorts (`p.qty < 0`), and was only ever reached when `decision.action ==
"SHORT"`. Four inverse ETFs (`SH` -1x, `SDS` -2x, `PSQ` -1x, `SQQQ` -3x —
see `_ETF_LEVERAGE` in `src/risk/rules.py`) are in the tradeable universe,
and a LONG position in one of them is bearish exposure — but it is a BUY
with `qty > 0`, so it was invisible to both the sum and the gate. The desk
could sit at the full 20% gross-short ceiling AND hold a full inverse-ETF
position simultaneously and be materially more bearish than the 20% limit
intends — worse at leverage, since a modest SQQQ notional is a large -3x
bearish bet.

The owner ratified (2026-08-30) that the inverse ETFs stay in the
universe, so the fix widens the ceiling's arithmetic and its gate rather
than removing them:

  1. the sum now also includes LONG positions whose signed
     `_effective_multiplier` is negative;
  2. the gate now fires on a SHORT of any symbol, OR a BUY whose
     `_effective_multiplier` is negative — not only `action == "SHORT"`.

`max_single_short_pct` (the tighter single-name cap) is deliberately NOT
extended to inverse-ETF BUYs — see
`test_single_name_short_cap_still_short_only_not_extended_to_inverse_etf_buy`
for why, and `src/risk/rules.py`'s comment at that rule.
"""

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from src.config import RiskConfig
from src.models import Position, TradeDecision
from src.pipeline import TradingPipeline
from src.risk.rules import RiskRuleEngine


def _cfg(**kw) -> RiskConfig:
    base = dict(
        max_position_pct=80.0, max_total_position_pct=300.0,
        max_daily_loss_pct=3.0, max_sector_pct=90.0,
        require_stop_loss=True, allow_margin=True,
    )
    base.update(kw)
    return RiskConfig(**base)


def _pos(symbol: str, qty: float, entry: float, price: float,
         sector: str = "Broad") -> Position:
    return Position(
        symbol=symbol, qty=qty, avg_entry=entry, current_price=price,
        market_value=qty * price, unrealized_pnl=qty * (price - entry),
        sector=sector,
    )


def _buy(symbol: str, allocation_pct: float, entry: float = 20.0) -> TradeDecision:
    return TradeDecision(
        action="BUY", symbol=symbol, allocation_pct=allocation_pct,
        entry_price=entry, stop_loss=entry * 0.9, take_profit=entry * 1.2,
        reasoning="t",
    )


def _short(symbol: str, allocation_pct: float, entry: float = 100.0) -> TradeDecision:
    return TradeDecision(
        action="SHORT", symbol=symbol, allocation_pct=allocation_pct,
        entry_price=entry, stop_loss=entry * 1.1, take_profit=entry * 0.8,
        reasoning="t",
    )


# ==========================================================================
# 1. An inverse-ETF LONG alone breaches the ceiling and is hard-blocked.
#    This is the core defect: on origin/main (pre-fix), the gate here was
#    `if is_short:` only, so a BUY never reached this check at all and
#    `violations` would be empty regardless of size.
# ==========================================================================

def test_inverse_etf_buy_alone_breaches_the_gross_bearish_ceiling():
    """SQQQ (-3x) BUY at 10% allocation = 30% gross bearish notional on its
    own, over the 20% default ceiling — with NO other position in the book
    and NOT a SHORT. Pre-fix this decision produced zero violations."""
    engine = RiskRuleEngine(_cfg(max_gross_bearish_pct=20.0))
    decision = _buy("SQQQ", allocation_pct=10.0)
    violations = engine.check(
        decision=decision, positions=[], total_value=100_000, daily_pnl=0.0,
    )
    rules = {v.rule for v in violations}
    assert "max_gross_bearish_pct" in rules, (
        f"a lone inverse-ETF BUY exceeding the gross bearish ceiling must "
        f"hard-block; got {rules}"
    )
    violation = next(v for v in violations if v.rule == "max_gross_bearish_pct")
    assert violation.value == pytest.approx(30.0)
    assert violation.limit == 20.0


def test_inverse_etf_buy_alone_is_hard_blocked_by_the_pipeline_filter():
    """Same scenario, exercised through `TradingPipeline._filter_hard_risk_decisions`
    — proves `max_gross_bearish_pct` is actually wired into HARD_BLOCK_RULES,
    not just returned as an advisory `RiskViolation`."""
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.risk_engine = RiskRuleEngine(_cfg(max_gross_bearish_pct=20.0))
    decisions = [_buy("SQQQ", allocation_pct=10.0)]

    with patch("src.pipeline._get_sector", return_value="Broad"), patch(
        "src.execution.broker._get_sector", return_value="Broad"
    ):
        allowed, violations, blocked = pipeline._filter_hard_risk_decisions(
            decisions, positions=[], total_value=100_000, daily_pnl=0,
        )

    assert allowed == []
    assert any("gross bearish" in b.lower() for b in blocked)


# ==========================================================================
# 2. A true short plus an inverse-ETF long: each passes alone, together
#    they breach.
# ==========================================================================

def test_true_short_and_inverse_etf_buy_each_pass_alone_but_breach_together():
    engine = RiskRuleEngine(_cfg(max_gross_bearish_pct=20.0, max_single_short_pct=50.0))

    # Held: a 12% true short (gross_mul 1x) — alone, well under 20%.
    positions = [_pos("XYZ", qty=-120, entry=100, price=100)]  # -$12,000 = 12%
    short_alone = engine.check(
        decision=_short("XYZ", allocation_pct=0.0), positions=positions,
        total_value=100_000, daily_pnl=0.0,
    )
    assert "max_gross_bearish_pct" not in {v.rule for v in short_alone}

    # The SQQQ BUY alone (against a clean book) is also under 20%: 3% raw *
    # 3x gross = 9%.
    buy_alone = engine.check(
        decision=_buy("SQQQ", allocation_pct=3.0), positions=[],
        total_value=100_000, daily_pnl=0.0,
    )
    assert "max_gross_bearish_pct" not in {v.rule for v in buy_alone}

    # Together — the held 12% short PLUS the new 9% SQQQ BUY = 21% — breach.
    combined = engine.check(
        decision=_buy("SQQQ", allocation_pct=3.0), positions=positions,
        total_value=100_000, daily_pnl=0.0,
    )
    rules = {v.rule for v in combined}
    assert "max_gross_bearish_pct" in rules, (
        f"held short (12%) + new inverse-ETF BUY (9%) = 21% must breach the "
        f"20% ceiling even though each alone passes; got {rules}"
    )


# ==========================================================================
# 3. Two inverse-ETF BUYs in the same batch see each other via
#    `pending_gross_bearish_investment`.
# ==========================================================================

def test_two_inverse_etf_buys_in_one_batch_see_each_other_via_the_pending_accumulator():
    """SQQQ 4% (gross 12%) then PSQ 10% (gross 10%) — each passes checked
    alone against an empty book, but 12% + 10% = 22% breaches 20%. Without
    the pending accumulator, the second BUY would be checked only against
    the (still empty) real book and wrongly pass."""
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.risk_engine = RiskRuleEngine(_cfg(max_gross_bearish_pct=20.0))
    decisions = [
        _buy("SQQQ", allocation_pct=4.0),   # 4% * 3x = 12% gross
        _buy("PSQ", allocation_pct=10.0),   # 10% * 1x = 10% gross -> 22% combined
    ]

    with patch("src.pipeline._get_sector", return_value="Broad"), patch(
        "src.execution.broker._get_sector", return_value="Broad"
    ):
        allowed, violations, blocked = pipeline._filter_hard_risk_decisions(
            decisions, positions=[], total_value=100_000, daily_pnl=0,
        )

    assert [d.symbol for d in allowed] == ["SQQQ"]
    assert any("gross bearish" in b.lower() for b in blocked)


def test_two_shorts_and_an_inverse_etf_buy_all_share_one_accumulator():
    """The accumulator is genuinely BEARISH, not short-only or ETF-only —
    a SHORT and an inverse-ETF BUY in the same batch must see each other
    too, not just same-type pairs."""
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.risk_engine = RiskRuleEngine(_cfg(max_gross_bearish_pct=20.0, max_single_short_pct=50.0))
    decisions = [
        _short("XYZ", allocation_pct=12.0),  # 12% * 1x = 12% gross
        _buy("SQQQ", allocation_pct=3.0),    # 3% * 3x = 9% gross -> 21% combined
    ]

    with patch("src.pipeline._get_sector", return_value="Broad"), patch(
        "src.execution.broker._get_sector", return_value="Broad"
    ):
        allowed, violations, blocked = pipeline._filter_hard_risk_decisions(
            decisions, positions=[], total_value=100_000, daily_pnl=0,
        )

    assert [d.symbol for d in allowed] == ["XYZ"]
    assert any("gross bearish" in b.lower() for b in blocked)


# ==========================================================================
# 4. Leverage applied correctly: SQQQ (3x) consumes 3x, PSQ (1x) consumes 1x.
# ==========================================================================

def test_leverage_is_applied_correctly_sqqq_3x_vs_psq_1x():
    engine = RiskRuleEngine(_cfg(max_gross_bearish_pct=20.0))

    # SQQQ -3x: 10% raw -> 30% gross bearish -> breaches 20%.
    sqqq_violations = engine.check(
        decision=_buy("SQQQ", allocation_pct=10.0), positions=[],
        total_value=100_000, daily_pnl=0.0,
    )
    sqqq_rules = {v.rule for v in sqqq_violations}
    assert "max_gross_bearish_pct" in sqqq_rules
    sqqq_v = next(v for v in sqqq_violations if v.rule == "max_gross_bearish_pct")
    assert sqqq_v.value == pytest.approx(30.0)

    # PSQ -1x: the SAME 10% raw allocation -> 10% gross bearish -> passes.
    psq_violations = engine.check(
        decision=_buy("PSQ", allocation_pct=10.0), positions=[],
        total_value=100_000, daily_pnl=0.0,
    )
    assert "max_gross_bearish_pct" not in {v.rule for v in psq_violations}, (
        "PSQ is -1x — the same 10% allocation that breaches for 3x SQQQ "
        "must NOT breach for 1x PSQ"
    )


def test_a_new_inverse_etf_added_to_the_leverage_table_is_picked_up_automatically():
    """The ceiling's bearish test is `_effective_multiplier(symbol) < 0`, not
    a hardcoded ticker list — a fund added to `_ETF_LEVERAGE` later must be
    covered with no other code change."""
    import src.risk.rules as rules_mod

    engine = RiskRuleEngine(_cfg(max_gross_bearish_pct=20.0))
    with patch.dict(rules_mod._ETF_LEVERAGE, {"FAKEBEAR": -2.0}):
        violations = engine.check(
            decision=_buy("FAKEBEAR", allocation_pct=15.0), positions=[],
            total_value=100_000, daily_pnl=0.0,
        )
    rules = {v.rule for v in violations}
    assert "max_gross_bearish_pct" in rules, (
        "15% raw * -2x = 30% gross bearish on a symbol only just added to "
        "_ETF_LEVERAGE must still hard-block — nothing else should need to "
        "change for a new inverse fund to be covered"
    )


# ==========================================================================
# 5. A COVER is never blocked (the existing exemption survives).
# ==========================================================================

def test_cover_is_never_blocked_by_the_gross_bearish_ceiling():
    engine = RiskRuleEngine(_cfg(max_gross_bearish_pct=20.0, max_single_short_pct=10.0))
    # A short position already far over both ceilings.
    positions = [_pos("SQQQ", qty=-500, entry=100, price=100)]  # -$50,000 = 50%
    decision = TradeDecision(
        action="COVER", symbol="SQQQ", allocation_pct=50.0,
        entry_price=0.0, stop_loss=0.0, take_profit=0.0, reasoning="reduce",
    )
    violations = engine.check(
        decision=decision, positions=positions, total_value=100_000, daily_pnl=0.0,
    )
    assert violations == []


# ==========================================================================
# 6. An ordinary long is completely unaffected — a no-op wall.
# ==========================================================================

def test_ordinary_long_buy_never_trips_the_gross_bearish_ceiling():
    """MSFT is not in `_ETF_LEVERAGE` (multiplier +1.0) — it must never be
    gated by `max_gross_bearish_pct`, even against a book that is already
    heavily bearish, and even at a large allocation."""
    engine = RiskRuleEngine(_cfg(max_gross_bearish_pct=20.0, max_position_pct=90.0))
    # Book already sits at 25% short gross — well over the ceiling for any
    # bearish decision, but MSFT is not one.
    positions = [_pos("XYZ", qty=-250, entry=100, price=100)]  # -$25,000 = 25%
    decision = _buy("MSFT", allocation_pct=10.0, entry=400.0)
    violations = engine.check(
        decision=decision, positions=positions, total_value=100_000, daily_pnl=0.0,
    )
    rules = {v.rule for v in violations}
    assert "max_gross_bearish_pct" not in rules, (
        f"an ordinary long BUY must never be gated by the bearish ceiling; "
        f"got {rules}"
    )


def test_single_name_short_cap_still_short_only_not_extended_to_inverse_etf_buy():
    """Deliberate asymmetry (item 4): `max_single_short_pct` stays gated on
    `is_short` only. An inverse-ETF BUY large enough to trip it AS IF it
    were a short must NOT trip `max_single_short_pct` — a short's loss is
    unbounded, an inverse-ETF long's is bounded at the position value, so
    it does not earn that specific tighter treatment. It is still caught by
    `max_gross_bearish_pct` (and, separately, `max_position_pct`)."""
    engine = RiskRuleEngine(_cfg(
        max_single_short_pct=10.0, max_gross_bearish_pct=90.0, max_position_pct=90.0,
    ))
    # SQQQ 10% raw * 3x = 30% gross — well over a hypothetical 10%
    # single-short-style cap, but this is a BUY, not a SHORT.
    decision = _buy("SQQQ", allocation_pct=10.0)
    violations = engine.check(
        decision=decision, positions=[], total_value=100_000, daily_pnl=0.0,
    )
    rules = {v.rule for v in violations}
    assert "max_single_short_pct" not in rules, (
        "max_single_short_pct must remain SHORT-only; it must not fire for "
        "an inverse-ETF BUY no matter how large"
    )


# ==========================================================================
# 7. The old config key fails loudly instead of being silently ignored.
# ==========================================================================

def test_old_config_key_max_short_gross_pct_raises_a_clear_error():
    with pytest.raises(ValidationError) as exc_info:
        RiskConfig(
            max_position_pct=20.0, max_total_position_pct=90.0,
            max_daily_loss_pct=3.0, max_sector_pct=40.0,
            require_stop_loss=True, max_short_gross_pct=20.0,
        )
    message = str(exc_info.value)
    assert "max_short_gross_pct" in message
    assert "max_gross_bearish_pct" in message
