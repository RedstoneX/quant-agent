"""The sector-crowding dial and the weights it reads, lifted out of `__init__`.

Bodies moved verbatim from `PortfolioConstructor`; the config and the refusal
recorder are passed in, and `_accrue_sector` mutates the caller's
`last_order_sectors` dict in place.
"""

from __future__ import annotations

import logging

from src.models import Position, TradeDecision
from src.portfolio_constructor.config import STOP_REFUSAL_SECTOR_AT_HARD_CEILING

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


def _apply_sector_dial(
    cfg,
    note_refusal,
    symbol: str,
    allocation_pct: float,
    *,
    sector_weights: dict[tuple[str, str], float],
    total_value: float,
    action: str = "BUY",
) -> tuple[float, str]:
    """Spec §10.3. Shrink a crowded sector's next trade instead of vetoing it.

    Returns `(allocation_pct, note)`. `allocation_pct` is RAW notional
    percent (the units every downstream consumer spends); the sector
    budget is GROSS, so the conversion happens here exactly once, the
    same way the single-name clamp above does it.

    Spec §12.2: the budget consulted is the one for THIS ORDER'S SIDE.
    A crowded long book in a sector does not shrink a short into it, and
    the reverse — "a long and a short in the same sector is not a hedge",
    so neither is it a shared budget. This is what keeps the owner's pair
    trade (long the leader, short the laggard in one hot sector) legal.

    Returns a NEGATIVE allocation to mean "refuse" — either the sector is
    at its absolute ceiling, or what crowding leaves is too small to be
    worth trading. The callers already treat `<= 0` as no order.
    """
    from src.risk.rules import (
        _gross_multiplier,
        decision_side,
        sector_allowance_pct,
        sector_size_scale,
    )
    from src.sector_reference import _get_sector

    sector = _get_sector(symbol)
    if not sector or sector == "Unknown":
        # Sizing (this pass) still skips the dial for an unresolved
        # sector — a deliberate, unrelated design choice, not a gap.
        # 2026-09-01 audit: the ENGINE (RiskRuleEngine.check, rule 5)
        # no longer matches this — it now pools "Unknown" as its own
        # bucket and gates it, so an order this pass declines to shrink
        # still cannot silently over-concentrate; the engine's hard wall
        # catches what this pass does not pre-shrink. See
        # src/risk/rules.py rule 5's comment for the full defect.
        return allocation_pct, ""

    side = decision_side(action)
    current_pct = sector_weights.get((sector, side), 0.0)
    scale = sector_size_scale(
        current_pct,
        soft_cap_pct=cfg.max_sector_pct,
        hard_cap_pct=cfg.max_sector_hard_pct,
    )
    allowance_gross = sector_allowance_pct(
        current_pct,
        soft_cap_pct=cfg.max_sector_pct,
        hard_cap_pct=cfg.max_sector_hard_pct,
    )
    gross_mul = _gross_multiplier(symbol)
    # Below the diversification target the dial is inert (scale == 1.0)
    # and the allowance is wider than any single name may take anyway —
    # say nothing, change nothing, so an uncrowded trade's audit trail
    # is not cluttered with a cap that never bound.
    scaled = allocation_pct * scale
    allowance_raw = allowance_gross / gross_mul
    final = min(scaled, allowance_raw)
    if final >= allocation_pct:
        return allocation_pct, ""

    if scale <= 0.0 or allowance_raw <= 0.0:
        # Board item 10 (2026-09-14, second pass). Both dial refusals
        # logged a sentence the capture's regex happens to match, so
        # they were never invisible — but a matched sentence lands as a
        # generic `constructor_dropped` row, not as a code the funnel
        # can count. Filed here rather than at the two `return None`
        # sites in the builders, because only this method knows WHICH
        # of the two ends fired.
        note_refusal(
            symbol,
            "short" if side == "short" else "long",
            STOP_REFUSAL_SECTOR_AT_HARD_CEILING,
            f"sector '{sector}' ({side} side) is at {current_pct:.1f}% of "
            f"equity, at or past the {cfg.max_sector_hard_pct:.0f}% "
            f"absolute ceiling; no size is available. Concentration "
            f"scales size, but not without end.",
        )
        return -1.0, (
            f" [constructor: REFUSED — sector '{sector}' ({side} side) is "
            f"at {current_pct:.1f}% of equity, at or past the "
            f"{cfg.max_sector_hard_pct:.0f}% absolute ceiling. "
            f"Concentration scales size, but not without end]"
        )

    # Fixed 2026-09-24 (real incident: a genuine ~$295 / 2.95%-of-equity
    # MRVL trade was refused here as "under the $500 minimum order ...
    # pays full commission"). `min_order_usd` was an arbitrary flat $500
    # with no broker minimum behind it (config/number_ledger.yaml), and
    # Alpaca charges NO stock commission — the refusal was a bad
    # decision justified by a false reason. A sector-crowded trade is no
    # longer refused for notional size; it goes through at whatever
    # `final` leaves, however small (no commission + fractional shares
    # mean a small trade is not actually costly to hold). This
    # deliberately does NOT invent a new, arbitrary sliver threshold —
    # see the board note for why. `STOP_REFUSAL_SECTOR_BELOW_MIN_ORDER`
    # is kept defined (tests reference it) even though this path no
    # longer raises it.

    logger.info(
        "Constructor: %s size scaled for sector crowding "
        "(%.2f%% → %.2f%%; sector '%s' at %.1f%% gross, target %.0f%%, "
        "ceiling %.0f%%, dial %.2f)",
        symbol,
        allocation_pct,
        final,
        sector,
        current_pct,
        cfg.max_sector_pct,
        cfg.max_sector_hard_pct,
        scale,
    )
    # Provenance for the AI Risk Manager and the owner. A smaller position
    # than the PM asked for must never be silently applied — someone
    # seeing an unexpectedly small position has to be able to find out
    # why, and this is the string that tells them.
    return final, (
        f" [constructor: size scaled {allocation_pct:.2f}% → {final:.2f}% "
        f"because sector '{sector}' ({side} side) is already "
        f"{current_pct:.1f}% of equity, over the "
        f"{cfg.max_sector_pct:.0f}% concentration "
        f"target. The idea was judged on its own merits and taken, just "
        f"smaller; it is refused only past {cfg.max_sector_hard_pct:.0f}%. "
        f"Deterministic, not PM inconsistency]"
    )


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
