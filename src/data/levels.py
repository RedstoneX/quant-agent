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

import math
from collections.abc import Sequence
from dataclasses import dataclass, asdict

import numpy as np

from src.models import OHLCV
from src.data.technical import atr_series
from src.risk.constants import is_trend_trade

# A pivot is a bar whose high (or low) is the most extreme within this many
# bars either side. 5 keeps genuine swing structure while ignoring single-bar
# wiggles; smaller values produce noise, larger ones miss real turning points.
PIVOT_WINDOW = 5

# Two pivots within this percentage of each other are the same level. Price
# does not respect a number to the cent — it respects a zone.
CLUSTER_TOLERANCE_PCT = 1.0

# A level touched once is a coincidence, not structure.
MIN_TOUCHES = 2

# No MAX_DISTANCE_PCT here. Until 2026-09-12 a level only counted if it sat
# within a flat 40% of the current price — a number with no derivation that
# did not scale: 40% on a utility that moves 1.2% a day is thirty-plus days
# of typical travel, 40% on a name that moves 8% a day is five. The window
# is now READ from the instrument: a level is relevant if the stock can
# plausibly reach it, using the SAME reachability estimate
# `derive_structural_target` already uses to decide whether a target is
# reachable (`horizon_reach`, ATR x sqrt(sessions) x the reach multiple),
# taken at the longest horizon the desk permits any trade to state
# (`MAX_HORIZON_SESSIONS`). See `find_structural_levels`.

# No RECENCY_HALFLIFE_SESSIONS here. Until 2026-09-02 this was 252.0 (~1
# trading year), decaying each touch's contribution to `strength` by
# `0.5 ** (age_sessions / 252)`. It was picked for being round, never
# measured, and is gone rather than slowed down — see the strength
# computation in `find_structural_levels` for the measurement that replaced
# it and docs/RESEARCH_FINDINGS.md §7 for the full caveats.

# How many levels to report per side. Enough to describe the structure, few
# enough to stay readable in a prompt.
MAX_LEVELS_PER_SIDE = 6

# Bad-print detection. A bar is rejected only when its high exceeds, or its low
# falls below, the median of its immediate neighbours by this factor. 5x is
# deliberately permissive: real markets gap, halt and limit-move, and throwing
# away genuine volatility is worse than tolerating a rare bad print.
_CLEAN_WINDOW = 10
_CLEAN_FACTOR = 5.0

# ---------------------------------------------------------------------------
# Reachability — the ONE estimate of how far an instrument travels
# ---------------------------------------------------------------------------
# These two constants belong to the target derivation further down (see the
# "Deriving the TARGET from structure" section for the full reasoning and
# the asymmetry note). They are defined up here because the level scan now
# reuses them: the same question — "can price plausibly get there?" — decides
# both whether a level is a reachable TARGET and whether a level is RELEVANT
# at all. One estimate, two consumers, so they cannot drift apart.

#: How far price can plausibly get within the horizon, in sqrt(session)-scaled
#: ATRs. Deliberately looser than the measured-move projection — see the note
#: on asymmetry in the target section.
MAX_REACH_ATR_MULTIPLE = 1.5

#: Ceiling on `expected_horizon_sessions` before it enters the sqrt() travel
#: estimate. An analyst claiming a 250-session horizon would otherwise
#: licence a target ~16 ATRs out; this also absorbs a nonsense value. It is
#: also, by construction, the LONGEST horizon over which any trade on this
#: desk may ask how far the instrument travels — which is why the level scan
#: reads it as the relevance horizon.
MAX_HORIZON_SESSIONS = 60


def travel_over(atr: float, horizon_sessions: int) -> float:
    """Typical excursion over `horizon_sessions`: ``ATR * sqrt(sessions)``.

    Square-root scaling, not linear: daily ranges accumulate as a random
    walk, and ``ATR * N`` overstates an N-session excursion by roughly
    sqrt(N). This is the desk's single definition of "how far does this
    instrument travel in that many sessions".
    """
    return float(atr) * math.sqrt(max(1, int(horizon_sessions)))


def horizon_reach(
    atr: float | None,
    horizon_sessions: int | None,
    *,
    max_reach_atr_multiple: float = MAX_REACH_ATR_MULTIPLE,
    max_horizon_sessions: int = MAX_HORIZON_SESSIONS,
) -> float | None:
    """The furthest price can plausibly get within the horizon, in dollars.

    ``ATR * sqrt(min(horizon, cap)) * max_reach_atr_multiple`` — the exact
    quantity `derive_structural_target` reports as `horizon_reach` and uses
    to decide whether a structural level is a reachable target. Exposed so
    `find_structural_levels` can ask the SAME question of every candidate
    level ("could any trade on this desk plausibly get here?") instead of
    applying a flat percentage that meant something different on every
    instrument.

    Returns None when there is no usable ATR or horizon: reachability
    cannot be measured, and nothing here guesses it.
    """
    volatility = _finite_positive(atr)
    if volatility is None:
        return None
    try:
        horizon = int(horizon_sessions) if horizon_sessions is not None else 0
    except (TypeError, ValueError):
        horizon = 0
    if horizon <= 0:
        return None
    horizon = min(horizon, max(1, int(max_horizon_sessions)))
    return travel_over(volatility, horizon) * float(max_reach_atr_multiple)


def _finite_positive(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


@dataclass(frozen=True)
class Level:
    """One structural price level."""

    price: float
    kind: str  # "support" | "resistance"
    touches: int  # pivots clustered into this level
    last_touch_sessions_ago: int  # informational only — not a strength input, see below
    strength: float  # touch count, distance-discounted; higher = more significant

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
    atr: float | None = None,
    max_reach_atr_multiple: float = MAX_REACH_ATR_MULTIPLE,
    max_horizon_sessions: int = MAX_HORIZON_SESSIONS,
    max_per_side: int = MAX_LEVELS_PER_SIDE,
) -> tuple[list[Level], list[Level]]:
    """Return ``(support_levels, resistance_levels)``, most significant first.

    Support is below the last close, resistance above it — classified by where
    the level sits *now*, not by whether the pivots forming it were highs or
    lows. A ceiling that price has broken through becomes a floor, and treating
    it as resistance because it was once a swing high would be wrong.

    **Relevance window (2026-09-12).** A repeated turning point only counts
    as actionable structure if the stock can plausibly reach it. "Plausibly
    reach" is not a percentage: it is `horizon_reach` — ``ATR x sqrt(H) x
    MAX_REACH_ATR_MULTIPLE`` — the same estimate `derive_structural_target`
    uses to decide whether a level is a reachable target, evaluated at
    ``H = max_horizon_sessions``, the longest horizon the desk lets any
    trade state. So the window is exactly "the furthest any permitted trade
    could travel": every level the target derivation could ever accept is
    inside it, and a level outside it could be neither a target for any
    stated horizon nor a stop (stops sit ~1.3-1.8 ATR away). It widens on
    a volatile name and narrows on a quiet one because ATR does, with no
    number chosen here. Until 2026-09-12 this was a flat 40% of price,
    which on a 1.2%-ATR utility looked thirty-plus days of travel away and
    on an 8%-ATR name looked five days away — and, once "no floor, no
    trade" became a rule, would have refused good trades on volatile names
    for a level that existed but sat just past an arbitrary line.

    `atr` may be supplied by a caller that already has the instrument's
    ATR(14) (`src/data/technical.py::atr_series` — the one implementation);
    otherwise it is computed here from the same cleaned bars. No ATR means
    reachability cannot be measured, and the scan returns nothing rather
    than fall back to a distance it cannot justify.

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

    volatility = _finite_positive(atr)
    if volatility is None:
        series = atr_series(clean)
        volatility = _finite_positive(series[-1]) if series.size else None
    window = horizon_reach(
        volatility, max_horizon_sessions,
        max_reach_atr_multiple=max_reach_atr_multiple,
        max_horizon_sessions=max_horizon_sessions,
    )
    if window is None:
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
        if abs(price - last_close) > window:
            continue

        newest_index = max(p[0] for p in cluster)
        sessions_ago = last_index - newest_index

        # Strength is touch count, discounted by distance — no age term.
        # A recency half-life stood here until 2026-09-02, weighting each
        # touch down by `0.5 ** (age_sessions / 252)` so old touches barely
        # counted. It is gone because it was checked, not because it was
        # suspected: `src/data/level_quality.py` fit a decay curve directly
        # against our own bars (7,218 daily touch episodes, 101 symbols, 5
        # years) and found no age effect at all — likelihood ratio 0.00
        # against a constant-probability null, on both an age-of-level and a
        # time-since-last-touch definition (docs/RESEARCH_FINDINGS.md §7). A
        # level defended three years ago predicts a bounce exactly as well as
        # one defended last week, in the data we actually have.
        #
        # The same measurement DOES support touches: pooled bounce
        # probability rises from 0.516 (first touch) to 0.644 (5+ prior
        # touches) on real price series, against a flat ~0.48-0.51 on
        # shuffled controls (slope +0.0249 real vs +0.0049 shuffled) — a
        # real, reproduced effect. Treat it as promising, not settled: two
        # settings were changed after an initial run found nothing, and the
        # shuffle control rules out the return distribution as an
        # explanation but does not isolate levels from ordinary volatility
        # clustering (§7 states both caveats plainly). Touch count is the
        # honest way to use a promising-not-settled finding without
        # overclaiming it.
        #
        # The measured probability CURVE itself is deliberately not imported
        # as scoring weights, for two reasons visible in the numbers above:
        # it is non-monotonic at low touch counts (0.507 at one prior touch,
        # BELOW 0.516 at zero), and it pools every level past 5 touches into
        # one number for posterior-sample-size reasons, which would score a
        # 5-touch level and a 20-touch level identically here. Checked
        # directly on the desk's 101-symbol universe (2026-09-02): swapping
        # in that curve moves MORE of the top-6 selection than dropping decay
        # did (33.9% of side-slots vs 27.1%, both measured the same way), so
        # it is not a free upgrade sitting next to the simpler option — it is
        # a different and less defensible ranking. Distance is untouched:
        # nothing here measured it, so nothing here changes it.
        strength = float(len(cluster)) / (1.0 + distance_pct / 10.0)

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


# ===========================================================================
# Deriving the TARGET from structure
# ===========================================================================
#
# Why this exists (2026-09-01)
# ----------------------------
# Reward:risk is `(target - entry) / (entry - stop)`. Since 2026-08-27 the
# STOP has been derived in code: structure places it, and
# `min_stop_atr_multiple` pushes it out when structure put it inside ordinary
# volatility. The TARGET was still `TechAnalysisResult.reference_target` — a
# number a language model wrote down. So the gate divided a measured quantity
# by an opinion. Those two are not commensurable, and the failure is
# systematic rather than random: a correctly-sized wide stop plus a modestly
# guessed target fails a 1.5 floor as a matter of arithmetic, whatever the
# trade is actually worth.
#
# Evidence, morning run of 2026-09-01 (`run-64290730`): 38 actionable
# signals, 30 of them (79%) under the floor before any judgement was applied.
# SLB `strong_buy`/`high` scored 1.28 on a 7.7% stop; AGX `sell`/`high`
# scored 0.84. Zero trades were placed.
#
# The floor is not the defect — its numerator is. So the numerator is
# computed here, from the same bars the stop already comes from, and the
# model's guess is demoted from arithmetic input to evidence.
#
# The rule
# --------
# Find the nearest structural level in the trade's direction that is more
# than `min_target_atr_multiple` ATRs away (a "target" inside one ordinary
# session's range is not a destination — price is already there). Then:
#
#   level exists, within reach  ->  the level IS the target. Price has to get
#                                   through the first ceiling before any
#                                   further one, and reaching past it to a
#                                   further level to make the ratio work is
#                                   exactly the invention being removed.
#
#   level exists, beyond reach  ->  nothing structural stands between entry
#                                   and as far as this symbol travels in the
#                                   intended hold, so structure does not
#                                   bound this trade. Target = measured move.
#
#   no level in the direction   ->  nothing overhead is expected to stop this
#                                   trade. Target = measured move.
#                                   (2026-09-11, funnel item 6: this used to
#                                   REFUSE unless `setup_type` separately said
#                                   "breakout". The other reading of an empty
#                                   direction — a chart nothing can be read
#                                   from — is already caught one branch
#                                   earlier as REFUSAL_NO_STRUCTURE, so the
#                                   label was adding nothing but refusals.
#                                   `risk.constants.is_trend_trade` now owns
#                                   this condition, shared with the
#                                   reward:risk exemption.)
#
# "Reach" and the measured move are the same estimate of travel:
# `ATR * sqrt(sessions) * multiple`. Square-root scaling, not linear: daily
# ranges accumulate as a random walk, and `ATR * N` overstates an N-session
# excursion by roughly sqrt(N). `expected_horizon_sessions` is the analyst's
# own estimate of time-to-resolution, already pinned at entry for exactly
# this kind of use.
#
# Note the deliberate asymmetry between the reach multiple and the projection
# multiple. Reach answers "could price plausibly get there at all?", so it is
# loose — a trade that works is by definition an above-typical move. The
# projection answers "how far do I claim it goes when no level says
# otherwise?", so it is the typical excursion and nothing more. The two are
# separately configurable because they are answering different questions.
#
# Everything fails closed. No levels, no level in the direction, no ATR, no
# horizon -> no target and a NAMED reason. There is deliberately no default
# and no fallback: a manufactured target is the defect being removed, and a
# manufactured target with better provenance is still manufactured.
#
# Interaction with `min_stop_atr_multiple` is arithmetic and worth stating
# outright, because it is the binding constraint on the measured-move branch.
# A stop at `k` ATRs and a target at `p * ATR * sqrt(H)` clear a floor `f`
# only when `sqrt(H) >= f * k / p`. At today's settings (p = 1.0, f = 1.5,
# and k = 1.5 scaled by setup: 1.35 for a range, 1.5 for a breakout) that
# is H >= ~6 sessions on the bare base, ~5 for a range setup and ~6 for a
# breakout. A stated horizon shorter than that cannot clear the floor
# however the trade is judged — a legitimate refusal about the trade's
# geometry, reported distinctly from "the model guessed badly".
#
# THIS IS THE ARITHMETIC THAT WAS CLOSING THE FUNNEL. Until 2026-09-04 the
# base `k` was 3.0 (range 3.45, breakout 2.55), which put the same thresholds
# at H >= ~21 / ~27 / ~15 sessions. This desk has never stated a 27-session
# horizon, so the range branch — the majority setup — could not clear the
# floor for ANY real signal, and measured against the record it did not: 0 of
# 222. The stop floor was re-derived from real Maximum Adverse Excursion data
# (see `risk.min_stop_atr_multiple` in config/settings.yaml); these session
# counts fall out of that change, they were not tuned to a target.
#
# The structural-level branch is looser, because the level does not have to
# be a full projection away: it needs `W >= f*k*ATR` to clear the floor and
# `W <= 1.5*ATR*sqrt(H)` to be reachable, so H >= (f*k/1.5)^2 — about 2
# sessions for a range setup (it was ~12).

#: A target closer than this many ATRs is not a destination. Price is
#: already there and the "reward" is one ordinary session's noise.
MIN_TARGET_ATR_MULTIPLE = 1.0

#: Measured move, in sqrt(session)-scaled ATRs, claimed when no level stands
#: in the way. 1.0 = the typical excursion over the stated horizon, and
#: nothing more.
BREAKOUT_PROJECTION_ATR_MULTIPLE = 1.0

#: `MAX_REACH_ATR_MULTIPLE` and `MAX_HORIZON_SESSIONS` — the reach multiple
#: and the horizon ceiling this section reasons about — are defined near the
#: top of the module (the "Reachability" block) since 2026-09-12, because the
#: level scan reuses them. Their meaning is unchanged.

#: Refusal codes. Each names a DIFFERENT thing being wrong, because "no
#: trade" without a reason is what let the original defect survive unnoticed.
REFUSAL_NO_ENTRY = "no_entry_price"
REFUSAL_NO_VOLATILITY = "no_volatility_reading"
REFUSAL_NO_HORIZON = "no_expected_horizon"
REFUSAL_NO_STRUCTURE = "no_structural_levels"
REFUSAL_NO_LEVEL_IN_DIRECTION = "no_level_in_direction"
REFUSAL_PROJECTION_IMPLAUSIBLE = "projection_implausible"


@dataclass(frozen=True)
class TargetDerivation:
    """Outcome of deriving a target. ``price is None`` means REFUSED.

    `refusal` is machine-readable and `detail` is the one line a human (or a
    downstream model reading the order's reasoning) needs to tell a refusal
    about missing data apart from a refusal about the trade's geometry.
    """

    price: float | None
    basis: str = ""          # "structural_level" | "measured_move" | "" (refused)
    refusal: str = ""        # one of the REFUSAL_* codes; "" on success
    detail: str = ""
    level_used: float | None = None
    horizon_reach: float | None = None      # ATR * sqrt(H) * max_reach_atr_multiple
    model_target: float | None = None       # the LLM's guess, kept as evidence
    divergence_pct: float | None = None     # computed vs. the model's guess

    @property
    def refused(self) -> bool:
        return self.price is None

    def as_dict(self) -> dict:
        return asdict(self)


def _refused(code: str, detail: str, model_target: float | None) -> TargetDerivation:
    return TargetDerivation(
        price=None, refusal=code, detail=detail, model_target=model_target,
    )


def unfilled_gap_edge(bars: Sequence[OHLCV], direction: str) -> float | None:
    """The near edge of the binding unfilled gap on the STOP side, or None.

    Owner ruling, 2026-09-12: *"A shelf $30 below, on the far side of a gap
    the market has repriced through, isn't support anyone is defending."*
    Price never traded through the gap, so nobody bought or defended
    anything in that range — a level below it is a number on a chart, not
    support, and leaning a stop on it reintroduces exactly the "no
    defensible stop" case the floor rule exists to prevent.

    Derived from the chart, no new constants: an unfilled up-gap is a
    session whose low sits above the prior session's high, where no later
    session has traded back down to that prior high. `src/data/context.py::
    find_unfilled_gaps` is the desk's ONE gap detector and is reused here
    (the Tech Analyst's context block already reports the same gaps); what
    that inherits is its pre-existing 2% minimum gap size, which was never
    derived for this rule — flagged, not hidden.

    For a long the answer is the BOTTOM of the highest unfilled up-gap
    below price (the prior session's high): every level at or below it is on
    the far side. For a short the mirror: the TOP of the lowest unfilled
    down-gap above price (the prior session's low). A level built INSIDE a
    partly-filled gap — price came back, bounced twice — sits above the
    edge and counts; that is the base the owner described forming.

    A gap that fills stops disqualifying anything automatically, because
    this is re-read from the bars each session. No timer, no bar count.
    """
    from src.data.context import find_unfilled_gaps  # one detector, no copy

    clean = _clean_bars(list(bars or []))
    if len(clean) < 2:
        return None
    gaps = find_unfilled_gaps(clean, limit=len(clean))
    if str(direction or "").strip().lower() == "short":
        tops = [g.from_price for g in gaps if g.direction == "down"]
        return min(tops) if tops else None
    bottoms = [g.from_price for g in gaps if g.direction == "up"]
    return max(bottoms) if bottoms else None


def structural_floor(
    levels: Sequence[float],
    entry_price: float | None,
    direction: str,
    *,
    gap_edge: float | None = None,
) -> float | None:
    """The nearest computed level on the STOP side of this entry, or None.

    Owner decision, 2026-09-12: **"No floor, no trade. No ceiling is
    fine."** A stop has to sit on something the chart actually defended;
    without a level beneath a long (or above a short — the short's floor is
    overhead) the desk would be picking a stop distance and hoping, which is
    the failure mode this rule refuses to hold. The other side of the trade
    needs nothing: a stock at new highs has nothing overhead by definition,
    that IS the breakout setup, and `derive_structural_target` already
    projects its target from ATR. Only the stop side is required.

    `levels` is `TechAnalysisResult.computed_levels` — the union of every
    level `find_structural_levels` found within the instrument's own
    reach — partitioned here against THIS entry rather than the last close,
    for the same reason `derive_structural_target` re-partitions. A level
    qualifies with the scan's own minimum touches (`MIN_TOUCHES`); no
    further count is demanded here, because "bounced twice off a price it
    built after the gap" is precisely the case that is meant to qualify.

    `gap_edge` (owner ruling, same day — see `unfilled_gap_edge`): a level
    on the far side of an unfilled gap is not a floor. For a long, levels at
    or below the edge are ignored; for a short, levels at or above it.

    Returns the nearest such level so a log can name the floor. None means
    there is none — "not yet", not "never": the levels are re-read every
    session, and a name that gaps into new territory earns a floor the day
    the chart shows one ABOVE the gap, with no timer.
    """
    entry = _finite_positive(entry_price)
    if entry is None:
        return None
    usable = [p for p in (_finite_positive(lv) for lv in levels or ()) if p is not None]
    edge = _finite_positive(gap_edge)
    if str(direction or "").strip().lower() == "short":
        side = [p for p in usable if p > entry and (edge is None or p < edge)]
        return min(side) if side else None
    side = [p for p in usable if p < entry and (edge is None or p > edge)]
    return max(side) if side else None


def derive_structural_target(
    *,
    entry_price: float | None,
    direction: str,
    levels: Sequence[float],
    atr: float | None,
    horizon_sessions: int | None,
    setup_type: str | None,
    model_target: float | None = None,
    min_target_atr_multiple: float = MIN_TARGET_ATR_MULTIPLE,
    breakout_projection_atr_multiple: float = BREAKOUT_PROJECTION_ATR_MULTIPLE,
    max_reach_atr_multiple: float = MAX_REACH_ATR_MULTIPLE,
    max_horizon_sessions: int = MAX_HORIZON_SESSIONS,
) -> TargetDerivation:
    """Compute where the instrument actually travels, or refuse by name.

    Works both directions. For a long the target is ABOVE entry and drawn
    from levels above it; for a short it is BELOW entry and drawn from levels
    below it. Nothing about the rule is long-only — the comparison operators
    and the sign of the projection are the whole of the difference.

    `levels` is the UNION of the computed supports and resistances, as bare
    prices. That is deliberate: `find_structural_levels` classifies a level
    as support or resistance relative to the LAST CLOSE, while a trade is
    entered at a live price that may sit on the other side of it. What
    matters here is only whether a level is above or below THIS ENTRY, so the
    partition is redone against `entry_price` rather than inherited.

    `model_target` is never used to choose the answer. It is carried through
    so callers can log where the model's guess and the computed level
    disagree, which is the cheapest available read on whether the model's
    targets were ever worth anything.
    """
    guess = _finite_positive(model_target)

    entry = _finite_positive(entry_price)
    if entry is None:
        return _refused(REFUSAL_NO_ENTRY, "no usable entry price", guess)

    volatility = _finite_positive(atr)
    if volatility is None:
        return _refused(
            REFUSAL_NO_VOLATILITY,
            "no ATR reading, so neither the noise floor nor the reachable "
            "distance can be measured",
            guess,
        )

    try:
        horizon = int(horizon_sessions) if horizon_sessions is not None else 0
    except (TypeError, ValueError):
        horizon = 0
    if horizon <= 0:
        return _refused(
            REFUSAL_NO_HORIZON,
            "no expected_horizon_sessions, so there is no period over which "
            "to ask how far this symbol travels",
            guess,
        )
    horizon = min(horizon, max(1, int(max_horizon_sessions)))

    is_short = str(direction or "").strip().lower() == "short"
    travel = travel_over(volatility, horizon)
    # The same `horizon_reach` the level scan uses for relevance; the scan's
    # window (taken at `max_horizon_sessions`) is therefore always at least
    # this wide, so no level this derivation could accept was ever dropped
    # before it got here.
    reach = horizon_reach(
        volatility, horizon,
        max_reach_atr_multiple=max_reach_atr_multiple,
        max_horizon_sessions=max_horizon_sessions,
    )
    noise = volatility * min_target_atr_multiple
    setup = str(setup_type or "").strip().lower()

    def _divergence(price: float) -> float | None:
        if guess is None:
            return None
        return round((price - guess) / guess * 100.0, 2)

    usable = [p for p in (_finite_positive(lv) for lv in levels or ()) if p is not None]
    if not usable:
        # No structure at all is NOT the same as "no ceiling overhead". It
        # means the history was too short or too dirty to say anything, which
        # is what `find_structural_levels` returning empty lists is
        # documented to mean. Refuse, whatever the setup claims.
        return _refused(
            REFUSAL_NO_STRUCTURE,
            "no structural levels could be computed from the price history "
            "(insufficient or unusable bars)",
            guess,
        )

    if is_short:
        directional = [p for p in usable if p < entry - noise]
        nearest = max(directional) if directional else None
    else:
        directional = [p for p in usable if p > entry + noise]
        nearest = min(directional) if directional else None

    if nearest is not None and abs(nearest - entry) <= reach:
        price = round(nearest, 2)
        return TargetDerivation(
            price=price,
            basis="structural_level",
            detail=(
                f"nearest structural level {'below' if is_short else 'above'} "
                f"entry ${entry:,.2f} is ${price:,.2f} "
                f"({(price - entry) / entry * 100:+.1f}%), reachable inside "
                f"{horizon} sessions (ATR ${volatility:,.2f} x sqrt({horizon})"
                f" x {max_reach_atr_multiple:g} = ${reach:,.2f})"
            ),
            level_used=price,
            horizon_reach=round(reach, 4),
            model_target=guess,
            divergence_pct=_divergence(price),
        )

    # Past this point no level stands in the way within the horizon.
    #
    # **2026-09-11, docs/WORK.md funnel item 6.** This used to refuse when
    # `nearest is None` unless the analyst had separately typed
    # `setup_type="breakout"`, on the reasoning that no-level-in-direction is
    # ambiguous between a genuine breakout and a chart nothing can be read
    # from. That second possibility is ALREADY excluded above: a chart
    # nothing can be read from yields no `usable` levels at all and is
    # refused as REFUSAL_NO_STRUCTURE. So by the time we get here, the desk's
    # own level computation succeeded AND found nothing in the trade's
    # direction — a measured absence of a ceiling, which is exactly the
    # condition the ATR projection below is for. Requiring a label on top of
    # it refused real trades (funnel item 6) for a wording, while the correct
    # projection sat in this same function unreachable.
    #
    # `is_trend_trade` is the SINGLE definition of that condition, shared
    # with the reward:risk exemption in `PortfolioConstructor.
    # _widen_stop_past_noise` — the two agree by construction rather than by
    # coincidence. Passing the measured fact means this is now always True
    # here and REFUSAL_NO_LEVEL_IN_DIRECTION is unreachable; the constant is
    # kept because it appears in historical logs and in the census.
    if nearest is None and not is_trend_trade(setup_type, structural_ceiling=False):
        return _refused(
            REFUSAL_NO_LEVEL_IN_DIRECTION,
            f"no structural level {'below' if is_short else 'above'} entry "
            f"${entry:,.2f} beyond the ${noise:,.2f} noise floor, and "
            f"setup_type={setup or 'unset'!r} does not claim a breakout",
            guess,
        )

    projection = travel * breakout_projection_atr_multiple
    if projection <= noise:
        return _refused(
            REFUSAL_PROJECTION_IMPLAUSIBLE,
            f"a {horizon}-session measured move of ${projection:,.2f} does "
            f"not clear its own ${noise:,.2f} noise floor",
            guess,
        )
    raw = entry - projection if is_short else entry + projection
    if raw <= 0:
        # Only reachable on an extreme ATR-to-price ratio, but a short whose
        # projection runs through zero is arithmetic, not a trade.
        return _refused(
            REFUSAL_PROJECTION_IMPLAUSIBLE,
            f"a {horizon}-session measured move of ${projection:,.2f} puts "
            f"the short's target at or below zero from entry ${entry:,.2f}",
            guess,
        )

    price = round(raw, 2)
    if nearest is None:
        why = (
            f"no structural level {'below' if is_short else 'above'} entry on "
            f"a chart that yielded {len(usable)} level(s) elsewhere, so "
            f"nothing overhead is expected to stop this trade "
            f"(setup_type={setup or 'unset'!r})"
        )
    else:
        why = (
            f"nearest structural level ${nearest:,.2f} is "
            f"${abs(nearest - entry):,.2f} away, past the ${reach:,.2f} "
            f"reachable in {horizon} sessions, so nothing stands in the way"
        )
    return TargetDerivation(
        price=price,
        basis="measured_move",
        detail=(
            f"{why}; measured move = ATR ${volatility:,.2f} x sqrt({horizon})"
            f" x {breakout_projection_atr_multiple:g} = ${projection:,.2f} -> "
            f"${price:,.2f}"
        ),
        level_used=round(nearest, 2) if nearest is not None else None,
        horizon_reach=round(reach, 4),
        model_target=guess,
        divergence_pct=_divergence(price),
    )
