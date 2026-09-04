"""Opportunity-cost rotation surfacing — Phase 14.

**The gap this closes.** `src/risk/budget.py::allocate_risk_budget` and the
gross-exposure ladder both correctly BLOCK a new candidate when the book's
risk ceiling is full — but neither one, nor anything upstream of them, ever
asks whether the new candidate is actually a BETTER opportunity than
something the book is already holding. A desk that is fully committed to
weak, stale or barely-justified positions can refuse a genuinely stronger
new idea for no reason other than "no room", with nothing in the system
that ever compares the two. This module is the missing comparison.

**Grounded in established practice, not invented.** Ranking current
holdings and candidates on one shared scale and replacing the weakest
holding with a stronger candidate — subject to a real margin so the system
does not churn on noise-level rank differences — is standard in
cross-sectional/systematic portfolio construction:

  * Grinold & Kahn, *Active Portfolio Management* (2000), formalise the
    "no-trade region": under transaction costs, a rebalance is only worth
    making once the expected improvement clears a real breakeven, not at
    every marginal rank change. The margin below is this desk's own
    breakeven proxy — expressed on the verdict score rather than in
    dollars, since that is the unit this desk already ranks on
    (`src/verdicts.py`).
  * FTSE Russell's own published index-reconstitution methodology applies
    exactly this shape in production: a "banding"/buffer rule requires a
    candidate to clear a MATERIALLY different bar than an incumbent before
    a membership swap happens, specifically to damp turnover from
    marginal, noise-level rank changes at the boundary (see FTSE Russell,
    "Russell US Indexes Construction and Methodology", banding sections;
    summarised at https://www.lseg.com/en/insights/ftse-russell —
    "percentile banding... allows previous membership to be considered in
    order to limit unnecessary index turnover").

Neither source hands over one universal number — practitioner tolerance-
band discussions for rebalancing cluster loosely in the 5%-25% relative-
band range depending on the asset class and cost profile (see e.g. Alpha
Architect's writing on rebalancing tolerance bands). This module takes the
CONSERVATIVE (hardest-to-trigger) end of that range, 25% relative on the
composite verdict score, and marks it PROVISIONAL — the same posture
`src/verdicts.py::SEAT_WEIGHT` already uses for its own literature-derived
but unmeasured numbers: a considered starting point, not a measured fact,
revisable the moment this desk has its own rotation-outcome data to spend.

**Two tiers, not one.** A held position that no longer clears this desk's
OWN entry gates (`PortfolioManagerAgent.candidate_eligibility`) needs no
margin at all to flag: it would not be bought today, by the identical rule
a brand-new buy must clear, so there is nothing "noise-level" about the
comparison — it is categorical, not a ranking judgement. Only among held
positions that ARE still eligible does the ranking-margin rule apply, to
protect against churning on a real but marginal rank difference.

**Never forces anything.** This module returns one comparison — the
weakest thing to consider giving up, and the strongest thing there is no
room for — as data for the Portfolio Manager's OWN prompt
(`PortfolioManagerAgent._render_rotation_section`). It never edits a
position, never sizes a trade, and is silent unless capital is genuinely
constrained (real headroom under the desk's own existing risk-budget floor,
`STARTER_POSITION_RISK_PCT` / `RiskConfig.min_position_risk_pct` — the same
floor `allocate_risk_budget` already uses to decide "not worth trading").
Whatever the Portfolio Manager decides to do with it is still subject to
this desk's existing discipline on any edit to a held position: substantively
justified and verified, never a silent side effect (`docs/WORK.md` items
22-24).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.verdicts import RankedCandidate

__all__ = [
    "ROTATION_MARGIN_PCT",
    "RotationOpportunity",
    "evaluate_rotation_opportunity",
]

#: Relative margin the best-ranked new candidate must clear over the
#: weakest still-eligible held position's score before a rotation is
#: surfaced. PROVISIONAL — see module docstring for the citations and why
#: this is the conservative end of a real but unpinned range, not a
#: measured fact.
ROTATION_MARGIN_PCT = 0.25


@dataclass(frozen=True)
class RotationOpportunity:
    """One surfaced comparison: the strongest idea there is no room for,
    against the weakest thing currently holding that room.

    `held_score` is `None` for the categorical tier — the held symbol has
    no rank at all because it failed the desk's own entry gates outright,
    not because it ranked low among names that passed them.
    """

    new_symbol: str
    new_score: float
    held_symbol: str
    held_score: float | None
    #: "ineligible_hold" (categorical — no margin needed) or "ranked_margin"
    #: (both sides eligible; the margin below was cleared).
    tier: str
    #: The held symbol's own blocking reasons, "ineligible_hold" tier only.
    reasons: tuple[str, ...] = field(default_factory=tuple)
    margin_pct: float = ROTATION_MARGIN_PCT


def evaluate_rotation_opportunity(
    *,
    ranked: list[RankedCandidate],
    blocked: dict[str, list[str]],
    held_symbols: set[str],
    headroom_pct: float,
    floor_pct: float,
    margin_pct: float = ROTATION_MARGIN_PCT,
) -> RotationOpportunity | None:
    """The one rotation comparison worth surfacing this session, or `None`.

    `ranked` / `blocked` are `PortfolioManagerAgent.rank_candidates`'s own
    output — both current holdings and new candidates already share one
    scale, since eligibility and ranking are computed over every analysed
    symbol without regard to whether it is currently held (Technical reads
    the whole configured universe every session, held names included).

    Silent (returns `None`) unless capital is genuinely constrained:
    `headroom_pct` — the risk-budget headroom computed against the EXISTING
    book alone, before this session's own asks — must already be under
    `floor_pct`, the same minimum this desk otherwise requires before it
    will size a new idea at all. A book with real room left has nothing to
    rotate for; refusing on "we might want the room later" is not this
    desk's rule anywhere else and is not invented here.
    """
    held = {s.upper() for s in held_symbols}
    if headroom_pct >= floor_pct:
        return None

    new_candidates = [c for c in ranked if c.symbol not in held]
    if not new_candidates:
        return None
    best_new = new_candidates[0]  # `ranked` is already sorted, best first

    # Tier 1 — categorical. A held name failing the desk's own entry gates
    # needs no ranking margin: it would not be bought today. Ties broken by
    # the MOST blocking reasons first (worse, more clearly stale), then
    # alphabetically, so the choice is reproducible when more than one held
    # name is ineligible.
    ineligible_held = {
        sym: tuple(reasons) for sym, reasons in blocked.items()
        if sym in held and reasons
    }
    if ineligible_held:
        held_symbol = min(
            ineligible_held, key=lambda s: (-len(ineligible_held[s]), s),
        )
        return RotationOpportunity(
            new_symbol=best_new.symbol,
            new_score=best_new.score,
            held_symbol=held_symbol,
            held_score=None,
            tier="ineligible_hold",
            reasons=ineligible_held[held_symbol],
            margin_pct=margin_pct,
        )

    # Tier 2 — ranked margin. Both sides eligible; the weakest held name is
    # simply the last held entry in `ranked`'s own (already-sorted) order.
    held_ranked = [c for c in ranked if c.symbol in held]
    if not held_ranked:
        return None  # nothing held is even ranked — no comparison to make
    weakest_held = held_ranked[-1]
    if weakest_held.score <= 0:
        # A relative margin against a non-positive score is not a
        # meaningful comparison; a score this weak with no eligibility
        # failure recorded is a state this module does not attempt to
        # interpret further, rather than divide by (near) zero and guess.
        return None
    if best_new.score >= weakest_held.score * (1.0 + margin_pct):
        return RotationOpportunity(
            new_symbol=best_new.symbol,
            new_score=best_new.score,
            held_symbol=weakest_held.symbol,
            held_score=weakest_held.score,
            tier="ranked_margin",
            margin_pct=margin_pct,
        )
    return None
