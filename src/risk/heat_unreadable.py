"""Portfolio heat that knows a stop could not be READ, as distinct from absent."""

from __future__ import annotations

from dataclasses import dataclass

from src.risk.metrics import PortfolioHeat, portfolio_heat


@dataclass(frozen=True)
class HeatWithUnreadable(PortfolioHeat):
    unreadable: frozenset[str] = frozenset()

    @property
    def unprotected(self) -> list[str]:
        return sorted(p.symbol for p in self.per_position if not p.protected and p.symbol not in self.unreadable)

    def unreadable_note(self) -> str:
        return (
            "- STOP COULD NOT BE READ from the broker (one may well exist; do "
            "NOT treat as unprotected; charged at full notional until it can "
            "be read): " + ", ".join(sorted(self.unreadable))
        )


def portfolio_heat_with_unreadable(*args, unreadable_stops=(), **kwargs):
    """`portfolio_heat`, with symbols whose stop read failed set apart."""
    h = portfolio_heat(*args, **kwargs)
    return HeatWithUnreadable(h.equity, h.per_position, frozenset(str(s).upper() for s in unreadable_stops))
