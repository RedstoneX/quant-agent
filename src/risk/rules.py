import logging
from dataclasses import dataclass
from src.config import RiskConfig
from src.models import TradeDecision, Position

logger = logging.getLogger(__name__)

# Leveraged/inverse ETF multipliers for effective exposure calculation.
# Negative = inverse/short (hedge-like against the underlying index).
_ETF_LEVERAGE = {
    "SH": -1.0,    # -1x S&P 500
    "SDS": -2.0,   # -2x S&P 500
    "PSQ": -1.0,   # -1x Nasdaq 100
    "SQQQ": -3.0,  # -3x Nasdaq 100
    "DRAM": 1.0,   # 1x (normal ETF, no adjustment)
    "SMH": 1.0,
}


def _effective_multiplier(symbol: str) -> float:
    """Signed exposure multiplier (negative for inverse ETFs).

    Used for net directional exposure — hedges cancel out.
    """
    return _ETF_LEVERAGE.get(symbol, 1.0)


def _gross_multiplier(symbol: str) -> float:
    """Unsigned leverage magnitude.

    Used for per-symbol and per-sector size limits where direction doesn't matter
    (a 3x ETF still consumes 3x notional regardless of long/short bias).
    """
    return abs(_ETF_LEVERAGE.get(symbol, 1.0))


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
# thesis. An inverse-ETF LONG is long-side exposure in its sector; the
# separate `max_gross_bearish_pct` cap answers the directional question and
# is untouched here.
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
# sector, an ordinary 20% sector-wide drawdown costs 15% of equity — FIVE
# TIMES the 3% daily-loss circuit breaker (`max_daily_loss_pct`), and it will
# trip the de-levering ladder. That is the accepted price of a concentrated
# trading desk, not an oversight.
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
# Shared polarity vocabulary. `PortfolioManagerAgent.validate_grounding`
# (src/agents/portfolio_manager.py) uses this to decide whether a
# provenance claim's stance "supports" a target's direction; the
# constructor's agreement ceiling (`src/portfolio_constructor.py`) uses the
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


def count_aligned_sources(symbol: str, sources: dict[str, str], direction: str) -> int:
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
    """
    wants_bullish = direction != "short"
    return sum(
        1 for source, stance in sources.items()
        if stance_is_aligned(source, symbol, stance, wants_bullish=wants_bullish)
    )


def agreement_ceiling_for_count(schedule: list[float] | tuple[float, ...], count: int) -> float:
    """The risk-allocation ceiling for `count` aligned sources.

    `schedule[i]` is the ceiling for `i + 1` aligned sources. A count of
    zero is treated exactly like one: the technical analysis mandatory for
    any BUY/SHORT (`validate_grounding`'s "lacks a current-run Technical
    analysis" rule) is always itself a registry entry, but it can rate
    neutral or opposite to what the PM proposes — so zero aligned sources
    is a real case, not a hypothetical one, and it is not punished any
    harder than one. A count past the end of the schedule uses the last
    (least restrictive) entry — this book has never measured more than
    `len(schedule)` independent seats agreeing on one symbol, and the
    ratified per-trade envelope (`RiskConfig.max_position_risk_pct`) is
    the hard ceiling regardless, enforced independently of this schedule.
    """
    if not schedule:
        return float("inf")  # no schedule configured — this ceiling is inert
    index = max(0, min(count, len(schedule)) - 1)
    return schedule[index]


DRAWDOWN_BUY_SCALE = 0.5
"""Multiplier applied to every new BUY while the system is in drawdown.

`config/prompts/portfolio_manager.md` has instructed the LLM to halve new BUYs
whenever `in_drawdown=true` since the rule was written, and
`config/prompts/risk_manager.md` told the Risk Manager it was "the only check"
because no deterministic code enforced it. A safety rule that depends on a
language model remembering to apply it is not a rule (audit §1.1), so the
halving now lives here, in Python, and the PM prompt no longer pre-applies it —
two independent halvings would quarter the position.
"""


def apply_drawdown_scale(
    decisions: list[TradeDecision], in_drawdown: bool,
) -> tuple[list[TradeDecision], list[str]]:
    """Halve every BUY's (and Stage-3 SHORT's) allocation while in drawdown.

    Returns `(decisions, notes)`. Mutates each scaled decision in place and
    appends provenance to its `reasoning`: the AI Risk Manager audits
    CONSTRUCTED orders against PM's prose, and an unexplained size
    difference reads to it as PM contradicting itself — on 2026-08-20
    exactly that mismatch drew a full-plan veto over deterministic math
    (see `portfolio_constructor.py` `cap_note`).

    SELL, COVER and HOLD are untouched: de-risking (in either direction)
    during a drawdown is the point.
    """
    if not in_drawdown:
        return decisions, []
    notes: list[str] = []
    for decision in decisions:
        # Stage 3: a SHORT opens new risk exactly as a BUY does, so the
        # drawdown-halve applies to it too. SELL, COVER and HOLD stay
        # untouched — de-risking (in either direction) during a drawdown is
        # the point.
        if decision.action not in ("BUY", "SHORT") or decision.allocation_pct <= 0:
            continue
        before = decision.allocation_pct
        after = round(before * DRAWDOWN_BUY_SCALE, 2)
        if after <= 0:
            # Rounds to nothing — the halved trade is not worth submitting.
            decision.allocation_pct = 0.0
            notes.append(
                f"{decision.symbol} {before:.2f}% → 0% (halved below the "
                f"minimum tradable size by the drawdown rule)"
            )
            continue
        decision.allocation_pct = after
        decision.reasoning = (
            decision.reasoning
            + f" [risk engine: {before:.2f}% halved to {after:.2f}% — system "
              f"in_drawdown=true. Deterministic, not PM inconsistency.]"
        )[:800]
        notes.append(f"{decision.symbol} {before:.2f}% → {after:.2f}%")
    if notes:
        logger.warning(
            "Drawdown gate: halved %d BUY(s) — %s", len(notes), "; ".join(notes),
        )
    return decisions, notes


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
              total_value: float, daily_pnl: float,
              pending_investment: float = 0.0,
              # Spec §12.2: keyed by `(sector, side)`, not by sector alone —
              # a pending SHORT must not consume the same sector's LONG
              # budget. A plain-`str` key here is now a bug, and raises
              # nothing silently only because `.get()` on a tuple key simply
              # misses it; `accumulate_pending_sector` is the writer.
              pending_sector_investment: dict[tuple[str, str], float] | None = None,
              pending_symbol_investment: dict[str, float] | None = None,
              baseline: float | None = None,
              correlation_matrix: dict[str, dict[str, float]] | None = None,
              max_correlated_cluster_pct: float = 50.0,
              cash: float | None = None,
              pending_cash_outflow: float = 0.0,
              in_drawdown: bool = False,
              pending_gross_bearish_investment: float = 0.0) -> list[RiskViolation]:
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
        # max_position_pct / max_sector_pct / max_daily_loss_pct. Emit a
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

        # Daily-loss denominator: yesterday-close equity if provided, else current equity.
        # The fallback is only intended for first-day / fresh-account cases where Alpaca
        # legitimately has no last_equity. On an established account a missing baseline
        # usually signals a broker API glitch, so log a warning — the denominator silently
        # flipping from yesterday-close to current equity can make the loss cap appear
        # stricter (or more permissive) than intended within a single session.
        if baseline is None or baseline <= 0:
            logger.warning(
                "daily-loss baseline missing (%s); falling back to current total_value=%.2f",
                baseline, total_value,
            )
            baseline = total_value

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
        # SKIPPED for SHORT. This rule's arithmetic assumes `new_investment`
        # (always a positive magnitude) moves the position FURTHER in the
        # direction `current_symbol_raw` is already signed toward — true for
        # a BUY adding to a long (both positive, they sum), but wrong for a
        # SHORT adding to a short: `current_symbol_raw` is negative (a held
        # short's market_value), so `current_symbol_raw + new_investment`
        # OFFSETS toward zero instead of growing, understating a growing
        # short as shrinking and never tripping the cap. D9's
        # `max_single_short_pct` below is the correct, direction-aware
        # replacement for a SHORT — deliberately a tighter ceiling, not the
        # same one.
        if not is_short:
            current_symbol_raw = sum(p.market_value for p in positions if p.symbol == decision.symbol)
            current_symbol_raw += (pending_symbol_investment or {}).get(decision.symbol, 0.0)
            position_pct = (current_symbol_raw + new_investment) * gross_mul / total_value * 100
            if position_pct > self.config.max_position_pct:
                violations.append(RiskViolation(
                    rule="max_position_pct",
                    message=f"{decision.symbol} position would be {position_pct:.1f}% and exceed max {self.config.max_position_pct}%",
                    value=position_pct,
                    limit=self.config.max_position_pct,
                ))

        # D9 (Stage 3): the single-short notional cap. HARD BLOCK — in
        # HARD_BLOCK_RULES (src/pipeline.py) — on opening/adding a short;
        # never reached for a COVER (exempted at the top of this method) or
        # a BUY (guarded by `is_short` here — see the note below for why
        # this one, unlike the gross ceiling just after it, stays
        # short-only).
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
            single_short_pct = (
                (abs(current_short_raw) + pending_same_symbol + new_investment)
                * gross_mul / total_value * 100
            )
            if single_short_pct > self.config.max_single_short_pct:
                violations.append(RiskViolation(
                    rule="max_single_short_pct",
                    message=(
                        f"{decision.symbol} short would be {single_short_pct:.1f}% "
                        f"and exceed max {self.config.max_single_short_pct}% "
                        f"(half the {self.config.max_position_pct:.0f}% long "
                        f"single-name ceiling — a short's loss is unbounded)"
                    ),
                    value=single_short_pct,
                    limit=self.config.max_single_short_pct,
                ))
            # Deliberately NOT extended to a BUY of an inverse ETF, even
            # though such a BUY is bearish exposure and IS gated by the
            # gross ceiling just below. `max_single_short_pct` sits at half
            # of `max_position_pct` specifically because a SHORT's loss is
            # unbounded — a squeeze has no floor the way a long's does at
            # -100%. An inverse-ETF LONG's loss is bounded at the position's
            # notional exactly like any other long, so it does not earn
            # that extra-tight treatment; it stays governed by the ordinary
            # `max_position_pct` (rule 1 above), which already charges it at
            # its full gross leverage multiple.

        # Gross BEARISH exposure ceiling. HARD BLOCK — in HARD_BLOCK_RULES
        # (src/pipeline.py). Renamed from the old `max_short_gross_pct`
        # (2026-08-30) when it was widened to see inverse-ETF LONGs as
        # bearish exposure — and corrected again the same day for the
        # mirror-image error that first widening introduced: `is_short`
        # alone is NOT "bearish". Shorting a -3x fund like SQQQ is a
        # BULLISH bet — it profits when SQQQ falls, which is when the
        # index it inverts RISES — so gating on `decision.action ==
        # "SHORT"` charged a bullish position against the bearish ceiling.
        # `signed_new` (computed above; the same expression rule 2's net-
        # exposure check already relies on) is directionally correct in
        # all four quadrants:
        #   BUY   AAPL -> +new_investment    (bullish, excluded)
        #   BUY   SQQQ -> -3*new_investment  (bearish, INCLUDED)
        #   SHORT AAPL -> -new_investment    (bearish, INCLUDED)
        #   SHORT SQQQ -> +3*new_investment  (bullish, excluded)
        # so both the gate and the contribution key off ITS sign, not off
        # `decision.action` or a hardcoded ticker list — a fund added to
        # `_ETF_LEVERAGE` later is picked up automatically, in whichever
        # direction its sign implies.
        if signed_new < 0:
            # Same unified rule for the held book: a position's signed
            # bearish exposure is its (already-signed) market_value times
            # its signed multiplier; a negative product is bearish, and
            # `abs(...)` of it is what it costs against the ceiling. A
            # held SHORT of an ordinary name (negative mv * +1 mult) is
            # negative -> counted. A held LONG inverse ETF (positive mv *
            # negative mult) is negative -> counted. A held SHORT of an
            # INVERSE ETF (negative mv * negative mult) is POSITIVE ->
            # NOT counted — it's bullish exposure, same as a held LONG of
            # an ordinary name.
            current_gross_bearish = sum(
                abs(p.market_value * _effective_multiplier(p.symbol))
                for p in positions
                if p.market_value * _effective_multiplier(p.symbol) < 0
            )
            # `pending_gross_bearish_investment` is the running total of
            # OTHER bearish orders — by this same signed test, not by
            # `decision.action` — already allowed earlier in this same
            # batch (see `TradingPipeline._filter_hard_risk_decisions`) —
            # without it, two bearish orders in the same run would each be
            # checked against only the pre-existing book and never see
            # each other, the same gap `pending_investment` closes for net
            # exposure and `pending_sector_investment` closes for sector.
            gross_bearish_pct = (
                (current_gross_bearish + pending_gross_bearish_investment + abs(signed_new))
                / total_value * 100
            )
            if gross_bearish_pct > self.config.max_gross_bearish_pct:
                violations.append(RiskViolation(
                    rule="max_gross_bearish_pct",
                    message=(
                        f"Total gross bearish exposure (shorts + inverse-ETF "
                        f"longs) would be {gross_bearish_pct:.1f}% and exceed "
                        f"max {self.config.max_gross_bearish_pct}%"
                    ),
                    value=gross_bearish_pct,
                    limit=self.config.max_gross_bearish_pct,
                ))

        # 1b. Drawdown gate (audit §1.1). `apply_drawdown_scale` above has
        # already halved every BUY on the normal path; this is the fail-closed
        # backstop for any path that reaches the engine unscaled. It bounds the
        # NEW money only — deliberately not the whole position, because the
        # rule the prompts have always stated is "halve every new BUY", not
        # "force-trim existing winners during a drawdown".
        if in_drawdown:
            drawdown_new_cap = self.config.max_position_pct * DRAWDOWN_BUY_SCALE
            new_pct = decision.allocation_pct * gross_mul
            if new_pct > drawdown_new_cap:
                violations.append(RiskViolation(
                    rule="drawdown_buy_cap",
                    message=(
                        f"{decision.symbol} new BUY of {new_pct:.1f}% exceeds the "
                        f"{drawdown_new_cap:.1f}% drawdown cap "
                        f"({self.config.max_position_pct:.0f}% x "
                        f"{DRAWDOWN_BUY_SCALE}) — system is in drawdown"
                    ),
                    value=new_pct,
                    limit=drawdown_new_cap,
                ))

        # 2. Total net exposure limit — signed, so long+short hedges cancel
        current_net = sum(p.market_value * _effective_multiplier(p.symbol) for p in positions)
        net_exposure = current_net + pending_investment + signed_new
        total_pct = abs(net_exposure) / total_value * 100
        if total_pct > self.config.max_total_position_pct:
            violations.append(RiskViolation(
                rule="max_total_position_pct",
                message=f"Net exposure {total_pct:.1f}% would exceed max {self.config.max_total_position_pct}%",
                value=total_pct,
                limit=self.config.max_total_position_pct,
            ))

        # 3. Daily loss limit (% of the baseline — prior close equity).
        # NaN guard mirrors check_daily_loss (line 240): a NaN daily_pnl
        # (Alpaca portfolio_value glitches propagate into
        # total_value - last_equity) makes every numeric comparison
        # False, silently disabling rule 3 inside the per-BUY pipeline
        # path. Audit 2026-05-27: standalone check_daily_loss + force-
        # delever already had the guard; this per-BUY backup path did
        # not — inconsistent defense.
        if not math.isfinite(daily_pnl):
            logger.warning(
                "RiskRuleEngine.check: daily_pnl is non-finite (%s) — "
                "skipping per-BUY daily-loss rule for %s; standalone "
                "check_daily_loss + force_delever remain in force",
                daily_pnl, decision.symbol,
            )
        else:
            daily_loss_pct = abs(daily_pnl / baseline * 100) if daily_pnl < 0 else 0
            if daily_loss_pct > self.config.max_daily_loss_pct:
                violations.append(RiskViolation(
                    rule="max_daily_loss_pct",
                    message=f"Daily loss {daily_loss_pct:.1f}% exceeds max {self.config.max_daily_loss_pct}%. Trading paused.",
                    value=daily_loss_pct,
                    limit=self.config.max_daily_loss_pct,
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
        # not model). D9's dedicated caps, not this rule, are the control
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

    def check_daily_loss(self, baseline: float, daily_pnl: float) -> RiskViolation | None:
        """Standalone daily loss check. `baseline` is the % denominator (e.g. last_equity).

        NaN handling: any NaN in `baseline` or `daily_pnl` (Alpaca has been
        observed to return NaN for `portfolio_value` during market-open
        glitches; that propagates into `last_equity` and `daily_pnl` via
        `total_value - last_equity`) makes every comparison False, which
        would SILENTLY DISABLE the circuit breaker on exactly the kind of
        broken-snapshot day where the breaker is most valuable. So:
          - NaN baseline → can't compute %, treat as "no signal" + LOG so
            the operator knows the breaker was bypassed.
          - NaN daily_pnl → same.
        Both raise no violation but emit a WARNING; force_delever is the
        downstream safety net for the actual cash-deficit case.
        """
        import math
        if not math.isfinite(baseline):
            logger.warning(
                "check_daily_loss: baseline is non-finite (%s) — circuit "
                "breaker bypassed for this call. Likely Alpaca returned "
                "NaN portfolio_value/last_equity; force_delever is the "
                "downstream safety net.",
                baseline,
            )
            return None
        if not math.isfinite(daily_pnl):
            logger.warning(
                "check_daily_loss: daily_pnl is non-finite (%s) — circuit "
                "breaker bypassed for this call.",
                daily_pnl,
            )
            return None
        if baseline <= 0:
            return None
        daily_loss_pct = abs(daily_pnl / baseline * 100) if daily_pnl < 0 else 0
        if daily_loss_pct > self.config.max_daily_loss_pct:
            return RiskViolation(
                rule="max_daily_loss_pct",
                message=f"Daily loss {daily_loss_pct:.1f}% exceeds max {self.config.max_daily_loss_pct}%",
                value=daily_loss_pct,
                limit=self.config.max_daily_loss_pct,
            )
        return None
