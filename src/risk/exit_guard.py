"""Deterministic guard on exit reasoning — spec Phase 3.2, audit §1.5.

The Position Reviewer had no memory of its own prior review. It rebuilt its
view of every position from scratch twice a day, so it could report a position
deteriorating while every number it had itself recorded six hours earlier had
improved. On 2026-08-26 it sold EPD for "not progressing" when thesis progress
had risen 16% -> 20% and distance-to-stop had improved since its own midday
read. The evening reviewer then graded that exit **premature**, and the same
thing happened to MRVL.

This module is the deterministic half of the fix. It does not decide anything
about the market; it only refuses to let a **deterioration claim** stand when
the deterioration did not happen. The reviewer keeps full authority to exit on
new information — adverse news, an earnings miss, a regime shift, a
triggered `thesis_invalid_if`. Those are judgments about
the world. "It is stalling" is a claim about numbers, and the numbers are
right here.

Directionality matters and is the whole point:
  - `thesis_progress_pct` rising is improvement.
  - `distance_to_stop_pct` rising is improvement ONLY when the PRICE moved
    away from the stop, on EITHER side — see `veto_contradicted_exit`'s
    docstring for the 2026-09-18 fix that made the underlying metric
    actually side-correct for a short. It is also a function of the stop
    as much as of the price, so widening the stop also makes it rise —
    which is the desk removing its own protection, not the position
    getting better. Verified on real 2026-09-01 snapshots for V, CMCSA and
    DIS, all three of which were deteriorating at the time. Decomposed
    since 2026-09-18; see `_STOP_DEPENDENT_METRIC` and
    `MetricDeltas.stop_driven`, both side-aware since the same date.
  - `r_multiple` rising is improvement.
  - `pace` rising is improvement.
A verdict may not call a position stalled while its own measured deltas are
positive.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

__all__ = [
    "MetricDeltas",
    "compute_deltas",
    "is_deterioration_claim",
    "veto_contradicted_exit",
    "DETERIORATION_PATTERNS",
    "EXTERNAL_INFORMATION_PATTERNS",
    "cites_external_information",
    "adverse_move_is_noise",
    "noise_band_atr",
    "NOISE_BAND_ATR_MULTIPLE",
    "BREAK_CONFIRMATION_ATR_MULTIPLE",
    "ThesisInvalidationCheck",
    "check_thesis_invalid_if",
    "TRUSTED_MACRO_STATUSES",
    "claims_regime_flip",
    "claims_bearish_state_change",
    "claims_thesis_invalidation",
    "holding_discipline_false_claim",
    "HoldingDisciplineClaimCheck",
    "holding_discipline_claim_check",
    "StructuralProtectionCheck",
    "check_structural_protection",
    "structural_protection_broken",
]


#: Phrases that assert a position is going backwards. Deliberately narrow:
#: these are claims about the position's OWN trajectory, which is exactly what
#: the stored metrics can adjudicate. Anything about the world (news, earnings,
#: regime, invalidation) is not here and is never vetoed by this module.
DETERIORATION_PATTERNS: tuple[str, ...] = (
    r"\bstall(?:ed|ing|s)?\b",
    r"\bnot progress(?:ing)?\b",
    r"\bno progress\b",
    r"\black of progress\b",
    r"\bfail(?:ed|ing|s)? to progress\b",
    r"\bgoing nowhere\b",
    r"\bdead money\b",
    r"\blosing momentum\b",
    r"\bmomentum (?:has )?fad(?:ed|ing)\b",
    r"\bdeteriorat(?:ed|ing|ion)\b",
    r"\bweaken(?:ed|ing)\b",
    r"\bslow(?:ing|ed)? (?:down|progress)\b",
    r"\bbehind schedule\b",
    r"\bstagnant\b",
    r"\bdrifting\b",
)

_DETERIORATION_RE = re.compile("|".join(DETERIORATION_PATTERNS), re.IGNORECASE)

#: Metrics where a HIGHER value means the position is doing better.
_HIGHER_IS_BETTER = ("thesis_progress_pct", "distance_to_stop_pct", "r_multiple", "pace")

#: The one metric in `_HIGHER_IS_BETTER` that is a function of the desk's
#: OWN protection as much as of the market.
#:
#: `distance_to_stop_pct = (current - stop) / current * 100`
#: (`TradingPipeline._build_position_facts`). Both terms move. Raise the
#: price and it improves — that is a real improvement. LOWER THE STOP and it
#: also improves, and nothing about the position got better; the desk just
#: took off some of its own protection.
#:
#: Verified on real recorded snapshots, 2026-09-01 midday vs the 2026-08-31
#: close (`specialist_evidence`, agent_name='position_reviewer',
#: kind='review_metrics'):
#:   V      distance-to-stop 1.42 -> 3.41 "improved" while r_multiple fell
#:          -0.11 -> -0.82. Price fell and distance rose, which is only
#:          possible if the stop moved down (entry 381.18 / stop 374.27 at
#:          entry; the implied stop by 09-01 midday is ~362.7).
#:   CMCSA  3.53 -> 5.58 "improved" while r_multiple fell -0.04 -> -0.33.
#:   DIS    1.85 -> 5.07 "improved" while thesis progress fell
#:          -3.06 -> -12.50.
#: In all three the position was demonstrably deteriorating and one of the
#: four "things improved" measures said otherwise.
#:
#: The fix is decomposition, not deletion — see
#: `MetricDeltas.stop_driven`. Deleting the metric would lose the genuine
#: and load-bearing case: a position whose PRICE has risen away from a
#: FIXED stop really has improved, and that is the case the metric was
#: added for (the 2026-08-26 EPD premature exit in this module's own
#: header cites distance-to-stop having improved).
_STOP_DEPENDENT_METRIC = "distance_to_stop_pct"

#: Snapshot fields that are not metrics but are needed to attribute a
#: `distance_to_stop_pct` move to the price or to the stop. Carried
#: alongside the metrics by `TradingPipeline._REVIEW_METRIC_KEYS`. `qty`
#: (2026-09-18 follow-up) supplies the SIDE: `distance_to_stop_pct` mirrors
#: its numerator for a short (`stop - current` instead of `current -
#: stop`), and the counterfactual recomputation below must mirror the same
#: way or it silently reproduces the pre-fix long-only formula for every
#: short reviewed.
_PROVENANCE_KEYS = ("stop_loss", "current_price", "qty")

#: How much a metric must move before it counts as a real change rather than
#: rounding noise. Expressed in each metric's own units.
_NOISE_FLOOR = {
    "thesis_progress_pct": 0.5,
    "distance_to_stop_pct": 0.1,
    "r_multiple": 0.05,
    "pace": 0.05,
}


def _finite(value: object) -> float | None:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


@dataclass(frozen=True)
class MetricDeltas:
    """Change in one position's metrics since the previous review."""

    symbol: str
    changes: dict[str, tuple[float, float]] = field(default_factory=dict)
    prior_timestamp: str | None = None
    #: `{stop_loss: (before, after), current_price: (before, after),
    #: qty: (before, after)}` when the snapshot pair carried them. Not
    #: metrics and never scored — they exist only to attribute a
    #: `distance_to_stop_pct` move (see `_STOP_DEPENDENT_METRIC`) and, via
    #: `qty`'s sign, to know which side's formula to replay. Empty for a
    #: snapshot written before 2026-09-18, which is handled explicitly
    #: rather than guessed.
    provenance: dict[str, tuple[float, float]] = field(default_factory=dict)

    @property
    def has_prior(self) -> bool:
        return bool(self.changes)

    def _distance_move_is_price_driven(self) -> bool | None:
        """Did `distance_to_stop_pct` improve because the PRICE moved?

        True  - it improves even holding the stop where it was.
        False - it only improves because the stop moved; the price alone
                does not carry it past the same noise floor.
        None  - unattributable: this pair of snapshots does not carry the
                stop and the price, or the recorded side flipped between
                the two snapshots, so the question cannot be answered.

        The test is the metric's own formula with the stop pinned to its
        prior value — an exact decomposition, not an estimate, and it
        introduces no number: it reuses `_NOISE_FLOOR` exactly as
        `improved` does. It must mirror
        `TradingPipeline._build_position_facts`'s side-aware formula
        exactly (2026-09-18 fix + follow-up), or it silently reproduces
        the pre-fix long-only arithmetic for every short reviewed —
        recomputing the wrong number is worse than not recomputing at all.
        """
        pair = self.changes.get(_STOP_DEPENDENT_METRIC)
        if pair is None:
            return None
        before_stop = self.provenance.get("stop_loss")
        prices = self.provenance.get("current_price")
        if before_stop is None or prices is None:
            return None
        stop_then = before_stop[0]
        price_now = prices[1]
        if price_now <= 0:
            return None
        # `qty` is the side. Missing (a snapshot pair that predates this
        # field, or a hand-built long-only fixture) defaults to LONG rather
        # than refusing to attribute: that reproduces this function's own
        # pre-existing behaviour exactly, which is correct for an actual
        # long and, for an actual short, merely continues for one more
        # review cycle the same long-only misattribution this whole fix
        # closes — never worse than the status quo it replaces, and it
        # self-heals once both snapshots carry `qty`.
        qty_pair = self.provenance.get("qty")
        if qty_pair is None:
            is_short = False
        else:
            qty_before, qty_after = qty_pair
            # A side flip between snapshots (long closed and a short opened
            # on the same symbol, or vice versa) means the "prior stop" is
            # not even the same position's stop. Refuse to attribute rather
            # than guess — same fail-safe posture as every other None here.
            if (qty_before < 0) != (qty_after < 0):
                return None
            is_short = qty_after < 0
        # Mirrors `dist_stop_pct` in `TradingPipeline._build_position_facts`
        # exactly: SHORT numerator is `(stop - current)` (stop sits above
        # price), LONG is `(current - stop)`. The stop is held at its
        # PRIOR value; only price is let move, to isolate its contribution.
        numerator = (stop_then - price_now) if is_short else (price_now - stop_then)
        counterfactual = numerator / price_now * 100
        floor = _NOISE_FLOOR.get(_STOP_DEPENDENT_METRIC, 0.0)
        return counterfactual - pair[0] > floor

    @property
    def stop_driven(self) -> list[str]:
        """Metrics whose apparent IMPROVEMENT is not the position improving.

        Today this is only ever `distance_to_stop_pct`, and only when the
        improvement survives on the arithmetic solely because the stop
        moved. `improved` excludes these, so loosening protection can no
        longer make a deteriorating position read as improving.

        An unattributable case (no stop/price in the snapshot pair) is
        listed here too. That is the conservative direction on this path:
        not counting an improvement can only make
        `veto_contradicted_exit` fire LESS, and a veto stranding the desk
        in a losing position is the worse failure (the call site's own
        disclosed reasoning).
        """
        if _STOP_DEPENDENT_METRIC not in self.changes:
            return []
        before, after = self.changes[_STOP_DEPENDENT_METRIC]
        if after - before <= _NOISE_FLOOR.get(_STOP_DEPENDENT_METRIC, 0.0):
            return []  # not an apparent improvement at all
        return [] if self._distance_move_is_price_driven() else [_STOP_DEPENDENT_METRIC]

    @property
    def improved(self) -> list[str]:
        """Metrics that moved in the position's favour beyond the noise floor.

        Excludes a `distance_to_stop_pct` rise that came from the stop
        moving rather than the price moving (`stop_driven`) — the desk
        does not get to call loosening its own protection an improvement.
        """
        excluded = set(self.stop_driven)
        out = []
        for name, (before, after) in self.changes.items():
            if name not in _HIGHER_IS_BETTER or name in excluded:
                continue
            if after - before > _NOISE_FLOOR.get(name, 0.0):
                out.append(name)
        return sorted(out)

    @property
    def worsened(self) -> list[str]:
        """Metrics that moved against the position beyond the noise floor.

        Deliberately NOT decomposed. A `distance_to_stop_pct` FALL can
        also be stop-driven (the stop was tightened), but discounting it
        would remove an entry from `worsened` and could turn
        `net_improved` True — making the veto fire more often and
        blocking an exit on paperwork. The asymmetry is the fail-open
        direction this path requires, not an oversight.
        """
        out = []
        for name, (before, after) in self.changes.items():
            if name not in _HIGHER_IS_BETTER:
                continue
            if before - after > _NOISE_FLOOR.get(name, 0.0):
                out.append(name)
        return sorted(out)

    @property
    def net_improved(self) -> bool:
        """True when something improved and nothing measurably worsened.

        Deliberately strict. A mixed picture (progress up, distance-to-stop
        down) is a real judgment call and stays the reviewer's to make; only an
        unambiguous improvement contradicts a deterioration claim.
        """
        return bool(self.improved) and not self.worsened

    def render(self) -> str:
        """One line per changed metric, for the reviewer's prompt."""
        if not self.changes:
            return f"  {self.symbol}: no prior review on record (first look)"
        bits = []
        stop_driven = set(self.stop_driven)
        for name in sorted(self.changes):
            before, after = self.changes[name]
            arrow = "→"
            direction = ""
            if name in _HIGHER_IS_BETTER:
                floor = _NOISE_FLOOR.get(name, 0.0)
                if name in stop_driven:
                    # Never shown to the reviewer as "(improved)". It read
                    # that way on real 2026-09-01 snapshots for V, CMCSA
                    # and DIS while all three were deteriorating.
                    attributed = self._distance_move_is_price_driven()
                    direction = (
                        " (wider stop, NOT an improvement)"
                        if attributed is False
                        else " (rose, provenance unknown — not counted)"
                    )
                elif after - before > floor:
                    direction = " (improved)"
                elif before - after > floor:
                    direction = " (worsened)"
                else:
                    direction = " (flat)"
            bits.append(f"{name} {before:.2f} {arrow} {after:.2f}{direction}")
        stamp = f" since {self.prior_timestamp}" if self.prior_timestamp else ""
        return f"  {self.symbol}{stamp}: " + " | ".join(bits)


def compute_deltas(
    symbol: str,
    prior: dict | None,
    current: dict | None,
    prior_timestamp: str | None = None,
) -> MetricDeltas:
    """Metric-by-metric change, skipping anything missing on either side."""
    changes: dict[str, tuple[float, float]] = {}
    provenance: dict[str, tuple[float, float]] = {}
    if prior and current:
        for name in _HIGHER_IS_BETTER:
            before = _finite(prior.get(name))
            after = _finite(current.get(name))
            if before is None or after is None:
                continue
            changes[name] = (before, after)
        # Stop/price/qty are carried, never scored — they only answer "did
        # the distance-to-stop move because the market moved or because we
        # moved the stop, and on which side?". Absent on snapshots written
        # before 2026-09-18.
        for name in _PROVENANCE_KEYS:
            before = _finite(prior.get(name))
            after = _finite(current.get(name))
            if before is None or after is None:
                continue
            provenance[name] = (before, after)
    return MetricDeltas(
        symbol=symbol.upper(), changes=changes, prior_timestamp=prior_timestamp,
        provenance=provenance,
    )


def is_deterioration_claim(reason: str) -> bool:
    """True when `reason` asserts the position's own trajectory is worsening."""
    return bool(reason) and bool(_DETERIORATION_RE.search(reason))


def veto_contradicted_exit(
    action: str, reason: str, deltas: MetricDeltas,
) -> str | None:
    """Return a veto message when an exit contradicts its own numbers, else None.

    Vetoes only when ALL of these hold:
      - the action actually reduces the position (SELL / REDUCE / COVER —
        COVER is the short-side twin: it reduces/closes a SHORT exactly as
        SELL/REDUCE reduce/close a LONG),
      - the stated reason is a deterioration claim about the position itself,
      - a prior snapshot exists to compare against,
      - and every metric that moved, moved in the position's favour.

    Everything else passes through untouched. In particular a SELL/COVER
    citing news, earnings, a regime shift or a triggered invalidation is
    never vetoed here however good the numbers look — those are exits on
    new information, which the reviewer keeps full authority to make (spec
    Phase 3.8).

    `deltas` is trusted to already be direction-corrected for a short, so
    COVER needs no separate sign handling in this function. Verified
    per-metric, 2026-09-18 (each is computed in
    `TradingPipeline._build_position_facts` unless noted):
      - `thesis_progress_pct` — side-correct by construction: both
        `(cur - entry)` and `(progress_target - entry)` flip sign together
        for a short, so the ratio is unchanged.
      - `pace` — inherits `thesis_progress_pct`'s correctness; the
        denominator (`time_fraction`) is never signed.
      - `r_multiple` — side-correct by construction (`src/risk/metrics.py
        ::r_multiple` takes `qty`'s sign as the side and mirrors both the
        numerator and the risk-per-share denominator for a short).
      - `distance_to_stop_pct` — was NOT side-correct until 2026-09-18: it
        was computed as `(cur - stop_loss) / cur * 100` regardless of side,
        which is correct for a long (stop below price) but for a short
        (stop above price) is negative and moves the WRONG way — it gets
        MORE negative, i.e. reads as "worse" under `_HIGHER_IS_BETTER`, as
        the price moves further from the stop and the position gets safer.
        Fixed by mirroring the numerator for `qty < 0`. Any snapshot
        written before that fix still has old-formula (mis-signed for
        shorts) values, so a delta spanning that boundary is stale, not
        wrong — it self-heals after one review cycle.
    """
    if str(action).upper() not in ("SELL", "REDUCE", "COVER"):
        return None
    if not is_deterioration_claim(reason):
        return None
    if not deltas.has_prior or not deltas.net_improved:
        return None
    moved = ", ".join(
        f"{name} {deltas.changes[name][0]:.2f}→{deltas.changes[name][1]:.2f}"
        for name in deltas.improved
    )
    return (
        f"{deltas.symbol}: {action} vetoed — the reason claims the position is "
        f"deteriorating, but every metric that moved since the previous review "
        f"improved ({moved}). A deterioration verdict may not contradict the "
        f"reviewer's own recorded numbers. Exit on new information (news, "
        f"earnings, regime, invalidation) is unaffected."
    )


# ---------------------------------------------------------------------------
# Noise band on exits — spec Phase 3.6
# ---------------------------------------------------------------------------

#: An adverse move smaller than this many ATRs from entry, ON DAY ONE, cannot
#: be distinguished from one ordinary day's range. `TRAIL_STOP` already uses
#: 1.25x ATR14 against CURRENT price for the same reason; this is the
#: equivalent floor for a discretionary exit, measured from ENTRY.
#:
#: OKLO was bought and sold on 2026-08-26 for a 2.5% loss — **0.67 ATR**, on
#: day zero. The evening review graded the buy "wrong" and the sell "correct",
#: but the honest reading is that nothing had happened yet in either
#: direction: the position was never given one day's normal range to breathe.
#:
#: 2026-09-04 audit (real-data fix #1): this constant was applied FLAT,
#: regardless of how long the position had been held, so a position on day 10
#: was judged against the same one-day noise band as a position on day 0. On
#: 8 of 12 real positions opened before the 2026-08-27 stop-floor fix, that
#: flat band came out roughly the SAME WIDTH as the entry stop itself
#: (0.89-1.12 ATR) — meaning almost every attempted discretionary exit on an
#: aging position was blocked, and 5 of 9 real exits ended up as plain broker
#: stop-outs instead of a deliberate call. See `adverse_move_is_noise` below,
#: which now scales this constant by sqrt(sessions_held) — the same
#: convention `src/data/levels.py::derive_structural_target` already uses
#: for target projection (`ATR * sqrt(sessions)`), on the same random-walk
#: basis: expected price dispersion grows with the square root of elapsed
#: TRADING TIME, not linearly and not with calendar time. The `days_held`
#: parameter name below is legacy from the first pass of this fix (2026-09-04
#: audit, real-data fix #1); a follow-up on the same date caught it actually
#: being fed calendar days (`(today - entry_date).days`, weekends included)
#: rather than trading sessions, which over-widened the band by sqrt(3) on
#: every Friday-to-Monday hold — the opposite of the fix's own intent, since
#: only one real session's price action had occurred. Callers MUST pass a
#: trading-session count (see `AlpacaBroker.trading_sessions_held`, item
#: 165's holiday-aware counter — `trading_calendar.trading_sessions_held` is
#: a Mon-Fri-only fallback for callers with no broker connection),
#: never a raw calendar-day count. `sessions_held` is floored at 1 session so
#: day-zero/day-one behaviour is UNCHANGED — only positions held longer than
#: one session get a wider band than before.
NOISE_BAND_ATR_MULTIPLE = 1.0

#: BOARD ITEM 70, THE SPLIT (2026-09-26). Until this date ONE literal `1.0`
#: did TWO different jobs in the exit path: the noise band above (how far an
#: adverse move must travel from ENTRY before it stops being ordinary daily
#: wobble) and the BREAK MARGIN below (how far a daily CLOSE must sit beyond a
#: structural LEVEL before that level counts as broken). They are not the same
#: quantity — different reference points, different units in the literature,
#: and nothing ties their magnitudes — so a shared constant made each one
#: impossible to source without moving the other. They are now two names.
#:
#: NO BEHAVIOUR CHANGES IN THIS SPLIT. Both are 1.0 today, exactly as before;
#: the split exists so each can be answered on its own evidence. Neither value
#: was retuned, which item 70 explicitly forbids in the same pass.
#:
#: WHAT THE RESEARCH FOUND for THIS one (docs/INCIDENT_HISTORY.md, 2026-09-26).
#: The published literature answers "how far beyond a level is a real break" in
#: PERCENT and level-dependently, never in ATRs: Edwards & Magee use ~3% for a
#: major level and ~1% for a short-term one; Bulkowski's answer is essentially
#: "a decisive close", i.e. zero distance, with confirmation carried by the
#: close count and the throwback behaviour instead. There is therefore NO
#: published ATR basis for this quantity at all — it is not merely unsourced,
#: it is unidentifiable in the units it is expressed in. The confirmation RULE
#: around it IS sourced (`TREND_CONFIRMING_CLOSES`, Edwards & Magee's two
#: consecutive closes); only this margin is not. Kept at 1.0 and kept
#: `arbitrary` in config/number_ledger.yaml rather than dressed up as sourced.
BREAK_CONFIRMATION_ATR_MULTIPLE = 1.0

# ---------------------------------------------------------------------------
# TREND-SCALED break confirmation
# (owner mandate 2026-09-24; citation-backed spec — named TA authorities)
# ---------------------------------------------------------------------------
#
# A support/resistance break is confirmed by a decisive CLOSE beyond the level
# by the break margin (`BREAK_CONFIRMATION_ATR_MULTIPLE`, 1.0 ATR — the SAME margin for
# every regime, never a second ATR multiple), held over TWO consecutive closes
# with a reclaim in between resetting (the ratified spring floor). The owner
# mandate (2026-09-24) makes the desk MORE PATIENT only where a break is most
# likely a shakeout; it never lifts protection faster than that floor. Two
# regimes, both grounded, monotonic (nothing exits faster than the floor):
#
#   STRONG-WITH-TREND — a break WITH a strong uptrend (ADX>=25 AND price above a
#     RISING 200-day MA AND 50MA>200MA; short mirror): the shakeout-most-likely
#     case. Require the two-close floor AND that the PRIOR structural swing-low
#     has ALSO closed broken before lifting protection (Sperandeo 1-2-3
#     failed-retest; Bulkowski retest rate). Most patient.
#   DEFAULT — everything else (against the trend, weak/tangled tape, or no trend
#     data): the EXISTING ratified floor — two consecutive confirmed closes
#     beyond 1.0 ATR, reclaim resets (Edwards & Magee two-consecutive-day close
#     filter; Wyckoff spring).
#
# There is deliberately NO single-close path: lifting protection on one close
# would reverse the ratified two-close spring rule and add whipsaw. DIRECTION is
# Weinstein/Dow: price vs a RISING/FALLING 200-day MA (the SLOPE, so a real top
# is not read as "still uptrend") plus the 50/200 stack. STRENGTH is Wilder's
# ADX — only the single >=25 "strong" threshold is used, to ENTER the patient
# regime. The 25 threshold and the 14-session ADX period GATE behaviour, so they
# are trade-governing and sourced to Wilder (1978).
#
# Board item 70, 2026-09-26: this margin used to BE the noise-band constant.
# It is now its own name, `BREAK_CONFIRMATION_ATR_MULTIPLE`, at the same 1.0
# value and with no behaviour change — see that constant for why the published
# literature gives this quantity no ATR basis at all.

#: Consecutive confirmed daily closes beyond the level required to treat ANY
#: break as real. SOURCED: the two-consecutive-day close filter in Edwards &
#: Magee, Technical Analysis of Stock Trends — a level breaks on a decisive
#: close, confirmed on the next day's close, and a reclaim in between resets
#: (Wyckoff spring). Applies in BOTH regimes; the strong-with-trend regime adds
#: a STRUCTURAL condition (the prior swing-low must also break) on top, never a
#: larger close count.
TREND_CONFIRMING_CLOSES = 2

#: Wilder's ADX reading at or above which a trend is treated as STRONG (New
#: Concepts in Technical Trading Systems, 1978; the standard convention that
#: ADX>25 is a trend strong enough to trade with). GATES behaviour: at/above
#: this, a with-trend break enters the patient strong regime (two closes AND the
#: prior swing-low must also break). Below it, the default two-close floor.
ADX_STRONG_TREND_THRESHOLD = 25.0


#: The confirmation REGIME labels `classify_trend_context` returns. Each maps
#: to a sourced confirmation rule in `_break_confirmation_settings`.
REGIME_STRONG_WITH_TREND = "with_trend_strong"
REGIME_DEFAULT = "default"


def classify_trend_context(
    *,
    is_short: bool,
    current_price: float | None,
    ma_50: float | None,
    ma_200: float | None,
    ma_200_prior: float | None = None,
    adx: float | None = None,
) -> str:
    """Classify an adverse structural break into a confirmation REGIME, per the
    owner mandate 2026-09-24 (trend-scaled exit) and its citation-backed spec.
    Exit speed scales with how strongly the break aligns with the prevailing
    trend. Pure and side-aware.

    The "adverse break" is the one that would lift protection: for a LONG,
    price falling through support; for a SHORT, price rising through resistance.

    DIRECTION (Weinstein stage analysis; Dow theory). For a LONG, "with a strong
    trend" (an uptrend an adverse dip is likely a shakeout WITHIN) means price
    ABOVE a RISING 200-day MA *and* the 50MA above the 200MA. Anything else —
    price below a falling 200MA, 50MA<200MA, flat/tangled MAs, or no MA data — is
    the DEFAULT regime. For a SHORT it mirrors: strong-with-trend is price below
    a FALLING 200MA and 50MA<200MA (a strong downtrend). Using the 200MA SLOPE
    (not the level alone) is what stops a real top being read as "still an
    uptrend": once the 200MA rolls over, the position is no longer with-trend.

    STRENGTH (Wilder 1978, ADX): only the single >=`ADX_STRONG_TREND_THRESHOLD`
    (25) "strong" test is used, to ENTER the patient regime.

    Returns one of:
      - REGIME_STRONG_WITH_TREND — break with a STRONG uptrend (ADX>=25 and the
                                   structure above). The shakeout-most-likely
                                   case: the two-close floor AND the prior
                                   structural swing-low must also break.
      - REGIME_DEFAULT           — everything else (against/weak/tangled, or no
                                   trend data): the ratified two-consecutive-
                                   close floor, reclaim resets. Nothing exits
                                   faster than this.
    """
    price = _finite(current_price)
    m50 = _finite(ma_50)
    m200 = _finite(ma_200)
    a = _finite(adx)
    if price is None or m50 is None or m200 is None or a is None:
        # No direction/strength to read -> the ratified two-close floor.
        return REGIME_DEFAULT

    m200_prev = _finite(ma_200_prior)
    rising_200 = m200_prev is not None and m200 > m200_prev
    falling_200 = m200_prev is not None and m200 < m200_prev

    if is_short:
        # A short is "with a strong trend" in a strong DOWNtrend: price below a
        # falling 200MA and 50<200.
        with_trend = (price < m200) and (m50 < m200) and falling_200
    else:
        # A long is "with a strong trend" in an uptrend: price above a rising
        # 200MA and 50>200.
        with_trend = (price > m200) and (m50 > m200) and rising_200

    if with_trend and a >= ADX_STRONG_TREND_THRESHOLD:
        return REGIME_STRONG_WITH_TREND
    return REGIME_DEFAULT


def _break_confirmation_settings(trend_context: str) -> tuple[int, bool]:
    """Map a regime label to `(confirming_closes_needed, requires_prior_low)`.

    The break MARGIN is ALWAYS `BREAK_CONFIRMATION_ATR_MULTIPLE` (1.0 ATR) —
    there is no second, wider ATR multiple. Both regimes use the same two-close floor;
    the strong-with-trend regime adds the prior-swing-low structural condition,
    never a larger close count. There is no single-close path.

      - REGIME_STRONG_WITH_TREND -> 2 consecutive closes AND the prior
        structural swing-low must also close broken (Sperandeo 1-2-3
        failed-retest; Bulkowski retest rate).
      - REGIME_DEFAULT -> `TREND_CONFIRMING_CLOSES` (2) consecutive closes
        (Edwards & Magee two-consecutive-day close filter; reclaim = spring).
    """
    if trend_context == REGIME_STRONG_WITH_TREND:
        return TREND_CONFIRMING_CLOSES, True
    return TREND_CONFIRMING_CLOSES, False


def _prior_structural_level_broken(
    computed_levels: list | None,
    broken_level: float,
    cur: float,
    margin: float,
    *,
    is_short: bool,
) -> bool:
    """Regime 3's structural patience test: has the PRIOR swing point beyond the
    broken level ALSO closed broken?

    For a LONG the broken level is a swing higher-low; the prior swing-low is
    the NEXT structural level below it (from the desk's existing item-55
    `find_structural_levels` output, passed in as `computed_levels`). The strong
    uptrend's structure is only confirmed broken once price has ALSO closed
    below THAT level by the same margin. For a SHORT it mirrors upward.

    Returns True (the gate is satisfied / vacuous) when there is no prior
    structural level beyond the broken one — regime 3 then falls back to the
    plain two-consecutive-close confirmation rather than holding forever.

    NOTE (honest limitation): there is NO volume in the exit path, so this is
    not Wyckoff's volume-confirmed spring; the reclaim-resets-protection test
    elsewhere plus this prior-low break are the structural substitutes, weaker
    than a volume read.
    """
    lvls = [v for v in (_finite(x) for x in (computed_levels or [])) if v is not None]
    if is_short:
        prior = min((x for x in lvls if x > broken_level), default=None)
        if prior is None:
            return True
        return cur >= prior + margin
    prior = max((x for x in lvls if x < broken_level), default=None)
    if prior is None:
        return True
    return cur <= prior - margin


def _trend_clause(is_short: bool, trend_context: str) -> str:
    """The plain-language trend-context phrase for the owner message, correct
    for the position's side. Empty in the default regime."""
    if trend_context == REGIME_STRONG_WITH_TREND:
        return (
            "while it's still in a strong downtrend" if is_short
            else "while it's still in a strong uptrend"
        )
    return ""  # default regime — omit the trend clause


def _compose_owner_break_reason(
    *,
    is_short: bool,
    trigger_desc: str,
    trend_context: str,
    confirmed: bool,
    awaiting_prior_low: bool = False,
) -> str:
    """Assemble the plain-language, owner-facing reason clause for a decisive
    break outcome. Pure and side-aware; states the trigger, the trend context
    and the confirmation state in words. `render_owner_break_message` wraps a
    symbol and lead verb around this for the Telegram/board surfaces.

    Truthful about what the GATE did (it removes the hold-protection veto), not
    what the desk will do: a default confirmed break reads as a "real
    breakdown"; a break held with a strong trend reads as a "possible shakeout"
    the desk is waiting on.
    """
    trend = _trend_clause(is_short, trend_context)
    trend_suffix = f" {trend}" if trend else ""
    if confirmed:
        if trend_context == REGIME_STRONG_WITH_TREND:
            return (
                f"{trigger_desc}{trend_suffix}, and the prior swing-low has now "
                f"broken too, so even the strong trend's structure has failed — "
                f"a real breakdown, confirmed over two closes."
            )
        return (
            f"{trigger_desc}{trend_suffix}, confirmed over two closes — a real "
            f"breakdown, not noise."
        )
    # Pending confirmation — the desk is HOLDING through the break and waiting.
    if trend_context == REGIME_STRONG_WITH_TREND and awaiting_prior_low:
        return (
            f"{trigger_desc}{trend_suffix}, so this looks like a possible "
            f"shakeout; holding until the prior swing-low also breaks."
        )
    if trend_context == REGIME_STRONG_WITH_TREND:
        return (
            f"{trigger_desc}{trend_suffix}, so this looks like a possible "
            f"shakeout; holding one more session for a confirming close."
        )
    return (
        f"{trigger_desc}{trend_suffix}, but the break isn't confirmed yet; "
        f"holding one more session to rule out a one-day false breakdown."
    )


def render_owner_break_message(
    symbol: str, check: "StructuralProtectionCheck",
) -> str | None:
    """The full owner-facing sentence for a decisive structural-protection
    outcome, or None when there is nothing decisive to voice.

    Reused by the pipeline to push the SAME sentence to both owner surfaces —
    the Telegram alert and the dashboard/board journal. Pure: no I/O.

    Lead verb states only what the gate DID, never desk intent: a confirmed
    break REMOVES the hold-protection veto (protected=False lifts the veto only;
    the actual sale still runs the other exit gates, and for a rotation is
    contingent on a replacement buy). A pending break KEEPS the veto. It never
    says or implies the position was sold.
    """
    reason = (check.owner_reason or "").strip()
    if not reason:
        return None
    sym = (symbol or "").strip().upper() or "this position"
    if not check.protected:
        # Confirmed break — the hold-protection veto is removed (not a sale).
        return f"{sym}: hold-protection lifted — it {reason}"
    # Break held pending confirmation — the veto stays on.
    return f"{sym}: hold-protection kept — it {reason}"


def _consecutive_prior_break_count(
    prior_break_records: list | None,
    prior_session_dates: list | None,
    *,
    clears,
) -> int:
    """Count how many CONSECUTIVE prior trading-day closes confirm a break,
    walking back from the most recent completed session.

    Fixes two cross-day confirmation defects in the raw-streak approach:

      - ADJACENCY (#3). `prior_session_dates` is the exact sequence of
        completed trading sessions strictly before today's close, most-recent
        first, taken from the position's own daily bars (the authoritative
        trading calendar — it already skips weekends and holidays). A prior
        broken close counts toward the streak ONLY if it sits on the session
        immediately preceding the last-counted one. A session with no stored
        read, or a stored read that is not broken, ENDS the streak — so a gap
        (Friday's break then the following Friday's, with the week between
        never confirming) can never chain into a false confirmation.

      - MARGIN CONFLATION (#4). `clears(record)` decides whether that day's
        stored close cleared the margin IN FORCE UNDER TODAY's classification
        — not merely whether it was flagged broken at the time under whatever
        (possibly looser) margin then applied. A close that only cleared a
        looser margin does not count.

    Pure: it reads the records the caller supplies and returns a count. Returns
    0 when the caller supplies no records/sessions (the caller then falls back
    to the legacy boolean shorthand).
    """
    if not prior_break_records or not prior_session_dates:
        return 0
    by_date: dict[str, dict] = {}
    for r in prior_break_records:
        d = str(r.get("bar_date") or "").strip()
        # First-seen wins; the caller supplies at most one row per session, but
        # if duplicates arrive keep the earliest in iteration order.
        if d and d not in by_date:
            by_date[d] = r
    count = 0
    for d in prior_session_dates:
        r = by_date.get(str(d))
        if r is None or not r.get("raw_broken") or not clears(r):
            break
        count += 1
    return count


#: Triggers that come from OUTSIDE the price series. These bypass the noise
#: band entirely: an earnings miss is an earnings miss whether the stock has
#: moved 0.2 ATR or 3 ATR, and waiting for price confirmation before acting on
#: information is how you sell the bottom instead of the top.
#:
#: Everything NOT on this list — chiefly thesis invalidation, which in practice
#: means a level broke on the chart — is price-derived, and a price-derived
#: failure inside one ATR of entry has not yet distinguished itself from noise.
EXTERNAL_INFORMATION_PATTERNS: tuple[str, ...] = (
    r"\badverse news\b",
    r"\bmaterial news\b",
    r"\bsector shock\b",
    r"\bbearish earnings\b",
    r"\bearnings miss(?:ed)?\b",
    r"\bbearish filing\b",
    r"\bguidance cut\b",
    r"\bregime (?:shift|flip|flipped)\b",
    r"\brisk[- ]off\b",
    r"\bhigh[- ]?conviction bearish\b",
    r"\bhigh bearish\b",
    # `correlation (cluster) breach` was REMOVED here 2026-09-13 (WORK.md
    # item 44) alongside its removal from `pipeline._HARD_TRIGGER_KEYWORDS`.
    # Two reasons, and the second is the interesting one: (1) nothing in the
    # desk computes a correlation-breach event, so the claim was never
    # checkable; (2) a correlation IS a function of the price series, so even
    # taken at face value it is price-derived, not external — it never
    # belonged on a list whose defining property is "comes from OUTSIDE the
    # price series". An earnings miss is true regardless of the tape; a
    # correlation number is the tape.
    # `circuit breaker` / `daily loss` / `daily-loss` were REMOVED here
    # 2026-09-20 (WORK.md item 32), alongside their removal from
    # `pipeline._HARD_TRIGGER_KEYWORDS` and `exit_trigger.ExitTrigger`, and
    # for the first of the two correlation-breach reasons above: the owner
    # deleted the whole account-level loss alarm, so nothing in the desk
    # computes a daily-loss or circuit-breaker event and the claim was no
    # longer checkable. This list is the more dangerous of the two to leave
    # stale, because membership here BYPASSES the noise-band and ratchet
    # clamps rather than merely admitting a phrase.
    r"\bstop hit\b",
    r"\bstopped out\b",
)

_EXTERNAL_RE = re.compile("|".join(EXTERNAL_INFORMATION_PATTERNS), re.IGNORECASE)


def cites_external_information(reason: str) -> bool:
    """True when the reason names a trigger originating outside the tape."""
    return bool(reason) and bool(_EXTERNAL_RE.search(reason))


def noise_band_atr(days_held: int | float | None, *, multiple: float = NOISE_BAND_ATR_MULTIPLE) -> float:
    """The noise-band width, in ATRs, for a position held `days_held` sessions.

    `ATR * sqrt(sessions)` — the exact scaling convention already used by
    `src/data/levels.py::derive_structural_target` for target projection
    (`travel = volatility * math.sqrt(horizon)`), on the same random-walk
    basis: expected price dispersion from a fixed starting point (here,
    entry) grows with the square root of elapsed time, not linearly.

    Despite the parameter name (kept for call-site compatibility), this
    MUST be a TRADING-SESSION count, not a calendar-day count — see
    `AlpacaBroker.trading_sessions_held` (item 165, holiday-aware) for the
    counter `pipeline.py` feeds in. A 2026-09-04 audit follow-up caught this
    function being fed raw calendar days, which silently over-widened the
    band by sqrt(3) instead of sqrt(1) across a Friday-to-Monday hold (3
    calendar days, 1 real trading session) — the opposite of this fix's own
    intent.

    `days_held` is floored at 1 session — None, non-finite, zero, or negative
    all collapse to 1 — so a brand-new position gets exactly the old flat
    `multiple` behaviour (sqrt(1) == 1) and only positions held longer than
    one session see a wider band.
    """
    try:
        days = float(days_held) if days_held is not None else 1.0
    except (TypeError, ValueError):
        days = 1.0
    if not math.isfinite(days) or days < 1.0:
        days = 1.0
    return multiple * math.sqrt(days)


def adverse_move_is_noise(
    entry: float,
    current_price: float,
    atr: float | None,
    *,
    multiple: float = NOISE_BAND_ATR_MULTIPLE,
    side: str = "sell",
    days_held: int | float | None = None,
) -> bool:
    """True when the position has moved ADVERSELY from entry by less than
    the noise band for how long it has been held.

    The band is `multiple * ATR * sqrt(days_held)` (floor 1 session) — see
    `noise_band_atr`. Passing no `days_held` (the old call shape) reproduces
    the original flat `multiple * ATR` band exactly, since sqrt(1) == 1.

    `side` is the CLOSING side, same convention as
    `TradingPipeline._submit_protected_sell` / `_forced_close_side_and_qty`:
    "sell" (default, unchanged for every pre-shorts caller) means a long,
    where adverse is price falling (`entry - current`); "buy" means a
    short's cover, where adverse is the mirror — price rising
    (`current - entry`), since a short is hurt by the tape going up.

    Returns False — i.e. "not noise, let the caller proceed" — whenever the
    question cannot be answered: no ATR, non-finite inputs, or a position that
    is flat or in profit. This guard exists to stop premature exits on
    positions that have barely moved; it must never manufacture a block out of
    missing data, because that would strand a position the reviewer has real
    reason to leave.
    """
    ent = _finite(entry)
    cur = _finite(current_price)
    atr_f = _finite(atr) if atr is not None else None
    if ent is None or cur is None or atr_f is None or atr_f <= 0 or ent <= 0:
        return False
    adverse = (cur - ent) if str(side).lower() == "buy" else (ent - cur)
    if adverse <= 0:
        return False   # flat or winning — not this guard's business
    return adverse < noise_band_atr(days_held, multiple=multiple) * atr_f


# ---------------------------------------------------------------------------
# thesis_invalid_if checker — measured, not assumed
# ---------------------------------------------------------------------------
#
# `thesis_invalid_if` is free text an analyst writes at entry (e.g. "closes
# below the 50-day average", "loses the $142 level", "RSI drops below 30").
# Until now NOTHING checked whether it had actually become true when a SELL
# was later proposed citing it — pure honour system.
#
# General-purpose parsing of arbitrary English into an executable check was
# floated as too large/unreliable to attempt. Rather than accept that as a
# guess, it was measured against real recorded text: 20 real executed BUYs
# carrying an `(invalid if: ...)` / `(thesis_invalid_if: ...)` marker in
# `trades.reasoning`, plus 1,028 unique real, un-truncated
# `thesis_invalid_if` values recorded in `specialist_evidence` (tech-analyst
# candidates, 1,203 non-null occurrences before de-duplication) — see
# `docs/WORK.md` for the query and full bucket counts. Every one of those
# real values fell into exactly two checkable shapes:
#
#   - a moving-average reference ("closes below MA20", "the 50-day average")
#     — 984 of 1,028 unique specialist_evidence values (95.7%); every one of
#     the 20 real executed-trade examples that referenced an MA used MA20 or
#     MA50, never MA200 or any other period.
#   - a bare price level ("closes below the $218.51 support level", "loses
#     the $142 level") — 38 of 1,028 (3.7%).
#
# Everything else — a single Bollinger Band mention and a handful of
# qualitative conditions ("guidance pulled", "AI accelerator rollout faces
# delays") — was too rare (6 of 1,028, 0.6%) and, for the qualitative cases,
# inherently unparseable, to justify building or trusting a checker for. No
# RSI-threshold example was found in either real source at all, despite RSI
# thresholds being a plausible category in principle.
#
# So this checker covers ONLY the two shapes that are both common and safe:
# a bare numeric price level, and a moving-average reference where the
# caller already has the corresponding computed MA (20/50/200, matching
# `src/data/technical.py::compute_indicators` — no new indicator is computed
# here). It NEVER computes or fetches its own market data; the caller passes
# in whatever is already on hand from the same pipeline that would otherwise
# just trust the honour system.
#
# A compound condition ("closes below MA50 OR breaks $180 support") is
# deliberately refused (UNPARSEABLE) rather than partially evaluated: only
# unambiguous single conditions are checked. Same posture as the rest of
# this module — fail closed, only ever assert a fact that is provably true
# or provably false, never guess.
#
# NOT WIRED IN. This is a standalone, tested function. Wiring it into
# `RiskStage` or anywhere in the live pipeline is the next step, and is
# blocked on a parallel piece of work giving `thesis_invalid_if` its own
# dedicated, un-truncated field on `TradeDecision` — reading today's
# `reasoning`-embedded text here would mean trusting the same lossy,
# truncated string this whole effort exists to stop trusting.

#: Words that mean the condition fires when price goes DOWN through a level.
_DOWN_WORDS_RE = re.compile(r"\b(?:below|under|loses)\b", re.IGNORECASE)

#: Words that mean the condition fires when price goes UP through a level.
_UP_WORDS_RE = re.compile(r"\b(?:above|over|reclaims|clears)\b", re.IGNORECASE)

#: MA reference: "MA20", "MA 50", "SMA200", "50-day moving average",
#: "50-day MA". Captures the period so it can be matched to the caller's
#: ma_20 / ma_50 / ma_200 — only those three periods are ever computed
#: elsewhere in this pipeline, so anything else is UNPARSEABLE rather than
#: guessed at.
_MA_REF_RE = re.compile(
    r"\b(?:MA|SMA|EMA)[\s-]?(\d{2,3})\b|(\d{2,3})-day\s+(?:moving\s+)?(?:average|MA|SMA|EMA)\b",
    re.IGNORECASE,
)

#: A dollar-prefixed number anywhere in the text — the clearest possible
#: signal of a fixed price level, integer prices included ("$1000").
_DOLLAR_PRICE_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)")

#: A decimal number immediately next to a level/support/resistance word.
#: Decimal is required (no dollar sign) so this cannot mis-fire on an
#: unrelated integer such as an RSI threshold or a day count.
_LEVEL_WORD_PRICE_RE = re.compile(
    r"([\d,]+\.\d+)\s*(?:level|support|resistance)\b"
    r"|(?:support|resistance|level)\D{0,15}?([\d,]+\.\d+)",
    re.IGNORECASE,
)

#: A decimal number close after a direction word ("below 85.50", "above the
#: 108.69"). Decimal required for the same false-positive reason as above.
_DIRECTION_PRICE_RE = re.compile(
    r"\b(?:above|below|over|under)\b\D{0,12}?([\d,]+\.\d+)", re.IGNORECASE,
)

#: MA periods this checker will compare against — must match what
#: `src/data/technical.py::compute_indicators` actually computes.
_SUPPORTED_MA_PERIODS = (20, 50, 200)


@dataclass(frozen=True)
class ThesisInvalidationCheck:
    """Result of checking one `thesis_invalid_if` string against real data.

    `status` is one of:
      - "TRIGGERED"     — the condition is provably true right now.
      - "NOT_TRIGGERED" — the condition is provably false right now.
      - "UNPARSEABLE"   — cannot say, for any reason (compound condition,
        unsupported reference, qualitative/news condition, or a required
        market data point the caller didn't supply). Never a guess.
    """

    status: Literal["TRIGGERED", "NOT_TRIGGERED", "UNPARSEABLE"]
    detail: str


def _clean_number(raw: str) -> float:
    return float(raw.replace(",", ""))


def _extract_price_threshold(text: str) -> float | None:
    """The single fixed price named in `text`, or None if none is found.

    Tries, in order of confidence: a dollar-prefixed amount, a decimal
    number next to "level"/"support"/"resistance", a decimal number right
    after a direction word. Stops at the first match — this function is
    only reached once the caller has already established the condition is
    a plain price level (not an MA reference), so the first plausible
    number is the level.
    """
    for pattern in (_DOLLAR_PRICE_RE, _LEVEL_WORD_PRICE_RE, _DIRECTION_PRICE_RE):
        m = pattern.search(text)
        if m:
            group = next(g for g in m.groups() if g is not None)
            return _clean_number(group)
    return None


def check_thesis_invalid_if(
    thesis_invalid_if: str,
    current_price: float | None,
    *,
    ma_20: float | None = None,
    ma_50: float | None = None,
    ma_200: float | None = None,
) -> ThesisInvalidationCheck:
    """Check whether a `thesis_invalid_if` condition has become true.

    Covers exactly two shapes, chosen because they are both the common real
    cases (see the module-level note above for the measured counts) and
    checkable without guessing:

      - a bare numeric price level ("closes below the $218.51 support
        level", "loses the $142 level") — checked against `current_price`.
      - a moving-average reference ("closes below MA20", "the 50-day
        average") for period 20, 50 or 200 — checked against the matching
        `ma_20` / `ma_50` / `ma_200` the caller supplies.

    Everything else — a compound "A or B" condition, an unsupported MA
    period, an indicator threshold (RSI/MACD/Bollinger), a qualitative or
    news-based condition, or a required price/MA value the caller left as
    None — returns UNPARSEABLE. This function never fetches or computes
    market data itself; it only compares numbers the caller already has.
    """
    text = (thesis_invalid_if or "").strip()
    if not text:
        return ThesisInvalidationCheck("UNPARSEABLE", "empty thesis_invalid_if")

    if re.search(r"\bor\b", text, re.IGNORECASE):
        return ThesisInvalidationCheck(
            "UNPARSEABLE",
            "compound condition ('...or...') — refusing to partially evaluate",
        )

    down = bool(_DOWN_WORDS_RE.search(text))
    up = bool(_UP_WORDS_RE.search(text))
    if down == up:  # neither found, or both found (ambiguous/contradictory)
        return ThesisInvalidationCheck(
            "UNPARSEABLE", "no unambiguous direction (above/below) found",
        )
    direction: Literal["down", "up"] = "down" if down else "up"

    cur = _finite(current_price)

    ma_match = _MA_REF_RE.search(text)
    if ma_match:
        period = int(ma_match.group(1) or ma_match.group(2))
        if period not in _SUPPORTED_MA_PERIODS:
            return ThesisInvalidationCheck(
                "UNPARSEABLE",
                f"MA{period} referenced but only MA20/MA50/MA200 are computed "
                "upstream — not guessing at an uncomputed indicator",
            )
        ma_value = _finite({20: ma_20, 50: ma_50, 200: ma_200}[period])
        if cur is None or ma_value is None:
            return ThesisInvalidationCheck(
                "UNPARSEABLE",
                f"MA{period} condition recognised but current_price or "
                f"ma_{period} was not supplied",
            )
        fired = cur < ma_value if direction == "down" else cur > ma_value
        status = "TRIGGERED" if fired else "NOT_TRIGGERED"
        return ThesisInvalidationCheck(
            status,
            f"price {cur} vs MA{period} {ma_value}, condition was "
            f"'{direction}'",
        )

    threshold = _extract_price_threshold(text)
    if threshold is None:
        return ThesisInvalidationCheck(
            "UNPARSEABLE", "no MA reference or numeric price level found in text",
        )
    if cur is None:
        return ThesisInvalidationCheck(
            "UNPARSEABLE", "price level condition recognised but current_price "
            "was not supplied",
        )
    fired = cur < threshold if direction == "down" else cur > threshold
    status = "TRIGGERED" if fired else "NOT_TRIGGERED"
    return ThesisInvalidationCheck(
        status, f"price {cur} vs level {threshold}, condition was '{direction}'",
    )


# ---------------------------------------------------------------------------
# Holding-discipline compliance — spec item 25 (2026-09-03)
# ---------------------------------------------------------------------------
#
# WHAT WENT WRONG. `config/prompts/risk_manager.md` ("Holding-discipline
# compliance") asks the AI Risk Manager to itself verify, for every position
# held under 5 days, that a proposed SELL/REDUCE/COVER names one of exactly
# three allowed triggers: (a) a triggered `thesis_invalid_if`, (b) a regime
# flip to risk-off TODAY, or (c) a HIGH-conviction bearish state_change dated
# today that names the symbol. Nothing in Python checked any of this — the
# prompt told the model to grade its own homework against real data it was
# handed in the same message, with no deterministic pass afterward. This is
# the same "citation exists vs. citation is real" shape as the sub-floor
# catalyst gate (`PortfolioManagerAgent._catalyst_cites_state_change`) fixed
# the same day.
#
# UPDATE, 2026-09-03/04 — the flat "held under 5 days" window described
# above is GONE from the code (it never had a backtest behind it, and the
# owner rejected it as arbitrary). What counts as "protected" is now
# `check_structural_protection`'s data-driven answer, defined further down
# this file: intact unless the trade's own `thesis_invalid_if` or the
# structural level backing its stop has broken on the CLOSE of a second
# consecutive trading day (a same-day close is not enough — see
# `check_structural_protection`'s docstring on why: a "spring" false
# breakdown is a real, well-documented pattern), with a noise-band fallback
# (not an automatic unprotect) when neither exists. `holding_discipline_false_claim`
# below takes that answer as a plain `protected: bool` — everything in this
# comment block about (a)/(b)/(c) and what gets checked is otherwise
# unchanged.
#
# SCOPE — (b) and (c) ONLY. (a) is explicitly NOT verified here: evaluating
# an arbitrary free-text `thesis_invalid_if` condition against live price
# data is a real, separate feature (parsing "closes below the 50-day" or
# "loses the $142 level" into an executable check), and guessing at it would
# be worse than not attempting it. The only (a)-adjacent question this module
# *could* safely answer — "was a non-empty thesis_invalid_if actually
# recorded at entry, as opposed to fabricated after the fact" — turned out to
# have no reliable answer either: the only place an entry's
# `thesis_invalid_if` survives is embedded as free text inside the BUY's
# stored `TradeDecision.reasoning` (`PortfolioConstructor._build_buy` writes
# "... (invalid if: <text>)"; `_build_short`/`_build_sell` use a *different*
# "(thesis_invalid_if: <text>)" phrasing — the two builders do not even agree
# on the marker string), and that whole field is truncated to 500 characters
# at write time and again to 280 by `TradingPipeline._build_position_history`
# before anything downstream ever sees it. A long thesis can push the
# marker past either truncation point, which would make "no invalid_if
# found" indistinguishable from "one was recorded but cut off" — exactly the
# false-negative a discipline check must not manufacture. So (a) stays
# entirely unaddressed here, deliberately, and a SELL relying on it is never
# penalized by anything below for that reason alone.
#
# DESIGN — because (a) cannot be checked, a SELL that fails to prove (b) or
# (c) is NOT thereby suspect: it may be a perfectly legitimate (a)-based
# exit this module simply has no visibility into. Blocking on "(b) and (c)
# both come up empty" would veto every honest invalidation-based exit, which
# is worse than the discipline gap this fixes. The only thing this module
# ever acts on is a POSITIVELY CONTRADICTED claim: the decision's own
# reasoning text asserts a specific, checkable fact ("regime flipped to
# risk-off", "high-conviction bearish state change") and the verifiable data
# for TODAY says otherwise. That is provable dishonesty, not an unprovable
# gap, and is the only thing `holding_discipline_false_claim` flags.
#
# UPDATE, 2026-09-04 — a PROVEN-FALSE claim now BLOCKS the exit (owner
# approved). This module previously only ever LOGGED, and the "FLAGGED FOR
# REVIEW: should this ever escalate to a veto" question recorded here has
# been answered: yes, and only for `verdict == "false"`. What did NOT change
# is the bar for reaching that verdict — every reason the old note gave for
# caution is a reason the FALSE bar stays exactly where it was, not a reason
# to keep the finding toothless:
#
#   - claim-detection is phrase matching over free text (the two regexes
#     below plus `_is_negated`'s short negation-cue guard). Adversarial
#     review (2026-09-03) produced a concrete reproducing sentence — "No
#     regime shift to risk-off has occurred; exiting purely on
#     thesis_invalid_if" was matched as CLAIMING a flip before the negation
#     guard existed. `_is_negated` closes that specific, demonstrated case;
#     it is a short common-word list, not a general negation parser. So the
#     veto only ever fires when the text asserts a claim AND real recorded
#     data for TODAY affirmatively says the opposite.
#   - anything that merely CANNOT be checked (macro untrusted this run, or
#     no same-day state-change row names the symbol at all) is verdict
#     "unverifiable": still logged and recorded as a pipeline event, exactly
#     as before, and NEVER blocked and never alerted on. Absence of proof
#     stays absence of proof. This is the owner's explicit instruction and
#     it is the whole reason the verdict is three-valued rather than a bool.
#   - (a) `thesis_invalid_if` is still never evaluated here, so a SELL
#     resting on it is never touched by any of this.
#
# The residual risk the old note named is real and accepted with eyes open:
# a correctly-read false (b)/(c) claim does not by itself prove the SELL is
# wrong, because an unverifiable (a) might independently justify it. The
# owner's call is that an exit whose *stated* justification is provably
# contradicted by the desk's own recorded data should not execute on that
# justification — the RM can re-propose it next cycle citing something true.
# Every block fires a standalone owner alert (see `RiskStage`) precisely so
# the frequency of that trade-off is measured, not assumed.

#: Phrases that assert a regime flip to risk-off. Deliberately the same two
#: patterns `EXTERNAL_INFORMATION_PATTERNS` already uses for the identical
#: concept (regime shift / risk-off), so "does this reason claim a regime
#: flip" is answered identically everywhere in this module rather than by a
#: second, silently-diverging definition.
_REGIME_FLIP_CLAIM_RE = re.compile(
    r"\bregime (?:shift|flip|flipped)\b|\brisk[- ]off\b", re.IGNORECASE,
)

#: Phrases that assert a HIGH-conviction bearish state_change. Same two
#: "high[-]conviction bearish" / "high bearish" patterns as
#: `EXTERNAL_INFORMATION_PATTERNS`, plus the literal "bearish state change"
#: phrasing the risk_manager.md checklist item itself uses.
_BEARISH_STATE_CHANGE_CLAIM_RE = re.compile(
    r"\bhigh[- ]?conviction bearish\b|\bhigh bearish\b|\bbearish state change\b",
    re.IGNORECASE,
)

#: `data_status["macro"]` values that mean "this run's macro_analysis is a
#: real reading dated TODAY" — as opposed to absent, failed, or parse-error.
#: Reuses `TradingPipeline._carry_forward_macro` / `build_evidence_registry`'s
#: own distinction (see their docstrings) rather than inventing a second one:
#: "carried_from_morning" is explicitly this morning's read of TODAY, refused
#: by the producer itself whenever the stored state is not dated today, so it
#: is exactly as trustworthy as "ok" for this purpose.
TRUSTED_MACRO_STATUSES = frozenset({"ok", "carried_from_morning"})

#: Phrases that assert the trade's thesis has been INVALIDATED. Deliberately
#: the same five phrases `pipeline._HARD_TRIGGER_KEYWORDS` accepts under its
#: "Thesis invalidation" heading and no others, so "does this reason claim a
#: thesis invalidation" is answered by one definition rather than two that
#: can silently diverge.
#:
#: Why this predicate exists at all (2026-09-14, docs/WORK.md item 60). Of
#: the 26 hard-trigger keywords, 21 also match
#: `EXTERNAL_INFORMATION_PATTERNS` and therefore skip the ATR noise band
#: outright; these five are the only ones that do not. Thesis invalidation
#: is thus the entire non-redundant domain of that band — and it was also
#: the one exit class on which `check_structural_protection` was never
#: consulted, because the intraday assembler short-circuited on the absence
#: of a (b)/(c) claim. "Has the level backing this stop closed beyond it on
#: two consecutive sessions" is a checkable fact and is the actual question
#: a thesis-invalidation exit is asserting an answer to; "this move is
#: bigger than one ATR" is not an answer to the same question.
#:
#: Over-matching here is safe BY CONSTRUCTION and under-matching is not: a
#: match only buys a read-only structural read plus an audit row, and can
#: neither block an exit nor release one.
_THESIS_INVALIDATION_CLAIM_RE = re.compile(
    # No trailing \b after "invalid": the keyword list matches by plain
    # substring, so "thesis_invalid_if triggered" and "thesis invalidated"
    # are both hard triggers today and must both be recognised here.
    r"\bthesis[_ ]invalid|\binvalidation triggered\b|"
    r"\bbroken thesis\b|\bthesis broken\b",
    re.IGNORECASE,
)

#: A negation cue in the ~6 words immediately before a matched phrase flips
#: what the phrase means — "regime shift to risk-off" asserts one, "NO
#: regime shift to risk-off has occurred" denies it, and the bare pattern
#: cannot tell them apart. Found by adversarial review (2026-09-03) with a
#: concrete reproducing sentence, not a theoretical gap: without this guard,
#: a SELL reasoning that explicitly DENIES a regime flip or a bearish state
#: change gets misread as CLAIMING one, and — if today's real data happens
#: to disagree with the denied claim — produces a "contradiction" finding
#: for a decision whose reasoning never actually contradicted anything.
#: Deliberately a short, common word list, not a general negation parser:
#: this module already stops short of veto power precisely because
#: phrase-matching cannot fully understand text, and a fancier negation
#: detector would just move the same risk to different sentences rather
#: than remove it. This closes the demonstrated case; it does not claim to
#: close every case.
_NEGATION_CUE_RE = re.compile(
    r"\b(?:no|not|never|isn'?t|wasn'?t|hasn'?t|didn'?t|doesn'?t|without|"
    r"lack(?:ing|s)? of|absent(?:\s+any)?|no\s+evidence\s+of)\b",
    re.IGNORECASE,
)

#: How many characters before a matched claim to scan for a negation cue.
#: ~6 words at typical reasoning-sentence length; wide enough to catch "no
#: regime shift to risk-off has occurred" (cue precedes the match by ~28
#: chars) without reaching back into an unrelated prior clause.
_NEGATION_LOOKBACK_CHARS = 40


def _is_negated(text: str, match: re.Match) -> bool:
    window = text[max(0, match.start() - _NEGATION_LOOKBACK_CHARS):match.start()]
    return bool(_NEGATION_CUE_RE.search(window))


def claims_regime_flip(reason: str) -> bool:
    """True when `reason` asserts (not denies) a regime flip to risk-off."""
    if not reason:
        return False
    match = _REGIME_FLIP_CLAIM_RE.search(reason)
    return bool(match) and not _is_negated(reason, match)


def claims_bearish_state_change(reason: str) -> bool:
    """True when `reason` asserts (not denies) a HIGH-conviction bearish
    state_change."""
    if not reason:
        return False
    match = _BEARISH_STATE_CHANGE_CLAIM_RE.search(reason)
    return bool(match) and not _is_negated(reason, match)


def claims_thesis_invalidation(reason: str) -> bool:
    """True when `reason` asserts (not denies) that the thesis is invalid.

    Purely a ROUTING predicate — see `_THESIS_INVALIDATION_CLAIM_RE`. It
    decides whether the structural check is worth consulting and recording
    for this exit; it is deliberately NOT an input to
    `holding_discipline_claim_check`, which continues to leave (a)
    `thesis_invalid_if` unjudged. Nothing gates a block or a release on
    this function.
    """
    if not reason:
        return False
    match = _THESIS_INVALIDATION_CLAIM_RE.search(reason)
    return bool(match) and not _is_negated(reason, match)


@dataclass(frozen=True)
class HoldingDisciplineClaimCheck:
    """Three-valued verdict on a SELL/REDUCE/COVER's stated (b)/(c) trigger.

    The three-valued shape is the whole point, and is the owner's explicit
    2026-09-04 instruction: a claim the desk's own recorded data
    AFFIRMATIVELY CONTRADICTS is a different thing from a claim the desk
    simply could not check this run, and only the first may block a trade.
    Collapsing the two into one bool is exactly how "we could not verify it"
    turns into "we proved it false", which would veto honest exits.

    `verdict`:
      "ok"           - no checkable (b)/(c) claim was made, or every claim
                       made was CONFIRMED by real data, or the decision is
                       out of scope (not an exit / not a protected position).
                       Nothing is logged, nothing blocks.
      "unverifiable" - a (b)/(c) claim WAS made but the data needed to judge
                       it is not available this run (macro status outside
                       `TRUSTED_MACRO_STATUSES`, or no same-day state-change
                       row names the symbol at all). LOGGED ONLY: never
                       blocks, never alerts. Absence of proof is not proof.
      "false"        - a (b)/(c) claim was made and real recorded data for
                       TODAY says the opposite. BLOCKS the decision and
                       fires a standalone owner alert.

    `finding` is the human-readable audit-trail sentence (None when
    `verdict` is "ok"). `reasons` holds the individual contradiction or
    unverifiability clauses, so an alert can name them without re-parsing
    the rendered sentence.
    """

    verdict: Literal["ok", "unverifiable", "false"]
    finding: str | None = None
    reasons: tuple[str, ...] = ()

    @property
    def blocks(self) -> bool:
        """True only for a PROVEN-FALSE claim. The one thing callers gate a
        veto on — deliberately not `finding is not None`, which would also
        be true for the log-only unverifiable case."""
        return self.verdict == "false"


def holding_discipline_claim_check(
    *,
    action: str,
    reason: str,
    symbol: str,
    protected: bool,
    macro_regime_today: str | None,
    macro_status: str | None,
    active_state_changes: str = "",
    asof: date | None = None,
    exit_trigger: object = None,
) -> HoldingDisciplineClaimCheck:
    """Judge whether a PROTECTED position's exit states a (b)/(c) trigger
    that real recorded data CONTRADICTS, merely cannot CHECK, or CONFIRMS.

    `protected` replaces the old flat `days_held < 5` gate (owner decision,
    2026-09-03/04 — see `check_structural_protection`'s module note for the
    full replacement rationale). The caller computes it once via
    `check_structural_protection(...).protected` — data-driven and no
    longer time-bound at all — and passes the single bool in here; this
    function itself only decides whether the STATED (b)/(c) trigger is
    provably real.

    Checks ONLY:
      (b) a claimed regime flip to risk-off. CONTRADICTED when today's macro
          read (`macro_status` in `TRUSTED_MACRO_STATUSES`) shows a
          DIFFERENT, non-risk-off regime; UNVERIFIABLE when the macro status
          is not trusted this run or no regime was read at all.
      (c) a claimed HIGH-conviction bearish state_change. CONTRADICTED when a
          same-day `active_state_changes` row DOES name the symbol but with a
          recorded direction that is NOT bearish (parsed via
          `PortfolioManagerAgent._state_change_symbols_by_date`, the exact
          function that already owns this parsing for the sub-floor catalyst
          gate — not reimplemented here); UNVERIFIABLE when no same-day row
          names the symbol at all, because the news pipeline can simply not
          have logged a real catalyst as a formal `state_change` row yet.

    Returns verdict "ok" (nothing to say) for:
      - an action other than SELL/REDUCE/COVER;
      - a position that is not currently `protected` (its thesis-backing
        level has broken and been confirmed, or it has no basis and is
        outside the noise band) — a plain SELL there needs no special
        justification, so nothing here is worth checking;
      - a reason that makes neither claim;
      - a claim real data CONFIRMS;
      - (a) `thesis_invalid_if` — not itself re-evaluated here (it already
        fed into `protected` upstream), so a SELL resting entirely on it is
        never flagged just because (b) and (c) are absent or unverifiable.

    A "false" verdict is a veto (see the module note above for the owner
    decision and the accepted trade-off). An "unverifiable" verdict is an
    audit-trail record and nothing more.
    """
    if str(action).upper() not in ("SELL", "REDUCE", "COVER"):
        return HoldingDisciplineClaimCheck("ok")
    if not protected:
        return HoldingDisciplineClaimCheck("ok")
    reason = reason or ""
    symbol_u = symbol.strip().upper()
    contradictions: list[str] = []
    unverifiable: list[str] = []

    # 2026-09-18: which claim is being made is read from the STRUCTURED
    # trigger first and from the prose only as a fallback. That is the
    # whole point of `PositionAction.exit_trigger`. Before it existed this
    # function asked two regexes what the sentence claimed, so the two
    # real 2026-09-16 exits — whose entire reason was the words "adverse
    # news" — made no claim either regex recognised, returned "ok", and
    # were never checked against anything.
    #
    # `adverse_news` is routed to the same-day state-change check because
    # that is the record the claim is ABOUT: an adverse news event naming
    # this symbol today. `sector_shock` is deliberately NOT routed here —
    # it is a sector-level assertion and the desk records no sector-shock
    # row, so pointing a symbol-level check at it would manufacture an
    # "unverifiable" on every such exit and tell the reviewer nothing.
    from src.risk.exit_trigger import ExitTrigger, normalize_trigger
    _trigger = normalize_trigger(exit_trigger)
    _claims_regime = (
        claims_regime_flip(reason) or _trigger is ExitTrigger.REGIME_SHIFT
    )
    _claims_bearish = claims_bearish_state_change(reason) or _trigger in (
        ExitTrigger.BEARISH_STATE_CHANGE, ExitTrigger.ADVERSE_NEWS,
    )

    if _claims_regime:
        if macro_status in TRUSTED_MACRO_STATUSES and macro_regime_today:
            if macro_regime_today != "risk-off":
                contradictions.append(
                    f"claims a regime flip to risk-off today, but today's "
                    f"macro read ({macro_status}) shows regime="
                    f"{macro_regime_today!r}, not risk-off"
                )
            # else: the claim is CONFIRMED — say nothing.
        else:
            # Macro unavailable/untrusted this run. Recorded so the gap is
            # visible in the audit trail, but never blocked and never
            # alerted on: this is the exact case the owner separated out.
            unverifiable.append(
                f"claims a regime flip to risk-off today, but this run's "
                f"macro read (status={macro_status!r}) cannot confirm or "
                f"deny it"
            )

    if _claims_bearish:
        # Named after the claim actually made, so an alert reads truthfully
        # whether the trigger arrived as prose or as a structured field.
        claim_label = (
            "an adverse news event naming it"
            if _trigger is ExitTrigger.ADVERSE_NEWS
            and not claims_bearish_state_change(reason)
            else "a HIGH-conviction bearish state change"
        )
        from src.agents.portfolio_manager import PortfolioManagerAgent

        by_date = PortfolioManagerAgent._state_change_symbols_by_date(
            active_state_changes, asof,
        )
        try:
            from src.trading_calendar import et_today
            today_iso = str(asof) if asof is not None else str(et_today())
        except Exception:  # pragma: no cover - clock/tz failure
            today_iso = None
        directions = by_date.get(today_iso) if today_iso else None
        symbol_directions = directions.get(symbol_u) if directions else None
        if symbol_directions is None:
            # No same-day row names the symbol at all -> unverifiable, not
            # false. A missing state-change row is much weaker evidence than
            # a definite non-matching macro regime: the news pipeline can
            # simply not have logged a real catalyst as a formal row yet, so
            # treating "not found" as "false" would manufacture false
            # positives on legitimate exits.
            unverifiable.append(
                f"claims {claim_label} today, but "
                f"no same-day Active News State Change row names {symbol_u} "
                f"either way"
            )
        elif "bearish" not in symbol_directions:
            rendered = ", ".join(sorted(symbol_directions)) or "no recorded direction"
            contradictions.append(
                f"claims {claim_label} today, but "
                f"today's Active News State Change block names {symbol_u} "
                f"with direction(s) {rendered} instead of bearish"
            )
        # else: the claim is CONFIRMED — say nothing.

    if contradictions:
        # A contradiction outranks any co-occurring unverifiable clause: one
        # provably false claim is enough, and mixing "we could not check the
        # other one" into the same verdict would only muddy it.
        return HoldingDisciplineClaimCheck(
            "false",
            f"{symbol_u}: {action} on a structurally-protected position — "
            f"reasoning " + "; and ".join(contradictions) +
            f". This is a provable contradiction of a checkable claim: the "
            f"exit is BLOCKED on the justification given. thesis_invalid_if "
            f"(which this module cannot verify either way) may independently "
            f"justify this exit — if so it can be re-proposed citing that.",
            tuple(contradictions),
        )
    if unverifiable:
        return HoldingDisciplineClaimCheck(
            "unverifiable",
            f"{symbol_u}: {action} on a structurally-protected position — "
            f"reasoning " + "; and ".join(unverifiable) +
            f". NOT treated as false and NOT blocked — absence of proof is "
            f"not proof of a false claim. Recorded for review only.",
            tuple(unverifiable),
        )
    return HoldingDisciplineClaimCheck("ok")


def holding_discipline_false_claim(
    *,
    action: str,
    reason: str,
    symbol: str,
    protected: bool,
    macro_regime_today: str | None,
    macro_status: str | None,
    active_state_changes: str = "",
    asof: date | None = None,
) -> str | None:
    """Return the finding string for a PROVABLY FALSE holding-discipline
    claim, else None.

    Thin wrapper over `holding_discipline_claim_check` kept because
    "provably false, or nothing" is genuinely the question most callers
    want, and because collapsing it here — in one place, explicitly on
    `verdict == "false"` — is safer than letting each caller decide what
    counts as false. An UNVERIFIABLE claim returns None here by design: use
    `holding_discipline_claim_check` directly to see (and log) that case.
    """
    result = holding_discipline_claim_check(
        action=action,
        reason=reason,
        symbol=symbol,
        protected=protected,
        macro_regime_today=macro_regime_today,
        macro_status=macro_status,
        active_state_changes=active_state_changes,
        asof=asof,
    )
    return result.finding if result.blocks else None


# ---------------------------------------------------------------------------
# Structural (data-driven) holding protection — spec item 25, 2026-09-03
# ---------------------------------------------------------------------------
#
# WHAT THIS REPLACES. `holding_discipline_false_claim` used to treat every
# position with `days_held < 5` as "protected" from a plain SELL/REDUCE/COVER,
# full stop — a flat day-count with no backtest behind it, traced to an April
# 2026 commit that stated a philosophy ("give a thesis room to work") and
# never measured one. The owner rejected the day count as arbitrary and
# approved this replacement: a position is protected from a plain
# no-real-trigger exit UNLESS the structural level actually backing its
# thesis has been broken by price. Nothing here is time-bound; a position
# held 30 days with an intact level is exactly as protected as one held
# zero days with the same level intact, and a position whose level breaks
# on day zero has no protection at all.
#
# WHAT "backing its thesis" MEANS, in priority order:
#   1. The trade's own `thesis_invalid_if` (the analyst's stated falsifier,
#      `TradeDecision.thesis_invalid_if` — PR #250), checked for real against
#      today's price/MA data via `check_thesis_invalid_if` above. TRIGGERED
#      means broken; NOT_TRIGGERED means intact; UNPARSEABLE falls through
#      to (2) rather than being treated as either — an unparseable condition
#      is not evidence either way, and a fallback the desk already trusts
#      elsewhere is a better answer than a coin flip.
#   2. Absent a stated condition (or given one `check_thesis_invalid_if`
#      cannot read), the nearest VERIFIED structural level backing the
#      position's actual stop — the exact machinery
#      `PortfolioConstructor._level_backing_stop` already uses to decide
#      whether a stop earns an exemption from the ATR noise floor:
#      `computed_levels` / `computed_level_touches` (real levels, attached
#      to the analysis in Python by `TechAnalystAgent`, never asserted by
#      the model — see that method's docstring), gated on
#      `min_level_touches` prior touches (the already-ratified
#      `min_level_touches_for_stop_honor` bar, docs/RESEARCH_FINDINGS.md
#      §7), matched to the stop by whether the stop falls inside that
#      level's own zone (`level_cluster_tolerance_pct` of the level price
#      — `src.data.levels.CLUSTER_TOLERANCE_PCT`, the constant that built
#      the zone). No new constant is introduced here — both bars are the
#      ones `_level_backing_stop` already uses, reused rather than
#      duplicated. A long's level is "broken" when price is at or through
#      it by more than one ATR noise band (a different question in a
#      different unit — see `check_structural_protection`'s docstring);
#      the mirror for a short is price at or through a resistance level
#      from above.
#   3. Neither (1) nor (2) resolves — no stated condition (or an
#      unparseable one) AND no qualifying structural level under the stop.
#      This is an INTENTIONAL, owner-flagged behaviour change: a thesis
#      with nothing concrete backing it is not entitled to an automatic
#      pass just because it is young. The caller must log this case
#      visibly (see `holding_discipline_false_claim` below) rather than
#      silently letting the position fall through as either protected or
#      not.
#
# This module never fetches data itself — every value is a plain number,
# string or mapping the caller already has lying around from the same
# machinery `PortfolioConstructor` uses (bars → `compute_indicators` →
# atr/MAs, `find_structural_levels` → computed_levels/touches). No LLM call
# anywhere in this path.


@dataclass(frozen=True)
class StructuralProtectionCheck:
    """Whether a position's thesis-backing level is still intact.

    `protected` is the one field callers gate on. `basis` names which of
    the three cases above decided it, and `detail` is a human-readable
    reason for the audit trail / log line — see the module note above for
    why the no-basis case in particular must never be silent.
    """

    protected: bool
    basis: Literal[
        "thesis_invalid_if_triggered",
        "thesis_invalid_if_pending_confirmation",
        "thesis_invalid_if_intact",
        "structural_level_broken",
        "structural_level_pending_confirmation",
        "structural_level_intact",
        "noise_band_intact",
        "noise_band_broken",
    ]
    detail: str
    #: True when TODAY's close (independent of the confirmation gate below)
    #: found the thesis/level basis broken. Callers must persist this value
    #: keyed by symbol AND today's close date, so it can be fed back in as
    #: `break_seen_prior_close` on the NEXT TRADING DAY's read — that is the
    #: only state this module needs to implement confirmation, and it holds
    #: none of it itself (pure function in, pure value out).
    raw_broken: bool = False
    #: The confirmation REGIME this read selected — one of
    #: `classify_trend_context`'s labels (against_or_weak / with_trend_moderate
    #: / with_trend_strong / insufficient_context), or "" on a basis where it
    #: does not apply (a non-broken level, the noise-band fallback). Recorded so
    #: the audit trail and the owner message can say WHICH regime the desk read.
    trend_context: str = ""
    #: How many consecutive confirming daily closes this regime needs before it
    #: lifts protection (1 for against/weak, 2 for a with-trend break), and how
    #: many have confirmed so far (today's close plus the prior consecutive
    #: streak, capped at needed). 0 on a basis where confirmation does not apply.
    confirming_closes_needed: int = 0
    confirming_closes_seen: int = 0
    #: PLAIN-LANGUAGE, owner-facing reason for a DECISIVE break outcome — a
    #: confirmed break that lifts protection ("real breakdown"), or a break
    #: held pending confirmation ("possible shakeout, waiting"). Empty on every
    #: non-decisive basis (intact level, thesis intact, noise-band, no data).
    #: Carries the trigger, the trend context and the confirmation state in
    #: words, no bare numbers standing alone. `render_owner_break_message`
    #: composes the symbol and action verb around it for the Telegram/board
    #: surfaces; this field is the reusable clause.
    owner_reason: str = ""


def _structural_level_backing_stop(
    *,
    entry_price: float,
    stop_loss: float,
    is_short: bool,
    computed_levels: list | None,
    computed_level_touches: dict | None,
    min_level_touches: int,
    level_cluster_tolerance_pct: float,
) -> float | None:
    """The verified structural level nearest `stop_loss`, or None.

    Exact same matching rule as
    `PortfolioConstructor._level_backing_stop` (side-correctness relative
    to entry, `min_level_touches` prior touches, closest level whose own
    ZONE contains the stop) — reimplemented here as a free function, over
    the same plain data, because that method lives on a class this module
    must not import (it would be a risk module depending on the
    constructor, backwards from every other dependency in this codebase)
    and because the bars it reads off `self.cfg` are passed in here
    directly by the caller instead. Any behavioural drift between the two
    would be a bug; there is deliberately only one set of numbers (the
    caller's), never a second one invented here.

    `level_cluster_tolerance_pct` is NOT a knob. It is
    `src.data.levels.CLUSTER_TOLERANCE_PCT`, the constant
    `find_structural_levels` used to build these zones, passed in rather
    than imported only because this module is deliberately stdlib-only.
    Every caller must pass exactly that; `tests/test_level_match_zone.py`
    checks that they all do. Until 2026-09-13 this was an ATR multiple
    (`level_match_atr_tolerance`, 0.25) claiming to be "at least as wide"
    as the 1% zone — a claim in a different unit that only held above 4%
    ATR — see docs/WORK.md item 46 / docs/INCIDENT_HISTORY.md.
    """
    touches_by_price = computed_level_touches or {}
    best: float | None = None
    best_gap = float("inf")
    for raw in computed_levels or []:
        try:
            price = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(price) or price <= 0:
            continue
        if is_short and price < entry_price:
            continue
        if not is_short and price > entry_price:
            continue
        touches = touches_by_price.get(price)
        if touches is None or touches < min_level_touches:
            continue
        # This level's OWN zone — the same bound
        # `src.data.levels.level_zone_halfwidth` derives, restated here in
        # one line only because this module imports nothing.
        tolerance = price * level_cluster_tolerance_pct / 100.0
        if tolerance <= 0:
            continue
        gap = abs(stop_loss - price)
        if gap <= tolerance and gap < best_gap:
            best, best_gap = price, gap
    return best


def check_structural_protection(
    *,
    thesis_invalid_if: str | None,
    current_price: float | None,
    entry_price: float | None,
    stop_loss: float | None,
    atr: float | None,
    is_short: bool = False,
    computed_levels: list | None = None,
    computed_level_touches: dict | None = None,
    min_level_touches: int,
    level_cluster_tolerance_pct: float,
    ma_20: float | None = None,
    ma_50: float | None = None,
    ma_200: float | None = None,
    ma_200_prior: float | None = None,
    adx: float | None = None,
    break_seen_prior_close: bool = False,
    prior_break_streak: int | None = None,
    prior_break_records: list | None = None,
    prior_session_dates: list | None = None,
) -> StructuralProtectionCheck:
    """Decide whether a position's thesis-backing level is still intact.

    Pure function — every input is a plain value or mapping the caller
    already has; nothing here calls an LLM, a broker, or a market-data
    endpoint. See the module note above for the three-case priority order.

    TREND-SCALED CONFIRMATION (owner mandate 2026-09-24; citation-backed spec).
    `adx`, the 50/200 MA stack and the 200-MA SLOPE (`ma_200`/`ma_200_prior`)
    and the close are passed to `classify_trend_context`, which selects one of
    two sourced regimes (see the module note above): the DEFAULT regime
    (against/weak/tangled, or no trend data) uses the ratified two-consecutive-
    close floor with reclaim reset; the STRONG-WITH-TREND regime (ADX>=25 with a
    rising-200/50>200 up-structure) additionally requires the prior structural
    swing-low to also break before lifting protection. There is NO single-close
    path — nothing exits faster than the two-close floor. The break MARGIN is
    ALWAYS `BREAK_CONFIRMATION_ATR_MULTIPLE` (1.0 ATR) — the regime changes only whether
    the prior-low condition applies, never the margin or the close count. The
    decisive outcomes (a confirmed break, or a break held pending confirmation)
    fill `owner_reason` with a plain-language sentence for the owner surfaces.

    CONFIRMATION STATE carried across days. Preferred inputs are
    `prior_break_records` — one record per prior completed session for this
    position (each `{bar_date, raw_broken, close}`) — and `prior_session_dates`
    — the exact completed trading sessions strictly before today's close,
    most-recent first, taken from the position's own daily bars (the
    authoritative calendar, weekends/holidays already removed). From those the
    count of CONSECUTIVE confirming prior closes is reconstructed here, so a
    skipped/gap session resets the streak (#3) and a prior close that only
    cleared a LOOSER margin than today's classification demands does not count
    (#4). When those richer inputs are absent, the legacy shorthands apply:
    `prior_break_streak` (an int count) or `break_seen_prior_close` (a bool,
    a prior streak of exactly one).

    `current_price` MUST be the latest completed DAILY CLOSE for the
    thesis/level basis below — never a live/intraday quote. Real trading
    practice (and this codebase's own noise-band reasoning elsewhere) is
    explicit that a level "breaks" on a decisive close beyond it, not on a
    wick that pierces it and closes back inside; a same-day intrabar dip
    through a level must never register as a break at all, closed or not.
    `ma_20`/`ma_50`/`ma_200` must be computed off the same close.

    CONFIRMATION GATE (owner refinement, 2026-09-04, corrected same day
    after review against real technical-analysis practice). A thesis-break
    or a structural-level break must not lift protection off a single
    day's close — a "spring" (a level briefly breaking then reclaiming,
    often itself a BULLISH signal) is a well-documented pattern, not a
    real breakdown, and can take a day or two to resolve. A break lifts
    protection only once the SAME break condition holds on the close of
    TWO CONSECUTIVE TRADING DAYS. `break_seen_prior_close` carries that
    state IN — true when the immediately preceding TRADING DAY's close
    (not merely the last time this ran — several same-day pipeline cycles
    must not double-count one close), for this same position, already
    came back `raw_broken=True`. This read only lifts protection
    (`protected=False`) when it is ALSO broken today, i.e. two consecutive
    confirming closes; a single broken close returns `protected=True` with
    a `*_pending_confirmation` basis, and a reclaim the next day resets —
    it does NOT carry forward toward a future confirmation. The caller is
    responsible for persisting `raw_broken` keyed by symbol AND the close's
    own date, and feeding the prior TRADING DAY's value back in as
    `break_seen_prior_close`; this module holds no state of its own and
    does not know what a "day" or a "cycle" is. This gate applies ONLY to
    the thesis/level basis below — the no-level noise-band fallback, and
    the two independent regime-flip / bearish-state-change triggers in
    `holding_discipline_false_claim`, all lift protection immediately,
    unaffected by this gate.

    The margin for "beyond the level" is `BREAK_CONFIRMATION_ATR_MULTIPLE`
    (1.0), NOT the level-zone tolerance used to MATCH a level to a stop's
    placement. Until 2026-09-26 it shared the noise-band constant and this
    docstring called that 1.0 "already ratified" — it never was, on either
    job (board item 70); the two jobs now have two names and two ledger
    entries, both still open. The two are not
    even the same kind of quantity: matching is an identity question about
    a zone defined as a percentage of price, breaking is a question about
    whether a move exceeded the name's own noise, which is an ATR
    question. See docs/WORK.md item 46 for why conflating the two units
    was the defect here in the first place.
    """
    # Trend regime — classify ONCE up front so the thesis and structural-level
    # branches below share the same read. `needed_closes` and whether the prior
    # structural swing-low must also break come from the sourced regime; the
    # break margin is always `BREAK_CONFIRMATION_ATR_MULTIPLE`.
    trend_context = classify_trend_context(
        is_short=is_short, current_price=current_price,
        ma_50=ma_50, ma_200=ma_200, ma_200_prior=ma_200_prior, adx=adx,
    )
    needed_closes, requires_prior_low = _break_confirmation_settings(
        trend_context,
    )

    def _prior_streak(clears) -> int:
        """Consecutive prior confirming closes under `clears`. Prefers the
        adjacency/margin-aware record walk; falls back to the legacy int/bool
        shorthands when no records were supplied."""
        if prior_break_records and prior_session_dates:
            return _consecutive_prior_break_count(
                prior_break_records, prior_session_dates, clears=clears,
            )
        if prior_break_streak is not None:
            return max(0, int(prior_break_streak))
        return 1 if break_seen_prior_close else 0

    # Thesis-branch base streak: adjacency only (a thesis_invalid_if break has
    # no ATR margin to conflate, so every adjacent broken close counts). The
    # structural-level branch recomputes this with a margin-conflation guard
    # once its own break margin is known.
    prior_streak = _prior_streak(clears=lambda r: True)
    # Today's own broken close is the first confirming close; the prior streak
    # supplies the rest. Capped for display so "seen" never exceeds "needed".
    closes_seen = min(prior_streak + 1, needed_closes)
    confirmed = prior_streak + 1 >= needed_closes

    text = (thesis_invalid_if or "").strip()
    if text:
        check = check_thesis_invalid_if(
            text, current_price, ma_20=ma_20, ma_50=ma_50, ma_200=ma_200,
        )
        if check.status == "TRIGGERED":
            if confirmed:
                return StructuralProtectionCheck(
                    protected=False, basis="thesis_invalid_if_triggered",
                    detail=(
                        f"thesis_invalid_if triggered on {closes_seen} "
                        f"confirming trading-day close(s) "
                        f"(trend context: {trend_context}): {check.detail}"
                    ),
                    raw_broken=True,
                    trend_context=trend_context,
                    confirming_closes_needed=needed_closes,
                    confirming_closes_seen=closes_seen,
                    owner_reason=_compose_owner_break_reason(
                        is_short=is_short, trigger_desc="its stated exit "
                        "condition was met on the close",
                        trend_context=trend_context, confirmed=True,
                    ),
                )
            return StructuralProtectionCheck(
                protected=True, basis="thesis_invalid_if_pending_confirmation",
                detail=(
                    f"thesis_invalid_if triggered on today's close "
                    f"({closes_seen} of {needed_closes} confirming closes, "
                    f"trend context: {trend_context}) — still protected "
                    f"pending confirmation (guards against a one-day "
                    f"spring/false-breakdown): {check.detail}"
                ),
                raw_broken=True,
                trend_context=trend_context,
                confirming_closes_needed=needed_closes,
                confirming_closes_seen=closes_seen,
                owner_reason=_compose_owner_break_reason(
                    is_short=is_short, trigger_desc="its stated exit "
                    "condition was met on the close",
                    trend_context=trend_context, confirmed=False,
                ),
            )
        if check.status == "NOT_TRIGGERED":
            return StructuralProtectionCheck(
                protected=True, basis="thesis_invalid_if_intact",
                detail=f"thesis_invalid_if not triggered: {check.detail}",
                raw_broken=False,
            )
        # UNPARSEABLE — falls through to the structural-level check below
        # rather than being treated as protected or broken by default.

    ent = _finite(entry_price)
    stop = _finite(stop_loss)
    atr_f = _finite(atr)
    if ent is not None and stop is not None and atr_f is not None and atr_f > 0:
        level = _structural_level_backing_stop(
            entry_price=ent, stop_loss=stop, is_short=is_short,
            computed_levels=computed_levels,
            computed_level_touches=computed_level_touches,
            min_level_touches=min_level_touches,
            level_cluster_tolerance_pct=level_cluster_tolerance_pct,
        )
        if level is not None:
            cur = _finite(current_price)
            # NOTE: matching WHICH level backs the stop (above, via
            # `_structural_level_backing_stop`) asks an IDENTITY question
            # and is answered inside that level's own 1%-of-price zone.
            # Deciding whether that level has since BROKEN is a different
            # question — it is about whether an adverse move is real, which
            # IS a volatility question — so it uses a wider, decisive
            # ATR-based margin, `BREAK_CONFIRMATION_ATR_MULTIPLE` (see this
            # function's docstring). Two questions, two units, on purpose.
            # The break margin is ALWAYS that constant (owner mandate
            # 2026-09-24, trend-scaled exit): the regime changes how many closes
            # and the prior-low condition, NEVER the margin.
            break_margin = BREAK_CONFIRMATION_ATR_MULTIPLE * atr_f
            # MARGIN-CONSISTENCY GUARD (#4). Now that this branch's break margin
            # is known, recompute the prior streak counting a prior close only
            # if it cleared THIS margin — a close that only cleared a looser
            # margin (a wider ATR band on a lower-ATR day) does not confirm a
            # break under the margin now in force.
            # Legacy break rows written before the stored-close change carry no
            # "close", so `_finite` returns None and they cannot confirm a break;
            # this self-heals within ~one session as fresh rows (with close) are
            # written each cycle.
            def _cleared_current_margin(r) -> bool:
                rc = _finite(r.get("close"))
                if rc is None:
                    return False
                return (rc >= level + break_margin) if is_short else (
                    rc <= level - break_margin
                )

            level_prior_streak = _prior_streak(clears=_cleared_current_margin)
            closes_seen = min(level_prior_streak + 1, needed_closes)
            streak_confirmed = level_prior_streak + 1 >= needed_closes
            # A long's support is broken when the CLOSE has fallen to/through
            # it by at least the noise-band margin; a short's resistance is
            # broken when the close has risen to/through it by the same
            # margin from below.
            if cur is None:
                # No close to judge against — cannot say the level has
                # broken, so the level stays trusted (fail toward
                # protection, same "fail closed on the side that does not
                # ship a false 'safe to sell'" posture as
                # `_level_backing_stop` itself uses for touch counts).
                return StructuralProtectionCheck(
                    protected=True, basis="structural_level_intact",
                    detail=(
                        f"structural level {level} backs the stop but no "
                        f"current_price (closing price) supplied — treated "
                        f"as intact"
                    ),
                    raw_broken=False,
                )
            if is_short:
                broken = cur >= level + break_margin
            else:
                broken = cur <= level - break_margin
            if broken:
                level_desc = (
                    f"its {level:g} resistance" if is_short
                    else f"its {level:g} support"
                )
                # REGIME-3 STRUCTURAL PATIENCE. For a strong-with-trend break the
                # close streak alone is not enough: the PRIOR structural
                # swing-low (long) / swing-high (short) must ALSO have closed
                # broken before protection lifts. When there is no prior
                # structural level, the gate is vacuous and regime 3 falls back
                # to the plain two-consecutive-close rule. (No volume in the exit
                # path, so this is a structural, not a Wyckoff-volume, read.)
                awaiting_prior_low = False
                if requires_prior_low and streak_confirmed:
                    prior_low_broken = _prior_structural_level_broken(
                        computed_levels, level, cur, break_margin,
                        is_short=is_short,
                    )
                    confirmed = prior_low_broken
                    awaiting_prior_low = not prior_low_broken
                else:
                    confirmed = streak_confirmed
                if confirmed:
                    return StructuralProtectionCheck(
                        protected=False, basis="structural_level_broken",
                        detail=(
                            f"structural level {level} backing the stop has "
                            f"closed beyond it on {closes_seen} confirming "
                            f"trading-day close(s) (regime: {trend_context}"
                            f"{'; prior swing-low also broken' if requires_prior_low else ''}): "
                            f"close {cur} vs level {level} "
                            f"(break margin {break_margin:.4g})"
                        ),
                        raw_broken=True,
                        trend_context=trend_context,
                        confirming_closes_needed=needed_closes,
                        confirming_closes_seen=closes_seen,
                        owner_reason=_compose_owner_break_reason(
                            is_short=is_short,
                            trigger_desc=(
                                f"closed decisively "
                                f"{'above' if is_short else 'below'} "
                                f"{level_desc}"
                            ),
                            trend_context=trend_context, confirmed=True,
                        ),
                    )
                pending_reason = (
                    "awaiting prior swing-low break" if awaiting_prior_low
                    else f"{closes_seen} of {needed_closes} confirming closes"
                )
                return StructuralProtectionCheck(
                    protected=True,
                    basis="structural_level_pending_confirmation",
                    detail=(
                        f"structural level {level} backing the stop closed "
                        f"beyond it today ({pending_reason}, regime: "
                        f"{trend_context}) — still protected pending "
                        f"confirmation (guards against a one-day "
                        f"spring/false-breakdown): close {cur} vs level "
                        f"{level} (break margin {break_margin:.4g})"
                    ),
                    raw_broken=True,
                    trend_context=trend_context,
                    confirming_closes_needed=needed_closes,
                    confirming_closes_seen=closes_seen,
                    owner_reason=_compose_owner_break_reason(
                        is_short=is_short,
                        trigger_desc=(
                            f"{'rose above' if is_short else 'dipped below'} "
                            f"{level_desc}"
                        ),
                        trend_context=trend_context, confirmed=False,
                        awaiting_prior_low=awaiting_prior_low,
                    ),
                )
            return StructuralProtectionCheck(
                protected=True, basis="structural_level_intact",
                detail=(
                    f"structural level {level} backing the stop is intact: "
                    f"close {cur} vs level {level} (break margin "
                    f"{break_margin:.4g})"
                ),
                raw_broken=False,
                trend_context=trend_context,
            )

    # Neither a checkable thesis_invalid_if nor a qualifying structural
    # level under the stop. Owner refinement 2026-09-04: this must NOT
    # default to zero protection — that would systematically strip
    # protection from breakout/momentum trades that don't have classic
    # multi-touch support/resistance by design. Fall back instead to the
    # noise band already used elsewhere in this module
    # (`adverse_move_is_noise` / `NOISE_BAND_ATR_MULTIPLE`) — no second
    # noise-band constant. A position with nothing concrete backing its
    # thesis stays protected unless the adverse move against it exceeds
    # that already-ratified band. This fallback lifts protection
    # immediately — it is not gated by the confirmation rule above, which
    # applies only to the thesis/level basis.
    cur = _finite(current_price)
    if ent is not None and atr_f is not None and atr_f > 0 and cur is not None:
        adverse = (cur - ent) if is_short else (ent - cur)
        if adverse <= 0:
            # Flat or in profit — never this fallback's business.
            return StructuralProtectionCheck(
                protected=True, basis="noise_band_intact",
                detail=(
                    "no thesis_invalid_if and no verified structural level "
                    "under the stop, but price is flat/favourable versus "
                    "entry — protected"
                ),
                raw_broken=False,
            )
        is_noise = adverse_move_is_noise(
            ent, cur, atr_f, side=("buy" if is_short else "sell"),
        )
        if is_noise:
            return StructuralProtectionCheck(
                protected=True, basis="noise_band_intact",
                detail=(
                    f"no thesis_invalid_if and no verified structural level "
                    f"under the stop; adverse move ({adverse:.4g}) is within "
                    f"the {NOISE_BAND_ATR_MULTIPLE}x ATR noise band — "
                    f"protected"
                ),
                raw_broken=False,
            )
        return StructuralProtectionCheck(
            protected=False, basis="noise_band_broken",
            detail=(
                f"no thesis_invalid_if and no verified structural level "
                f"under the stop; adverse move ({adverse:.4g}) exceeds the "
                f"{NOISE_BAND_ATR_MULTIPLE}x ATR noise band — not protected"
            ),
            raw_broken=False,
        )

    # No basis AND no usable price/ATR to even judge the noise band —
    # cannot say the position has moved against it at all. Fail toward
    # protection rather than manufacture a block out of missing data (same
    # posture `adverse_move_is_noise` itself takes).
    return StructuralProtectionCheck(
        protected=True, basis="noise_band_intact",
        detail=(
            "no thesis_invalid_if, no verified structural level under the "
            "stop, and insufficient price/ATR data to evaluate the noise "
            "band — treated as protected"
        ),
        raw_broken=False,
    )


def structural_protection_broken(
    *,
    thesis_invalid_if: str | None,
    current_price: float | None,
    entry_price: float | None,
    stop_loss: float | None,
    atr: float | None,
    is_short: bool = False,
    computed_levels: list | None = None,
    computed_level_touches: dict | None = None,
    min_level_touches: int,
    level_cluster_tolerance_pct: float,
    ma_20: float | None = None,
    ma_50: float | None = None,
    ma_200: float | None = None,
    ma_200_prior: float | None = None,
    adx: float | None = None,
    break_seen_prior_close: bool = False,
    prior_break_streak: int | None = None,
    prior_break_records: list | None = None,
    prior_session_dates: list | None = None,
) -> bool:
    """True when the position's thesis-backing level has broken and that
    break is CONFIRMED (no protection); False when it is still intact or
    the break is only pending confirmation (protected).

    Thin bool wrapper over `check_structural_protection` — see that
    function and the module note above for the full priority order and the
    confirmation gate. Kept as a separate function because most callers
    only need the yes/no answer; the caller that needs to log WHY, or that
    needs `raw_broken` to persist for the next cycle's
    `break_seen_prior_close` (see `holding_discipline_false_claim`), should
    call `check_structural_protection` directly.
    """
    return not check_structural_protection(
        thesis_invalid_if=thesis_invalid_if,
        current_price=current_price,
        entry_price=entry_price,
        stop_loss=stop_loss,
        atr=atr,
        is_short=is_short,
        computed_levels=computed_levels,
        computed_level_touches=computed_level_touches,
        min_level_touches=min_level_touches,
        level_cluster_tolerance_pct=level_cluster_tolerance_pct,
        ma_20=ma_20, ma_50=ma_50, ma_200=ma_200, ma_200_prior=ma_200_prior,
        adx=adx,
        break_seen_prior_close=break_seen_prior_close,
        prior_break_streak=prior_break_streak,
        prior_break_records=prior_break_records,
        prior_session_dates=prior_session_dates,
    ).protected

