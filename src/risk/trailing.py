"""Deterministic trailing stops — spec Phase 3.7.

Trailing is arithmetic. It belongs in Python, not in an LLM's discretion.

Until this landed, every stop movement came from the Position Reviewer
emitting a `TRAIL_STOP` action with a price it chose, clamped after the fact
by a ratchet cooldown and a 1.25x ATR noise band. Those clamps exist because
the discretionary version marched stops into the daily-noise band in three or
four sessions — GE was ratcheted 325 -> 350 in eight sessions on one flag.
Clamping a bad generator is not the same as having a good one.

The rule depends on how the position is MANAGED, which the Technical Analyst
decides at entry and which is pinned to the trade row alongside the horizon:

**Type A — `range`.** There is structure on both sides and the target is a
level someone is defending. Do not trail at all until price EXCEEDS that
target; the stop stays where it was placed at entry. Trailing a range trade
early is how a position gets stopped out inside the very range it was bought
to traverse. (Short mirror: the target is a level BELOW the short, and the
stop does not move until price falls PAST it.)

**Type B — `breakout`.** There is no overhead structure and the target is a
measured-move reference, not a level. Progress and pace are meaningless here
(see `pipeline._build_position_facts`), so trailing IS the management: ride it
and let structure decide when it is over. Trail from entry, under each
successive higher low, with a chandelier stop where structure is unclear.
(Short mirror: trail from entry, above each successive lower high, chandelier
above the lowest low since entry where structure is unclear.)

Invariants, all of them enforced below:
  - **Ratchet toward less risk only.** Up for a long, down for a short. A
    stop never moves the wrong way. Ever.
  - A move must clear the existing stop by `MIN_RATCHET_PCT` to be worth an
    order at all — otherwise every session nudges the stop a few cents and the
    cooldown is doing all the work.
  - A new stop is never placed inside `NOISE_BAND_ATR_MULTIPLE` ATRs of
    current price. That is the same floor the discretionary path already
    clamps to, applied at the source instead of after the fact.
  - Missing data yields no proposal, never a guess.

**Stage 2 of short selling (shorts-safe).** Every rule above was written and
tested against a long-only book, where "trail" only ever means "raise the
stop". A short's protective stop is a BUY stop ABOVE the market, and every
one of these rules mirrors through a price-axis flip: ratchet DOWN instead of
UP, trail under successive LOWER highs instead of higher lows, chandelier off
the LOWEST low instead of the highest high, noise band and ratchet-minimum
measured on the other side of the stop. `qty` supplies only the SIDE (same
convention as `risk.metrics.r_multiple`): negative is a short. No order path
in this repo can open a short yet, so `qty` defaults to +1.0 and every
existing call site — which only ever knows about longs — is unchanged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "TrailProposal",
    "compute_trailing_stop",
    "MIN_RATCHET_PCT",
    "CHANDELIER_ATR_MULTIPLE",
    "NOISE_BAND_ATR_MULTIPLE",
    "PIVOT_WINDOW",
]

#: A proposed stop must sit at least this far above the live stop. Mirrors the
#: reviewer's historical ">= 1.02x old stop" min-bump rule so the deterministic
#: path does not churn orders the discretionary one would have skipped.
MIN_RATCHET_PCT = 2.0

#: Chandelier distance below the highest high since entry, used only where
#: structure is unclear. 3x ATR is the conventional setting and is deliberately
#: loose: a fallback that strangles the trade defeats the purpose of Type B.
CHANDELIER_ATR_MULTIPLE = 3.0

#: A stop closer than this many ATRs to current price sits inside one ordinary
#: day's range. Same value the TRAIL_STOP clamp in `pipeline.py` uses.
NOISE_BAND_ATR_MULTIPLE = 1.25

#: Bars either side of a candidate swing low. Matches `src/data/levels.py`'s
#: pivot detection so "a higher low" means the same thing in both places.
PIVOT_WINDOW = 3


def _finite(value: object) -> float | None:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


@dataclass(frozen=True)
class TrailProposal:
    """A deterministic proposal to raise one position's stop."""

    symbol: str
    new_stop: float
    previous_stop: float
    source: str          # "structure" | "chandelier"
    reason: str


def _swing_lows(bars, window: int = PIVOT_WINDOW) -> list[float]:
    """Confirmed swing lows, oldest first.

    A low is confirmed only when `window` bars on BOTH sides are higher, so the
    most recent `window` bars can never produce one. That lag is the point: an
    unconfirmed low is just today's price, and trailing under today's price is
    how a stop ends up inside the noise band.
    """
    lows: list[float] = []
    n = len(bars)
    if n < window * 2 + 1:
        return lows
    values = [_finite(getattr(b, "low", None)) for b in bars]
    for i in range(window, n - window):
        centre = values[i]
        if centre is None:
            continue
        neighbourhood = [v for v in values[i - window:i + window + 1] if v is not None]
        if len(neighbourhood) < window + 1:
            continue
        if centre <= min(neighbourhood):
            lows.append(centre)
    return lows


def _swing_highs(bars, window: int = PIVOT_WINDOW) -> list[float]:
    """Confirmed swing highs, oldest first — the short's mirror of `_swing_lows`.

    A high is confirmed only when `window` bars on BOTH sides are lower, so
    the most recent `window` bars can never produce one, for the same lag
    reason as the long side: an unconfirmed high is just today's price, and
    trailing above today's price is how a short's stop ends up inside the
    noise band.
    """
    highs: list[float] = []
    n = len(bars)
    if n < window * 2 + 1:
        return highs
    values = [_finite(getattr(b, "high", None)) for b in bars]
    for i in range(window, n - window):
        centre = values[i]
        if centre is None:
            continue
        neighbourhood = [v for v in values[i - window:i + window + 1] if v is not None]
        if len(neighbourhood) < window + 1:
            continue
        if centre >= max(neighbourhood):
            highs.append(centre)
    return highs


def compute_trailing_stop(
    *,
    symbol: str,
    setup_type: str | None,
    entry: float,
    current_price: float,
    current_stop: float | None,
    reference_target: float | None,
    bars=None,
    atr: float | None = None,
    min_ratchet_pct: float = MIN_RATCHET_PCT,
    qty: float = 1.0,
) -> TrailProposal | None:
    """Propose a tightened stop, or None when no move is warranted.

    `bars` are the daily bars SINCE ENTRY (the caller slices them); only those
    matter, because a swing low/high from before the position existed is not
    a level this trade ever defended.

    `qty` supplies only the SIDE — same convention as `risk.metrics.r_multiple`.
    A negative qty is a short: everything below mirrors across the entry
    price. A long's stop lives BELOW price and ratchets UP toward it as the
    trade works; a short's stop lives ABOVE price and ratchets DOWN toward it.
    Defaults to +1.0 so every existing (long-only) call site is unchanged.
    """
    ent = _finite(entry)
    cur = _finite(current_price)
    stop = _finite(current_stop) if current_stop is not None else None
    atr_f = _finite(atr) if atr is not None else None

    if ent is None or cur is None or ent <= 0 or cur <= 0:
        return None
    if stop is None or stop <= 0:
        # No live stop means the position is unprotected, which is a repair
        # problem, not a trailing problem. Inventing a trailing stop here
        # would paper over a missing protective order.
        return None

    is_short = (_finite(qty) or 1.0) < 0

    # --- Type A: no trailing until the target is actually exceeded ---------
    if setup_type != "breakout":
        target = _finite(reference_target) if reference_target is not None else None
        if target is None:
            return None
        if is_short:
            # Short mirror: the target is a level BELOW entry someone is
            # defending. Hold the entry stop until price falls PAST it.
            if cur >= target:
                return None
        else:
            if cur <= target:
                return None

    # --- Candidate: structure first ---------------------------------------
    candidate: float | None = None
    source = ""
    if is_short:
        highs = _swing_highs(bars or [])
        # Only highs that are BELOW the current stop and ABOVE current price
        # are useful: above the stop is not a ratchet, below the price is not
        # a stop.
        usable = [hi for hi in highs if cur < hi < stop]
        if usable:
            # The LOWEST usable high is the tightest defensible stop —
            # mirror of the long side's `max(usable)`.
            candidate = min(usable)
            source = "structure"
    else:
        lows = _swing_lows(bars or [])
        # Only lows that are ABOVE the current stop and BELOW current price
        # are useful: below the stop is not a ratchet, above the price is not
        # a stop.
        usable = [lo for lo in lows if stop < lo < cur]
        if usable:
            candidate = max(usable)
            source = "structure"

    # --- Fallback: chandelier, where structure is unclear ------------------
    if candidate is None and atr_f is not None and atr_f > 0:
        if is_short:
            lows = [_finite(getattr(b, "low", None)) for b in (bars or [])]
            lows = [l for l in lows if l is not None]
            lowest = min(lows) if lows else cur
            chandelier = lowest + CHANDELIER_ATR_MULTIPLE * atr_f
            if cur < chandelier < stop:
                candidate = chandelier
                source = "chandelier"
        else:
            highs = [
                _finite(getattr(b, "high", None)) for b in (bars or [])
            ]
            highs = [h for h in highs if h is not None]
            highest = max(highs) if highs else cur
            chandelier = highest - CHANDELIER_ATR_MULTIPLE * atr_f
            if stop < chandelier < cur:
                candidate = chandelier
                source = "chandelier"

    if candidate is None:
        return None

    # --- Invariants --------------------------------------------------------
    # Ratchet toward less risk only, and only when the move is worth an order.
    if is_short:
        if candidate >= stop * (1 - min_ratchet_pct / 100.0):
            return None
    else:
        if candidate <= stop * (1 + min_ratchet_pct / 100.0):
            return None

    # Never inside one ordinary day's range of current price.
    if atr_f is not None and atr_f > 0:
        if is_short:
            noise_ceiling = cur + NOISE_BAND_ATR_MULTIPLE * atr_f
            if candidate < noise_ceiling:
                return None
        else:
            noise_floor = cur - NOISE_BAND_ATR_MULTIPLE * atr_f
            if candidate > noise_floor:
                return None

    candidate = round(candidate, 2)
    if is_short:
        if candidate >= stop or candidate <= cur:
            return None
    else:
        if candidate <= stop or candidate >= cur:
            return None

    locked = ""
    if is_short:
        if candidate <= ent:
            locked = " — at or below entry, so this position stops consuming risk budget"
    else:
        if candidate >= ent:
            locked = " — at or above entry, so this position stops consuming risk budget"
    return TrailProposal(
        symbol=symbol.upper(), new_stop=candidate, previous_stop=stop,
        source=source,
        reason=(
            f"deterministic trail ({source}): {setup_type or 'unknown'} setup, "
            f"stop ${stop:.2f} -> ${candidate:.2f} with price ${cur:.2f}"
            f"{locked}"
        ),
    )
