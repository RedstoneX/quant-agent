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

**This module never places an order.** It returns one comparison — the
weakest thing to consider giving up, and the strongest thing there is no
room for — as data for the Portfolio Manager's OWN prompt
(`PortfolioManagerAgent._render_rotation_section`). It never sizes a
trade, and is silent unless capital is genuinely constrained (real headroom
under the desk's own existing risk-budget floor, `STARTER_POSITION_RISK_PCT`
/ `RiskConfig.min_position_risk_pct` — the same floor `allocate_risk_budget`
already uses to decide "not worth trading").

**Phase 14b — the comparison can now be ACTED ON, behind a flag.** Owner
request: positions are reassessed several times a day, and when a genuinely
better-ranked opportunity exists while the book is full of something weaker
and stagnant, the weak one should be pruned and the better trade taken —
rather than the desk merely showing the model a comparison and hoping it
acts. With `execution.rotation_enabled` ON (default OFF — see
`config/settings.yaml`), `DecisionStage` (`src/pipeline_stages.py::
_apply_rotation_execution`) turns the CATEGORICAL tier only into an
ordinary zero-size PM target for the held symbol, which then travels the
identical path every PM-decided exit travels: `PortfolioConstructor`
(`_build_sell`), the hard risk rules, the AI Risk Manager's review and
per-symbol refusal, the holding-discipline claim check, and
`_submit_protected_sell`'s cancel-write-ahead → submit → restore-on-failure
discipline. No gate is bypassed, and no new exempt reason category exists.

Why the categorical tier was automated first: an ineligible holding fails the
same rule a brand-new buy must clear today, so the case is a rule outcome,
not a ranking judgement.

**`ranked_margin` execution — separate flag, separate risk.** A 25% score
gap is still a PROVISIONAL, unmeasured band (see above), and selling an
eligible, thesis-intact position on it is still, on its own, the "arbitrary
number decides a trade" pattern this desk refuses elsewhere — enabling
execution here does not resolve that; it is a distinct, still-live doctrine
question the owner accepts explicitly by turning on
`execution.rotation_ranked_margin_enabled` (default OFF, independent of
`rotation_enabled`), separately from the sequencing question below.

What execution DOES require, once that switch is on: `ranked_margin` fires
far more often than `ineligible_hold` (a ranking gap, not a rule failure),
so "sold, replacement buy refused downstream, owner-alerted" — tolerable as
a rare `ineligible_hold` edge case — would become routine churn if the same
behaviour applied here. `_drop_rotation_sell_if_buy_leg_refused`
(`src/pipeline_stages.py`, end of `RiskStage._run_review`) closes that: the
sell and the replacement buy are built and risk-reviewed together, in one
plan, before `ExecutionStage` ever submits either one, so if the buy did
not survive that review the sell is withdrawn there too — before either
order reaches the broker — rather than sold into an unfunded replacement.

Why the desk's holding discipline still binds: `docs/WORK.md` item 25 says a
position stays protected from a plain, no-real-trigger sale unless the level
backing its thesis has broken (confirmed on two consecutive closes, or the
noise-band fallback). Opportunity cost is not one of the three real triggers
that doctrine names, so a rotation may only close a holding whose
`check_structural_protection` verdict is ALREADY "not protected" — a stale
position whose thesis is still structurally intact is surfaced, never sold.
Whether opportunity cost should become a fourth trigger is an owner decision
this module does not take.

`rotation_sell_reason` writes the sale's reason from measured facts only —
the failed entry rules, the broken-protection basis, the candidate's rank and
score, the headroom — so the audit trail, the Risk Manager and the owner
alert all read the same checkable statement.

**Tier 2 is coverage-neutral as of 2026-09-14** (`docs/INCIDENT_HISTORY.md`,
that date, retired board item 66). On
2026-09-13 `src/verdicts.py::rank_verdicts` changed from a weighted AVERAGE
across seats to a weighted SUM — correctly: the average made a second,
fully AGREEING seat LOWER a candidate's rank. But a sum is not a rescaling
of an average; the divisor it deletes is the name's OWN seat count, which
differs per name. So the composite score of two different names stopped
being comparable term-for-term the moment their coverage differed, and
Tier 2's relative margin compares exactly two such names. It is a ratio, so
it survives a change of UNITS — it does not survive a per-name change of
term count, and the item's claim that "the margin is a ratio, so the scale
change does not affect it" was checked here and does not hold.

Concretely: a held name covered today by Technical alone (weighted score
2.4 at full strength) is displaced by a new name that Technical rates
identically but that also carries a live earnings filing and a confirmed
flow (2.4 + 1.2 + 0.4 = 4.0 >= 2.4 x 1.25). Nothing about the held name
changed; the earnings calendar moved. Measured against the desk's own
archive (`specialist_evidence`, 2026-08-17..2026-09-02), the set of seats
carrying a non-neutral read on a HELD name changes day to day, not over
weeks: DIS, MSFT and V each fell to zero scoring seats on individual
sessions and recovered, and RSG went from zero to one to two scoring seats
inside five sessions. Coverage churn is a daily fact of this book, so this
is reachable now, not eventually.

The fix introduces no number and does not touch the sum. Tier 2 now
computes the SAME weighted arithmetic over only the seats that scored BOTH
names — the intersection of their coverage — and requires the margin to
clear on that like-for-like sub-score as well as on the full composite. A
term present on one side and absent on the other cannot enter a comparison
between the two. When the two names share no scoring seat at all there is
no like-for-like comparison to make and Tier 2 declines outright, because
rotation refusing a sale it should have made costs an opportunity while
rotation making one it should not have costs a real position.

This is strictly a conjunction with the previous test: every rotation that
fires now would have fired before, and some that would have fired no longer
do. Tier 2 is therefore LESS likely to fire, never more — that is a
property of the arithmetic, not an estimate. Breadth is undiminished
everywhere else: it still orders `ranked`, still picks `best_new`, still
picks the weakest held name, and still drives Tier 1 in full.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.verdicts import RankedCandidate, score_verdict, seat_weight

__all__ = [
    "ROTATION_MARGIN_PCT",
    "RotationOpportunity",
    "RotationPrecheck",
    "evaluate_rotation_opportunity",
    "rotation_sell_reason",
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
    #: "ranked_margin" tier only — the seats that scored BOTH names, and
    #: each side's weighted sum over exactly those seats. This is the
    #: like-for-like comparison the margin was actually cleared on
    #: (`docs/INCIDENT_HISTORY.md` 2026-09-14); the full
    #: `new_score`/`held_score` above are
    #: coverage-sensitive and are not comparable term-for-term between two
    #: names whose seat sets differ. Empty for the categorical tier, which
    #: makes no ranking comparison at all.
    shared_seats: tuple[str, ...] = field(default_factory=tuple)
    held_shared_score: float | None = None
    new_shared_score: float | None = None


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
    if best_new.score < weakest_held.score * (1.0 + margin_pct):
        return None

    # `docs/INCIDENT_HISTORY.md` 2026-09-14. The full composite is a
    # weighted SUM over
    # whichever seats happen to cover each name today, so the two totals
    # above are not comparable term-for-term once the two names' coverage
    # differs — and it does, daily (see the module docstring for the
    # archive measurement). Re-run the identical arithmetic over the seats
    # that scored BOTH names and require the same margin there too. No
    # constant is introduced: `seat_weight` and `score_verdict` are
    # `src/verdicts.py`'s own, unchanged, and the margin is the same one.
    shared = _shared_seat_comparison(held=weakest_held, new=best_new)
    if shared is None:
        # No seat scored both names. There is no like-for-like comparison
        # to make, so this module declines rather than compare two
        # differently-composed sums. Failing toward NOT selling is the
        # asymmetry the desk chose: a missed rotation costs an
        # opportunity, a wrong one costs a real position.
        return None
    shared_seats, held_shared, new_shared = shared
    if held_shared <= 0:
        return None  # same non-positive-denominator refusal as above
    if new_shared < held_shared * (1.0 + margin_pct):
        return None

    return RotationOpportunity(
        new_symbol=best_new.symbol,
        new_score=best_new.score,
        held_symbol=weakest_held.symbol,
        held_score=weakest_held.score,
        tier="ranked_margin",
        margin_pct=margin_pct,
        shared_seats=shared_seats,
        held_shared_score=round(held_shared, 4),
        new_shared_score=round(new_shared, 4),
    )


def _shared_seat_comparison(
    *, held: RankedCandidate, new: RankedCandidate,
) -> tuple[tuple[str, ...], float, float] | None:
    """Each name's weighted score over ONLY the seats that scored both.

    Returns `(shared_seats, held_score, new_score)`, or `None` when the two
    names share no scoring seat.

    The arithmetic is `src/verdicts.py`'s own and is reproduced, not
    re-derived: a seat's contribution to `RankedCandidate.score` is
    `seat_weight(seat) * (magnitude + conviction_score)`, which is exactly
    `seat_weight(seat) * score_verdict(verdict)`. Summing that over a
    subset of seats therefore yields the same composite restricted to that
    subset — a partial sum of the real score, not a second scoring scheme.

    Neutral seats are deliberately NOT shared coverage. A seat that looked
    and came back with no lean contributes nothing to either name's score
    (`RankedCandidate.neutral_seats`, never `.verdicts`), so including it
    would add two zero terms and change nothing except to make an
    incomparable pair look comparable.
    """
    held_by_seat = {v.seat: v for v in held.verdicts}
    new_by_seat = {v.seat: v for v in new.verdicts}
    shared = sorted(set(held_by_seat) & set(new_by_seat))
    if not shared:
        return None
    held_total = sum(
        seat_weight(seat) * score_verdict(held_by_seat[seat]) for seat in shared
    )
    new_total = sum(
        seat_weight(seat) * score_verdict(new_by_seat[seat]) for seat in shared
    )
    return tuple(shared), held_total, new_total


@dataclass(frozen=True)
class RotationPrecheck:
    """Everything the PM's rotation section was rendered from, kept so the
    pipeline can act on the SAME numbers the model was shown — never a
    second evaluation a moment later against possibly different inputs.

    `opportunity` is `None` when nothing qualified; `telemetry_available`
    is False when the book's risk was not visible this session (the
    fail-open skip), in which case `headroom_pct` is meaningless.
    """

    opportunity: RotationOpportunity | None
    headroom_pct: float
    ceiling_pct: float
    floor_pct: float
    telemetry_available: bool = True


def rotation_sell_reason(
    opportunity: RotationOpportunity,
    *,
    protection_basis: str,
    protection_detail: str,
    headroom_pct: float,
    ceiling_pct: float,
    floor_pct: float,
) -> str:
    """The checkable reason a rotation sale carries, from measured facts only.

    Every clause names something recorded elsewhere this run: the held
    symbol's own `candidate_eligibility` failures, the `StructuralProtection
    Check` basis and detail, the new candidate's rank score, and the book's
    headroom. Nothing here is a feeling or a forecast, so the Risk Manager
    can check every claim against the blocks it is shown and the evening
    review can grade it.

    Kept compact (the rule list is capped at 100 characters, the protection
    detail at 60) because `PortfolioConstructor._build_sell` appends the
    thesis condition and then truncates the order's reasoning at 500; the
    untruncated detail lives in the `rotation` pipeline_event.

    Both tiers are supported: `ineligible_hold` (categorical — the held
    name fails today's own entry rules) and, since the `ranked_margin`
    sequencing pre-check (`src/pipeline_stages.py::
    _drop_rotation_sell_if_buy_leg_refused`), `ranked_margin` itself
    (both sides eligible; the ranking margin was cleared) — see
    `evaluate_rotation_opportunity` for what each tier means.
    """
    if opportunity.tier == "ineligible_hold":
        failed_rules = ("; ".join(opportunity.reasons) or "entry rules")[:100]
        basis = (
            f"{opportunity.held_symbol} fails the desk's own entry rules "
            f"today ({failed_rules})"
        )
    elif opportunity.tier == "ranked_margin":
        held_shared = opportunity.held_shared_score
        new_shared = opportunity.new_shared_score
        if held_shared is None or new_shared is None:
            # Defensive only — `evaluate_rotation_opportunity` always fills
            # both for this tier. Falls back to the full composite, labelled
            # as such, rather than crash on a hand-built/legacy opportunity.
            held_shared = opportunity.held_score or 0.0
            new_shared = opportunity.new_score
            seat_desc = "full composite score, no shared-seat figure recorded"
        else:
            seat_desc = (
                f"shared-seat score over {'/'.join(opportunity.shared_seats) or 'no shared seats'}"
            )
        basis = (
            f"{opportunity.held_symbol} still clears today's entry rules, "
            f"but ranks below {opportunity.new_symbol} by more than the "
            f"{opportunity.margin_pct:.0%} margin on the like-for-like "
            f"{seat_desc} ({held_shared:.2f} vs {new_shared:.2f})"
        )
    else:
        raise ValueError(f"unknown rotation tier {opportunity.tier!r}")
    return (
        f"ROTATION (deterministic, src/rotation.py): {basis}; structural "
        f"protection not intact ({protection_basis}: {protection_detail[:60]}). "
        f"Headroom {headroom_pct:.2f}% of the {ceiling_pct:.2f}% risk ceiling, "
        f"under the {floor_pct:.2f}% minimum. Full close to free room for "
        f"{opportunity.new_symbol}, the best-ranked eligible candidate (score "
        f"{opportunity.new_score:.2f}) the PM targeted."
    )
