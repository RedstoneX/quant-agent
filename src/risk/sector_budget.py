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


# --- Spec §10.3 "concentration scales size, it does not veto" -------------
#
# `max_sector_pct` used to be a HARD BLOCK: a sector at the cap refused the
# next trade outright, however good it was. The owner's ratified framing
# (2026-09-01) is that this inverts the question — "each trade opportunity is
# an opportunity on its own, and it should be based on the merits of that
# opportunity." A high-conviction idea in an already-crowded sector should be
# TAKEN, SMALLER. Concentration is a dial, not a gate.
#
# The dial is deterministic Python, deliberately. Same principle as the
# reward:risk fix (PR #202) and §10.2: the number comes from code, the agent
# brings judgement. No seat is asked "how much should we shave off for
# crowding?" — the answer is arithmetic on the live book.
#
# TWO knobs, and they mean different things:
#
#   `soft` (`risk.max_sector_pct`, 75 as of spec §12.3) — the concentration
#       TARGET. At or below it, crowding costs a trade nothing. Above it,
#       every additional trade in that sector is progressively shrunk.
#
#   `hard` (`risk.max_sector_hard_pct`, 90 as of spec §12.3) — the ABSOLUTE
#       ceiling, past which the answer is still no. A dial with no end is not
#       a dial: a sector could otherwise grow without limit through an
#       infinite series of ever-smaller additions.
#
# Spec §12.3 (owner-ratified 2026-09-01) moved the target from 40 to 75. The
# 40 was a retirement-portfolio number and does not survive `docs/OUTCOME.md`:
# *"This is a trading desk, not a long-term retirement desk."* Sector
# diversification is not a goal here; a sector limit's ONLY remaining job is
# bounding correlated blow-up risk — one shock taking several positions at
# once.
#
# THE COST, STATED PLAINLY BECAUSE IT IS REAL: at 75% of equity in one
# sector, an ordinary 20% sector-wide drawdown costs 15% of equity — three
# times the per-trade risk unit, and it will trip the de-levering ladder.
# That is the accepted price of a concentrated trading desk, not an
# oversight. Nothing halts the desk on an account-level loss reading any
# more; the per-position stop is the protection.
#
# The 90 ceiling is NOT in the ratified §12.3 text — the spec set the target
# and left the terminal bound unstated. 90 was chosen when §12.3 was built:
# the 1.5x multiple that produced 60 from 40 gives 112.5 from 75, which is
# meaningless, and a dial with no terminal bound bounds nothing. 90 keeps a
# real ceiling while leaving 15 points of scaling range. Configurable
# precisely because it is a judgement about how far a tilt may run.
#
# Both functions are pure, and both are used by BOTH consumers on purpose:
# `PortfolioConstructor` calls them to SIZE an order down before it is ever
# proposed, and `RiskRuleEngine.check` calls them to BLOCK anything that
# arrives above the allowance anyway. One definition, two consumers — a
# second, divergent notion of "how crowded is too crowded" here would let the
# constructor and the deterministic gate disagree about an identical book,
# which is exactly the failure `max_position_pct` already documents.


def sector_size_scale(
    current_sector_pct: float,
    *,
    soft_cap_pct: float,
    hard_cap_pct: float,
) -> float:
    """The dial itself: the fraction of its requested size a trade keeps,
    given how crowded its sector ALREADY is (before this trade).

    Returns 1.0 at or below the soft cap, tapering linearly to 0.0 at the
    hard ceiling. Monotonically non-increasing in `current_sector_pct`, and
    never negative — a heavier sector can only ever mean a smaller trade.
    """
    if hard_cap_pct <= soft_cap_pct:
        # Degenerate config (hard not above soft): behave like the old gate
        # rather than inventing headroom the operator never granted.
        return 1.0 if current_sector_pct <= soft_cap_pct else 0.0
    if current_sector_pct <= soft_cap_pct:
        return 1.0
    if current_sector_pct >= hard_cap_pct:
        return 0.0
    return (hard_cap_pct - current_sector_pct) / (hard_cap_pct - soft_cap_pct)


def sector_allowance_pct(
    current_sector_pct: float,
    *,
    soft_cap_pct: float,
    hard_cap_pct: float,
) -> float:
    """The most GROSS exposure (as % of equity) this sector may still take on.

    This is the wall behind the dial. `sector_size_scale` shrinks what a trade
    asks for; this bounds what it may receive no matter what it asked for, so
    the sector can never be pushed PAST the hard ceiling in a single step.

    Also monotonically non-increasing and never negative: it is
    `(hard - current)` scaled by the dial, which is `(hard - current)` below
    the soft cap and `(hard - current)^2 / (hard - soft)` between the caps.
    Continuous at the soft cap (both branches give `hard - soft` there), so
    there is no crowding level at which a heavier sector is granted MORE room
    than a lighter one.
    """
    headroom = max(0.0, hard_cap_pct - current_sector_pct)
    return headroom * sector_size_scale(
        current_sector_pct,
        soft_cap_pct=soft_cap_pct,
        hard_cap_pct=hard_cap_pct,
    )
