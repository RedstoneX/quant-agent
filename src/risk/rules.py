import logging
import math
import statistics
import sys
from dataclasses import dataclass, field
from src.config import RiskConfig
from src.models import TradeDecision, Position, AnalystVerdict
# The leverage table and the two multiplier functions now live in
# `src.quantities` — a dependency-free module OUTSIDE `src.risk`, so
# `src/api/` (forbidden by tests/test_api_safety.py from importing the risk
# stack) can share this one definition instead of hand-copying it. These
# aliases keep every existing `_ETF_LEVERAGE` / `_effective_multiplier` /
# `_gross_multiplier` reference in this module and its importers working,
# and bind the SAME dict object, so `patch.dict(rules._ETF_LEVERAGE, ...)`
# still reaches every consumer.
from src.quantities import (
    ETF_LEVERAGE as _ETF_LEVERAGE,
    effective_multiplier as _effective_multiplier,
    gross_multiplier as _gross_multiplier,
    net_exposure_pct,
    net_exposure_usd,
)

logger = logging.getLogger(__name__)


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
    positions, *, resolve_sector=None, include_unknown: bool = False,
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
    pending: dict[tuple[str, str], float], sector: str, action: str,
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
    positions, total_value: float, *, resolve_sector=None,
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
            positions, resolve_sector=resolve_sector,
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
    current_sector_pct: float, *, soft_cap_pct: float, hard_cap_pct: float,
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
    current_sector_pct: float, *, soft_cap_pct: float, hard_cap_pct: float,
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
        current_sector_pct, soft_cap_pct=soft_cap_pct, hard_cap_pct=hard_cap_pct,
    )


# --- Spec §9.4 "agreement earns size" -------------------------------------
#
# Since 2026-09-02 the quantity in play is a SIGNED SUM, not a headcount:
# `signed_source_score` nets opposed seats off aligned ones. Since
# 2026-09-14 it no longer earns SIZE at all — `agreement_refuses_trade`
# turns it into one yes/no refusal and nothing else (see that function for
# why the graduated ceiling was retired). The two counts below are still
# computed and still reported — a reader wants "2 for, 1 against", not only
# "net +1" — but neither of them sizes anything.
#
# Shared polarity vocabulary. `PortfolioManagerAgent.validate_grounding`
# (src/agents/portfolio_manager.py) uses this to decide whether a
# provenance claim's stance "supports" a target's direction; the
# constructor's agreement refusal (`src/portfolio_constructor.py`) uses the
# SAME rule to count how many of the canonical evidence registry's
# independent sources are directionally aligned with what the PM is
# actually proposing. One definition, two consumers, by design — a second,
# divergent notion of "aligned" here would let the ceiling and the
# grounding gate disagree about identical evidence.
_BULLISH_STANCES = frozenset({
    "strong_buy", "buy", "bullish", "positive", "risk_on",
    "overweight", "favorable",
})
_BEARISH_STANCES = frozenset({
    "strong_sell", "sell", "bearish", "negative", "risk_off",
    "underweight", "unfavorable",
})


def stance_is_aligned(source: str, symbol: str, stance: str, *, wants_bullish: bool) -> bool:
    """True when `stance` (a canonical registry stance for `source` on
    `symbol`) points the direction `wants_bullish` asks for.

    Carries the one twist `validate_grounding` has always applied: a
    risk-off MACRO stance supports owning an INVERSE ETF, so macro's
    polarity is flipped for a symbol with a negative effective multiplier
    — the rating still describes the ETF's own price, which moves
    opposite the index it inverts.
    """
    stance_is_bullish = stance in _BULLISH_STANCES
    stance_is_bearish = stance in _BEARISH_STANCES
    if source == "macro" and _effective_multiplier(symbol) < 0:
        stance_is_bullish, stance_is_bearish = stance_is_bearish, stance_is_bullish
    return stance_is_bullish if wants_bullish else stance_is_bearish


#: §9.4 freshness. An earnings stance older than this many days stops
#: COUNTING toward the agreement tally (it is still shown to the PM, and
#: still valid provenance — see `PortfolioManagerAgent.stale_evidence_sources`).
#:
#: Also (2026-09-02) the bound `EarningsDataProvider._get_existing_analysis`
#: (`src/data/earnings.py`) uses to decide whether a disk-cached analysis is
#: even eligible to be re-served as the CURRENT earnings view when no fresher
#: SEC filing exists — past this age that fallback returns None instead, so
#: an over-age analysis never reaches a session in the first place. That is a
#: stricter, earlier check than `stale_evidence_sources` above: this one
#: decides whether a stance is shown at all, that one decides whether a
#: stance that IS shown gets to count. Same threshold, so the two can never
#: disagree about what "too old" means.
#:
#: 90 is not a new number. `config/prompts/earnings_analyst.md` already tells
#: the seat that a filing more than 90 days old must cap its own `conviction`
#: at `low` and flag `stale_filing_<N>d`, and that past 180 days the filing
#: "should not have reached you"; `TradingPipeline._missed_ops_earnings_signal`
#: already refuses anything older than 90 days as "recent earnings evidence".
#: The seat and the reflector had a threshold; only the SIZING path did not
#: read it. Reusing 90 makes the sizing path agree with the opinion the desk
#: had already written down, rather than adding a fourth staleness rule.
EARNINGS_STANCE_MAX_AGE_DAYS = 90


def _count_sources(
    symbol: str,
    sources: dict[str, str],
    *,
    wants_bullish: bool,
    ignored_sources: frozenset[str] | set[str] | None,
) -> int:
    ignored = ignored_sources or frozenset()
    return sum(
        1 for source, stance in sources.items()
        if source not in ignored
        and stance_is_aligned(source, symbol, stance, wants_bullish=wants_bullish)
    )


def count_aligned_sources(
    symbol: str,
    sources: dict[str, str],
    direction: str,
    *,
    ignored_sources: frozenset[str] | set[str] | None = None,
) -> int:
    """The deterministic "agreement count": how many independent seats (of
    technical/news/earnings/macro/smart_money) recorded a stance for
    `symbol` that points the same way as `direction` ("long" wants
    bullish, "short" wants bearish).

    `sources` must be one symbol's slice of the canonical evidence
    registry (`PortfolioManagerAgent.build_evidence_registry`) — ALL
    current coverage, not just what a target's own `provenance` list
    happens to cite. That distinction is the point: this count is what
    earns size, so it has to come from evidence the PM cannot selectively
    quote from, not from the PM's own (possibly incomplete) claims about
    itself.

    `ignored_sources` names the seats whose stance for THIS symbol is too
    stale to earn size — currently only `earnings`, gated at
    `EARNINGS_STANCE_MAX_AGE_DAYS` by
    `PortfolioManagerAgent.stale_evidence_sources`. It is a REMOVAL from the
    tally, never an addition, so it can only ever lower a ceiling. A stale
    stance stays in the registry (it is still real coverage, and the PM may
    still cite it) — it simply stops being paid for.
    """
    return _count_sources(
        symbol, sources,
        wants_bullish=(direction != "short"),
        ignored_sources=ignored_sources,
    )


def count_opposing_sources(
    symbol: str,
    sources: dict[str, str],
    direction: str,
    *,
    ignored_sources: frozenset[str] | set[str] | None = None,
) -> int:
    """How many independent seats took the side OPPOSITE `direction`.

    The exact mirror of `count_aligned_sources`: on a long it counts bearish
    stances, on a short bullish ones, through the same `stance_is_aligned`
    vocabulary (inverse-ETF macro flip included). Neutral and mixed stances
    are in NEITHER count — a seat with no view took no side.

    Reported on its own (PM prompt, constructor log, order note) as well as
    consumed by `signed_source_score`, because "2 for, 1 against" and
    "net +1" are different facts about the same evidence and a reader wants
    both. The SIZING consumer is the score, never this count alone.
    """
    return _count_sources(
        symbol, sources,
        wants_bullish=(direction == "short"),
        ignored_sources=ignored_sources,
    )


#: §9.4 signed sum. Every seat enters the score at UNIT magnitude — +1 when it
#: is aligned with the proposed direction, -1 when it is opposed, 0 when it is
#: neutral or silent. Equal weighting is not a placeholder for a better number:
#: it is what published composite-index construction literally does, and what
#: Grinold & Kahn's `alpha = volatility x IC x score` reduces to when per-source
#: skill is equal.
#:
#: **Do not replace this with a per-seat weight table.** `src/conviction_ledger.py`
#: records the desk's standing rule (owner, 2026-08-31): a confidence weight may
#: only be DERIVED from an analyst's own measured history, never chosen up front,
#: and `_CONVICTION_OUTCOME_MIN_N` (`src/storage/db.py`) puts the minimum sample
#: at 20 resolved calls. The book has 7 closed equity round-trips, all carrying
#: conviction NULL — there is no sample to derive from, so any weight introduced
#: today would be a chosen one. `tests/test_signed_dissent.py` enforces this
#: mechanically rather than by memory.
SEAT_WEIGHT: int = 1


def signed_source_score(
    symbol: str,
    sources: dict[str, str],
    direction: str,
    *,
    ignored_sources: frozenset[str] | set[str] | None = None,
) -> int:
    """`S` — the §9.4 signed sum: aligned seats minus opposed seats.

    `S = sum(s_i)` over the five evidence seats, where `s_i` is `+SEAT_WEIGHT`
    for a seat pointing the way `direction` proposes, `-SEAT_WEIGHT` for one
    pointing the other way, and 0 for a seat that is neutral or absent.

    Expressed as the DIFFERENCE of the two counts rather than as a second
    traversal, on purpose: the aligned and opposed counts are what the PM is
    shown and what the order note records, so the number that sizes the trade
    is arithmetically the same number the reader was given. A second walk over
    `sources` here would be a second definition of "aligned" — the exact defect
    `stance_is_aligned`'s one-vocabulary comment exists to prevent.

    WHAT THIS REPLACED (2026-09-02). §9.4 used to ceiling size on the aligned
    count alone, so a seat that stayed SILENT, a seat that rated NEUTRAL and a
    seat that ACTIVELY DISAGREED all contributed exactly zero. `three aligned`
    and `three aligned, one against` bought identical size. No published
    composite methodology counts only agreers: a disagreeing input enters a
    composite as a negative number in a signed sum, and that is what this is.
    """
    return SEAT_WEIGHT * (
        count_aligned_sources(
            symbol, sources, direction, ignored_sources=ignored_sources,
        )
        - count_opposing_sources(
            symbol, sources, direction, ignored_sources=ignored_sources,
        )
    )


def agreement_refuses_trade(score: int) -> bool:
    """Does the net evidence REFUSE this trade outright? Not a ceiling.

    True for a signed source score at or below zero — a name with no net
    evidence for the direction proposed, or with net dissent against it. The
    caller maps that to `SizeOverride.no_trading()`: the target is dropped,
    no order is built, and anything already held is left exactly where it is
    (refusing to BUY is not a decision to SELL).

    False for any net score of 1 or more, and that is the WHOLE of what
    agreement does to size now. There is no rung, no ladder and no per-score
    number: a target that clears the refusal is bounded by the ratified
    per-trade envelope (`RiskConfig.max_position_risk_pct`) and by the
    portfolio budget allocator, exactly like every other target.

    **Why the graduated ceiling was retired (owner decision, 2026-09-14.)**
    Until this change §9.4 scaled permitted risk by the net seat count as
    `max_position_risk_pct x sqrt(n / 5)`. The square-root law is the
    statistics of averaging INDEPENDENT estimates, and this desk's seats are
    not independent: technical, news, earnings, macro and smart_money read
    overlapping evidence (the same tape, the same bars, the same filings)
    and several are the same underlying model behind different prompts. The
    archetype's one precondition is unmet, so the schedule could not be
    justified at any slope — and no honest correlation haircut exists to
    replace it with, only an invented number.

    Secondarily, a graduated ceiling cannot tell "the seats disagreed" from
    "the seats had nothing to look at". A thinly-covered name and a
    contested one arrive at the same low net score and were sized the same.
    That is the identical defect already fixed in the rotation rule (retired
    board item 66), and the refusal below is the only place the distinction
    is safe to act on: at or below zero the desk declines, and above zero it
    does not pretend to grade.

    What agreement still does, unchanged: it ORDERS which candidates get
    funded first, through `src/verdicts.py::rank_verdicts` and
    `allocate_risk_budget`'s `priority` (retired board item 49). Agreement
    earns the QUEUE POSITION. It no longer sets the size.
    """
    return score <= 0


# --- Owner mandate 2026-09-25 — the ROLE-BASED conviction bar (R7) ---------
#
# "Earn the right to ENTER and to STAY." A name clears this bar only when a
# SUPPORTIVE seat carries a real, falsifiable reason AND no seat is opposed AND
# the chart is not fighting the trade. It is deliberately SEPARATE from
# `agreement_refuses_trade` (the §9.4 net-evidence floor) and from the
# continuous ranking score in `src/verdicts.py`: those grade and order, this
# one is a role-aware yes/no.
#
# WHY THIS INTRODUCES NO ARBITRARY NUMBER. The whole rule is counts, booleans
# and non-empty checks — "at least one" is existence, not a dial; "no seat
# opposed" is zero, not a chosen floor; the technical veto is a boolean. There
# is nothing here to ledger as `arbitrary` (contrast the discarded HIGH-count
# gate, which would have needed two owner-appetite numbers). The wider change
# carries no number at all: the STAY side culls a held name ONLY when a seat is
# ACTIVELY OPPOSED (owner ruling 2026-09-25), which is zero-versus-count, not a
# dial — there is no confirmation window and no streak.

#: Reasons these functions emit all start with this tag, the SAME string
#: `src.rotation.CONVICTION_BAR_REASON_PREFIX` matches on, so the STAY cull
#: (rotation's `ineligible_hold` tier) recognises exactly these reasons and
#: `holdings_below_entry_bar` counts them. A test pins the two equal.
OWN_BAR_REASON_PREFIX = "R7 conviction bar"


def _has_supported_directional_thesis(v: "AnalystVerdict", aligned: str) -> bool:
    """A NON-technical seat that took a SUPPORTED DIRECTIONAL side.

    MECHANICAL DEFINITION, honest about what the seats actually emit
    (2026-09-25, renamed 2026-09-25 to match what this actually enforces).
    Only Technical carries a machine-readable named invalidation LEVEL (a stop
    price); Earnings carries a genuine analyst-authored falsifier
    (`bear_case`/`bull_case`) or its verdict refuses to build; Macro carries a
    real trigger when the analyst stated one, else a generic fallback; News and
    Smart-money ALWAYS synthesise a templated/constructed invalidation. So there
    is no distinct "has a named falsifier" boolean to read downstream — that
    distinction is lost when `to_verdict()` runs, and News/Smart-money can
    NEVER fail this check on specificity grounds since their invalidation is
    always synthesised. This is NOT a test for a genuinely specific or
    falsifiable thesis — it cannot tell a templated invalidation from an
    analyst-authored one. Requiring a NON-generic invalidation string would
    couple this gate to those exact template sentences, which rot.

    What this actually checks, and all it checks: a supportive seat that took
    a DIRECTIONAL side (not a lukewarm neutral shrug), which
    `AnalystVerdict`'s own validator then FORCES to carry a non-empty
    invalidation condition AND at least one checkable evidence item. Technical
    is excluded — it adds no positive weight, it only gates (below).
    """
    return (
        v.seat != "technical"
        and v.direction == aligned
        and bool((v.invalidation or "").strip())
        and bool(v.evidence)
    )


def _is_broadcast_macro_verdict(v: "AnalystVerdict") -> bool:
    """True for a MACRO verdict whose direction is the MARKET-WIDE
    `equity_outlook` broadcast, not a name/sector-SPECIFIC stance.

    `MacroAnalysis.to_verdict` (src/models.py) sets a symbol's macro direction
    to its SECTOR's own stance when this read stated one for that sector, and
    falls back to the broad `equity_outlook` otherwise. It marks the difference
    on the verdict itself: a sector-specific direction carries a
    `sector_stance:<sector>` evidence label (added there precisely so a reader
    can see WHY a symbol's macro direction differs from the broad one); the
    broadcast fallback carries no such label.

    A market-wide macro view is NOT a name-specific edge — owner ruling: macro
    alone cannot drive a name decision. So a broadcast-macro verdict must never
    count as per-name OPPOSITION. Without this, one bearish `equity_outlook`
    flip broadcasts "macro opposed" onto every held long that has no bullish
    sector row, culling the whole non-price-protected long book on a single
    review and blocking every new entry in any cautious-macro regime. A
    sector-SPECIFIC bearish macro stance is a genuine name-level opposition and
    still counts.
    """
    if v.seat != "macro":
        return False
    return not any(
        str(getattr(ev, "label", "") or "").startswith("sector_stance:")
        for ev in (v.evidence or [])
    )


def own_bar_block_reason(
    seat_verdicts: list["AnalystVerdict"],
    *,
    direction: str,
) -> str | None:
    """The role-based conviction bar (owner mandate 2026-09-25), as one yes/no.

    `None` when the name CLEARS the bar for `direction`, else the one-line
    reason it does not. ONE definition drives ENTRY (`candidate_eligibility`
    R7) and STAYING (the same `blocked` set, via rotation's `ineligible_hold`
    tier). Pure: a function of the seat `AnalystVerdict`s for one name only.

    The bar clears iff ALL of:

      1. TECHNICAL TIMING VETO not triggered. Technical is a timing gate, not a
         yes-vote: it must be PRESENT and CONFIRMING (aligned with the trade).
         A bearish technical read (chart hostile), a neutral one (chart not
         confirming), or NO technical read at all (cannot confirm timing) each
         block ENTRY even when the fundamental thesis is strong — "right
         name, wrong time". Absence is treated as "cannot confirm", the
         CONSERVATIVE choice: a name with no chart read this review does not get
         the benefit of the doubt on timing.
      2. NO seat opposed. A single seat pointing the other way fails the name
         outright — the mandate is "no seat opposed". ONE carve-out
         (`_is_broadcast_macro_verdict`): a MACRO seat whose direction is the
         market-wide `equity_outlook` broadcast (no sector-specific stance) is
         NOT counted as opposition, because a market-wide view is not a
         name-specific edge; a sector-SPECIFIC bearish macro stance still is.
      3. At least one NON-technical seat took a SUPPORTED DIRECTIONAL side
         (see `_has_supported_directional_thesis`) — a real directional call
         backed by evidence and an invalidation, not a bare neutral shrug.
         This is not a genuine specificity/falsifiability test (News and
         Smart-money always synthesise their invalidation); it is "a
         supported directional non-technical seat exists". Technical
         confirming is necessary but NOT sufficient and is never counted
         here — it carries no positive weight.

    Supportive/opposed are read from `AnalystVerdict.direction` (a long is
    supported by a bullish verdict, a short by a bearish one), the one
    vocabulary every seat speaks, never the flat registry stance.
    """
    aligned = "bullish" if direction == "bullish" else "bearish"
    opposed = "bearish" if direction == "bullish" else "bullish"

    tech = [v for v in seat_verdicts if v.seat == "technical"]
    if not tech:
        return (
            f"{OWN_BAR_REASON_PREFIX} — no technical read this review; "
            "timing cannot be confirmed (right name, wrong time)"
        )
    if any(v.direction == opposed for v in tech):
        return (
            f"{OWN_BAR_REASON_PREFIX} — technical opposed; chart hostile to "
            "the trade (right name, wrong time)"
        )
    if not any(v.direction == aligned for v in tech):
        return (
            f"{OWN_BAR_REASON_PREFIX} — technical does not confirm timing; "
            "chart neutral/broken (right name, wrong time)"
        )

    other_opposed = sorted({
        v.seat for v in seat_verdicts
        if v.direction == opposed and v.seat != "technical"
        and not _is_broadcast_macro_verdict(v)
    })
    if other_opposed:
        return (
            f"{OWN_BAR_REASON_PREFIX} — {', '.join(other_opposed)} opposed "
            "(mandate: no seat may be opposed)"
        )

    if not any(
        _has_supported_directional_thesis(v, aligned) for v in seat_verdicts
    ):
        return (
            f"{OWN_BAR_REASON_PREFIX} — no non-technical seat took a "
            "supported directional side"
        )

    return None


def own_bar_opposition_reason(
    seat_verdicts: list["AnalystVerdict"],
    *,
    direction: str,
) -> str | None:
    """The OPPOSITION-only subset of the conviction bar — the STAY cull test.

    `None` unless a seat is ACTIVELY OPPOSED to the held `direction`, else the
    one-line reason it is culled. Owner ruling 2026-09-25: a currently-HELD name
    earns its right to STAY, and it is culled ONLY when a seat turns actively
    opposed (technical opposed OR any non-technical seat opposed) — NOT when it
    merely fails the ENTRY bar on SOFT grounds (no technical read this review, a
    neutral/non-confirming technical read, or support that faded to neutral).
    Those soft cases drop a held name from the ranked survivors but never cull
    it; only opposition does. A broadcast-macro verdict (market-wide
    `equity_outlook`, no sector-specific stance) is NOT opposition here either —
    the same `_is_broadcast_macro_verdict` carve-out entry uses — so a single
    macro flip cannot cull the whole non-price-protected long book.

    ENTRY still uses the full-strict `own_bar_block_reason`; this narrower test
    exists solely for the held side. It reuses the SAME aligned/opposed
    vocabulary and the SAME reason strings as the two opposition branches of
    `own_bar_block_reason`, so a held name that IS culled reads identically to
    an entry candidate refused for the same opposition.

    Pure: a function of the seat `AnalystVerdict`s for one name only.
    """
    aligned = "bullish" if direction == "bullish" else "bearish"
    opposed = "bearish" if direction == "bullish" else "bullish"

    tech = [v for v in seat_verdicts if v.seat == "technical"]
    if any(v.direction == opposed for v in tech):
        return (
            f"{OWN_BAR_REASON_PREFIX} — technical opposed; chart hostile to "
            "the trade (right name, wrong time)"
        )

    other_opposed = sorted({
        v.seat for v in seat_verdicts
        if v.direction == opposed and v.seat != "technical"
        and not _is_broadcast_macro_verdict(v)
    })
    if other_opposed:
        return (
            f"{OWN_BAR_REASON_PREFIX} — {', '.join(other_opposed)} opposed "
            "(mandate: no seat may be opposed)"
        )

    return None


# --- Spec §11.2 — gross exposure, its ceiling, and the de-levering ladder --
#
# WHAT DID NOT EXIST BEFORE THIS SECTION: any gross-exposure ceiling at all.
# `max_portfolio_risk_pct` (25) bounds capital AT RISK — the sum of stop
# distances. It is not a bound on how much the book OWNS, and nothing stopped it
# reaching the broker's 4x. Everything below is therefore a TIGHTENING.
#
# One definition, several consumers, deliberately — the same discipline
# §12.2 imposed on sector exposure after three divergent implementations of
# "how much is this sector holding" let a signed-vs-gross defect survive.
# `gross_exposure` is the ONLY place gross is measured. `resolve_gross_ceiling`
# is the ONLY place the ladder is read. `apply_gross_ceiling` is the ONLY
# place the ceiling is enforced, and it is what fixes the ORDER of the two
# responses (block first, trim second) so that ordering cannot be got wrong
# by a caller.

#: Peak-to-trough drawdown rungs, shallowest first: at or worse than
#: `threshold`, gross exposure may not exceed `ceiling_x` times equity.
#: The owner's ratified table (2026-09-01):
#:
#:     0% to  -8%      ->  2.0x   (the standing cap, no rung fires)
#:    -8% to -15%      ->  1.5x
#:   -15% to -20%      ->  1.0x
#:   worse than -20%   ->  0.5x, and the owner is alerted
#:
#: The 2.0x row is the CONFIGURED cap (`risk.max_gross_exposure_x`), not a
#: rung — the ladder can only ever tighten it, never raise it, so an operator
#: who lowers the setting lowers every rung with it.
#:
#: **This is now the desk's ONLY account-level drawdown response**
#: (2026-09-20, owner instruction, docs/WORK.md item 32 retired). The desk
#: used to carry a second, separate mechanism alongside it: a daily
#: circuit breaker that halted all new trading on the day's loss, and
#: 5-day / 20-day rolling-return "drawdown brakes" that halved every new
#: BUY and SHORT. Both were removed in full. The owner's reason, verbatim:
#: "I'm starting to think that I'm fine with the stops on the individual
#: stocks and I do not want a nuclear option so remove the whole secondary
#: halt on portfolio completely because there's too many things you keep
#: finding where a slight normal fluctuation in the market can liquidate or
#: halt everything — that's too dangerous to leave — the proper stop losses
#: should be enough."
#:
#: This ladder was deliberately left untouched by that removal, and its
#: character is why: it TRIMS gross exposure gradually at -8% / -15% /
#: -20% peak-to-trough, it never halts the desk, and it never sells a
#: position outright. It is a ratified owner table about how much the book
#: may OWN, not a loss alarm. Do not reintroduce a halt or a sizing brake
#: on the strength of this table.
GROSS_LADDER: tuple[tuple[float, float], ...] = (
    (-8.0, 1.5),
    (-15.0, 1.0),
    (-20.0, 0.5),
)

#: At or worse than this drawdown the ceiling is the floor rung AND the owner
#: is told. Kept as its own constant rather than inferred from the last
#: `GROSS_LADDER` row so that adding a rung never silently moves the alert.
GROSS_LADDER_ALERT_PCT = -20.0

#: Name of the deterministic hard-block rule this ceiling raises. Listed in
#: `HARD_BLOCK_RULES` (below in this file) — one string, two places.
GROSS_EXPOSURE_RULE = "max_gross_exposure"

#: WHICH RULES ACTUALLY STOP AN ORDER.
#:
#: Moved here from `src/pipeline.py` on 2026-09-23 (board item 162). It is
#: the ONLY code-level answer to "is this entry a hard limit or an advisory",
#: and the risk seat's renderer (`src/agents/risk_manager.py`) must be able
#: to ask it. That module cannot import `src.pipeline` — `src.pipeline`
#: imports it — so the set had to sit below both. `src.pipeline` re-exports
#: this name, so every existing `from src.pipeline import HARD_BLOCK_RULES`
#: still resolves.
#:
#: Membership is the WHOLE distinction, and it is never derivable from a
#: rule's NAME: `max_sector_pct` and `max_sector_hard_pct` differ by one
#: word and sit on opposite sides of it.

HARD_BLOCK_RULES = {
    "max_total_position_pct",
    "max_position_pct",
    "require_stop_loss",
    # Spec §10.3 (owner-ratified 2026-09-01): `max_sector_pct` is NO LONGER
    # a hard block and is deliberately absent from this set. It is now the
    # diversification TARGET — breaching it emits an ADVISORY violation the
    # AI Risk Manager and the audit trail see, while the constructor shrinks
    # the order for crowding instead of the pipeline dropping it. The hard
    # gate moved to `max_sector_hard_pct` below, which fires only past the
    # absolute ceiling or on an order that never went through that sizing.
    # Removing it from here is the whole of "concentration is a dial, not a
    # gate" at the pipeline level; putting it back reinstates the veto.
    "max_sector_hard_pct",
    "cash_only",
    # Spec §11.2 (owner-ratified 2026-09-01). Gross exposure — long market
    # value plus absolute short market value — may not exceed the ladder-
    # resolved multiple of equity. There was NO gross-exposure ceiling in
    # this codebase before: `max_portfolio_risk_pct` bounds capital at risk
    # and `max_total_position_pct` bounds NET exposure, where a hedge
    # cancels a long. Adding this hard block is a tightening.
    "max_gross_exposure",
}


def _positive_float(value, default: float = 0.0) -> float:
    """Coerce a config value to a usable positive float, or `default`.

    Same Mock-safety posture `pipeline.py::_risk_setting` already takes: a
    MagicMock config fixture auto-creates a child mock for any attribute
    access, and `mock > 0` raises rather than returning False. A ceiling that
    cannot be read is INERT (default 0.0 switches the rule off) rather than
    blocking. Production config always carries the validated field, so this
    path is a test/misconfiguration guard only.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        return default
    return value


@dataclass(frozen=True)
class GrossCeiling:
    """The resolved gross-exposure ceiling for one session.

    Computed from ACCOUNT STATE ONLY — equity, its high-water mark, and the
    configured cap. Nothing the Portfolio Manager produced is an input, and
    it has a correct value on a run where the PM returned nothing at all.
    That is not incidental: a blank PM response is a measured failure mode
    (one candidate model truncated mid-JSON on 1 run in 10), and a ceiling
    that depended on a parseable book would leave the desk fully levered at
    exactly the moment it should be shedding exposure.
    """
    ceiling_x: float
    base_x: float
    drawdown_pct: float | None
    alert_owner: bool
    rung: str
    reason: str

    @property
    def de_levered(self) -> bool:
        """True when a ladder rung has tightened the configured cap."""
        return self.ceiling_x < self.base_x


def resolve_gross_ceiling(
    drawdown_pct: float | None, *, base_x: float,
) -> GrossCeiling:
    """The de-levering ladder. A pure function of drawdown — apply it twice
    and you get the same answer, because a ceiling is a LEVEL, not a
    multiplier that compounds.

    That property is the whole defence against double-application. The PM
    prompt tells the model the ladder is the engine's arithmetic and never
    its own; if a future change applied a *multiplier* at two gates the book
    would de-lever twice as hard as intended. A level enforced twice is the
    same level.

    **Boundary rule: ties go to the TIGHTER rung.** A drawdown of exactly
    -8.00% resolves to 1.5x, not 2.0x. The ratified table's ranges touch at
    their endpoints, and fail-closed is the house rule everywhere else in
    this file.

    **Unknown drawdown is NOT treated as the deepest rung.** A fresh account
    with no equity history genuinely has no drawdown; forcing it to 0.5x
    would refuse every trade on day one and force-liquidate a book that never
    fell. It resolves to the configured cap, which is itself a real ceiling,
    and `apply_gross_ceiling` refuses to TRIM on an unmeasurable book. This
    matches how `_compute_recent_performance` has always treated an empty
    `daily_pnl` table.

    **But unknown is no longer SILENT (2026-09-18).** Holding the loosest
    cap was never the defect; doing it without telling anyone was. An
    unmeasurable drawdown now sets `alert_owner=True`, so it reaches the
    owner through the same leverage-line path `GROSS_LADDER_ALERT_PCT`
    already uses. This is the direct analogue of `apply_gross_ceiling`'s
    `measurable = False` branch, which likewise trims nothing and says so
    loudly rather than proceeding as if the book were fine. The ceiling
    itself is deliberately unchanged — tightening it to a rung would be
    picking a number for a state in which, by definition, nothing has been
    measured, and would force-liquidate the fresh-account case the paragraph
    above exists to protect.
    """
    base = float(base_x) if isinstance(base_x, (int, float)) and base_x > 0 else 0.0
    if not math.isfinite(base) or base <= 0:
        base = 0.0
    if drawdown_pct is None or not math.isfinite(drawdown_pct):
        return GrossCeiling(
            ceiling_x=base, base_x=base, drawdown_pct=None, alert_owner=True,
            rung="unknown",
            reason=(
                f"No measured equity history, so no drawdown could be "
                f"computed. Gross exposure is held to the standing "
                f"{base:.1f}x ceiling and nothing is trimmed on an "
                f"unmeasured book — but the de-levering ladder cannot "
                f"de-lever while this lasts, so the owner is being told."
            ),
        )
    drawdown = float(drawdown_pct)
    ceiling = base
    rung = "none"
    for threshold, rung_x in GROSS_LADDER:
        if drawdown <= threshold:
            ceiling = min(ceiling, rung_x)
            rung = f"{threshold:.0f}%"
    alert = drawdown <= GROSS_LADDER_ALERT_PCT
    if ceiling >= base:
        reason = (
            f"The book is {abs(drawdown):.1f}% below its equity high — inside "
            f"the {abs(GROSS_LADDER[0][0]):.0f}% band where no de-levering "
            f"applies. Gross exposure may reach {ceiling:.1f}x equity."
        )
    else:
        reason = (
            f"The book is {abs(drawdown):.1f}% below its equity high. The "
            f"de-levering ladder cuts the gross-exposure ceiling from "
            f"{base:.1f}x equity to {ceiling:.1f}x until the account recovers."
        )
    if alert:
        reason += (
            " This is past the -20% rung: the desk is at its most de-levered "
            "setting and the owner is being told."
        )
    return GrossCeiling(
        ceiling_x=ceiling, base_x=base, drawdown_pct=drawdown,
        alert_owner=alert, rung=rung, reason=reason,
    )


def peak_to_trough_pct(
    equity_history, current_equity: float | None,
) -> float | None:
    """Peak-to-trough drawdown in percent (<= 0), or None if unmeasurable.

    `equity_history` is any iterable of past equity values (order irrelevant
    — a high-water mark does not care). `current_equity` is today's live
    equity and is included in the peak, so a book making new highs reads 0.0
    rather than a stale negative.

    This is the desk's only measure of drawdown. The rolling 5-day /
    20-day return brakes that used to sit beside it ("has our recent edge
    degraded, so halve new BUYs") were removed on 2026-09-20 by owner
    instruction; this one answers "how far are we off the high-water mark,
    so how much may the book own", and it sets the ceiling.

    Guard 2 (2026-09-02 operational safety guard): a NaN/inf equity reading
    can NEVER win the `max()` below and become the high-water mark. Another
    system's documented failure mode is an incremental "if new > hwm: hwm =
    new" tracker, where a single NaN reading poisons `hwm` permanently —
    every later comparison against NaN is False, so it never updates again
    and the breaker is silently disabled for good. This function structurally
    cannot do that: the peak is a fresh `max()` over a filtered list on
    EVERY call, never a variable carried between calls, so a bad reading on
    one call cannot contaminate the next. A non-finite entry is dropped
    before `max()` ever sees it (`math.isfinite` below) rather than being
    excluded by a comparison a NaN could silently fail.

    Guard 3 (2026-09-18): **an equity curve with no usable PRIOR reading is
    unmeasurable, not a zero drawdown.** Until this guard, a history that
    was empty — or whose every entry had been dropped as non-finite — left
    `current_equity` alone in the list, so it became its own high-water mark
    and the function returned a confident `0.0`. `resolve_gross_ceiling`
    reads `0.0` as "inside the no-de-levering band" and holds the loosest
    cap, which means a wiped or unreadable `daily_pnl` table silently
    disabled the desk's only automatic seller AND looked, in every log line
    and every owner-facing message, exactly like a book at record highs.
    Losing the data and making new highs are opposite states and must not
    produce the same output.

    The boundary is deliberately zero prior readings, not a chosen minimum
    number of days. Zero is the line between *measured* and *unmeasured* —
    it is not a number anyone picked, and this file's standing rule forbids
    inventing one. Whether a SHORT-but-non-empty curve (two or three days
    after a reset) is long enough to carry a meaningful high-water mark is a
    real, separate question with a real answer somewhere in the desk's own
    data; it is NOT answered here, and no `min_history=N` default is
    smuggled in as if it had been.
    """
    history_values = []
    dropped_non_finite = 0
    for raw in list(equity_history or []):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        value = float(raw)
        if not math.isfinite(value):
            # Counted, not just skipped: a dropped entry could have been
            # the TRUE peak, and silently proceeding as if it never existed
            # is exactly the "NaN reaching a comparison silently passes"
            # failure mode this guard exists to avoid. The log line below
            # is the guard — it does not change what gets returned, it
            # makes sure the loss of data is never invisible.
            dropped_non_finite += 1
            continue
        if value > 0:
            history_values.append(value)
    if dropped_non_finite:
        logger.warning(
            "peak_to_trough_pct: dropped %d non-finite equity reading(s) "
            "(NaN/inf) rather than letting one win max() as a fabricated "
            "high-water mark. Alpaca has been observed to return NaN "
            "portfolio_value during market-open glitches. "
            "The remaining %d historical "
            "reading(s) still went into this call's peak.",
            dropped_non_finite, len(history_values),
        )
    if not (
        isinstance(current_equity, (int, float))
        and not isinstance(current_equity, bool)
        and math.isfinite(float(current_equity))
        and float(current_equity) > 0
    ):
        return None
    if not history_values:
        # Guard 3. Fail LOUD and UNMEASURABLE rather than quietly confident.
        logger.warning(
            "peak_to_trough_pct: no usable PRIOR equity reading (history "
            "empty or every entry unusable) — returning UNMEASURABLE rather "
            "than 0.0%%. Today's own equity is not a high-water mark: "
            "reporting 0.0%% here would make a wiped equity curve "
            "indistinguishable from a book at record highs and would hold "
            "the de-levering ladder at its loosest cap.",
        )
        return None
    peak = max(history_values + [float(current_equity)])
    if peak <= 0:
        return None
    return round((float(current_equity) - peak) / peak * 100, 2)


def distance_to_forced_liquidation_pct(
    gross: float, equity: float, *, maintenance_margin_pct: float = 25.0,
) -> float | None:
    """How far, in percent, the book could fall before the broker liquidates.

    Nothing in this codebase watched this before §11.2 — that was the gap.
    Below the maintenance requirement the broker sells, at the worst moment,
    without asking.

    Let `f` be the fractional fall in every position. Gross becomes
    `G(1-f)`, equity becomes `E - Gf`, and the broker acts when equity drops
    below `m` x gross:

        E - Gf = m x G(1 - f)   ->   f = (E - mG) / (G(1 - m))

    At the ratified 2.0x with 25% maintenance this returns 33.3%, and at
    1.5x it returns 55.6% — the two figures the §11.2 spec entry publishes,
    reproduced rather than restated.

    Returns 100.0 when the book carries no borrowing (the positions can go
    to zero before any margin call), and None when the inputs cannot support
    the arithmetic.
    """
    for value in (gross, equity, maintenance_margin_pct):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(float(value)):
            return None
    gross = float(gross)
    equity = float(equity)
    maintenance = float(maintenance_margin_pct) / 100.0
    if equity <= 0 or not (0.0 < maintenance < 1.0):
        return None
    if gross <= 0:
        return 100.0
    fraction = (equity - maintenance * gross) / (gross * (1.0 - maintenance))
    if fraction >= 1.0:
        return 100.0
    if fraction <= 0.0:
        return 0.0
    return round(fraction * 100, 1)


def unmeasurable_gross_symbols(
    positions, *, cash_park_symbol: str | None = None,
) -> list[str]:
    """Held symbols whose market value cannot be trusted for gross math.

    Alpaca has been observed returning NaN `market_value` during market-open
    glitches. `NaN > ceiling` is False, so an unguarded comparison switches
    the ceiling OFF on exactly the broken-snapshot day it matters most —
    the same failure the sector and single-name caps were hardened against
    (2026-07-16 audit). Callers fail closed on a non-empty result.
    """
    park = (cash_park_symbol or "").strip().upper()
    bad: list[str] = []
    for p in positions or []:
        symbol = str(getattr(p, "symbol", "") or "").strip().upper()
        if park and symbol == park:
            continue
        market_value = getattr(p, "market_value", 0.0)
        if market_value is None or not math.isfinite(float(market_value)):
            bad.append(symbol or "?")
    return sorted(bad)


def gross_exposure(positions, *, cash_park_symbol: str | None = None) -> float:
    """Gross exposure in DOLLARS: long market value + |short market value|.

    Leverage multiples count, the same convention `sector_side_gross` and
    the single-name cap already use: a 3x fund consumes 3x its sticker
    notional whichever way it points.

    **The cash park does not count as exposure.** The sweep vehicle
    (`cash_sweep.symbol`, SGOV by default) is parked cash, not a position —
    it is already treated as cash-equivalent by the risk engine, by every
    LLM-facing view, by the stop-coverage audit and by `_force_delever`,
    which liquidates it first. Counting it here would consume the entire
    leverage allowance doing nothing. Callers pass the symbol from config;
    it is never hardcoded.

    Non-finite values are skipped — `unmeasurable_gross_symbols` is the
    caller's guard against acting on a total that silently excluded them.
    """
    park = (cash_park_symbol or "").strip().upper()
    total = 0.0
    for p in positions or []:
        symbol = str(getattr(p, "symbol", "") or "").strip().upper()
        if park and symbol == park:
            continue
        market_value = getattr(p, "market_value", 0.0) or 0.0
        try:
            market_value = float(market_value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(market_value):
            continue
        total += abs(market_value) * _gross_multiplier(symbol)
    return total


# --- One definition of "how invested is the book" ------------------------
#
# THE DEFECT THIS REPLACES (measured 2026-09-01, two implementations run on
# one book): `$50k AAPL long + $20k SQQQ` against a 60% target, $100k equity.
#
#   PM was told   `total_value - cash`            -> 70% -> "10pp OVER target"
#   RM was told   `abs(sum(mv * signed_mult))`    -> 10% -> "50pp UNDER target,
#                                                            do NOT scale down"
#
# Same book, same target, opposite sign, and each seat acted on its own
# number. `book_exposure` is now the only place either question is answered.
#
# WHY `deployed_pct` IS THE ONE COMPARED TO THE INVESTED TARGET, and not
# the signed leverage-aware number:
#
#  1. The target is DEFINED as the complement of cash. It used to be macro's
#     `target_invested_pct` (0-100, "sums to ~100" with its cash number);
#     since the owner mandate of 2026-09-17 it is the fixed
#     `DESK_INVESTED_TARGET_PCT` below. Either way it is a capital-deployment
#     target.
#  2. The consequence the gap exists to fix is idle cash — PMFacts renders it
#     as "the single largest P&L drag (idle cash in a rising market)" and
#     routes it to the `cash_target` step. Leverage does not make a dollar
#     less idle: $20k in a 3x fund is $20k of cash put to work, not $60k.
#  3. The signed measure has no honest comparison to a 0-100 target. A book
#     fully deployed in a 3x inverse ETF reads -300%, i.e. "375pp under a 75%
#     target, deploy more" — with no cash to deploy. A long $50k / short $50k
#     book reads 0% and asks for more of the money it has already spent.
#  4. The leverage-and-direction question is ALREADY answered, deterministically
#     and ENFORCED rather than advised, by `gross_exposure` + the §11.2
#     `apply_gross_ceiling`. It does not need
#     `macro_exposure_deviation` to answer it a second time, badly.
#
# `deployed_usd` sums |market_value|, which also repairs a quieter defect in
# the PM's old `total_value - cash`: equity is `cash + sum(market_value)`, and
# a held short's `market_value` is NEGATIVE, so a short made the book look
# LESS deployed to the PM, which then deployed more. Shorting is capital put
# to work, not capital returned to the pile.
#
# `net_usd` is kept and REPORTED rather than discarded, and it carries no
# `abs()`. The old `abs(existing_net + pending)` made a net-SHORT book read as
# positively invested — long and short of the same size were literally the
# same number. A negative `net_pct` now says "net short" out loud.
#
# The cash-sweep vehicle is not exposure in ANY of the three, matching
# `gross_exposure`, `_force_delever` and every LLM-facing position view.


# Owner mandate, 2026-09-17: "I want 100% invested. I don't want anything
# sitting in T-bills or any other positions that just yield interest." Cash
# earning less than inflation is a loss, and the desk can always go long OR
# short, so there is always something to own. This is a mandate, not a tuned
# number: it is the whole of equity. Macro no longer sets or lowers it — macro
# informs DIRECTION only. Compared against `BookExposure.deployed_pct` by the
# PM facts block and the pre-trade `deployment_gap` advisory, which reports
# UNDER-deployment only and never asks for a book to be scaled down.
DESK_INVESTED_TARGET_PCT = 100.0


def deployment_gap_band_pct(config) -> float:
    """The tolerance band for the `deployment_gap` advisory.

    Previously a flat 15pp with no source (owner rule: no arbitrary
    numbers). The only cash slice the desk has actually sourced and the
    owner accepted is the sweep reserve (`cash_sweep.reserve_pct` —
    deliberately-parked cash for fees/slippage, see `CashSweepConfig`).
    Reusing it means a book short of 100% by no more than the reserve is
    exactly at the fully-invested mandate, not "under" it; a book short by
    more than the reserve has real idle cash and the advisory should say
    so. No new constant — this tracks whatever the owner sets there.

    `config` is the pipeline's top-level config (or None — several ~58
    tests build `TradingPipeline` via `__new__` without one); a missing
    `cash_sweep` block falls back to `CashSweepConfig`'s own declared
    default rather than a number invented here.
    """
    from src.config import CashSweepConfig
    pct = getattr(getattr(config, "cash_sweep", None), "reserve_pct", None)
    if pct is None:
        pct = CashSweepConfig.model_fields["reserve_pct"].get_default()
    return float(pct)


@dataclass(frozen=True)
class BookExposure:
    """One book, measured three ways that can no longer drift apart.

    `deployed` — capital committed, unsigned, NO leverage multiple. The
        cash-complement measure, and the ONLY one comparable to the
        invested target (`DESK_INVESTED_TARGET_PCT`).
    `net` — signed and leverage-aware. Direction of the book. Negative means
        net short. Hedges cancel, which is the point of this one.
    `gross` — unsigned and leverage-aware. What the §11.2 ceiling caps.
    """
    equity: float
    deployed_usd: float
    net_usd: float
    gross_usd: float

    def _pct(self, usd: float) -> float:
        if not self.equity or self.equity <= 0 or not math.isfinite(self.equity):
            return 0.0
        return usd / self.equity * 100

    @property
    def deployed_pct(self) -> float:
        return self._pct(self.deployed_usd)

    @property
    def net_pct(self) -> float:
        return self._pct(self.net_usd)

    @property
    def gross_pct(self) -> float:
        return self._pct(self.gross_usd)


def book_exposure(
    positions,
    equity: float,
    *,
    cash_park_symbol: str | None = None,
    pending_deployed_usd: float = 0.0,
    pending_net_usd: float = 0.0,
    pending_gross_usd: float = 0.0,
) -> BookExposure:
    """The single source for "how invested is this book".

    `pending_*` are approved-but-unexecuted orders from the same batch, so a
    pre-trade gate can ask the question about the book it is ABOUT to hold.
    They are passed already-summed by the caller because the accumulation has
    to interleave with the per-decision approval loop.

    Non-finite `market_value` is skipped rather than poisoning the total to
    NaN — same convention as `gross_exposure`; `unmeasurable_gross_symbols`
    is the caller's guard against acting on a total that quietly excluded a
    position.
    """
    park = (cash_park_symbol or "").strip().upper()
    deployed = 0.0
    net = 0.0
    for p in positions or []:
        symbol = str(getattr(p, "symbol", "") or "").strip().upper()
        if park and symbol == park:
            continue
        market_value = getattr(p, "market_value", 0.0) or 0.0
        try:
            market_value = float(market_value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(market_value):
            continue
        deployed += abs(market_value)
        net += market_value * _effective_multiplier(symbol)
    return BookExposure(
        equity=float(equity or 0.0),
        deployed_usd=deployed + float(pending_deployed_usd or 0.0),
        net_usd=net + float(pending_net_usd or 0.0),
        gross_usd=gross_exposure(positions, cash_park_symbol=cash_park_symbol)
        + float(pending_gross_usd or 0.0),
    )


# --- One definition of a position's WEIGHT -------------------------------
#
# GROSS-leverage weight, signed. `market_value x |leverage| / equity`.
#
# THE DEFECT THIS REPLACES (measured 2026-09-01): the PM prompt states "All
# weights are GROSS-leverage weights", renders a position line reading
# `Weight: 18.0% ... DRIFT` from the gross number, and three lines later
# renders `drift-flagged: 0` from a raw one that never crossed the 12%
# threshold. One prompt, two weights, contradicting each other in view of the
# model that has to act on them.
#
# GROSS is the convention because it is the one the ENGINE enforces: the
# `max_position_pct` hard cap, the sector budgets and the constructor's
# current-weight comparison are all gross. Rendering a raw weight to the PM
# made it restate a 3x SQQQ's 6% raw as its target, which the constructor read
# as "cut 18% down to 6%" and turned into a 67% SELL nobody asked for.
#
# SIGNED, not absolute: a held short's weight is negative, which is how the
# drift heuristic stays a long-side question. A winning short's |market_value|
# SHRINKS toward zero, so it cannot drift into an oversized position the way
# an appreciating long can.


def weight_pct_of(market_value: float, symbol: str, equity: float) -> float:
    """Signed GROSS-leverage weight of `market_value` held in `symbol`.

    Takes a dollar figure rather than a position so the pre-trade gate can ask
    it about a projected `held + pending + new` exposure, not only about what
    is already on the books.
    """
    try:
        mv = float(market_value or 0.0)
        eq = float(equity or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not eq or eq <= 0 or not math.isfinite(eq) or not math.isfinite(mv):
        return 0.0
    return mv * _gross_multiplier(str(symbol or "").strip().upper()) / eq * 100


def position_weight_pct(position, equity: float) -> float:
    """`weight_pct_of` for a held position. The form most callers want."""
    return weight_pct_of(
        getattr(position, "market_value", 0.0) or 0.0,
        getattr(position, "symbol", "") or "",
        equity,
    )


@dataclass
class GrossCeilingOutcome:
    """What `apply_gross_ceiling` did, in the order it did it."""
    decisions: list = field(default_factory=list)
    #: Engine-authored SELL / COVER orders. Empty unless the HELD book alone
    #: is over the ceiling.
    trims: list = field(default_factory=list)
    #: Operator-readable lines, one per refusal or reduction, each naming the
    #: rule that fired.
    notes: list = field(default_factory=list)
    #: Symbols whose new exposure was refused outright.
    blocked: list = field(default_factory=list)
    #: {symbol: reason}, one entry per name in `blocked` — the SAME text
    #: appended to `notes`, without the "{GROSS_EXPOSURE_RULE}: {symbol}
    #: refused — " prefix. Board item 10 (2026-09-14): `notes` is relayed
    #: verbatim by `PortfolioConstructor` as `logger.warning("Constructor:
    #: %s", note)`, and every note built that way reads "Constructor:
    #: max_gross_exposure: SYMBOL refused — ..." — the rule name sits
    #: BETWEEN "Constructor:" and the symbol, which `_DropReasonCapture.
    #: _SYMBOL` requires to follow immediately. Every gross-ceiling block was
    #: therefore invisible to that regex. This field lets the constructor
    #: file a structured `_note_refusal` per blocked symbol instead of
    #: relying on the log scrape.
    blocked_detail: dict = field(default_factory=dict)
    ceiling: GrossCeiling | None = None
    ceiling_usd: float = 0.0
    held_gross: float = 0.0
    held_gross_after_exits: float = 0.0
    projected_gross: float = 0.0
    measurable: bool = True


def apply_gross_ceiling(
    decisions,
    positions,
    equity: float,
    ceiling: GrossCeiling,
    *,
    cash_park_symbol: str | None = None,
    # Fixed 2026-09-24: no longer used to refuse a new entry (see step 2's
    # comment) — kept only as an accepted, ignored parameter so existing
    # callers/tests that pass it do not need to change. An entry the
    # ceiling shrinks to near-nothing is granted, not refused, for being
    # small; only a real zero (`after <= 0`) still refuses.
    min_order_usd: float = 500.0,
    # The SIZING gate (`PortfolioConstructor`) sets this False: shrinking an
    # order it is about to propose is its job, authoring a de-lever of the
    # held book is not. One owner for trimming — the session preamble
    # (`TradingPipeline._enforce_gross_ceiling`), which runs before any agent
    # and therefore cannot be disabled by a blank model response.
    emit_trims: bool = True,
) -> GrossCeilingOutcome:
    """Enforce the §11.2 gross-exposure ceiling, blocking BEFORE trimming.

    **The ordering is the rule, and it is not optional.** A drawdown must not
    trigger the panic-selling the ladder exists to prevent, so:

    1. Planned exits are counted first — a book already being reduced is
       judged on what it will hold, not on what it holds now.
    2. **New exposure is blocked or shrunk to fit the ceiling.** Every BUY
       and SHORT is rationed against the remaining headroom. Fixed
       2026-09-24: one shrunk to a small but nonzero size is no longer
       refused outright — it is placed at whatever headroom remains (no
       stock commission and fractional shares make a small order fine); only
       a genuine zero (`after <= 0`) is refused.
    3. **Only then**, and only if the HELD book ALONE still exceeds the
       ceiling, are trims emitted. Proposed new exposure is not an input to
       that test, structurally — so the engine can never sell something you
       own to make room for something you do not.

    **This function does not need the Portfolio Manager.** Pass `decisions=[]`
    — the case where the PM returned nothing at all, a measured failure mode
    — and steps 1 and 2 are trivially satisfied while step 3 still trims a
    book that is over its ceiling. That is the whole point of computing the
    ceiling from account state.

    Returns a `GrossCeilingOutcome`. Entry decisions are mutated in place
    (their `allocation_pct` reduced and their `reasoning` annotated) so the AI
    Risk Manager does not read deterministic arithmetic as the PM
    contradicting itself.
    """
    out = GrossCeilingOutcome(
        decisions=list(decisions or []), ceiling=ceiling,
    )
    positions = list(positions or [])
    park = (cash_park_symbol or "").strip().upper()

    if (
        not isinstance(ceiling, GrossCeiling)
        or _positive_float(ceiling.ceiling_x) <= 0
        or isinstance(equity, bool)
        or not isinstance(equity, (int, float))
        or not math.isfinite(float(equity))
        or float(equity) <= 0
    ):
        # No trustworthy equity figure means no trustworthy ceiling. Refuse
        # every new position and trim nothing — the same fail-closed posture
        # `RiskRuleEngine.check` takes on a `total_value <= 0` broker blip.
        out.measurable = False
        for decision in out.decisions:
            if decision.action in ("BUY", "SHORT") and decision.allocation_pct > 0:
                decision.allocation_pct = 0.0
                out.blocked.append(decision.symbol)
                detail = (
                    f"the account equity figure ({equity}) is not usable, so "
                    f"the gross-exposure ceiling cannot be computed. No new "
                    f"position opens on an unreadable account."
                )
                out.blocked_detail[decision.symbol] = detail
                out.notes.append(
                    f"{GROSS_EXPOSURE_RULE}: {decision.symbol} refused — {detail}"
                )
        if out.notes:
            logger.warning(
                "Gross-exposure ceiling: equity unusable (%s) — refused %d "
                "new position(s)", equity, len(out.blocked),
            )
        return out

    equity = float(equity)
    out.ceiling_usd = ceiling.ceiling_x * equity
    unmeasurable = unmeasurable_gross_symbols(
        positions, cash_park_symbol=park or None,
    )
    out.measurable = not unmeasurable
    out.held_gross = gross_exposure(positions, cash_park_symbol=park or None)

    positions_by_symbol = {
        str(getattr(p, "symbol", "") or "").strip().upper(): p for p in positions
    }

    # --- STEP 1: planned exits shrink the book before anything is judged ---
    exit_relief: dict[str, float] = {}
    for decision in out.decisions:
        if decision.action not in ("SELL", "COVER"):
            continue
        if decision.allocation_pct <= 0:
            continue  # allocation_pct == 0 means SKIP, not full exit
        symbol = str(decision.symbol or "").strip().upper()
        if park and symbol == park:
            continue  # unparking cash is not a reduction in exposure
        held = positions_by_symbol.get(symbol)
        if held is None:
            continue
        market_value = getattr(held, "market_value", 0.0) or 0.0
        if not math.isfinite(float(market_value)):
            continue
        position_gross = abs(float(market_value)) * _gross_multiplier(symbol)
        fraction = min(100.0, float(decision.allocation_pct)) / 100.0
        exit_relief[symbol] = max(
            exit_relief.get(symbol, 0.0), position_gross * fraction,
        )
    out.held_gross_after_exits = max(
        0.0, out.held_gross - sum(exit_relief.values()),
    )

    # --- STEP 2: BLOCK NEW EXPOSURE FIRST ---------------------------------
    headroom = max(0.0, out.ceiling_usd - out.held_gross_after_exits)
    entries = [
        d for d in out.decisions
        if d.action in ("BUY", "SHORT") and d.allocation_pct > 0
    ]
    # Largest commitment first, symbol as a deterministic tie-break — the
    # same rationing order `construct_orders` already sorts its BUYs into,
    # so highest conviction gets the scarce headroom.
    entries.sort(key=lambda d: (-float(d.allocation_pct), str(d.symbol)))
    granted = 0.0
    for decision in entries:
        symbol = str(decision.symbol or "").strip().upper()
        multiplier = _gross_multiplier(symbol)
        wanted = equity * (float(decision.allocation_pct) / 100.0) * multiplier
        available = 0.0 if unmeasurable else max(0.0, headroom - granted)
        if wanted <= available + 1e-9:
            granted += wanted
            continue
        if unmeasurable:
            reason = (
                f"the broker returned an unusable market value for "
                f"{', '.join(unmeasurable)}, so gross exposure cannot be "
                f"measured this session"
            )
        else:
            reason = (
                f"the book would own ${out.held_gross_after_exits + granted + wanted:,.0f} "
                f"against a ${out.ceiling_usd:,.0f} ceiling "
                f"({ceiling.ceiling_x:.1f}x equity)"
            )
        before = float(decision.allocation_pct)
        # Fixed 2026-09-24: this used to refuse outright whenever
        # `available / multiplier` (the NOTIONAL the ceiling still allows)
        # was under the flat `min_order_usd` floor — an arbitrary $500 with
        # no broker minimum behind it (config/number_ledger.yaml), justified
        # by a false "pays commission" claim (Alpaca charges none). A small
        # entry is no longer refused for that reason; it is granted whatever
        # headroom is left, however small. Only a genuinely empty headroom
        # (`after <= 0` below) still refuses — that is a real "nothing to
        # buy", not an arbitrary-floor judgement call.
        #
        # Round DOWN to 2dp so the granted size can never land back above the
        # headroom that permitted it.
        after = math.floor(
            (available / (equity * multiplier) * 100.0) * 100.0
        ) / 100.0
        if after <= 0:
            decision.allocation_pct = 0.0
            out.blocked.append(decision.symbol)
            detail = (
                f"{reason}, and no headroom is left under the ceiling. "
                f"{ceiling.reason}"
            )
            out.blocked_detail[decision.symbol] = detail
            out.notes.append(f"{GROSS_EXPOSURE_RULE}: {decision.symbol} refused — {detail}")
            continue
        decision.allocation_pct = after
        decision.reasoning = (
            decision.reasoning
            + f" [risk engine: {before:.2f}% cut to {after:.2f}% — "
              f"{GROSS_EXPOSURE_RULE} ceiling {ceiling.ceiling_x:.1f}x equity. "
              f"Deterministic, not PM inconsistency.]"
        )[:800]
        granted += equity * (after / 100.0) * multiplier
        out.notes.append(
            f"{GROSS_EXPOSURE_RULE}: {decision.symbol} cut from {before:.2f}% "
            f"to {after:.2f}% of equity — {reason}. {ceiling.reason}"
        )
    out.projected_gross = out.held_gross_after_exits + granted

    # --- STEP 3: trim ONLY if the HELD book alone is still over -----------
    #
    # `held_gross_after_exits` deliberately excludes every proposed entry, so
    # no amount of new buying can provoke a trim. If this book fits under the
    # ceiling, step 2 has already done the whole job.
    over = out.held_gross_after_exits - out.ceiling_usd
    if not emit_trims:
        return out
    if unmeasurable:
        if over > 0:
            logger.warning(
                "Gross-exposure ceiling: book may be over its %.1fx ceiling but "
                "%s returned an unusable market value — refusing to trim on a "
                "broken snapshot (new exposure is already blocked)",
                ceiling.ceiling_x, ", ".join(unmeasurable),
            )
        return out
    if over <= 1e-6:
        return out

    already_exiting_fully = {
        str(d.symbol or "").strip().upper()
        for d in out.decisions
        if d.action in ("SELL", "COVER") and d.allocation_pct >= 100
    }
    candidates = []
    for p in positions:
        symbol = str(getattr(p, "symbol", "") or "").strip().upper()
        if park and symbol == park:
            continue  # parked cash is not exposure; selling it frees nothing
        if symbol in already_exiting_fully:
            continue
        market_value = float(getattr(p, "market_value", 0.0) or 0.0)
        if not math.isfinite(market_value) or market_value == 0:
            continue
        position_gross = (
            abs(market_value) * _gross_multiplier(symbol)
            - exit_relief.get(symbol, 0.0)
        )
        if position_gross <= 0:
            continue
        candidates.append((p, symbol, position_gross))
    # Biggest-loser-first, largest position as tie-break, then symbol for a
    # deterministic order across runs. The SAME ordering `_force_delever`
    # already uses for the cash-only safety net — a second, divergent notion
    # of "which position goes first" is exactly the sprawl §12.2 cleaned up.
    candidates.sort(
        key=lambda item: (
            float(getattr(item[0], "unrealized_pnl", 0.0) or 0.0),
            -item[2],
            item[1],
        )
    )
    for position, symbol, position_gross in candidates:
        if over <= 1e-6:
            break
        take = min(position_gross, over)
        fraction_pct = min(100.0, max(1.0, round(take / position_gross * 100, 1)))
        is_short = position_side(position) == SECTOR_SIDE_SHORT
        trim = TradeDecision(
            action="COVER" if is_short else "SELL",
            symbol=position.symbol,
            allocation_pct=fraction_pct,
            entry_price=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            reasoning=(
                f"Deterministic de-lever ({GROSS_EXPOSURE_RULE}): the book "
                f"already owns ${out.held_gross_after_exits:,.0f} against a "
                f"${out.ceiling_usd:,.0f} ceiling ({ceiling.ceiling_x:.1f}x "
                f"equity). {ceiling.reason} Reducing {position.symbol} by "
                f"{fraction_pct:.0f}%. New exposure was blocked first; this "
                f"trim runs only because the book is over the ceiling on its "
                f"own."
            )[:500],
        )
        out.trims.append(trim)
        out.notes.append(
            f"{GROSS_EXPOSURE_RULE}: trimming {fraction_pct:.0f}% of "
            f"{position.symbol} — the book already owns more than the "
            f"{ceiling.ceiling_x:.1f}x ceiling allows, with no new buying "
            f"involved. {ceiling.reason}"
        )
        over -= position_gross * (fraction_pct / 100.0)
    if out.trims:
        logger.warning(
            "Gross-exposure ceiling: held book $%.0f over the $%.0f ceiling "
            "(%.1fx equity) — de-levering %d position(s): %s",
            out.held_gross_after_exits, out.ceiling_usd, ceiling.ceiling_x,
            len(out.trims), ", ".join(t.symbol for t in out.trims),
        )
    return out


@dataclass
class RiskViolation:
    rule: str
    message: str
    value: float
    limit: float


class RiskRuleEngine:
    def __init__(self, config: RiskConfig):
        self.config = config

    def check(self, decision: TradeDecision, positions: list[Position],
              total_value: float,
              pending_investment: float = 0.0,
              # Spec §12.2: keyed by `(sector, side)`, not by sector alone —
              # a pending SHORT must not consume the same sector's LONG
              # budget. A plain-`str` key here is now a bug, and raises
              # nothing silently only because `.get()` on a tuple key simply
              # misses it; `accumulate_pending_sector` is the writer.
              pending_sector_investment: dict[tuple[str, str], float] | None = None,
              pending_symbol_investment: dict[str, float] | None = None,
              correlation_matrix: dict[str, dict[str, float]] | None = None,
              max_correlated_cluster_pct: float = 50.0,
              cash: float | None = None,
              pending_cash_outflow: float = 0.0,
              # --- Spec §11.2 ---------------------------------------------
              # The EXECUTION half of the gross-exposure ceiling. The sizing
              # half lives in `PortfolioConstructor`, which shrinks orders to
              # fit; this is the hard block for anything that reaches the
              # engine without that sizing (a legacy notional target, an
              # agent-authored modification, any future caller) — exactly the
              # relationship `max_position_pct` already has with its
              # constructor clamp.
              #
              # `gross_ceiling` is the ladder-resolved ceiling for this
              # session (`resolve_gross_ceiling`). None falls back to the
              # configured cap with no drawdown applied, so a caller that
              # forgets it still gets a ceiling rather than none.
              gross_ceiling: "GrossCeiling | None" = None,
              pending_gross_investment: float = 0.0,
              cash_park_symbol: str | None = None) -> list[RiskViolation]:
        # D10 (Stage 3): a COVER can never be hard-blocked, mirroring the
        # deliberate asymmetry already used for exits — entries fail
        # closed, exits fail open, because being unable to close a
        # position is strictly worse than being unable to open one. A
        # COVER is mechanically a buy at the broker, so without this it
        # would be caught by the cash_only rule below exactly like a BUY.
        if decision.action in ("SELL", "COVER"):
            return []
        # total_value <= 0 (or NaN) means we can't compute risk percentages.
        # Pre-fix the early return was `[]` which has the same shape as
        # "all checks passed" — so an Alpaca portfolio_value=0 blip during
        # market-open silently approved every BUY, bypassing cash_only /
        # max_position_pct / max_sector_pct. Emit a
        # synthetic violation in HARD_BLOCK_RULES so the pipeline filter
        # blocks the BUY instead. The empty list reserved exclusively for
        # "checked, found no violations" semantics.
        import math
        if not math.isfinite(total_value) or total_value <= 0:
            return [RiskViolation(
                rule="max_total_position_pct",   # in HARD_BLOCK_RULES
                message=(
                    f"total_value={total_value} is not a valid equity figure "
                    f"(broker glitch or fresh account) — refusing to risk-check "
                    f"BUY for {decision.symbol}; blocking until next snapshot"
                ),
                value=0.0,
                limit=0.0,
            )]

        # A single non-finite position market_value poisons every sum below.
        # NaN comparisons are all False, so `sector_pct > cap` and
        # `total_pct > cap` silently evaluate False — the exposure and sector
        # caps switch OFF for the whole session on exactly the broken-snapshot
        # day they matter most (2026-07-16 audit; Alpaca has been observed to
        # return NaN market_value during market-open glitches). Block instead,
        # mirroring the total_value guard above: no risk-check, no BUY.
        bad_mv = [p.symbol for p in positions if not math.isfinite(p.market_value)]
        if bad_mv:
            return [RiskViolation(
                rule="max_total_position_pct",   # in HARD_BLOCK_RULES
                message=(
                    f"non-finite market_value for {', '.join(sorted(bad_mv))} — "
                    f"exposure / sector caps cannot be computed; refusing to "
                    f"risk-check BUY for {decision.symbol}; blocking until the "
                    f"next clean snapshot"
                ),
                value=0.0,
                limit=0.0,
            )]

        # Non-finite cash disables the cash_only comparison the same silent
        # way a NaN market_value disabled the caps (audit round 2:
        # `NaN < 0` is False, so every BUY passed). Fail closed.
        if cash is not None and not math.isfinite(cash):
            return [RiskViolation(
                rule="max_total_position_pct",   # in HARD_BLOCK_RULES
                message=(
                    f"non-finite cash={cash} — cash_only cannot be evaluated; "
                    f"refusing to risk-check BUY for {decision.symbol}; "
                    f"blocking until the next clean snapshot"
                ),
                value=0.0,
                limit=0.0,
            )]

        violations = []
        is_short = decision.action == "SHORT"
        signed_mul = _effective_multiplier(decision.symbol)  # net direction
        gross_mul = _gross_multiplier(decision.symbol)       # size magnitude
        new_investment = total_value * (decision.allocation_pct / 100)
        # A SHORT moves net exposure the OPPOSITE way a BUY of the same
        # symbol would (it adds negative, not positive, directional
        # exposure) — flip the sign so rule 2 below stays correct instead
        # of reading a growing short as growing long exposure.
        signed_new = new_investment * signed_mul * (-1.0 if is_short else 1.0)
        gross_new = new_investment * gross_mul

        # 1. Single position size limit (gross — a 3x ETF consumes 3x regardless of direction)
        #
        # SHORT takes the branch below instead. This arithmetic assumes
        # `new_investment` (always a positive magnitude) moves the position
        # FURTHER in the direction `current_symbol_raw` is already signed
        # toward — true for a BUY adding to a long, wrong for a SHORT adding
        # to a short (`current_symbol_raw` is negative, so the sum OFFSETS
        # toward zero). The SHORT branch measures the same cap
        # direction-aware.
        if not is_short:
            current_symbol_raw = sum(p.market_value for p in positions if p.symbol == decision.symbol)
            current_symbol_raw += (pending_symbol_investment or {}).get(decision.symbol, 0.0)
            # `weight_pct_of` is the ONE definition of a gross-leverage
            # weight — the same function the constructor's current-weight
            # map, the PM's position lines and the PM facts drift check
            # call. Identical arithmetic to the inline form it replaces
            # (`(mv) * gross_mul / equity * 100`); routed through the shared
            # function so the cap and the numbers the PM sizes against
            # cannot drift apart again.
            position_pct = weight_pct_of(
                current_symbol_raw + new_investment, decision.symbol, total_value,
            )
            if position_pct > self.config.max_position_pct:
                violations.append(RiskViolation(
                    rule="max_position_pct",
                    message=f"{decision.symbol} position would be {position_pct:.1f}% and exceed max {self.config.max_position_pct}%",
                    value=position_pct,
                    limit=self.config.max_position_pct,
                ))

        # The SAME single-position cap for a short (owner decision
        # 2026-09-17: shorts carry the same limits as longs). Same rule name
        # and same limit as the long branch above, so the two can never
        # drift apart; only the measurement is direction-aware. Never reached
        # for a COVER (exempted at the top of this method).
        if is_short:
            current_short_raw = sum(
                p.market_value for p in positions
                if p.symbol == decision.symbol and p.qty < 0
            )
            # `pending_symbol_investment` (like `new_investment`) is always
            # an UNSIGNED dollar magnitude — see the accumulation in
            # `TradingPipeline._filter_hard_risk_decisions`, the same
            # convention a same-batch BUY already uses. `current_short_raw`
            # is the only signed term here (a short's market_value is
            # negative), so it alone needs `abs()`.
            pending_same_symbol = (pending_symbol_investment or {}).get(decision.symbol, 0.0)
            position_pct = weight_pct_of(
                abs(current_short_raw) + pending_same_symbol + new_investment,
                decision.symbol, total_value,
            )
            if position_pct > self.config.max_position_pct:
                violations.append(RiskViolation(
                    rule="max_position_pct",
                    message=(
                        f"{decision.symbol} short would be {position_pct:.1f}% "
                        f"and exceed max {self.config.max_position_pct}%"
                    ),
                    value=position_pct,
                    limit=self.config.max_position_pct,
                ))

        # 1c. Spec §11.2 — the GROSS-exposure ceiling. HARD BLOCK (in
        # HARD_BLOCK_RULES, above in this file).
        #
        # Distinct from rule 2 below in the way that matters: rule 2 measures
        # NET exposure, where a hedge cancels a long. That does not answer "how much does the book OWN", which is what
        # decides whether a 33% fall triggers a margin call. Nothing in this
        # codebase answered that question before §11.2.
        #
        # The cash park is excluded — it is parked cash, not a position, and
        # counting it would consume the whole allowance doing nothing.
        gross_ceiling_x = (
            _positive_float(gross_ceiling.ceiling_x)
            if isinstance(gross_ceiling, GrossCeiling)
            else _positive_float(
                getattr(self.config, "max_gross_exposure_x", None)
            )
        )
        if gross_ceiling_x > 0:
            held_gross = gross_exposure(
                positions, cash_park_symbol=cash_park_symbol,
            )
            projected_gross = held_gross + pending_gross_investment + gross_new
            gross_x = projected_gross / total_value
            if gross_x > gross_ceiling_x + 1e-9:
                ladder_note = (
                    f" {gross_ceiling.reason}" if gross_ceiling is not None else ""
                )
                violations.append(RiskViolation(
                    rule=GROSS_EXPOSURE_RULE,
                    message=(
                        f"{decision.symbol} would put the book at "
                        f"{gross_x:.2f}x equity in gross exposure "
                        f"(${projected_gross:,.0f} owned against "
                        f"${total_value:,.0f} of equity), over the "
                        f"{gross_ceiling_x:.2f}x ceiling. Parked cash is not "
                        f"counted.{ladder_note}"
                    ),
                    value=round(gross_x, 4),
                    limit=round(gross_ceiling_x, 4),
                ))

        # 2. Total net exposure limit — signed, so long+short hedges cancel.
        #
        # Reads the SAME `book_exposure` that PM's `invested_pct`, the PM
        # prompt's Account Status line and the `macro_exposure_deviation`
        # advisory read, so this cap can no longer be enforced against a book
        # measured differently from the one the seats were shown.
        #
        # The `abs()` here is DELIBERATE and stays: this is a magnitude
        # ceiling, and a book 150% net SHORT is as far over it as one 150%
        # net long. That is the opposite of the `abs()` removed from the
        # macro advisory, which was erasing the direction of a number whose
        # whole job was to report it. Non-finite market values are already
        # hard-blocked above, so `book_exposure`'s skip cannot hide one here.
        projected_book = book_exposure(
            positions, total_value,
            pending_net_usd=pending_investment + signed_new,
        )
        total_pct = abs(projected_book.net_pct)
        # Cross-check (fix/dashboard-and-duplicate-definitions): the
        # dashboard serves this same magnitude from src/quantities.py, which
        # cannot import src.risk. A guard test asserts the two agree, so the
        # bar and the ceiling it is drawn against cannot drift apart.
        if total_pct > self.config.max_total_position_pct:
            violations.append(RiskViolation(
                rule="max_total_position_pct",
                message=f"Net exposure {total_pct:.1f}% would exceed max {self.config.max_total_position_pct}%",
                value=total_pct,
                limit=self.config.max_total_position_pct,
            ))

        # 4. Stop loss required
        if self.config.require_stop_loss and decision.stop_loss <= 0:
            violations.append(RiskViolation(
                rule="require_stop_loss",
                message=f"{decision.symbol} has no stop loss set",
                value=decision.stop_loss,
                limit=0,
            ))

        # 4b. Correlation cluster (advisory) — catches the "all-AI" concentration problem
        # that sector caps miss. If the proposed BUY plus the held positions highly correlated
        # with it (|corr| >= 0.7) together exceed max_correlated_cluster_pct, flag.
        if correlation_matrix:
            from src.data.correlation import highly_correlated_peers, CLUSTER_CORRELATION_THRESHOLD
            held_symbols = [p.symbol for p in positions]
            peers = highly_correlated_peers(decision.symbol, held_symbols, correlation_matrix)
            if peers:
                # Apply gross multiplier consistently with sector / position
                # caps below — a 3x inverse ETF (SQQQ) in a cluster consumes
                # 3x notional, even though its directional sign cancels for
                # NET exposure (#2). Pre-fix this rule treated SQQQ as 1x
                # which silently under-counted cluster concentration.
                # The cluster must include the BUY symbol's OWN existing
                # position, not just its peers: `highly_correlated_peers`
                # (correctly) excludes the symbol itself, so an ADD to the
                # largest member of a cluster counted only the ADD's notional
                # and none of the stack already held — the concentration this
                # rule exists to catch was invisible exactly when it was worst
                # (2026-07-16 audit). A symbol is trivially correlated 1.0
                # with itself, so it belongs in its own cluster total.
                cluster_symbols = set(peers) | {decision.symbol}
                peer_value = sum(
                    p.market_value * _gross_multiplier(p.symbol)
                    for p in positions if p.symbol in cluster_symbols
                )
                cluster_pct = (peer_value + gross_new) / total_value * 100
                if cluster_pct > max_correlated_cluster_pct:
                    violations.append(RiskViolation(
                        rule="correlation_cluster",
                        message=(
                            f"{decision.symbol} + correlated holdings [{', '.join(peers)}] "
                            f"would total {cluster_pct:.0f}% of book, exceeding "
                            f"{max_correlated_cluster_pct:.0f}% cluster cap (advisory). "
                            f"Pairwise corr > {CLUSTER_CORRELATION_THRESHOLD}."
                        ),
                        value=cluster_pct,
                        limit=max_correlated_cluster_pct,
                    ))

        # 4c. Cash-only policy — when allow_margin is False, no BUY may spend more
        # than the cash remaining after prior BUYs in this session. `cash` is the
        # session-start broker cash; `pending_cash_outflow` is the dollar total of
        # BUYs already allowed earlier in the same filter pass. Sector / leverage
        # multipliers don't apply here — cash is spent at gross dollar notional
        # regardless of whether the symbol is an inverse / leveraged ETF.
        #
        # SHORT is exempt: opening a short does not spend the settled-cash
        # pool this rule was written to protect — it sells borrowed shares,
        # crediting cash (against a margin requirement this codebase does
        # not model). The position, gross and net caps, not this rule, are the control
        # surface for a short (D11).
        if not self.config.allow_margin and cash is not None and not is_short:
            projected_cash = cash - pending_cash_outflow - new_investment
            if projected_cash < 0:
                violations.append(RiskViolation(
                    rule="cash_only",
                    message=(
                        f"{decision.symbol} BUY for ${new_investment:,.0f} would "
                        f"spend beyond available cash (cash=${cash:,.0f}, pending "
                        f"BUYs=${pending_cash_outflow:,.0f}); margin is disabled"
                    ),
                    value=abs(projected_cash),
                    limit=max(cash - pending_cash_outflow, 0.0),
                ))

        # 5. Sector concentration — GROSS (unsigned) and SIDE-SPLIT (spec §12.2).
        #
        # The long book and the short book carry SEPARATE budgets in each
        # sector, and neither offsets the other. Before §12.2 this summed
        # SIGNED `market_value`, so a held short made its sector look smaller
        # and a long book could over-concentrate behind it. `sector_side_gross`
        # is the single definition; the constructor sizes against the same one.
        #
        # THE DEFECT (2026-09-01 audit): "Unknown" used to mean EXEMPT here —
        # `new_sector != "Unknown"` skipped this entire block, so a symbol
        # whose sector lookup failed (or timed out) paid NEITHER the soft
        # advisory NOR the 90%-hard-ceiling that borrowed money now sits
        # behind. 80 of 101 universe symbols depend on a live network lookup
        # with no offline fallback (only ~21 ETFs have a static table — see
        # `_ETF_SECTORS`), so a network blip silently switched the sector
        # cap OFF for most of the book — on a leveraged (2.0x) book, in
        # effect no concentration limit at all. Symmetrically, a HELD
        # position stamped sector="Unknown" the same way was invisible to
        # `sector_side_gross`'s default (`include_unknown=False`, "matches
        # the gate" — see its docstring) and vanished from every sector's
        # exposure.
        #
        # FIX: pass `include_unknown=True` so a held "Unknown" position
        # counts, pool "Unknown" as its own `(sector, side)` bucket exactly
        # like a real sector name, and run the SAME soft-advisory /
        # hard-block pair against it — conservative (every unresolved
        # symbol, new or held, competes for one shared budget), not exempt.
        # This deliberately does NOT touch `sector_side_gross`'s DEFAULT
        # (still `include_unknown=False` for the constructor's sizing pass
        # — a separate, unrelated design choice about how orders are
        # pre-shrunk, not about whether the gate can be silently switched
        # off) — only this call site, the deterministic gate, is changed.
        from src.execution.broker import _get_sector, _sector_resolution_status_for
        new_sector = _get_sector(decision.symbol)
        if new_sector:
            side = decision_side(decision.action)
            held_by_side = sector_side_gross(positions, include_unknown=True)
            sector_value = held_by_side.get((new_sector, side), 0.0)
            sector_value += (pending_sector_investment or {}).get(
                (new_sector, side), 0.0,
            )
            sector_value += gross_new
            sector_pct = sector_value / total_value * 100
            side_label = "long" if side == SECTOR_SIDE_LONG else "short"
            sector_display = new_sector
            if new_sector == "Unknown":
                sector_display = "Unknown (pooled — unresolved symbols are constrained, not exempt)"
            # Spec §10.3. `max_sector_pct` is now the concentration TARGET,
            # and breaching it is ADVISORY — it is reported to the AI Risk
            # Manager and the audit trail, but it no longer drops the trade.
            # The constructor has already shrunk the order for crowding
            # (`sector_size_scale`); a sector over its target is information
            # about the book, not a verdict on this idea.
            if sector_pct > self.config.max_sector_pct:
                violations.append(RiskViolation(
                    rule="max_sector_pct",
                    message=(
                        f"Sector '{sector_display}' {side_label} exposure would be "
                        f"{sector_pct:.1f}%, over the "
                        f"{self.config.max_sector_pct}% concentration target "
                        f"(advisory — size was scaled for crowding, not refused; "
                        f"the hard ceiling is {self.config.sector_hard_ceiling_pct:.0f}%). "
                        f"Long and short budgets are separate (§12.2) — the "
                        f"other side of this sector is not netted against it"
                    ),
                    value=sector_pct,
                    limit=self.config.max_sector_pct,
                ))
            # The HARD BLOCK. Same allowance function the constructor sized
            # against, so an order built by the constructor never trips this
            # — exactly the relationship `max_position_pct` already has with
            # its constructor clamp. What this catches is an order that
            # reached the engine WITHOUT that sizing (a legacy notional
            # target, an agent-authored modification, any future caller), and
            # the absolute ceiling past which no conviction buys more
            # concentration. Post-fix this is also what actually stops an
            # unresolved-sector order from concentrating without limit — the
            # constructor's sizing pass does not shrink for "Unknown"
            # (unchanged, out of scope here), so this hard wall is the only
            # thing standing between a lookup failure and an unbounded add.
            prior_sector_pct = (sector_value - gross_new) / total_value * 100
            gross_new_pct = gross_new / total_value * 100
            allowance_pct = sector_allowance_pct(
                prior_sector_pct,
                soft_cap_pct=self.config.max_sector_pct,
                hard_cap_pct=self.config.sector_hard_ceiling_pct,
            )
            # Tolerance: the constructor rounds `allocation_pct` to 2dp, so an
            # order sized to exactly the allowance can land a hair above it
            # here. Blocking on float dust would resurrect the veto this
            # section exists to remove.
            if gross_new_pct > allowance_pct + 1e-6:
                violations.append(RiskViolation(
                    rule="max_sector_hard_pct",
                    message=(
                        f"{decision.symbol} would add {gross_new_pct:.1f}% gross to "
                        f"the {side_label} side of sector '{sector_display}', already at "
                        f"{prior_sector_pct:.1f}%. Crowding permits at most "
                        f"{allowance_pct:.2f}% more "
                        f"(hard ceiling {self.config.sector_hard_ceiling_pct:.0f}%)"
                    ),
                    value=gross_new_pct,
                    limit=allowance_pct,
                ))

            # Loud-failure requirement (2026-09-01): a symbol resolving to
            # "Unknown" must never pass silently. Advisory (never in
            # HARD_BLOCK_RULES on its own) — mirrors the non-blocking seam
            # `data_degraded` / `correlation_coverage_gap` /
            # `pm_audit_step_missing` already use elsewhere in this
            # pipeline: it reaches the Risk Manager's prompt via
            # `rule_violations` and (src/pipeline_stages.py) sets
            # `data_status["sector"]`, which is what puts the plain
            # "degraded" line in the session output and the owner's
            # Telegram alert. A transient lookup failure and a genuinely
            # sector-less instrument are DIFFERENT conditions — one
            # self-heals on the next call, the other won't — so they get
            # different rule names and different wording rather than
            # reading the same.
            if new_sector == "Unknown":
                status = _sector_resolution_status_for(decision.symbol)
                if status == "no_sector":
                    alert_rule = "sector_unresolved_no_sector"
                    reason = (
                        f"{decision.symbol}: sector lookup succeeded but returned "
                        f"no sector — this instrument may genuinely be unclassified "
                        f"(e.g. an ETF outside the static table)."
                    )
                elif status == "lookup_failed":
                    alert_rule = "sector_unresolved_lookup_failed"
                    reason = (
                        f"{decision.symbol}: sector lookup failed or timed out "
                        f"(transient — not cached, will self-heal once the "
                        f"lookup succeeds again)."
                    )
                else:
                    alert_rule = "sector_unresolved"
                    reason = f"{decision.symbol}: sector did not resolve."
                violations.append(RiskViolation(
                    rule=alert_rule,
                    message=(
                        f"{reason} Treated as constrained in the pooled 'Unknown' "
                        f"sector bucket ({sector_pct:.1f}% {side_label} of book) and "
                        f"checked against both max_sector_pct and "
                        f"max_sector_hard_pct — NOT exempt. This is the failure "
                        f"mode that used to switch the sector cap off silently."
                    ),
                    value=sector_pct,
                    limit=self.config.max_sector_pct,
                ))

        return violations

