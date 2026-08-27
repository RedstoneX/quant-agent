"""Deterministic support/resistance detection from completed daily bars.

Why this exists
---------------
Until 2026-08-27 the Tech Analyst was shown the last **20** daily bars and asked
to report "the two or three key levels". Twenty bars is one month: a
consolidation from six weeks ago, a swing high from last spring, a level price
has respected four times since — all invisible. The only things the analyst
could honestly cite were the moving averages and the 20-day high/low, so the
structural levels the whole exit system depends on were effectively absent, and
`PortfolioConstructor` invented stops and targets to fill the gap.

The history was never the problem: the pipeline already fetched hundreds of
bars to compute MA200, then showed the analyst twenty of them.

Finding where price repeatedly stopped is arithmetic, not judgment, so it
belongs here rather than in a language model. Computing five years of levels
costs single-digit milliseconds and zero tokens, and — unlike asking a model to
eyeball a wall of numbers — the same chart yields the same levels every time,
which is what makes the behaviour testable and back-testable.

The model still does the part it is good at: deciding which of these levels
matters right now, and what kind of setup the chart is showing.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np

from src.models import OHLCV

# A pivot is a bar whose high (or low) is the most extreme within this many
# bars either side. 5 keeps genuine swing structure while ignoring single-bar
# wiggles; smaller values produce noise, larger ones miss real turning points.
PIVOT_WINDOW = 5

# Two pivots within this percentage of each other are the same level. Price
# does not respect a number to the cent — it respects a zone.
CLUSTER_TOLERANCE_PCT = 1.0

# A level touched once is a coincidence, not structure.
MIN_TOUCHES = 2

# Levels further than this from the current price are history, not actionable
# structure. Filters out artefacts like a pre-IPO/SPAC period spent pinned at
# $10 while the stock now trades at $42 — technically hundreds of "touches",
# entirely irrelevant to today's decision.
MAX_DISTANCE_PCT = 40.0

# Touches decay with age: a level defended last month matters more than one
# defended three years ago. Half-life in trading sessions (~1 year).
RECENCY_HALFLIFE_SESSIONS = 252.0

# How many levels to report per side. Enough to describe the structure, few
# enough to stay readable in a prompt.
MAX_LEVELS_PER_SIDE = 6

# Bad-print detection. A bar is rejected only when its high exceeds, or its low
# falls below, the median of its immediate neighbours by this factor. 5x is
# deliberately permissive: real markets gap, halt and limit-move, and throwing
# away genuine volatility is worse than tolerating a rare bad print.
_CLEAN_WINDOW = 10
_CLEAN_FACTOR = 5.0


@dataclass(frozen=True)
class Level:
    """One structural price level."""

    price: float
    kind: str  # "support" | "resistance"
    touches: int  # pivots clustered into this level (pre-decay count)
    last_touch_sessions_ago: int
    strength: float  # recency-weighted score; higher = more significant

    def as_dict(self) -> dict:
        return asdict(self)


def _clean_bars(bars: list[OHLCV]) -> list[OHLCV]:
    """Drop bars that cannot be true, and single-bar print errors.

    Vendor feeds produce bad prints: a high 10x the surrounding range, a zero
    low, an inverted bar. A single bad print creates a phantom pivot, and a
    phantom pivot becomes a phantom level that a stop then gets placed against.

    The comparison is deliberately **local**. An earlier version compared every
    bar against the median of the whole series, which discarded most of the
    real history of any stock that trended hard — OKLO ran from $10 to $114, so
    a global median rejected its entire recent range as "outliers". A stock
    that multiplied is not corrupt data; a bar five times its immediate
    neighbours is. Only egregious deviations are removed, so genuine gaps and
    limit moves survive.
    """
    usable = [
        b for b in bars
        if b.high > 0 and b.low > 0 and b.close > 0 and b.high >= b.low
    ]
    n = len(usable)
    if n < _CLEAN_WINDOW * 2 + 1:
        return usable

    highs = np.array([b.high for b in usable], dtype=float)
    lows = np.array([b.low for b in usable], dtype=float)

    keep: list[OHLCV] = []
    for i, bar in enumerate(usable):
        lo = max(0, i - _CLEAN_WINDOW)
        hi = min(n, i + _CLEAN_WINDOW + 1)
        # Neighbourhood excluding the bar under test — a bad print must not be
        # allowed to widen the band that is supposed to catch it.
        neighbour_highs = np.concatenate([highs[lo:i], highs[i + 1:hi]])
        neighbour_lows = np.concatenate([lows[lo:i], lows[i + 1:hi]])
        if neighbour_highs.size == 0 or neighbour_lows.size == 0:
            keep.append(bar)
            continue
        local_high = float(np.median(neighbour_highs))
        local_low = float(np.median(neighbour_lows))
        if local_high <= 0 or local_low <= 0:
            keep.append(bar)
            continue
        if bar.high > local_high * _CLEAN_FACTOR:
            continue
        if bar.low < local_low / _CLEAN_FACTOR:
            continue
        keep.append(bar)
    return keep


def _find_pivots(bars: list[OHLCV], window: int) -> list[tuple[int, float, str]]:
    """Locate swing highs and lows. Returns (index, price, "R"|"S")."""
    n = len(bars)
    if n < window * 2 + 1:
        return []
    highs = np.array([b.high for b in bars], dtype=float)
    lows = np.array([b.low for b in bars], dtype=float)

    pivots: list[tuple[int, float, str]] = []
    for i in range(window, n - window):
        lo, hi = i - window, i + window + 1
        if highs[i] >= highs[lo:hi].max():
            pivots.append((i, float(highs[i]), "R"))
        if lows[i] <= lows[lo:hi].min():
            pivots.append((i, float(lows[i]), "S"))
    return pivots


def _cluster(
    pivots: list[tuple[int, float, str]], tolerance_pct: float
) -> list[list[tuple[int, float, str]]]:
    """Group pivots that sit within `tolerance_pct` of each other into zones."""
    if not pivots:
        return []
    ordered = sorted(pivots, key=lambda p: p[1])
    clusters: list[list[tuple[int, float, str]]] = []
    current = [ordered[0]]
    for pivot in ordered[1:]:
        anchor = current[0][1]
        if anchor > 0 and abs(pivot[1] - anchor) / anchor * 100.0 <= tolerance_pct:
            current.append(pivot)
        else:
            clusters.append(current)
            current = [pivot]
    clusters.append(current)
    return clusters


def find_structural_levels(
    bars: list[OHLCV],
    *,
    pivot_window: int = PIVOT_WINDOW,
    tolerance_pct: float = CLUSTER_TOLERANCE_PCT,
    min_touches: int = MIN_TOUCHES,
    max_distance_pct: float = MAX_DISTANCE_PCT,
    max_per_side: int = MAX_LEVELS_PER_SIDE,
) -> tuple[list[Level], list[Level]]:
    """Return ``(support_levels, resistance_levels)``, most significant first.

    Support is below the last close, resistance above it — classified by where
    the level sits *now*, not by whether the pivots forming it were highs or
    lows. A ceiling that price has broken through becomes a floor, and treating
    it as resistance because it was once a swing high would be wrong.

    Returns two empty lists when there is not enough clean history to say
    anything. Callers must treat that as "no structure identified" and decline
    the trade, never as "no structure exists".
    """
    clean = _clean_bars(bars)
    if len(clean) < pivot_window * 2 + 1:
        return [], []

    last_close = clean[-1].close
    if last_close <= 0:
        return [], []

    last_index = len(clean) - 1
    pivots = _find_pivots(clean, pivot_window)

    supports: list[Level] = []
    resistances: list[Level] = []

    for cluster in _cluster(pivots, tolerance_pct):
        if len(cluster) < min_touches:
            continue

        price = float(np.mean([p[1] for p in cluster]))
        if price <= 0:
            continue

        distance_pct = abs(price - last_close) / last_close * 100.0
        if distance_pct > max_distance_pct:
            continue

        newest_index = max(p[0] for p in cluster)
        sessions_ago = last_index - newest_index

        # Each touch contributes on a recency curve, so many ancient touches
        # cannot outrank a level being actively defended today.
        strength = float(
            sum(
                0.5 ** ((last_index - idx) / RECENCY_HALFLIFE_SESSIONS)
                for idx, _price, _side in cluster
            )
        )
        # Nearby levels are more actionable than distant ones: a stop or target
        # 3% away is a decision, one 35% away is trivia.
        strength *= 1.0 / (1.0 + distance_pct / 10.0)

        level = Level(
            price=round(price, 2),
            kind="support" if price < last_close else "resistance",
            touches=len(cluster),
            last_touch_sessions_ago=int(sessions_ago),
            strength=round(strength, 4),
        )
        (supports if level.kind == "support" else resistances).append(level)

    supports.sort(key=lambda lv: -lv.strength)
    resistances.sort(key=lambda lv: -lv.strength)
    return supports[:max_per_side], resistances[:max_per_side]


def format_levels_block(
    supports: list[Level], resistances: list[Level], last_close: float
) -> str:
    """Render levels for the Tech Analyst prompt.

    Resistance descends toward the price and support descends away from it, so
    the block reads top-to-bottom like a chart's vertical axis.
    """
    if not supports and not resistances:
        return (
            "Structural levels: NONE IDENTIFIED — insufficient clean price "
            "history. Do not invent levels; rate this symbol neutral."
        )

    def line(lv: Level) -> str:
        gap = (lv.price - last_close) / last_close * 100.0
        return (
            f"    ${lv.price:,.2f} ({gap:+.1f}%) · {lv.touches} touches · "
            f"last {lv.last_touch_sessions_ago}d ago"
        )

    out = ["Structural levels (computed from the full price history):", "  Resistance (nearest last):"]
    if resistances:
        out.extend(line(lv) for lv in sorted(resistances, key=lambda x: -x.price))
    else:
        out.append("    none within range")
    out.append(f"  >>> last close ${last_close:,.2f} <<<")
    out.append("  Support (nearest first):")
    if supports:
        out.extend(line(lv) for lv in sorted(supports, key=lambda x: -x.price))
    else:
        out.append("    none within range")
    return "\n".join(out)
