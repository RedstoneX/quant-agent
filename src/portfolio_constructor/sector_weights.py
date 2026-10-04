"""Sector bookkeeping of `PortfolioConstructor`, moved out of `__init__.py`
verbatim. `_current_sector_weights` is a pure function of (positions,
total_value); `_note_reducing_order_sectors` touches only
`self.last_order_sectors`, so both run against a `SimpleNamespace` stub.
"""
from __future__ import annotations

from src.models import Position


def _note_reducing_order_sectors(self, orders) -> None:
    """Board item 224: a session that only REDUCED still records the
    `(sector, side)` of what it built. Entries are noted while sizing;
    SELL/COVER orders never pass through sizing, so resolve theirs here
    with the same lookup and the same None-means-unknown rule."""
    from src.sector_reference import _get_sector
    for d in (orders or ()):
        if getattr(d, "action", None) in ("SELL", "COVER"):
            sector = _get_sector(d.symbol)
            self.last_order_sectors[d.symbol] = (
                sector if sector and sector != "Unknown" else None
            )


def _current_sector_weights(
    positions: list[Position], total_value: float,
) -> dict[tuple[str, str], float]:
    """Held GROSS exposure per `(sector, side)`, as % of equity.

    Spec §10.3 (the dial) and §12.2 (the split). Calls the SAME
    `sector_side_weights` that `RiskRuleEngine.check` measures with,
    rather than restating the arithmetic — the constructor sizing against
    a different book than the gate measures is how a scaled order gets
    blocked anyway, and three hand-written copies of this sum is how the
    signed-vs-gross defect survived.

    §12.2 CORRECTION: `market_value` used to be summed SIGNED, so a held
    short REDUCED its sector's measured weight even though the engine's
    own comment on that block claimed "gross ... unsigned magnitude". It
    is now an unsigned magnitude booked to the short side's own budget.
    A long and a short in the same sector do not offset.

    Sector is read off the POSITION's own `sector` field rather than
    `_get_sector(symbol)` — the engine sums held positions the first way
    and resolves only the CANDIDATE symbol the second way, so
    `_apply_sector_dial` does the same.
    """
    from src.risk.rules import sector_side_weights
    return sector_side_weights(positions, total_value)
