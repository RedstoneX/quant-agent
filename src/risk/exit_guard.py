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
correlation breach, a triggered `thesis_invalid_if`. Those are judgments about
the world. "It is stalling" is a claim about numbers, and the numbers are
right here.

Directionality matters and is the whole point:
  - `thesis_progress_pct` rising is improvement.
  - `distance_to_stop_pct` rising is improvement (further from the stop).
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
    "NOISE_BAND_ATR_MULTIPLE",
    "ThesisInvalidationCheck",
    "check_thesis_invalid_if",
    "TRUSTED_MACRO_STATUSES",
    "claims_regime_flip",
    "claims_bearish_state_change",
    "holding_discipline_false_claim",
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

    @property
    def has_prior(self) -> bool:
        return bool(self.changes)

    @property
    def improved(self) -> list[str]:
        """Metrics that moved in the position's favour beyond the noise floor."""
        out = []
        for name, (before, after) in self.changes.items():
            if name not in _HIGHER_IS_BETTER:
                continue
            if after - before > _NOISE_FLOOR.get(name, 0.0):
                out.append(name)
        return sorted(out)

    @property
    def worsened(self) -> list[str]:
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
        for name in sorted(self.changes):
            before, after = self.changes[name]
            arrow = "→"
            direction = ""
            if name in _HIGHER_IS_BETTER:
                floor = _NOISE_FLOOR.get(name, 0.0)
                if after - before > floor:
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
    if prior and current:
        for name in _HIGHER_IS_BETTER:
            before = _finite(prior.get(name))
            after = _finite(current.get(name))
            if before is None or after is None:
                continue
            changes[name] = (before, after)
    return MetricDeltas(
        symbol=symbol.upper(), changes=changes, prior_timestamp=prior_timestamp,
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

    `deltas` is trusted to already be direction-corrected for a short (see
    `TradingPipeline._build_position_facts` / `_pnl_pct` — every metric here
    is "higher is better" regardless of which side is held), so COVER needs
    no separate sign handling in this function.
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

#: An adverse move smaller than this many ATRs from entry cannot be
#: distinguished from one ordinary day's range. `TRAIL_STOP` already uses
#: 1.25x ATR14 against CURRENT price for the same reason; this is the
#: equivalent floor for a discretionary exit, measured from ENTRY.
#:
#: OKLO was bought and sold on 2026-08-26 for a 2.5% loss — **0.67 ATR**, on
#: day zero. The evening review graded the buy "wrong" and the sell "correct",
#: but the honest reading is that nothing had happened yet in either
#: direction: the position was never given one day's normal range to breathe.
NOISE_BAND_ATR_MULTIPLE = 1.0

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
    r"\bcorrelation (?:cluster )?breach\b",
    r"\bcircuit breaker\b",
    r"\bdaily[- ]loss\b",
    r"\bdaily loss\b",
    r"\bstop hit\b",
    r"\bstopped out\b",
)

_EXTERNAL_RE = re.compile("|".join(EXTERNAL_INFORMATION_PATTERNS), re.IGNORECASE)


def cites_external_information(reason: str) -> bool:
    """True when the reason names a trigger originating outside the tape."""
    return bool(reason) and bool(_EXTERNAL_RE.search(reason))


def adverse_move_is_noise(
    entry: float,
    current_price: float,
    atr: float | None,
    *,
    multiple: float = NOISE_BAND_ATR_MULTIPLE,
    side: str = "sell",
) -> bool:
    """True when the position has moved ADVERSELY from entry by less than
    `multiple` ATRs.

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
    return adverse < multiple * atr_f


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
# Even a provable false claim is only ever LOGGED and recorded as a pipeline
# event here — never used to veto or drop the trade. Two reasons: first, the
# claim-detection is phrase matching over free text (see the two regexes
# below), which cannot distinguish "regime flipped to risk-off" from
# "market is NOT showing a regime flip to risk-off" — a negation would be
# misread as an assertion, and a veto on a misread would be exactly the kind
# of wrongly-blocked-legitimate-exit failure this fix must not introduce.
# Second, even a correctly-read false claim about (b)/(c) does not itself
# prove the SELL is wrong — (a) might still independently justify it, and
# this module cannot tell. FLAGGED FOR REVIEW: whether this should ever
# escalate to a veto (once claim-detection is more than phrase matching) is
# a judgment call left to a human, not resolved here.

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


def claims_regime_flip(reason: str) -> bool:
    """True when `reason` asserts a regime flip to risk-off."""
    return bool(reason) and bool(_REGIME_FLIP_CLAIM_RE.search(reason))


def claims_bearish_state_change(reason: str) -> bool:
    """True when `reason` asserts a HIGH-conviction bearish state_change."""
    return bool(reason) and bool(_BEARISH_STATE_CHANGE_CLAIM_RE.search(reason))


def holding_discipline_false_claim(
    *,
    action: str,
    reason: str,
    symbol: str,
    days_held: int | None,
    macro_regime_today: str | None,
    macro_status: str | None,
    active_state_changes: str = "",
    asof: date | None = None,
) -> str | None:
    """Return a finding string when `reason` makes a PROVABLY FALSE holding-
    discipline claim about a `<5d` position's exit, else None.

    Checks ONLY:
      (b) a claimed regime flip to risk-off, contradicted when today's macro
          read (`macro_status` in `TRUSTED_MACRO_STATUSES`) shows a DIFFERENT,
          non-risk-off regime;
      (c) a claimed HIGH-conviction bearish state_change, contradicted when a
          same-day `active_state_changes` row DOES name the symbol but with a
          recorded direction that is NOT bearish (parsed via
          `PortfolioManagerAgent._state_change_symbols_by_date`, the exact
          function that already owns this parsing for the sub-floor catalyst
          gate — not reimplemented here).

    Never flags:
      - an action other than SELL/REDUCE/COVER;
      - a position with `days_held` unknown or >= 5 (outside the protection
        period this checklist item governs);
      - a reason that makes neither claim;
      - a claim that cannot be checked at all (macro status not trusted, or
        no same-day row names the symbol) — absence of proof is not proof of
        a false claim, and in particular a missing state-change row is much
        weaker evidence than a definite non-matching macro regime: the news
        pipeline can simply not have logged a real catalyst as a formal
        `state_change` row yet, so treating "not found" as "false" here
        would manufacture false positives on legitimate exits;
      - (a) `thesis_invalid_if` — not evaluated at all, by design (see the
        module-level note above), so a SELL resting entirely on (a) is never
        flagged just because (b) and (c) are absent or unverifiable.

    This is a FINDING for the audit trail, not a veto: see the module-level
    note above for why a provable false claim still is not, by itself, made
    to block the trade here.
    """
    if str(action).upper() not in ("SELL", "REDUCE", "COVER"):
        return None
    if days_held is None or days_held >= 5:
        return None
    reason = reason or ""
    symbol_u = symbol.strip().upper()
    findings: list[str] = []

    if claims_regime_flip(reason):
        if macro_status in TRUSTED_MACRO_STATUSES and macro_regime_today:
            if macro_regime_today != "risk-off":
                findings.append(
                    f"claims a regime flip to risk-off today, but today's "
                    f"macro read ({macro_status}) shows regime="
                    f"{macro_regime_today!r}, not risk-off"
                )
        # else: macro unavailable/untrusted this run — cannot verify, so
        # this claim is silently left unchecked rather than flagged.

    if claims_bearish_state_change(reason):
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
        if symbol_directions is not None and "bearish" not in symbol_directions:
            rendered = ", ".join(sorted(symbol_directions)) or "no recorded direction"
            findings.append(
                f"claims a HIGH-conviction bearish state change today, but "
                f"today's Active News State Change block names {symbol_u} "
                f"with direction(s) {rendered} instead of bearish"
            )
        # symbol_directions is None (no same-day row names the symbol at
        # all) -> unverifiable, not flagged; see docstring.

    if not findings:
        return None
    return (
        f"{symbol_u}: {action} on a position held {days_held}d (<5d "
        f"protection period) — reasoning " + "; and ".join(findings) +
        f". This is a provable contradiction of a checkable claim, not a "
        f"veto — thesis_invalid_if (which this module cannot verify either "
        f"way) may independently justify this exit. Recorded for review."
    )
