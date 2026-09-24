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

**2026-09-23 — the precondition was pointed at a constraint this book has
never hit, so the comparison has never once been reached.** Across every
retained log rotation (2026-08-21 .. 2026-09-23) the rendered Opportunity
Rotation section took the "real room exists, so there is nothing to rotate
for" branch 51 times out of 51; "Capital is constrained" has occurred zero
times. Rendered RISK-budget headroom ranged 12.93%-24.53% against a 25%
ceiling and never came within twelve percentage points of the 0.50%
`STARTER_POSITION_RISK_PCT` floor the precondition tested it against.

The constraint that actually binds this desk is the §11.2 gross-exposure
ladder and settled cash. On 2026-09-23 the book was at 1.9908x gross
against a 2.00x ceiling with about $92 of ladder headroom on roughly
$10,000 of equity, and the PM's own prompt that run said "the book is
already invested at 199.1% of equity with only $92.20 of ladder headroom" —
while this module, looking at the same moment, saw 14.50% risk headroom and
stayed silent. The desk told its decision-maker there was real room at the
exact moment it could not fund anything.

So the precondition is now **constrained on ANY binding constraint**, not
re-pointed at the gross ladder alone. Two reasons, and the second is the
one that decides it:

  * What rotation is FOR is the question "we cannot take both — is the best
    idea better than the worst holding?" That question is live whenever the
    desk cannot take a new position, and it does not become a different
    question according to which limit stopped it.
  * Re-pointing at the ladder alone would reproduce this very defect
    mirrored. A book at 1.2x gross with plenty of cash but with its risk
    budget exhausted cannot fund a starter position either, and rotation
    would go silent again for exactly the reason it has been silent for a
    month. Replacing one single-constraint blind spot with another is not a
    fix, it is a rotation of the blind spot.

The funding half reuses the desk's own computation and does not write a
second one: `src/pipeline_stages.py::_entry_deployment_budget` is the
number EXECUTION sizes entries against, and it already resolves the §11.2
ladder, mins it against settled cash when margin is disabled, and falls
back to raw cash when the ladder is unreadable. The PM prompt is already
handed that exact figure (`margin_headroom_usd` / `margin_ladder_backed`,
threaded so the Margin Capacity section "never derives its own number"),
so the rotation pre-check is handed the same object rather than a second
opinion about it. "Cannot fund a starter position" was, until 2026-09-24,
the §10.3 notional floor `cash_sweep.min_order_usd` — an arbitrary $500 with
no broker minimum behind it, and Alpaca charges no stock commission, so a
real rotation was being refused on a false "too small to matter" basis. The
`below_min_notional` gate this used to name is retired outright (see
`REQUIRED_BUY_LEG_GATES`) — a rotation whose replacement buy re-sizes to a
genuine ZERO is still refused, via `insufficient_cash`, but a small, nonzero
re-sized buy is no longer rejected for being under that flat floor. No
number is introduced here.

**And every refusal now writes a durable row.** This module used to return
`None` at seven distinct points and record nothing at any of them, while
`rotation_precheck` wrapped the result as a bare `opportunity=None`. The
prompt named no symbol, no score and no ratio, and nothing was persisted —
which is the desk's own standing rule against dropping a candidate without
a durable, per-symbol, machine-readable reason, breached in the one place
that most needs the dataset. `evaluate_rotation` now returns a
`RotationOutcome` carrying a `RotationRefusal` for whichever point was hit,
with the holding and candidate compared, the shared seats, both shared
scores and the ratio; `src/pipeline_stages.py::_record_rotation_refusal`
lands it as a `rotation`/`not_surfaced` pipeline_event through the same
`_record_pipeline_event` path `_rotation_skip` already uses for the
acted-on case. The comparison facts are computed even at the points that
refuse before the comparison matters, because a row saying only "the book
had room" is not a dataset and board item 39(a) — the open research
question behind the unmeasured 25% margin — cannot progress without one.

Neither change touches `ROTATION_MARGIN_PCT` or
`rotation_ranked_margin_enabled`. Both remain blocked by 39(a). What
changed is whether the comparison is ever REACHED and whether its outcome
is RECORDED.

**2026-09-23 — "natural selection" mandate: what it is, and the route that
was designed and REJECTED for it.** The owner's framing: the book runs full
up to the 2.00x gross ceiling (kept at 2.00x — the broker permits ~2x
overnight under Reg-T and the owner does not want forced daily trims), and
"every stock must keep earning its right to be in the portfolio"; when a
better candidate exists and the book is full, the weakest holding should be
displaced for it — a SWAP that never raises gross past the ceiling (the sale
frees the room the buy then takes, sequenced buy-behind-sell as board item
39 already builds). The owner defined "weakest" as lowest CONVICTION, not
worst P&L.

Read literally — rank the still-eligible holdings by conviction and prune
the lowest when a better candidate qualifies — that mandate IS the
ranked-margin tier, and it stays blocked, for two independent reasons, not
one:

  * board item 39(a): "how much better" is `ROTATION_MARGIN_PCT`, an
    arbitrary, UNIDENTIFIABLE constant on the ordinal score (only four
    distinct score ratios occur across the whole band; the fraction of pairs
    clearing any threshold from 0.05 to 0.25 is identical), and 39(a) says
    do not promote it to an execution gate until answered; and
  * holding discipline (retired item 25, `docs/INCIDENT_HISTORY.md`): a
    still-eligible, thesis-intact position is protected from a plain sale
    with no real trigger, and opportunity cost is not one of the triggers —
    so even a ZERO margin (pure "strictly better" ordering) would not make
    an eligible, protected holding sellable. The margin is not the only
    thing in the way.

What the desk therefore delivers, non-arbitrarily and today, is the
CATEGORICAL tier: a holding that has fallen below the desk's own fresh-entry
bar has, by the identical rule a new buy must clear, "stopped earning its
place," and a candidate that clears that same bar displaces it — pass/fail,
no score margin, no invented number. It is live and, since PR #604 pointed
the precondition at the union of every binding limit, actually reachable on
a full book. Its real firing rate is UNMEASURED (it had fired zero times in
the retained logs, and the rate at which a holding's structural protection
breaks on this book — the other conjunct — is itself unmeasured, PR #604
"found not fixed"), so it is honestly described as "categorically reachable,
delivery rate unmeasured," never as "the mandate is fully delivered."

REJECTED, do not re-propose (adversary review, 2026-09-23): choosing WHICH
below-the-bar holding to prune by seat-weighted CONVICTION score. The
categorical set (`blocked`) is not in `ranked` and carries no conviction
rank, so it would have to be scored on the full composite — the exact
coverage-sensitive weighted SUM the 2026-09-14 fix above proved is not
comparable term-for-term across names, and the pairwise shared-seat escape
cannot order N of them. It also inverts intent: an R3 "not BUY-eligible"
name keeps its high seat score and would sort SAFEST, while an R2-neutral
name (no read, score ~0) would always sort weakest. An ordering escapes the
LETTER of the no-arbitrary-numbers rule but not its spirit when the scale is
one the desk's own research condemns. The incumbent tie-break (most blocking
entry-rules failed, then alphabetical) is the better "most clearly stale"
proxy and is kept.

What this change adds is measurement, not a mechanism: `precheck_record`
now carries `held_below_entry_bar` / `_count` (`holdings_below_entry_bar`)
every session — the holdings that would not be bought today — so the desk
can finally see how many of its own names have stopped earning their place,
which is the dataset "natural selection" and 39(a) both lack and which no
new number can substitute for.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.verdicts import RankedCandidate, score_verdict, seat_weight

__all__ = [
    "REQUIRED_BUY_LEG_GATES",
    "ROTATION_MARGIN_PCT",
    "ROTATION_REFUSAL_POINTS",
    "RotationClearance",
    "RotationOpportunity",
    "RotationOutcome",
    "RotationPrecheck",
    "RotationRefusal",
    "evaluate_rotation",
    "evaluate_rotation_opportunity",
    "holdings_below_entry_bar",
    "rotation_binding_constraints",
    "funding_view_measured",
    "rotation_constraint_clause",
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
    # The list was SIX long until 2026-09-23. `daily_loss_recheck` (retired-ok) led it
    # (retired-ok), and it is gone because the refusal it named is gone:
    # the owner's ruling on board item 32 removed the account-level loss
    # halt outright (PR #584), so the execution stage no longer records a
    # skip under that name for a projection to anticipate. Nothing replaces
    # it and nothing
    # absorbs it — a gate for a refusal that cannot fire is a check that
    # always passes, which is worse than no check because it reads like
    # one. The gross-exposure half of what that halt used to police
    # survives untouched in the §11.2 ladder, gated through
    # `insufficient_cash`, measured by `_entry_deployment_budget` on its
    # ladder-backed branch.
    #
    # The list was FIVE long until 2026-09-24. `below_min_notional`
    # (retired-ok) is gone the same way: the flat $500 `min_order_usd`
    # notional floor it named was an arbitrary round number
    # (config/number_ledger.yaml), not a broker minimum, and Alpaca charges
    # no stock commission — a genuine ~$295 / 2.95%-of-equity trade was
    # refused on it as "pays full commission". `_prevent_rotation_naked_sale`
    # no longer refuses a rotation's replacement buy for re-sizing small but
    # nonzero; only a genuine zero still refuses, via `insufficient_cash`.
    "no_price",
    "stale_entry",
    "qty_zero",
    # Added after adversary review of attempt 3. Deterministic function of
    # the POST-SALE book, knowable before the sale — and the refusal a
    # rotation is most likely to hit, because a rotation only surfaces when
    # the risk headroom is already under the floor. Leaving it out was the
    # same class of omission as attempt 1's, one layer further down.
    "insufficient_cash",
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


#: Every point `evaluate_rotation` can decline at, in the order it reaches
#: them. Each one writes a `RotationRefusal` carrying whatever of the
#: comparison was knowable, so the evening review and board item 39(a) read
#: a named ground rather than an absence.
#:
#: `tests/test_rotation.py` pins that every member is reachable and that no
#: return path leaves the refusal unset — a refusal point added to the
#: function and not to this tuple is the failure mode this list exists to
#: stop, because a silent path is exactly what was being fixed.
ROTATION_REFUSAL_POINTS: tuple[str, ...] = (
    # The precondition. Renamed from the old headroom-only test: the book
    # has room on EVERY constraint the desk enforces, so there is genuinely
    # nothing to rotate for.
    "book_not_constrained",
    "no_new_candidates",
    "no_ranked_holdings",
    "held_score_non_positive",
    "full_composite_margin_not_cleared",
    "no_shared_scoring_seat",
    "shared_held_score_non_positive",
    "shared_composite_margin_not_cleared",
)


@dataclass(frozen=True)
class RotationRefusal:
    """Why no rotation was surfaced this session, in a shape a machine can
    group on and a person can read.

    Nothing here is prose-only. `point` is the comparable CODE (one of
    `ROTATION_REFUSAL_POINTS`) and `detail` is the reader-facing sentence —
    the same split `_record_accounted_candidate` uses for exactly the same
    reason: two sessions are compared on the named ground, not on the
    module's choice of words.

    Every field the comparison produced is carried even when the refusal
    happened before that field mattered. A row saying only "the book had
    room" cannot answer board item 39(a); a row saying "the book had room,
    and had it not, NVDA at 4.80 would have been compared against RSG at
    4.20 on Technical+Earnings for a ratio of 1.14" can.
    """

    point: str
    detail: str
    #: Which holding was compared, and which candidate against it. `None`
    #: when this session had no such name at all (an empty book, or no new
    #: candidate ranked) — which is itself the finding.
    held_symbol: str | None = None
    new_symbol: str | None = None
    #: The full composite scores. Coverage-sensitive; see the module
    #: docstring. Kept because the margin's FIRST test is run on them.
    held_score: float | None = None
    new_score: float | None = None
    #: The seats that scored BOTH names, and each side's weighted sum over
    #: exactly those seats — the like-for-like comparison.
    shared_seats: tuple[str, ...] = field(default_factory=tuple)
    held_shared_score: float | None = None
    new_shared_score: float | None = None
    #: `new / held` on the like-for-like sub-score when there is one, else
    #: on the full composite. This is the quantity 39(a) has to be answered
    #: from: the distribution of ratios the desk actually sees. `None` when
    #: no comparable pair existed or the denominator was non-positive.
    ratio: float | None = None
    #: The margin the ratio was (or would have been) judged against, so a
    #: row read months later is not silently re-interpreted under a
    #: different one.
    margin_pct: float = ROTATION_MARGIN_PCT
    #: Which limits were binding when this refusal was taken, in
    #: `rotation_binding_constraints` order. Empty means none were, which
    #: is the `book_not_constrained` case.
    binding: tuple[str, ...] = field(default_factory=tuple)

    def event_kwargs(self) -> dict:
        """The `_record_pipeline_event` payload for this refusal.

        Flat, JSON-safe scalars only: the evidence row is stored as JSON and
        is queried by the evening review, so a nested object here would be
        one more thing to unpack before the dataset is usable.
        """
        return {
            "stage": "rotation",
            "outcome": "not_surfaced",
            "reason": self.point,
            "detail": self.detail[:400],
            "held_symbol": self.held_symbol,
            "new_symbol": self.new_symbol,
            "held_score": self.held_score,
            "new_score": self.new_score,
            "shared_seats": ",".join(self.shared_seats),
            "held_shared_score": self.held_shared_score,
            "new_shared_score": self.new_shared_score,
            "ratio": self.ratio,
            "margin_pct": self.margin_pct,
            "binding": ",".join(self.binding),
        }


@dataclass(frozen=True)
class RotationOutcome:
    """What one rotation evaluation produced: at most one of these is set.

    Exactly one is always set. A `None` opportunity with a `None` refusal
    would be the silent drop this shape exists to make impossible.
    """

    opportunity: RotationOpportunity | None = None
    refusal: RotationRefusal | None = None


def rotation_binding_constraints(
    *,
    headroom_pct: float,
    floor_pct: float,
    entry_budget_usd: float | None,
    min_order_usd: float | None,
) -> tuple[str, ...]:
    """Which of the desk's limits currently stop it taking a new position.

    Two, and the union of them is the precondition (see the module
    docstring for why the union and not the binding one alone):

      * `risk_budget` — `allocate_risk_budget`'s headroom against the
        EXISTING book is already under the minimum this desk will size a
        new idea at. This is the original test, unchanged and still in
        force; it has simply stopped being the only one.
      * `funding` — the dollars `_entry_deployment_budget` says may still
        be deployed will not fund even the §10.3 minimum order. That one
        figure already carries the §11.2 gross ladder, settled cash, and
        the min of the two when margin is disabled, so a single test covers
        both the ladder and the cash constraint without this module
        computing either.

    `entry_budget_usd` / `min_order_usd` of `None` mean the funding view was
    not resolvable this session; the funding test is then simply absent
    rather than guessed at, exactly as `existing_risk_pct=None` already
    disables the risk test. Silence from an unreadable input must not read
    as "there is room" NOR as "the book is full".
    """
    binding: list[str] = []
    if headroom_pct < floor_pct:
        binding.append("risk_budget")
    if (
        isinstance(entry_budget_usd, (int, float))
        and not isinstance(entry_budget_usd, bool)
        and isinstance(min_order_usd, (int, float))
        and not isinstance(min_order_usd, bool)
        and float(entry_budget_usd) < float(min_order_usd)
    ):
        binding.append("funding")
    return tuple(binding)


def funding_view_measured(
    entry_budget_usd: float | None, min_order_usd: float | None,
) -> bool:
    """Was the funding constraint actually READ this session?

    2026-09-23, adversary review. `rotation_binding_constraints` returning
    `()` means "nothing is binding", and the prompt, the owner's report and
    the refusal row all render that as "there is room". When the funding
    figure was never resolvable, `()` would therefore have ASSERTED cash and
    borrowing room from a number nobody read — and the direction is adverse,
    because `_entry_deployment_budget` reports `ladder_backed=False` exactly
    when the gross ceiling is unresolvable, which is the branch where
    execution falls back to raw settled cash and is MOST constrained.

    So the three renderers ask this and say "not measured" rather than "there
    is room". The fallback has never fired in the retained logs (zero
    occurrences of the ladder-unreadable warning), which under this desk's
    own rule is a reason to make it safe, not a reason to trust it.
    """
    return (
        isinstance(entry_budget_usd, (int, float))
        and not isinstance(entry_budget_usd, bool)
        and isinstance(min_order_usd, (int, float))
        and not isinstance(min_order_usd, bool)
    )


def holdings_below_entry_bar(
    blocked: dict[str, list[str]], held_symbols: set[str],
) -> tuple[str, ...]:
    """Which currently-held names would NOT be bought today.

    Owner mandate (2026-09-23): "every stock must keep earning its right to
    be in the portfolio." A held name that now appears in `blocked` — i.e.
    fails the desk's own `candidate_eligibility` entry bar (R2 rating / R3
    BUY-eligible / R5 net evidence / R6 constructor-refused) — has, by that
    identical rule a brand-new buy must clear, stopped earning its place. It
    is the exact set the categorical rotation tier ("ineligible_hold") is
    allowed to prune from.

    This returns the WHOLE set, upper-cased and sorted, as a session-level
    telemetry fact — separate from `evaluate_rotation`'s choice of the ONE
    name to surface. It is recorded every session (see `precheck_record`) so
    the desk can measure how often, and on how many names, a holding has
    decayed below its own entry bar while still on the book — a number
    nothing recorded before, and the one that says whether "natural
    selection" has anything to act on at all. It is a COUNT of a categorical
    membership, not a score or a threshold, so it introduces no arbitrary
    number (board item 39(a) stays untouched).
    """
    held = {str(s).strip().upper() for s in held_symbols if str(s).strip()}
    return tuple(sorted(
        sym.upper() for sym, reasons in blocked.items()
        if reasons and sym.upper() in held
    ))


def evaluate_rotation_opportunity(
    *,
    ranked: list[RankedCandidate],
    blocked: dict[str, list[str]],
    held_symbols: set[str],
    headroom_pct: float,
    floor_pct: float,
    margin_pct: float = ROTATION_MARGIN_PCT,
    entry_budget_usd: float | None = None,
    min_order_usd: float | None = None,
) -> RotationOpportunity | None:
    """`evaluate_rotation`'s opportunity, for callers that want only that.

    Kept because it is this module's long-standing entry point and because
    a caller that genuinely has nowhere to persist a refusal should not be
    forced to pretend otherwise. Every caller inside the pipeline uses
    `evaluate_rotation` and records the refusal.
    """
    return evaluate_rotation(
        ranked=ranked, blocked=blocked, held_symbols=held_symbols,
        headroom_pct=headroom_pct, floor_pct=floor_pct, margin_pct=margin_pct,
        entry_budget_usd=entry_budget_usd, min_order_usd=min_order_usd,
    ).opportunity


def evaluate_rotation(
    *,
    ranked: list[RankedCandidate],
    blocked: dict[str, list[str]],
    held_symbols: set[str],
    headroom_pct: float,
    floor_pct: float,
    margin_pct: float = ROTATION_MARGIN_PCT,
    entry_budget_usd: float | None = None,
    min_order_usd: float | None = None,
) -> RotationOutcome:
    """The one rotation comparison worth surfacing this session, or `None`.

    `ranked` / `blocked` are `PortfolioManagerAgent.rank_candidates`'s own
    output — both current holdings and new candidates already share one
    scale, since eligibility and ranking are computed over every analysed
    symbol without regard to whether it is currently held (Technical reads
    the whole configured universe every session, held names included).

    Silent unless capital is genuinely constrained — on ANY of the limits
    the desk enforces, not just the risk budget. `rotation_binding_
    constraints` is the test: the risk-budget headroom against the EXISTING
    book is under `floor_pct`, OR the dollars `_entry_deployment_budget`
    says are still deployable will not fund the §10.3 minimum order. A book
    with real room on every constraint has nothing to rotate for; refusing
    on "we might want the room later" is not this desk's rule anywhere else
    and is not invented here. See the module docstring for why this is the
    union and for the measurement that forced the change.

    Returns a `RotationOutcome`. When no opportunity is surfaced it carries
    a `RotationRefusal` naming which of `ROTATION_REFUSAL_POINTS` was hit
    and every comparison fact that was knowable there — never a bare
    `None`.
    """
    held = {s.upper() for s in held_symbols}
    binding = rotation_binding_constraints(
        headroom_pct=headroom_pct, floor_pct=floor_pct,
        entry_budget_usd=entry_budget_usd, min_order_usd=min_order_usd,
    )

    new_candidates = [c for c in ranked if c.symbol not in held]
    best_new = new_candidates[0] if new_candidates else None
    held_ranked = [c for c in ranked if c.symbol in held]
    weakest_held = held_ranked[-1] if held_ranked else None

    def _refuse(point: str, detail: str) -> RotationOutcome:
        """One refusal row, with the comparison described as far as it
        exists.

        The description is computed for EVERY point, including the ones
        that decline before the comparison could matter. That is the whole
        value of the row: 39(a) needs the distribution of ratios this book
        actually throws off, and the sessions where the book had room are
        not a different population — they are most of the population.
        """
        return RotationOutcome(refusal=_describe_refusal(
            point=point, detail=detail, best_new=best_new,
            weakest_held=weakest_held, margin_pct=margin_pct, binding=binding,
        ))

    if not binding:
        measured = funding_view_measured(entry_budget_usd, min_order_usd)
        return _refuse(
            "book_not_constrained",
            f"risk headroom {headroom_pct:.2f}% is at or above the "
            f"{floor_pct:.2f}% floor, and "
            + (
                f"${entry_budget_usd:,.2f} deployable is at or above the "
                f"${min_order_usd:,.2f} minimum order — real room on every "
                "constraint"
                if measured else
                "the funding view was NOT MEASURED this session, so no "
                "funding constraint could be tested"
            ),
        )

    if best_new is None:
        return _refuse(
            "no_new_candidates",
            f"every one of the {len(ranked)} ranked names is already held, "
            f"so there is no candidate to rotate INTO",
        )

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
        return RotationOutcome(opportunity=RotationOpportunity(
            new_symbol=best_new.symbol,
            new_score=best_new.score,
            held_symbol=held_symbol,
            held_score=None,
            tier="ineligible_hold",
            reasons=ineligible_held[held_symbol],
            margin_pct=margin_pct,
        ))

    # Tier 2 — ranked margin. Both sides eligible; the weakest held name is
    # simply the last held entry in `ranked`'s own (already-sorted) order.
    if weakest_held is None:
        # nothing held is even ranked — no comparison to make
        return _refuse(
            "no_ranked_holdings",
            f"{len(held)} symbol(s) held but none of them appears in the "
            f"ranking, so there is no holding to compare against",
        )
    if weakest_held.score <= 0:
        # A relative margin against a non-positive score is not a
        # meaningful comparison; a score this weak with no eligibility
        # failure recorded is a state this module does not attempt to
        # interpret further, rather than divide by (near) zero and guess.
        return _refuse(
            "held_score_non_positive",
            f"{weakest_held.symbol} scores {weakest_held.score:.4f}; a "
            f"relative margin against a non-positive score is not a "
            f"meaningful comparison",
        )
    if best_new.score < weakest_held.score * (1.0 + margin_pct):
        return _refuse(
            "full_composite_margin_not_cleared",
            f"{best_new.symbol} ({best_new.score:.4f}) does not clear "
            f"{weakest_held.symbol} ({weakest_held.score:.4f}) by "
            f"{margin_pct:.0%} on the full composite",
        )

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
        return _refuse(
            "no_shared_scoring_seat",
            f"no seat scored both {weakest_held.symbol} and "
            f"{best_new.symbol}, so the two weighted sums are not "
            f"comparable term-for-term",
        )
    shared_seats, held_shared, new_shared = shared
    if held_shared <= 0:
        # same non-positive-denominator refusal as above
        return _refuse(
            "shared_held_score_non_positive",
            f"{weakest_held.symbol} scores {held_shared:.4f} over the "
            f"shared seats ({', '.join(shared_seats)}); no relative margin "
            f"is meaningful against that",
        )
    if new_shared < held_shared * (1.0 + margin_pct):
        return _refuse(
            "shared_composite_margin_not_cleared",
            f"{best_new.symbol} ({new_shared:.4f}) does not clear "
            f"{weakest_held.symbol} ({held_shared:.4f}) by {margin_pct:.0%} "
            f"on the seats scoring both ({', '.join(shared_seats)}), though "
            f"it cleared on the full composite",
        )

    return RotationOutcome(opportunity=RotationOpportunity(
        new_symbol=best_new.symbol,
        new_score=best_new.score,
        held_symbol=weakest_held.symbol,
        held_score=weakest_held.score,
        tier="ranked_margin",
        margin_pct=margin_pct,
        shared_seats=shared_seats,
        held_shared_score=round(held_shared, 4),
        new_shared_score=round(new_shared, 4),
    ))


def _describe_refusal(
    *,
    point: str,
    detail: str,
    best_new: RankedCandidate | None,
    weakest_held: RankedCandidate | None,
    margin_pct: float,
    binding: tuple[str, ...],
) -> RotationRefusal:
    """One refusal row with the comparison filled in as far as it exists.

    The like-for-like sub-score is computed here even at the refusal points
    that never needed it, and the ratio is reported on that sub-score when
    there is one and on the full composite otherwise. The sub-score is the
    comparison the margin is actually judged on
    (`docs/INCIDENT_HISTORY.md` 2026-09-14), so a dataset that recorded
    only the full-composite ratio would be a dataset about the wrong
    quantity.

    Nothing here can raise: this runs on the refusal path, and a row that
    fails to be written is the defect being fixed, not a smaller version of
    it.
    """
    held_symbol = weakest_held.symbol if weakest_held is not None else None
    new_symbol = best_new.symbol if best_new is not None else None
    held_score = weakest_held.score if weakest_held is not None else None
    new_score = best_new.score if best_new is not None else None
    shared_seats: tuple[str, ...] = ()
    held_shared: float | None = None
    new_shared: float | None = None
    if best_new is not None and weakest_held is not None:
        shared = _shared_seat_comparison(held=weakest_held, new=best_new)
        if shared is not None:
            shared_seats, held_raw, new_raw = shared
            held_shared = round(held_raw, 4)
            new_shared = round(new_raw, 4)
    # The ratio 39(a) has to be answered from: like-for-like where one
    # exists, full composite where it does not, and `None` rather than an
    # infinity or a divide-by-zero where the denominator is non-positive.
    ratio: float | None = None
    if held_shared is not None and new_shared is not None and held_shared > 0:
        ratio = round(new_shared / held_shared, 4)
    elif (
        held_score is not None and new_score is not None and held_score > 0
    ):
        ratio = round(new_score / held_score, 4)
    return RotationRefusal(
        point=point,
        detail=detail,
        held_symbol=held_symbol,
        new_symbol=new_symbol,
        held_score=None if held_score is None else round(held_score, 4),
        new_score=None if new_score is None else round(new_score, 4),
        shared_seats=shared_seats,
        held_shared_score=held_shared,
        new_shared_score=new_shared,
        ratio=ratio,
        margin_pct=margin_pct,
        binding=binding,
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

    `refusal` is set whenever `opportunity` is `None` and the evaluation
    actually ran, and is what `_record_rotation_refusal` persists. It is
    `None` only in the telemetry-unavailable case, where there was no
    evaluation to refuse — that case has always had its own prompt line and
    is not a silent drop.

    `entry_budget_usd` / `min_order_usd` / `binding` are the funding view the
    precondition was decided on, kept for the same reason `headroom_pct` is:
    the prompt and the execution stage must read the SAME numbers the
    comparison was made against, never a second measurement taken a moment
    later.
    """

    opportunity: RotationOpportunity | None
    headroom_pct: float
    ceiling_pct: float
    floor_pct: float
    telemetry_available: bool = True
    refusal: RotationRefusal | None = None
    entry_budget_usd: float | None = None
    min_order_usd: float | None = None
    binding: tuple[str, ...] = field(default_factory=tuple)
    #: Owner mandate 2026-09-23 ("every stock must keep earning its place").
    #: Every held name that would NOT be bought today because it now fails
    #: the desk's own entry bar (`holdings_below_entry_bar`). Session-level
    #: telemetry, recorded whether or not the book is constrained: it is the
    #: set the categorical tier may prune from, and its size says whether
    #: natural selection has anything to act on. A categorical membership,
    #: never a score — no arbitrary number.
    held_below_entry_bar: tuple[str, ...] = field(default_factory=tuple)


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

    `projected_positions` / `projected_equity` / `projected_entry_budget` /
    `projected_budget_basis` are recorded so the audit row states the
    numbers the sale was actually cleared on, rather than asserting that a
    check happened.
    """

    held_symbol: str
    new_symbol: str
    #: Every gate evaluated. Must cover `REQUIRED_BUY_LEG_GATES`.
    gates_checked: tuple[str, ...]
    #: What `_entry_deployment_budget` said was deployable on the projected
    #: post-sale book, and the note it gave for WHICH pool that was — the
    #: §11.2 gross-ladder headroom or settled cash. This pair replaced the
    #: projected day-change and its limit rung on 2026-09-23, when the
    #: account-level loss halt those described was removed (PR #584). It is
    #: the quantity the funding gates below actually clear the sale on, so
    #: it is the one the audit row should carry.
    projected_entry_budget: float
    projected_budget_basis: str
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


def rotation_constraint_clause(
    *,
    binding: tuple[str, ...] = (),
    headroom_pct: float,
    ceiling_pct: float,
    floor_pct: float,
    entry_budget_usd: float | None = None,
    min_order_usd: float | None = None,
) -> str:
    """The clause a rotation's own audit record carries naming WHY there was
    no room. One sentence, checkable, and about the limit that was actually
    binding.

    2026-09-23, found by adversary review of this change. Re-pointing the
    precondition at the funding constraint without re-pointing this made the
    SALE's reason false in exactly the new case: the string written onto the
    broker order and handed to the Risk Manager would have read "Headroom
    14.50% of the 25.00% risk ceiling, under the 0.50% minimum" — an
    arithmetic claim that is plainly untrue — on a live sale, produced only
    by the branch this change exists to open. The PM prompt and the owner's
    report were re-pointed and this was not. Same class as the 2026-09-17
    CRM incident, one function over.

    An empty `binding` reproduces the legacy sentence byte-for-byte, so every
    caller that has not been threaded the funding view is unchanged.
    """
    if "funding" in binding and isinstance(entry_budget_usd, (int, float)) \
            and isinstance(min_order_usd, (int, float)):
        risk = (
            f"Risk headroom {headroom_pct:.2f}% of {ceiling_pct:.2f}%, under "
            f"the {floor_pct:.2f}% minimum; "
            if "risk_budget" in binding else ""
        )
        return (
            f"{risk}${float(entry_budget_usd):,.0f} deployable, under the "
            f"${float(min_order_usd):,.0f} minimum order."
        )
    return (
        f"Headroom {headroom_pct:.2f}% of the {ceiling_pct:.2f}% risk "
        f"ceiling, under the {floor_pct:.2f}% minimum."
    )


def rotation_sell_reason(
    opportunity: RotationOpportunity,
    *,
    protection_basis: str,
    protection_detail: str,
    headroom_pct: float,
    ceiling_pct: float,
    floor_pct: float,
    clearance: "RotationClearance | None" = None,
    #: 2026-09-23. The binding constraints and the funding view, so the
    #: clause naming WHY there was no room states the limit that actually
    #: bound rather than always quoting the risk ceiling. Empty/`None`
    #: reproduces the legacy sentence byte-for-byte.
    binding: tuple[str, ...] = (),
    entry_budget_usd: float | None = None,
    min_order_usd: float | None = None,
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
            binding=binding,
            entry_budget_usd=entry_budget_usd,
            min_order_usd=min_order_usd,
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
        f"{rotation_constraint_clause(binding=binding, headroom_pct=headroom_pct, ceiling_pct=ceiling_pct, floor_pct=floor_pct, entry_budget_usd=entry_budget_usd, min_order_usd=min_order_usd)} Full close to free room for "
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
    binding: tuple[str, ...] = (),
    entry_budget_usd: float | None = None,
    min_order_usd: float | None = None,
) -> str:
    """The checkable reason a RANKED-MARGIN rotation sale carries.

    Same discipline as the categorical reason above — every clause names
    something recorded elsewhere this run — with two additions the
    categorical tier does not need:

      * the like-for-like sub-score the margin was actually cleared on
        (`shared_seats`, `docs/INCIDENT_HISTORY.md` 2026-09-14), because
        the full composite is a coverage-sensitive weighted SUM and is not
        comparable term-for-term between two names; and
      * the deployable budget the projected post-sale book produced, which
        is what the replacement BUY was cleared against, so the Risk
        Manager and the evening review can check that the sale was
        sequenced behind the buy's gates rather than ahead of them (board
        item 39).

    Kept compact for the same 500-character truncation in
    `PortfolioConstructor._build_sell`.
    """
    seats = ("; ".join(opportunity.shared_seats) or "none")[:40]
    held_shared = opportunity.held_shared_score
    new_shared = opportunity.new_shared_score
    held_score = opportunity.held_score
    tail = (
        f"BUY pre-cleared post-sale (deployable "
        f"${clearance.projected_entry_budget:.0f}, "
        f"{clearance.projected_budget_basis})."
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
        f"intact ({protection_basis}: {protection_detail[:40]}). "
        f"{rotation_constraint_clause(binding=binding, headroom_pct=headroom_pct, ceiling_pct=ceiling_pct, floor_pct=floor_pct, entry_budget_usd=entry_budget_usd, min_order_usd=min_order_usd)} {tail}"
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
    binding: tuple[str, ...] = (),
    entry_budget_usd: float | None = None,
    min_order_usd: float | None = None,
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
            binding=binding,
            entry_budget_usd=entry_budget_usd,
            min_order_usd=min_order_usd,
        )
    return rotation_sell_reason(
        opportunity,
        protection_basis=protection_basis,
        protection_detail=protection_detail,
        headroom_pct=headroom_pct,
        ceiling_pct=ceiling_pct,
        floor_pct=floor_pct,
        binding=binding,
        entry_budget_usd=entry_budget_usd,
        min_order_usd=min_order_usd,
    )


# ---------------------------------------------------------------------------
# owner-facing account of the pre-check (board item: the missing "why not")
# ---------------------------------------------------------------------------
#
# The pre-check runs every session and its result reached NOTHING the owner
# reads. Worse, its commonest outcome — capital constrained AND no candidate
# good enough to sell a holding for — returned silently from
# `_apply_rotation_execution`, so it left no log line and no durable row
# either. The owner's words (2026-09-23): "Yes portfolio is full. But we're
# still reviewing things, which is how we built it. Report has to show that
# properly."
#
# ONE vocabulary, two consumers. `precheck_record` turns the pre-check into
# the durable audit row; `owner_precheck_lines` renders THAT SAME row for the
# owner. The model-facing prompt text in
# `PortfolioManagerAgent._render_rotation_section` is deliberately left
# byte-for-byte alone — rewording a live seat's prompt is a behaviour change
# on a trading path — but the four cases below are the same four cases it
# branches on, in the same order, and `tests/test_rotation.py` holds them to
# that.

#: The four mutually exclusive outcomes of one pre-check. A `reason` value,
#: i.e. a rule name and never prose — the same split `pm_accounting` uses.
ROTATION_TELEMETRY_UNAVAILABLE = "telemetry_unavailable"
ROTATION_ROOM_AVAILABLE = "room_available"
ROTATION_FULL_NOTHING_BETTER = "full_nothing_outranked_a_holding"
ROTATION_FULL_OPPORTUNITY = "full_candidate_outranked_a_holding"


def precheck_outcome(precheck) -> str:
    """Which of the four cases this pre-check is. Pure, no config read.

    2026-09-23: "is the book full" is `precheck.binding` — whether ANY of
    the desk's limits stops it taking a new position — and no longer the
    risk budget alone. It was the risk budget alone, which is why this
    reported `room_available` on all 51 retained sessions including the one
    where $92.20 of deployable budget could not fund a $500 order. The
    owner's report was telling him the desk had room at the moment it had
    none; see the module docstring.
    """
    if not getattr(precheck, "telemetry_available", True):
        return ROTATION_TELEMETRY_UNAVAILABLE
    if getattr(precheck, "opportunity", None) is not None:
        return ROTATION_FULL_OPPORTUNITY
    if _precheck_binding(precheck):
        return ROTATION_FULL_NOTHING_BETTER
    return ROTATION_ROOM_AVAILABLE


def _precheck_binding(precheck) -> tuple[str, ...]:
    """Which limits bound this pre-check, recomputed when the object does
    not carry them.

    A `RotationPrecheck` built before `binding` existed (an older durable
    row replayed, a test fixture, a caller that has not been updated) still
    carries `headroom_pct` and `floor_pct`, and dropping the risk-budget
    constraint for those would be a silent behaviour change in the opposite
    direction from the one this file is fixing. The recomputation is
    `rotation_binding_constraints` itself, not a second copy of the rule.
    """
    carried = tuple(getattr(precheck, "binding", ()) or ())
    if carried:
        return carried
    return rotation_binding_constraints(
        headroom_pct=float(getattr(precheck, "headroom_pct", 0.0) or 0.0),
        floor_pct=float(getattr(precheck, "floor_pct", 0.0) or 0.0),
        entry_budget_usd=getattr(precheck, "entry_budget_usd", None),
        min_order_usd=getattr(precheck, "min_order_usd", None),
    )


def precheck_record(
    precheck, *, execute_enabled: bool, ranked_margin_enabled: bool,
) -> dict:
    """The durable audit payload for one pre-check, and the only input
    `owner_precheck_lines` reads.

    Carries the two switches as measured facts, not as advice: the report
    must be able to say that a comparison the desk is not permitted to act
    on was information only, rather than letting the owner believe the desk
    weighed it and declined.
    """
    opportunity = getattr(precheck, "opportunity", None)
    record = {
        "outcome": precheck_outcome(precheck),
        "headroom_pct": float(getattr(precheck, "headroom_pct", 0.0) or 0.0),
        "ceiling_pct": float(getattr(precheck, "ceiling_pct", 0.0) or 0.0),
        "floor_pct": float(getattr(precheck, "floor_pct", 0.0) or 0.0),
        "execute_enabled": bool(execute_enabled),
        "ranked_margin_enabled": bool(ranked_margin_enabled),
        # 2026-09-23. The funding view the precondition was decided on, and
        # which limits were binding when it was. Without these the row
        # cannot be read back: "the book was full" and "the book had room"
        # are the same sentence under two different definitions of full.
        "entry_budget_usd": _opt_float(getattr(precheck, "entry_budget_usd", None)),
        "min_order_usd": _opt_float(getattr(precheck, "min_order_usd", None)),
        "binding": ",".join(_precheck_binding(precheck)),
        # Owner mandate 2026-09-23. How many current holdings would not be
        # bought today, and which — the "has every stock kept earning its
        # place" signal. Recorded every session, constrained or not, because
        # a name that decayed below the entry bar while the book still had
        # room is precisely the case natural selection is not yet acting on.
        "held_below_entry_bar": ",".join(
            getattr(precheck, "held_below_entry_bar", ()) or ()
        ),
        "held_below_entry_bar_count": len(
            getattr(precheck, "held_below_entry_bar", ()) or ()
        ),
    }
    if opportunity is not None:
        record.update({
            "tier": str(getattr(opportunity, "tier", "") or ""),
            "held_symbol": str(getattr(opportunity, "held_symbol", "") or ""),
            "new_symbol": str(getattr(opportunity, "new_symbol", "") or ""),
        })
        return record
    # 2026-09-23 — the near-miss half. `evaluate_rotation` declined at one of
    # `ROTATION_REFUSAL_POINTS` and knows which holding was weighed against
    # which candidate, on what seats, at what ratio. Folded into THIS row
    # rather than written as a second one: two rows for one session's one
    # comparison is the duplicate-shape defect this file has already been
    # bitten by, and the reader would have to join them to learn anything.
    refusal = getattr(precheck, "refusal", None)
    if isinstance(refusal, RotationRefusal):
        record.update(refusal.event_kwargs())
        # `event_kwargs` carries its own stage/outcome/reason for the
        # standalone shape; this row's outcome is the pre-check's, and the
        # refusal point rides as its own named field so neither overwrites
        # the other.
        record.pop("stage", None)
        record.pop("outcome", None)
        record["refusal_point"] = record.pop("reason", "")
        record["outcome"] = precheck_outcome(precheck)
    return record


def _opt_float(value) -> float | None:
    """A finite float, or `None` for anything that is not one. `None` here
    means NOT MEASURED, and must never be flattened to 0.0 — a zero budget
    and an unread budget are opposite facts about whether the desk may
    trade."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def _pct(value) -> str:
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "?"


def _full_book_cause(record: dict, *, headroom: str, ceiling: str,
                     floor: str) -> str:
    """Why the book is full, in the owner's words, naming the limit that
    actually stopped it.

    Both limits can bind at once and then both are said, because "we are out
    of risk budget" and "we are out of cash" are different problems with
    different answers and collapsing them would hide whichever the owner
    could do something about.
    """
    binding = [
        b for b in str(record.get("binding") or "").split(",") if b
    ]
    causes: list[str] = []
    if "risk_budget" in binding:
        causes.append(
            f"only {headroom} of risk headroom left under the desk's "
            f"{ceiling} ceiling, below the {floor} smallest position this "
            "desk will trade"
        )
    if "funding" in binding:
        budget = record.get("entry_budget_usd")
        minimum = record.get("min_order_usd")
        causes.append(
            f"only ${float(budget):,.0f} of cash and borrowing room left, "
            f"below the ${float(minimum):,.0f} smallest order worth placing"
            if isinstance(budget, (int, float))
            and isinstance(minimum, (int, float))
            else "no cash or borrowing room left to open a new position"
        )
    if not causes:
        # An older row, written before `binding` existed. Say what that row
        # can support rather than inventing a cause for it.
        causes.append(
            f"only {headroom} of risk headroom left under the desk's "
            f"{ceiling} ceiling"
        )
    return " and ".join(causes) + "."


def owner_precheck_lines(record: dict | None) -> list[str]:
    """The pre-check said to the owner, in plain words, on a phone.

    NOT a warning and never rendered as one. A full book is a normal
    operating state of this desk — the owner asked specifically that it read
    that way — so the only thing this block reports is what was compared and
    what the comparison concluded.
    """
    if not isinstance(record, dict) or not record:
        return []
    outcome = str(record.get("outcome") or "")
    headroom = _pct(record.get("headroom_pct"))
    ceiling = _pct(record.get("ceiling_pct"))
    floor = _pct(record.get("floor_pct"))

    if outcome == ROTATION_TELEMETRY_UNAVAILABLE:
        return [
            "🔄 Rotation check: not run this session — the book's own risk "
            "figures could not be read, so nothing was compared. Nothing "
            "was sold or bought because of this."
        ]
    if outcome == ROTATION_ROOM_AVAILABLE:
        # Adversary review 2026-09-23: only claim the cash side when it was
        # actually read. Asserting "enough cash to open a new position" from
        # a figure that came back unreadable is the same silence this change
        # exists to remove, wearing a reassuring sentence.
        if funding_view_measured(
            record.get("entry_budget_usd"), record.get("min_order_usd"),
        ):
            return [
                f"🔄 Rotation check: not needed — there is still {headroom} "
                f"of risk headroom under the desk's {ceiling} ceiling and "
                "enough cash and borrowing room to open a new position, so "
                "nothing had to be sold to make room for a new idea."
            ]
        return [
            f"🔄 Rotation check: not needed on risk — there is still "
            f"{headroom} of risk headroom under the desk's {ceiling} "
            "ceiling. The desk could NOT read how much cash and borrowing "
            "room it had this session, so that side was not checked and "
            "nothing was sold on it."
        ]

    # 2026-09-23. Name the limit that is ACTUALLY binding. Before this the
    # sentence always quoted risk headroom against the risk ceiling, and
    # would have gone on quoting it while the real cause was $92 of cash
    # against a $500 minimum order — a true-sounding sentence about the
    # wrong number.
    full = "🔄 Rotation check: the book is FULL — " + _full_book_cause(
        record, headroom=headroom, ceiling=ceiling, floor=floor,
    ) + " This is a normal state, not a fault."
    if outcome == ROTATION_FULL_NOTHING_BETTER:
        return [
            full,
            "   Every new candidate was still ranked against what is "
            "already held, and none of them beat a holding by enough to be "
            "worth selling one for. The desk is keeping what it has on "
            "stronger conviction.",
        ]
    if outcome != ROTATION_FULL_OPPORTUNITY:
        return []

    held = str(record.get("held_symbol") or "?").upper()
    new = str(record.get("new_symbol") or "?").upper()
    tier = str(record.get("tier") or "")
    lines = [
        full,
        f"   Every new candidate was ranked against what is already held, "
        f"and one comparison came out the other way: {new} outranks {held}, "
        f"the weakest thing currently using the room.",
    ]
    if not record.get("execute_enabled"):
        lines.append(
            "   The desk is not switched on to act on this by itself, so "
            "this is information only — nothing was sold."
        )
    elif tier == "ranked_margin" and not record.get("ranked_margin_enabled"):
        lines.append(
            "   This is the score-margin kind of comparison, which the desk "
            "is not switched on to act on. It was not weighed and declined "
            "— it was never put to the desk to act on at all."
        )
    return lines
