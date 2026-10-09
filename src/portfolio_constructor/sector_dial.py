"""Current position weights and per-order sector bookkeeping, lifted out of `__init__`.

The sector-crowding dial that used to live here (a 75% target / 90% ceiling
ramp on sector LABELS) was deleted 2026-10-09: related-stock concentration is
bounded by the correlation cluster cap in `src/risk/budget.py`.
`_accrue_sector` mutates the caller's `last_order_sectors` dict in place.
"""

from __future__ import annotations

import logging

from src.models import Position, TradeDecision

# The constructor's own logger, so log capture and filters keep matching.
logger = logging.getLogger("src.portfolio_constructor")


def _current_weights(
    positions: list[Position],
    total_value: float,
) -> dict[str, float]:
    """Current-position weights as gross-leverage percentages.

    Uses the same `_gross_multiplier` convention as
    `RiskRuleEngine.check` (risk/rules.py:28). For inverse / leveraged
    ETFs (SH=−1x, SDS=−2x, PSQ=−1x, SQQQ=−3x) the gross multiplier
    is the unsigned magnitude — a $10K SQQQ position consumes 30%
    gross notional, not 10% raw, exactly as the risk engine
    evaluates it.

    Pre-fix this used raw `market_value / total_value`, so a PM
    target_weight_pct=20 on SQQQ (intended as the 20% single-name
    cap) computed as 20% raw in the constructor but 60% gross at
    the engine — the engine then hard-blocked every leveraged-ETF
    target at the ceiling, while the constructor's delta math saw
    no trim needed. Now constructor + engine agree on the
    semantics: target_weight_pct IS gross-leverage percentage.
    """
    if total_value <= 0:
        return {}
    # Local import to avoid the cyclic risk -> portfolio_constructor
    # import chain at module load.
    from src.risk.rules import position_weight_pct

    # SIGNED, not absolute. A short has a negative qty and a negative
    # market_value (Alpaca convention), so it lands in the map as a
    # NEGATIVE weight. Signed is the correct choice because every consumer
    # of this map does exposure arithmetic, not magnitude arithmetic:
    #   - the delta loop computes `target_pct - current_pct`, and only the
    #     signed form makes "held -8%, want 0%" read as +8% of buying to
    #     do rather than 8% of selling;
    #   - the close test `target_pct == 0 and current_pct > 0` must NOT
    #     fire for a short, because a SELL on a short adds to it;
    #   - `_build_sell` already refuses `current_pct <= 0`, so a short is
    #     structurally excluded from the sell path rather than mis-sized.
    # An absolute weight would make a short indistinguishable from a long
    # of the same size at exactly the places where the direction is the
    # whole question. The previous `p.qty > 0` filter dropped shorts from
    # the map entirely, so `current_weights.get(sym, 0.0)` reported a held
    # short as unheld and the delta loop would re-open it every session.
    return {p.symbol: position_weight_pct(p, total_value) for p in positions if p.qty != 0}


def _accrue_sector(
    last_order_sectors,
    sector_weights: dict[tuple[str, str], float],
    decision: TradeDecision,
) -> None:
    """Book an order's GROSS sector consumption so the NEXT order in the
    same batch sees a book that already contains it.

    Without this, three targets in one crowded sector would each be sized
    against the same stale starting weight and collectively breach the
    ceiling — the identical accumulator the pipeline's risk filter keeps
    in `pending_sector_investment`, for the identical reason.
    """
    from src.risk.rules import _gross_multiplier, decision_side
    from src.sector_reference import _get_sector

    if decision.action not in ("BUY", "SHORT"):
        return
    sector = _get_sector(decision.symbol)
    # Board item 224 recording: keep what this lookup said, including
    # that it said nothing. None means "could not determine", and the
    # realised-weights row stores it as NULL rather than as a bucket.
    last_order_sectors[decision.symbol] = sector if sector and sector != "Unknown" else None
    if not sector or sector == "Unknown":
        return
    # Spec §12.2 — into THIS order's side. A SHORT booked into the long
    # bucket would shrink the next long for crowding that is not there.
    key = (sector, decision_side(decision.action))
    sector_weights[key] = sector_weights.get(key, 0.0) + (decision.allocation_pct * _gross_multiplier(decision.symbol))
