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

Why only the categorical tier is automated: an ineligible holding fails the
same rule a brand-new buy must clear today, so the case is a rule outcome,
not a ranking judgement. The ranked-margin tier stays information-only
because a 25% score gap is a PROVISIONAL, unmeasured band (see above), and
selling an eligible, thesis-intact position on an unmeasured margin would be
exactly the "arbitrary number decides a trade" pattern this desk refuses
elsewhere.

**Board item 39, 2026-09-20 — what changed and what did NOT.** The
ranked-margin tier now HAS an execution path, behind a second switch
(`execution.rotation_ranked_margin_enabled`, default False), and that path
is sequenced correctly: the replacement BUY is run through the downstream
refusal gates against the book as it will be AFTER the sale, and both legs
are withdrawn together if it would be refused, so the desk cannot end up
sold out of a position with nothing bought
(`src/pipeline_stages.py::_drop_rotation_legs_if_buy_would_be_refused`).
The sale additionally cannot be BUILT without a `RotationClearance` below —
an object carrying the projected numbers the buy was cleared on, which no
configuration can produce.

The paragraph above is still the reason the switch is OFF, and it is not a
formality. `ROTATION_MARGIN_PCT` is recorded in
`config/number_ledger.yaml` as `arbitrary` — "a round quarter percent with
no source". Closing the sequencing defect removed a reason this tier could
not be turned on; it did not answer the one stated here. `docs/WORK.md`
item 39(a) is the remaining blocker: it lists what has already been ruled
out (the citations above among them — the SHAPE is sourced, the NUMBER is
not) and what would settle it. Note what 39(a) itself says: "do not
promote it to an execution gate until answered."

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
    "REQUIRED_BUY_LEG_GATES",
    "ROTATION_MARGIN_PCT",
    "RotationClearance",
    "RotationOpportunity",
    "RotationPrecheck",
    "evaluate_rotation_opportunity",
    "rotation_proposal_reason",
    "rotation_sell_reason",
]

#: Board item 39. Every downstream refusal path that can drop the rotation's
#: REPLACEMENT BUY *after* the SELL has already been submitted, and that is
#: knowable before the sale. A `ranked_margin` sale may not be built unless a
#: `RotationClearance` proves EVERY name here was evaluated this run against
#: PROJECTED POST-SALE state.
#:
#: The names are the `_record_execution_skip` reason codes the execution
#: stage itself uses, so this list and the code it mirrors can be compared
#: mechanically (`tests/test_rotation_sequencing.py` pins that).
#:
#: This list is deliberately NOT "every way a BUY can fail" — see
#: `rotation_sell_reason` for the paths that remain open by construction and
#: why no pre-check can close them.
REQUIRED_BUY_LEG_GATES = (
    "daily_loss_recheck",
    "no_price",
    "stale_entry",
    "qty_zero",
    # Added after adversary review of attempt 3. Both are deterministic
    # functions of the POST-SALE book, so both are knowable before the
    # sale — and both are the refusals a rotation is most likely to hit,
    # because a rotation only surfaces when the risk headroom is already
    # under the floor. Leaving them out was the same class of omission as
    # attempt 1's, one layer further down.
    "insufficient_cash",
    "below_min_notional",
)

#: Relative margin the best-ranked new candidate must clear over the
#: weakest still-eligible held position's score before a rotation is
#: surfaced. PROVISIONAL — see module docstring for the citations and why
#: this is the conservative end of a real but unpinned range, not a
#: measured fact.
ROTATION_MARGIN_PCT = 0.25

#: `PortfolioConstructor._build_sell` truncates an order's reasoning at 500
#: characters after appending the thesis condition. A rotation reason that
#: overruns it loses its LAST clause first — which is the one naming what
#: the sale was cleared on, or that it is contingent. Not a threshold on
#: anything traded: it is the length of a field this text has to fit in.
ROTATION_REASON_MAX_CHARS = 500


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


@dataclass(frozen=True)
class RotationClearance:
    """Proof that a rotation's replacement BUY was run through every gate in
    `REQUIRED_BUY_LEG_GATES` against PROJECTED POST-SALE state, and survived.

    **This object is the safety net, and it is not a flag.** Board item 39's
    first fix attempt replaced the structural barrier that made a
    `ranked_margin` sale impossible to build with a plain config boolean,
    which meant one truthy value anywhere in the settings chain was enough
    to put a real sale on the wire. A boolean cannot carry evidence. This
    can, and `rotation_sell_reason` refuses — unconditionally, with no
    config read anywhere in the refusal — to build the sale's reason
    without one whose contents match the opportunity in hand.

    `projected_positions` / `projected_equity` / `projected_daily_pnl` /
    `projected_basis` are recorded so the audit row states the numbers the
    sale was actually cleared on, rather than asserting that a check
    happened.
    """

    held_symbol: str
    new_symbol: str
    #: Every gate evaluated. Must cover `REQUIRED_BUY_LEG_GATES`.
    gates_checked: tuple[str, ...]
    #: The day-change number the projected post-sale book produced, and
    #: which rung of `daily_loss_limit_pct` governed it.
    projected_daily_pnl: float
    projected_basis: str
    #: Held names remaining after every SELL/COVER this session, the
    #: rotation's own included, and the equity those were measured against.
    projected_positions: tuple[str, ...]
    projected_equity: float

    def covers(self, *, held_symbol: str, new_symbol: str) -> bool:
        """Is this clearance about THIS rotation, and did it check every
        gate? A clearance minted for a different pair, or one missing a
        gate, is not a clearance for this sale."""
        if self.held_symbol.strip().upper() != held_symbol.strip().upper():
            return False
        if self.new_symbol.strip().upper() != new_symbol.strip().upper():
            return False
        checked = {str(g).strip() for g in self.gates_checked}
        return all(gate in checked for gate in REQUIRED_BUY_LEG_GATES)


def rotation_sell_reason(
    opportunity: RotationOpportunity,
    *,
    protection_basis: str,
    protection_detail: str,
    headroom_pct: float,
    ceiling_pct: float,
    floor_pct: float,
    clearance: "RotationClearance | None" = None,
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
    """
    if opportunity.tier == "ranked_margin":
        # Board item 39. The ranked-margin tier sells a position that still
        # passes the desk's own entry gates, purely to fund a replacement.
        # If the replacement is then refused downstream the desk has closed
        # a position for a reason that never materialised — so the sale may
        # only be built once the replacement has been run through
        # `REQUIRED_BUY_LEG_GATES` against the book as it will be AFTER the
        # sale.
        #
        # This raise is unconditional and reads no config. A feature flag
        # decides whether the CALLER gets as far as asking; it cannot
        # decide whether the question is answerable.
        if not isinstance(clearance, RotationClearance):
            raise ValueError(
                "rotation_sell_reason: the ranked-margin tier requires a "
                "RotationClearance proving the replacement BUY was checked "
                "against projected post-sale state "
                f"(gates: {', '.join(REQUIRED_BUY_LEG_GATES)}). No config "
                "flag substitutes for it — board item 39, attempt 1."
            )
        if not clearance.covers(
            held_symbol=opportunity.held_symbol,
            new_symbol=opportunity.new_symbol,
        ):
            raise ValueError(
                "rotation_sell_reason: the RotationClearance supplied is not "
                f"for {opportunity.held_symbol} -> {opportunity.new_symbol} "
                "with every required gate checked (it covers "
                f"{clearance.held_symbol} -> {clearance.new_symbol}, gates "
                f"{', '.join(clearance.gates_checked)})."
            )
        return _ranked_margin_sell_reason(
            opportunity,
            protection_basis=protection_basis,
            protection_detail=protection_detail,
            headroom_pct=headroom_pct,
            ceiling_pct=ceiling_pct,
            floor_pct=floor_pct,
            clearance=clearance,
        )
    if opportunity.tier != "ineligible_hold":
        raise ValueError(
            "rotation_sell_reason knows two tiers, 'ineligible_hold' and "
            f"'ranked_margin'; {opportunity.tier!r} is neither and is not "
            "executable"
        )
    failed_rules = ("; ".join(opportunity.reasons) or "entry rules")[:100]
    return (
        f"ROTATION (deterministic, src/rotation.py): {opportunity.held_symbol} "
        f"fails the desk's own entry rules today ({failed_rules}); structural "
        f"protection not intact ({protection_basis}: {protection_detail[:60]}). "
        f"Headroom {headroom_pct:.2f}% of the {ceiling_pct:.2f}% risk ceiling, "
        f"under the {floor_pct:.2f}% minimum. Full close to free room for "
        f"{opportunity.new_symbol}, the best-ranked eligible candidate (score "
        f"{opportunity.new_score:.2f}) the PM targeted."
    )


def _ranked_margin_sell_reason(
    opportunity: RotationOpportunity,
    *,
    protection_basis: str,
    protection_detail: str,
    headroom_pct: float,
    ceiling_pct: float,
    floor_pct: float,
    clearance: RotationClearance | None,
) -> str:
    """The checkable reason a RANKED-MARGIN rotation sale carries.

    Same discipline as the categorical reason above — every clause names
    something recorded elsewhere this run — with two additions the
    categorical tier does not need:

      * the like-for-like sub-score the margin was actually cleared on
        (`shared_seats`, `docs/INCIDENT_HISTORY.md` 2026-09-14), because
        the full composite is a coverage-sensitive weighted SUM and is not
        comparable term-for-term between two names; and
      * the projected post-sale daily-loss number the replacement BUY was
        cleared against, so the Risk Manager and the evening review can
        check that the sale was sequenced behind the buy's gates rather
        than ahead of them (board item 39).

    Kept compact for the same 500-character truncation in
    `PortfolioConstructor._build_sell`.
    """
    seats = ("; ".join(opportunity.shared_seats) or "none")[:40]
    held_shared = opportunity.held_shared_score
    new_shared = opportunity.new_shared_score
    held_score = opportunity.held_score
    tail = (
        f"BUY pre-cleared post-sale (day chg "
        f"${clearance.projected_daily_pnl:.0f}, {clearance.projected_basis})."
        if clearance is not None else
        "CONTINGENT on the replacement BUY clearing its gates; withdrawn "
        "with it if it does not."
    )
    reason = (
        f"ROTATION (ranked margin, src/rotation.py): {opportunity.held_symbol}"
        f" is the weakest still-eligible holding (score "
        f"{0.0 if held_score is None else held_score:.2f}); "
        f"{opportunity.new_symbol} ({opportunity.new_score:.2f}) clears the "
        f"{opportunity.margin_pct * 100:.0f}% margin on the seats covering "
        f"both ({seats}: {held_shared} vs {new_shared}). Protection not "
        f"intact ({protection_basis}: {protection_detail[:40]}). Headroom "
        f"{headroom_pct:.2f}% of the {ceiling_pct:.2f}% ceiling, under the "
        f"{floor_pct:.2f}% minimum. {tail}"
    )
    # `PortfolioConstructor._build_sell` appends the thesis condition and
    # truncates the order's reasoning at 500 characters, and the clause
    # that makes a CONTINGENT proposal honest is the last one — so it is
    # the first thing a long seat list or a long protection detail would
    # eat. The two variable-length clauses are capped above, and this
    # asserts the result rather than trusting the caps
    # (`tests/test_rotation_sequencing.py` pins the worst case).
    if len(reason) > ROTATION_REASON_MAX_CHARS:
        raise ValueError(
            f"rotation reason is {len(reason)} characters, past the "
            f"{ROTATION_REASON_MAX_CHARS} the constructor truncates at — "
            f"the clause naming what the sale was cleared on would be cut"
        )
    return reason


def rotation_proposal_reason(
    opportunity: RotationOpportunity,
    *,
    protection_basis: str,
    protection_detail: str,
    headroom_pct: float,
    ceiling_pct: float,
    floor_pct: float,
) -> str:
    """The reason text a PROPOSED rotation close carries into the Risk
    Manager's review — never an authorisation to sell.

    Board item 39. The ranked-margin tier's replacement BUY cannot be
    checked at proposal time: its entry price, stop and size do not exist
    until `PortfolioConstructor` has run, and the gates that refuse it read
    live quotes at execution. So the proposal says so, in the text the Risk
    Manager and the audit row both read, and the SALE itself is built
    separately by `rotation_sell_reason` — which will not produce a string
    at all without a `RotationClearance`.

    Keeping these two apart is the point. A single function that returned a
    usable reason with and without evidence would put the entire guard on
    the caller remembering to pass the evidence.
    """
    if opportunity.tier == "ranked_margin":
        return _ranked_margin_sell_reason(
            opportunity,
            protection_basis=protection_basis,
            protection_detail=protection_detail,
            headroom_pct=headroom_pct,
            ceiling_pct=ceiling_pct,
            floor_pct=floor_pct,
            clearance=None,
        )
    return rotation_sell_reason(
        opportunity,
        protection_basis=protection_basis,
        protection_detail=protection_detail,
        headroom_pct=headroom_pct,
        ceiling_pct=ceiling_pct,
        floor_pct=floor_pct,
    )
