"""Owner ruling 2026-10-09: whole exits only.

A sell/cover below the held weight is NOT a "trim" with its own evidence
rule: the fact-checker judges it as a full close by order side. Sell of a
long: bearish supports, bullish conflicts. Cover of a short: bullish supports,
bearish conflicts.
"""

import pytest

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.models import PortfolioDecision, Position
from tests.test_pm_grounding import _earnings, _held_long


def _held_short(symbol: str) -> Position:
    return Position(
        symbol=symbol,
        qty=-100.0,
        avg_entry=230.0,
        current_price=229.0,
        market_value=-22_900.0,
        unrealized_pnl=100.0,
        sector="Technology",
    )


def _errors(symbol, held, direction, stance, relationship):
    decision = PortfolioDecision.model_validate(
        {
            "reasoning_chain": {
                k: "x"
                for k in (
                    "macro_filter",
                    "news_check",
                    "earnings_check",
                    "signal_conflicts",
                    "sizing_logic",
                    "portfolio_balance",
                    "cash_target",
                )
            },
            "targets": [
                {
                    "symbol": symbol,
                    "direction": direction,
                    "risk_allocation_pct": 1.0,  # below the 1.91 held: a "partial"
                    "conviction": "medium",
                    "thesis": "below-weight exit",
                    "provenance": [
                        {
                            "source": "earnings",
                            "observed_stance": stance,
                            "relationship": relationship,
                            "evidence": "earnings",
                        }
                    ],
                }
            ],
            "portfolio_view": "exit",
        }
    )
    return PortfolioManagerAgent.validate_grounding(
        decision,
        analyses=[],
        positions=[held],
        news_intel=None,
        earnings_analyses=[_earnings(symbol, stance)],
        macro_analysis=None,
        total_value=100_000,
        existing_risk_pct={symbol: 1.91},
    )


@pytest.mark.parametrize(
    "held_fn,direction,good,bad",
    [(_held_long, "long", "bearish", "bullish"), (_held_short, "short", "bullish", "bearish")],
)
def test_below_weight_exit_is_judged_as_full_close(held_fn, direction, good, bad):
    held = held_fn("AAPL")
    # Evidence for the exit's own side supports it.
    assert _errors("AAPL", held, direction, good, "supports") == []
    # Evidence for keeping the position can no longer "support" a partial exit.
    assert _errors("AAPL", held, direction, bad, "supports") != []
    # ...and recorded as a conflict it is accepted, as for a full close.
    assert _errors("AAPL", held, direction, bad, "conflicts") == []
    # Exit-side evidence tagged as conflicting is a false claim.
    assert _errors("AAPL", held, direction, good, "conflicts") != []
