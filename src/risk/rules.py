import logging
import math
from dataclasses import dataclass, field
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


# --- Spec §11.2 — gross exposure, its ceiling, and the de-levering ladder --
#
# WHAT DID NOT EXIST BEFORE THIS SECTION: any gross-exposure ceiling at all.
# `max_portfolio_risk_pct` (25) bounds capital AT RISK — the sum of stop
# distances — and `max_gross_bearish_pct` (20) bounds the bearish side only.
# Neither is a bound on how much the book OWNS, and nothing stopped it
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
#: `HARD_BLOCK_RULES` (src/pipeline.py) — one string, two files.
GROSS_EXPOSURE_RULE = "max_gross_exposure"


def _positive_float(value, default: float = 0.0) -> float:
    """Coerce a config value to a usable positive float, or `default`.

    Same Mock-safety posture `pipeline.py::_risk_setting` already takes: a
    MagicMock config fixture auto-creates a child mock for any attribute
    access, and `mock > 0` raises rather than returning False. A ceiling that
    cannot be read is INERT (default 0.0 switches the rule off) rather than
    blocking, matching `agreement_ceiling_for_count`'s "no schedule
    configured — this ceiling is inert". Production config always carries the
    validated field, so this path is a test/misconfiguration guard only.
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
    `daily_pnl` table (`in_drawdown: False`).
    """
    base = float(base_x) if isinstance(base_x, (int, float)) and base_x > 0 else 0.0
    if not math.isfinite(base) or base <= 0:
        base = 0.0
    if drawdown_pct is None or not math.isfinite(drawdown_pct):
        return GrossCeiling(
            ceiling_x=base, base_x=base, drawdown_pct=None, alert_owner=False,
            rung="unknown",
            reason=(
                f"No measured equity history, so no drawdown could be "
                f"computed. Gross exposure is held to the standing "
                f"{base:.1f}x ceiling and nothing is trimmed on an "
                f"unmeasured book."
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

    Deliberately NOT the same measure as `in_drawdown` (rolling 5-day /
    20-day returns). Those two answer different ratified questions: "has our
    recent edge degraded, so halve new BUYs" versus "how far are we off the
    high-water mark, so how much may the book own". Both are drawdown; only
    one sets the ceiling.
    """
    values = []
    for raw in list(equity_history or []) + [current_equity]:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        value = float(raw)
        if math.isfinite(value) and value > 0:
            values.append(value)
    if not values:
        return None
    if not (
        isinstance(current_equity, (int, float))
        and not isinstance(current_equity, bool)
        and math.isfinite(float(current_equity))
        and float(current_equity) > 0
    ):
        return None
    peak = max(values)
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
# WHY `deployed_pct` IS THE ONE COMPARED TO `target_invested_pct`, and not
# the signed leverage-aware number:
#
#  1. The target is DEFINED as the complement of cash by the seat that emits
#     it. `MacroPositionGuidance` bounds `target_invested_pct` and
#     `cash_recommendation_pct` to 0-100 each, and the macro prompt requires
#     them to "sum to ~100". It is a capital-deployment target.
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
#     `apply_gross_ceiling`, and by `max_gross_bearish_pct`. It does not need
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


@dataclass(frozen=True)
class BookExposure:
    """One book, measured three ways that can no longer drift apart.

    `deployed` — capital committed, unsigned, NO leverage multiple. The
        cash-complement measure, and the ONLY one comparable to macro's
        `target_invested_pct`.
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
       and SHORT is rationed against the remaining headroom; one shrunk
       below `min_order_usd` is refused outright rather than placed as a
       token position (§10.3's floor — a position too small to pay for its
       own risk is not a smaller trade, it is a worse one).
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
    (their `allocation_pct` reduced and their `reasoning` annotated), the
    same convention `apply_drawdown_scale` uses so the AI Risk Manager does
    not read deterministic arithmetic as the PM contradicting itself.
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
                out.notes.append(
                    f"{GROSS_EXPOSURE_RULE}: {decision.symbol} refused — the "
                    f"account equity figure ({equity}) is not usable, so the "
                    f"gross-exposure ceiling cannot be computed. No new "
                    f"position opens on an unreadable account."
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
        # `available` is GROSS dollars; `min_order_usd` is a NOTIONAL floor —
        # what the order actually costs, which is what pays the commission.
        # For a leveraged ETF the two differ: $600 of gross headroom in SQQQ
        # (3x) buys a $200 order, which is below the floor. Comparing gross
        # against a notional threshold would let exactly that token position
        # through on the two tickers whose multiplier exceeds 1 (SDS 2x,
        # SQQQ 3x); for everything else the two figures are identical.
        if (available / multiplier) < max(0.0, min_order_usd):
            decision.allocation_pct = 0.0
            out.blocked.append(decision.symbol)
            out.notes.append(
                f"{GROSS_EXPOSURE_RULE}: {decision.symbol} refused — {reason}, "
                f"and what the ceiling still allows is below the "
                f"${min_order_usd:,.0f} minimum worth trading. "
                f"{ceiling.reason}"
            )
            continue
        # Round DOWN to 2dp so the granted size can never land back above the
        # headroom that permitted it.
        after = math.floor(
            (available / (equity * multiplier) * 100.0) * 100.0
        ) / 100.0
        if after <= 0 or (equity * (after / 100.0)) < min_order_usd:
            decision.allocation_pct = 0.0
            out.blocked.append(decision.symbol)
            out.notes.append(
                f"{GROSS_EXPOSURE_RULE}: {decision.symbol} refused — {reason}, "
                f"and what the ceiling still allows is below the "
                f"${min_order_usd:,.0f} minimum worth trading. "
                f"{ceiling.reason}"
            )
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
    *, ceiling: GrossCeiling | None = None,
) -> tuple[list[TradeDecision], list[str]]:
    """Halve every BUY's (and Stage-3 SHORT's) allocation while in drawdown.

    **The §11.2 gross-exposure ceiling is NOT computed here, and must never
    be.** Scaling proposed decisions and bounding the live book are two
    different jobs, and only one of them may depend on the Portfolio Manager
    producing a parseable book. This function takes a decision list; a blank
    PM response makes it a no-op, which is correct for sizing and would be
    catastrophic for a ceiling. `resolve_gross_ceiling` therefore reads
    account state alone and `apply_gross_ceiling` enforces it with
    `decisions=[]` on exactly those runs. `ceiling` is accepted here only so
    the note this function writes can NAME the rung that is also in force —
    it changes no arithmetic, and passing None changes nothing.

    The two are wired to one ladder rather than two mechanisms: there is a
    single `GROSS_LADDER`, a single `resolve_gross_ceiling`, and this
    function's halving is the ratified rolling-window rule it always was
    (`DRAWDOWN_BUY_SCALE`), unchanged in threshold or magnitude by §11.2.
    See `peak_to_trough_pct` for why the two drawdown measures are
    deliberately distinct.

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
        rung_note = (
            f" Gross exposure is capped at {ceiling.ceiling_x:.1f}x equity by "
            f"the §11.2 de-levering ladder."
            if ceiling is not None and ceiling.de_levered else ""
        )
        decision.reasoning = (
            decision.reasoning
            + f" [risk engine: {before:.2f}% halved to {after:.2f}% — system "
              f"in_drawdown=true. Deterministic, not PM inconsistency."
              f"{rung_note}]"
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
              pending_gross_bearish_investment: float = 0.0,
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
            # Through `weight_pct_of` — the one definition of a
            # gross-leverage weight. Identical arithmetic to the inline
            # `* gross_mul / total_value * 100` it replaces.
            single_short_pct = weight_pct_of(
                abs(current_short_raw) + pending_same_symbol + new_investment,
                decision.symbol, total_value,
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

        # 1c. Spec §11.2 — the GROSS-exposure ceiling. HARD BLOCK (in
        # HARD_BLOCK_RULES, src/pipeline.py).
        #
        # Distinct from rule 2 below in the way that matters: rule 2 measures
        # NET exposure, where a hedge cancels a long, and from
        # `max_gross_bearish_pct` above, which measures only the bearish
        # side. Neither answers "how much does the book OWN", which is what
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
