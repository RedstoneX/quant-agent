"""Pipeline-layer binding of the holding-discipline claim check to the PM
agent's state-change parser.

`src/risk/exit_guard_claims.py` takes the parser as a required argument so the
guard never imports an agent. Callers in the pipeline layer, which may import
agents, use these wrappers to supply the PM agent's parser; passing
`state_change_parser=` explicitly still overrides it.
"""

from __future__ import annotations

from src.risk.exit_guard_claims import (
    HoldingDisciplineClaimCheck,
    holding_discipline_claim_check as _claim_check,
    holding_discipline_false_claim as _false_claim,
)

__all__ = ["holding_discipline_claim_check", "holding_discipline_false_claim"]


def _with_pm_parser(kwargs: dict) -> dict:
    from src.agents.portfolio_manager import PortfolioManagerAgent

    kwargs.setdefault(
        "state_change_parser",
        PortfolioManagerAgent._state_change_symbols_by_date,
    )
    return kwargs


def holding_discipline_claim_check(**kwargs) -> HoldingDisciplineClaimCheck:
    return _claim_check(**_with_pm_parser(kwargs))


def holding_discipline_false_claim(**kwargs) -> str | None:
    return _false_claim(**_with_pm_parser(kwargs))
