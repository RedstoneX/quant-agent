"""Sector budgets — spec §12.2 and §10.3.

What a sector already holds on each side, and how concentration SCALES
the next size instead of vetoing it.

Bodies moved VERBATIM from `src/risk/rules.py` (AST-identical to the
originals; `tests/test_risk_rules_parts_boundary.py` is the witness that this
part builds and runs alone). `src/risk/rules.py` keeps the engine, every
ledger-pinned number and the re-export mirror, so every existing
`from src.risk.rules import X` keeps resolving.
"""

from src.quantities import gross_multiplier as _gross_multiplier


# --- Spec §12.2 "long and short sector budgets are separate" --------------
#
# The defect this replaces: sector exposure was summed from SIGNED
# `market_value`, so a HELD SHORT made its sector look SMALLER and the book
# could over-concentrate unseen. The comment above that summation said
# "gross ... unsigned magnitude" while the code was signed — code and comment
# disagreed, and the comment was the one people read.
#
# Owner's ratified rule (2026-09-01), which governs the design: *"A long and
# a short in the same sector is not a hedge... We are trading opportunities."*
# So LONG sector exposure and SHORT sector exposure are tracked
# INDEPENDENTLY, each measured against the same limit. Neither offsets the
# other, and neither consumes the other's budget.
#
# GROSS SUMMING WAS EXPLICITLY REJECTED. Summing |long| + |short| into one
# bucket would block the pair trade the owner wants legal — long the leader
# and short the laggard in the same hot sector — by charging one sector
# budget twice for two independent opportunities.
#
# The split is by POSITION SIDE (long vs short), not by bullish/bearish
# thesis. An inverse-ETF LONG is long-side exposure in its sector.
#
# One definition, four consumers, on purpose: `RiskRuleEngine.check` (the
# gate), `PortfolioConstructor._current_sector_weights` (sizing),
# `PMFacts` (what the Portfolio Manager reads) and the pipeline's projected
# -portfolio preview all call these. Three independent implementations of
# "how much is this sector holding" is exactly how the signed-vs-gross
# defect survived for as long as it did.

SECTOR_SIDE_LONG = "long"
SECTOR_SIDE_SHORT = "short"


def position_side(position) -> str:
    """Which side of the book a HELD position sits on.

    `qty` is authoritative — Alpaca reports a short with negative qty AND
    negative market_value, but a position marked to a zero/uninitialised
    price still has an honest qty sign. Falls back to `market_value` only
    when qty is absent or exactly zero.
    """
    qty = getattr(position, "qty", 0.0) or 0.0
    if qty:
        return SECTOR_SIDE_SHORT if qty < 0 else SECTOR_SIDE_LONG
    market_value = getattr(position, "market_value", 0.0) or 0.0
    return SECTOR_SIDE_SHORT if market_value < 0 else SECTOR_SIDE_LONG


def decision_side(action: str) -> str:
    """Which side a proposed order would land on. SHORT is the only short."""
    return SECTOR_SIDE_SHORT if str(action).upper() == "SHORT" else SECTOR_SIDE_LONG


def sector_side_gross(
    positions,
    *,
    resolve_sector=None,
    include_unknown: bool = False,
) -> dict[tuple[str, str], float]:
    """Held GROSS (unsigned) exposure in DOLLARS, keyed by `(sector, side)`.

    Unsigned is the point: a short contributes its magnitude to the SHORT
    bucket rather than a negative number to the sector's single bucket.

    `resolve_sector` is an optional `(position) -> str` hook, because the
    consumers legitimately resolve a sector differently — the gate and the
    constructor read `position.sector` verbatim, while the PM-facing previews
    fall back to a `_get_sector` lookup when the broker left the field blank.
    That difference is about NAMING a sector, not about MEASURING one, and is
    deliberately left to the caller.

    `include_unknown=False` is the default the constructor's SIZING pass
    uses — it pre-shrinks an order for crowding it can actually measure, and
    leaves "Unknown" out of that measurement (a separate, unrelated design
    choice, unchanged here).

    2026-09-01 audit: this default used to ALSO describe the deterministic
    gate (`RiskRuleEngine.check` rule 5), which skipped the sector cap
    entirely for an unclassified symbol — counting "Unknown" here would have
    rationed against exposure the gate did not measure, so a network lookup
    failure silently switched the cap off. The gate now calls this with
    `include_unknown=True` instead, so a held "Unknown" position is counted
    (see rule 5's comment for the full defect and fix). This default stays
    `False` only for the sizing consumer described above.
    """
    out: dict[tuple[str, str], float] = {}
    for p in positions:
        gross = abs(getattr(p, "market_value", 0.0) or 0.0) * _gross_multiplier(p.symbol)
        if not gross:
            # A closed/zero position is not exposure. Skipping it also keeps a
            # spurious 0.0% row out of the sector tables the PM reads.
            continue
        sector = resolve_sector(p) if resolve_sector else getattr(p, "sector", "")
        sector = (sector or "").strip() or "Unknown"
        if sector == "Unknown" and not include_unknown:
            continue
        key = (sector, position_side(p))
        out[key] = out.get(key, 0.0) + gross
    return out


def accumulate_pending_sector(
    pending: dict[tuple[str, str], float],
    sector: str,
    action: str,
    gross_amount: float,
) -> None:
    """Book an approved-but-unexecuted order into the `(sector, side)`
    accumulator `RiskRuleEngine.check` reads.

    Exists so no caller has to remember that the key is a tuple. Keying it by
    the bare sector string silently misses every lookup — the accumulator
    would appear to work and enforce nothing, which is the whole failure mode
    §12.2 is cleaning up.

    2026-09-01 audit: "Unknown" used to be excluded here too, the batch-level
    twin of the same defect `RiskRuleEngine.check`'s rule 5 had — two
    unresolved-sector orders in the same run never saw each other's
    exposure. "Unknown" is now pooled into its own `(sector, side)` bucket
    like any other name, so it is checked, not skipped.
    """
    if not sector:
        return
    key = (sector, decision_side(action))
    pending[key] = pending.get(key, 0.0) + gross_amount


def sector_side_weights(
    positions,
    total_value: float,
    *,
    resolve_sector=None,
    include_unknown: bool = False,
) -> dict[tuple[str, str], float]:
    """`sector_side_gross` expressed as a PERCENT of equity.

    Empty for a non-positive `total_value` — there is no percentage of zero
    equity, and returning zeros would read as "no concentration".
    """
    if not total_value or total_value <= 0:
        return {}
    return {
        key: value / total_value * 100
        for key, value in sector_side_gross(
            positions,
            resolve_sector=resolve_sector,
            include_unknown=include_unknown,
        ).items()
    }
