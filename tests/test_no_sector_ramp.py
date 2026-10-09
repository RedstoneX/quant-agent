"""The sector sizing ramp (75% target / 90% ceiling on sector LABELS) is gone.

Deleted 2026-10-09: it counted sector labels on notional, a number nothing
measured. Related-stock concentration is bounded by the measured correlation
cluster cap (`src/risk/budget.py`: a cluster may take 40% of the 25% total
risk ceiling = 10% of equity at risk), which must still bind.
"""

from __future__ import annotations

from unittest.mock import patch

from src.config import RiskConfig
from src.models import Position, TradeDecision
from src.portfolio_constructor.config import ConstructorConfig
from src.risk.budget import RiskRequest, allocate_risk_budget
from src.risk.rules import HARD_BLOCK_RULES, RiskRuleEngine

EQUITY = 100_000.0


def _held(symbol: str, market_value: float) -> Position:
    return Position(
        symbol=symbol,
        qty=market_value / 100.0,
        avg_entry=100.0,
        current_price=100.0,
        market_value=market_value,
        unrealized_pnl=0.0,
        sector="Technology",
    )


def test_a_one_sector_heavy_book_inside_every_other_budget_is_not_refused_or_scaled():
    # 85% of equity already in Technology across four names, adding a fifth
    # Technology name at 10%: 95% in one sector. Single-name 10% < 65%, gross
    # 0.95x < 2.0x, net 95% < 200%. The old ramp hard-blocked this (90%
    # ceiling); nothing measured says it should be.
    engine = RiskRuleEngine(
        RiskConfig(
            max_position_pct=65.0,
            max_total_position_pct=200.0,
            max_gross_exposure_x=2.0,
            require_stop_loss=True,
            allow_margin=True,
        )
    )
    positions = [_held(s, 21_250.0) for s in ("AAPL", "MSFT", "NVDA", "AVGO")]
    decision = TradeDecision(
        action="BUY",
        symbol="AMD",
        allocation_pct=10.0,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=120.0,
        reasoning="t",
    )
    with patch("src.execution.broker._get_sector", return_value="Technology"):
        violations = engine.check(decision=decision, positions=positions, total_value=EQUITY, cash=EQUITY)

    assert not [v for v in violations if v.rule in HARD_BLOCK_RULES], [v.rule for v in violations]
    assert not [v for v in violations if "sector" in v.rule], [v.rule for v in violations]
    # Nothing left for the constructor to scale a crowded sector against.
    assert not any("sector" in f for f in ConstructorConfig.__dataclass_fields__)
    assert not any("sector" in r for r in HARD_BLOCK_RULES)


def test_a_correlated_cluster_is_still_capped_at_ten_percent_of_equity_at_risk():
    names = ["AAPL", "MSFT", "NVDA"]
    out = allocate_risk_budget(
        [RiskRequest(symbol=s, requested_pct=5.0) for s in names],
        clusters=[names],
        ceiling_pct=25.0,
        cluster_share_pct=40.0,
    )
    granted = sum(out.granted(s) for s in names)
    assert granted <= 10.0 + 1e-9
    assert granted > 0.0
    assert granted < 15.0  # 3 x 5% asked; the cluster cap, not the total, bound it
