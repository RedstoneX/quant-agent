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
